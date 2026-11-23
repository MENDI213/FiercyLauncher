"""Fast, verified downloads for launcher release assets.

GitHub release assets are downloaded using parallel HTTP Range requests when the
CDN supports them, with a safe sequential fallback for proxies/CDNs that do not.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urljoin

import requests

log = logging.getLogger("fiercy.update_download")
USER_AGENT = "FiercyLauncher-Updater/1.0"
ProgressCallback = Callable[[int, int, float, str], None]


class DownloadCancelled(Exception):
    """Raised when the user cancels a release download."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def _notify(callback: ProgressCallback | None, done: int, total: int,
            started: float, label: str = "Загрузка обновления") -> None:
    if callback is None:
        return
    elapsed = max(0.001, time.monotonic() - started)
    speed = done / elapsed
    try:
        callback(done, total, speed, label)
    except Exception:
        log.debug("Update progress callback failed", exc_info=True)


def _allowed_github_host(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return host in {"github.com", "api.github.com"} or host.endswith(".githubusercontent.com")


def _check_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not _allowed_github_host(parsed.hostname)
            or parsed.username or parsed.password):
        raise ValueError("Ссылка обновления должна вести на GitHub или его CDN по HTTPS.")


def _safe_get(url: str, **kwargs):
    """Follow only HTTPS redirects to GitHub-owned download hosts."""
    current = url
    for _ in range(6):
        _check_url(current)
        response = requests.get(current, allow_redirects=False, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        close = getattr(response, "close", None)
        if callable(close):
            close()
        if not location:
            raise ValueError("GitHub вернул перенаправление без адреса.")
        current = urljoin(current, location)
    raise ValueError("Слишком много перенаправлений при загрузке обновления.")


def _probe_ranges(url: str, timeout: tuple[int, int]) -> tuple[bool, int]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
        "Range": "bytes=0-0",
    }
    with _safe_get(url, headers=headers, timeout=timeout, stream=True) as response:
        _check_url(response.url)
        if response.status_code != 206:
            return False, int(response.headers.get("Content-Length", 0) or 0)
        match = re.fullmatch(r"bytes\s+0-0/(\d+)", response.headers.get("Content-Range", "").strip(), re.I)
        if not match:
            return False, 0
        return True, int(match.group(1))


def _download_single(url: str, temporary: Path, expected_size: int,
                     callback: ProgressCallback | None, cancel_event: threading.Event | None,
                     started: float, timeout: tuple[int, int]) -> int:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        temporary.unlink(missing_ok=True)
        done = 0
        try:
            headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            with _safe_get(url, stream=True, timeout=timeout, headers=headers) as response:
                response.raise_for_status()
                _check_url(response.url)
                response_size = int(response.headers.get("Content-Length", 0) or 0)
                total = expected_size or response_size
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled()
                        if not chunk:
                            continue
                        output.write(chunk)
                        done += len(chunk)
                        _notify(callback, done, total, started,
                                f"Загрузка {done / 1048576:.1f} МБ / {total / 1048576:.1f} МБ" if total else
                                f"Загрузка {done / 1048576:.1f} МБ")
                if total and done != total:
                    raise IOError(f"Сервер передал {done} байт вместо {total} байт.")
                return done
        except DownloadCancelled:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(0.7 * attempt)
    raise RuntimeError(f"Не удалось скачать архив после 3 попыток: {last_error}") from last_error


def download_verified_asset(
    url: str,
    expected_sha256: str,
    target: Path | str,
    expected_size: int = 0,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    max_workers: int = 6,
) -> Path:
    """Download an HTTPS GitHub release asset quickly and verify SHA-256.

    Parallel ranges are used only when the CDN explicitly responds with HTTP 206
    and a valid Content-Range header. Otherwise a retried single-stream download
    is used. The destination is atomically replaced only after hash validation.
    """
    _check_url(str(url))
    expected_sha256 = str(expected_sha256).lower().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Неверная контрольная сумма SHA-256.")

    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        sidecar = destination.with_name(destination.name + ".sha256")
        sidecar_tmp = sidecar.with_suffix(sidecar.suffix + ".new")
        sidecar_tmp.write_text(expected_sha256 + "\n", encoding="ascii")
        os.replace(sidecar_tmp, sidecar)
        size = destination.stat().st_size
        _notify(progress, size, size, time.monotonic(), f"Проверенный кеш: {destination.name}")
        return destination
    destination.unlink(missing_ok=True)

    started = time.monotonic()
    temporary = destination.with_name(destination.name + ".download")
    temporary.unlink(missing_ok=True)
    timeout = (15, 90)
    work_dir = Path(tempfile.mkdtemp(prefix="fiercy-ranges-", dir=str(destination.parent)))
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()

        try:
            ranges_supported, discovered_size = _probe_ranges(str(url), timeout)
        except Exception as exc:
            log.info("Range probe failed; using a normal download: %s", exc)
            ranges_supported, discovered_size = False, 0
        total = int(expected_size or discovered_size or 0)
        used_parallel = False

        if ranges_supported and total >= 4 * 1024 * 1024:
            worker_count = min(max(2, int(max_workers)), 8, max(2, math.ceil(total / (4 * 1024 * 1024))))
            chunk_size = math.ceil(total / worker_count)
            ranges: list[tuple[int, int, Path]] = []
            for index in range(worker_count):
                start = index * chunk_size
                end = min(total - 1, start + chunk_size - 1)
                if start <= end:
                    ranges.append((start, end, work_dir / f"part-{index:02d}.bin"))

            lock = threading.Lock()
            part_progress = {index: 0 for index in range(len(ranges))}
            aggregate_done = 0
            last_emit = 0.0

            def fetch_part(index: int, start: int, end: int, part_path: Path) -> int:
                nonlocal aggregate_done, last_emit
                expected_part = end - start + 1
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled()
                    with lock:
                        aggregate_done -= part_progress[index]
                        part_progress[index] = 0
                    part_path.unlink(missing_ok=True)
                    received = 0
                    try:
                        headers = {
                            "User-Agent": USER_AGENT,
                            "Accept-Encoding": "identity",
                            "Range": f"bytes={start}-{end}",
                        }
                        with _safe_get(url, headers=headers, stream=True, timeout=timeout) as response:
                            response.raise_for_status()
                            if response.status_code != 206:
                                raise IOError(f"Range-запрос вернул HTTP {response.status_code}, ожидался 206.")
                            _check_url(response.url)
                            expected_header = f"bytes {start}-{end}/{total}"
                            if response.headers.get("Content-Range", "").strip().lower() != expected_header.lower():
                                raise IOError("CDN вернул неправильный Content-Range для части архива.")
                            with part_path.open("wb") as output:
                                for chunk in response.iter_content(chunk_size=512 * 1024):
                                    if cancel_event is not None and cancel_event.is_set():
                                        raise DownloadCancelled()
                                    if not chunk:
                                        continue
                                    output.write(chunk)
                                    received += len(chunk)
                                    now = time.monotonic()
                                    with lock:
                                        part_progress[index] = received
                                        aggregate_done += len(chunk)
                                        if now - last_emit >= 0.18 or aggregate_done >= total:
                                            last_emit = now
                                            snapshot = aggregate_done
                                        else:
                                            snapshot = -1
                                    if snapshot >= 0:
                                        _notify(progress, snapshot, total, started,
                                                f"Быстрая загрузка: {snapshot / 1048576:.1f} / {total / 1048576:.1f} МБ")
                        if received != expected_part:
                            raise IOError(f"Часть архива повреждена: {received} вместо {expected_part} байт.")
                        return received
                    except DownloadCancelled:
                        part_path.unlink(missing_ok=True)
                        raise
                    except Exception as exc:
                        last_error = exc
                        part_path.unlink(missing_ok=True)
                        if attempt < 3:
                            time.sleep(0.5 * attempt)
                raise RuntimeError(f"Не удалось получить часть архива {index + 1}: {last_error}") from last_error

            try:
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="fiercy-update") as pool:
                    futures = [pool.submit(fetch_part, i, start, end, part)
                               for i, (start, end, part) in enumerate(ranges)]
                    for future in as_completed(futures):
                        future.result()
                with temporary.open("wb") as output:
                    for _, _, part_path in ranges:
                        with part_path.open("rb") as part:
                            shutil.copyfileobj(part, output, length=1024 * 1024)
                if temporary.stat().st_size != total:
                    raise IOError("Собранный архив имеет неверный размер.")
                used_parallel = True
                log.info("Release asset downloaded in %d parallel HTTP ranges", worker_count)
            except DownloadCancelled:
                raise
            except Exception as exc:
                log.warning("Parallel range download failed; falling back to one stream: %s", exc)
                temporary.unlink(missing_ok=True)

        if not used_parallel:
            _download_single(str(url), temporary, total, progress, cancel_event, started, timeout)

        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        actual = sha256_file(temporary)
        if actual != expected_sha256:
            raise ValueError("SHA-256 не совпал. Архив обновления удалён, текущая версия не изменена.")
        os.replace(temporary, destination)
        sidecar = destination.with_name(destination.name + ".sha256")
        sidecar_tmp = sidecar.with_suffix(sidecar.suffix + ".new")
        sidecar_tmp.write_text(expected_sha256 + "\n", encoding="ascii")
        os.replace(sidecar_tmp, sidecar)
        size = destination.stat().st_size
        _notify(progress, size, size, started, f"Загрузка завершена: {destination.name}")
        return destination
    except DownloadCancelled:
        raise
    finally:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)
