"""Клиент GitHub Releases и статических JSON-файлов (без собственного сервера)."""
from __future__ import annotations

import logging
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit, urljoin
from typing import Any

import requests
from packaging.version import InvalidVersion, Version
from PyQt5.QtCore import QThread, pyqtSignal

from version import (GITHUB_API_LATEST, CONTENT_URL, NEWS_URL, VERSIONS_URL,
                     LAUNCHER_VERSION, USER_AGENT)
from update_download import DownloadCancelled, download_verified_asset

log = logging.getLogger("fiercy.github")


def _allowed_github_host(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return host in {"github.com", "api.github.com", "raw.githubusercontent.com"} or host.endswith(".githubusercontent.com")


def _safe_github_get(url: str, *, allowed_hosts: set[str], **kwargs):
    """Follow redirects only to explicitly allowed HTTPS GitHub hosts."""
    current = str(url)
    for _ in range(6):
        parsed = urlsplit(current)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or parsed.username or parsed.password or not (
            host in allowed_hosts or host.endswith(".githubusercontent.com") or host.endswith(".github.io")
        ):
            raise ValueError("GitHub вернул небезопасный адрес перенаправления.")
        response = requests.get(current, allow_redirects=False, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location", "")
        response.close()
        if not location:
            raise ValueError("GitHub вернул перенаправление без адреса.")
        current = urljoin(current, location)
    raise ValueError("Слишком много перенаправлений GitHub.")


def _sha256_sums(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match:
            found[match.group(2).strip()] = match.group(1).lower()
    return found


def check_latest_release(release_url: str = GITHUB_API_LATEST,
                         timeout: tuple[int, int] = (5, 20)) -> dict[str, Any]:
    """Read the latest compatible GitHub Release and validate its asset metadata."""
    parsed_release_url = urlsplit(release_url)
    if (parsed_release_url.scheme != "https" or parsed_release_url.hostname != "api.github.com"
            or parsed_release_url.username or parsed_release_url.password):
        raise ValueError("Проверка обновлений должна идти через HTTPS GitHub API.")
    response = requests.get(
        release_url, timeout=timeout, allow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    if response.status_code == 404:
        return {"has_release": False, "update_available": False,
                "current_version": LAUNCHER_VERSION}
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        # /releases includes prereleases; /releases/latest does not. This lets
        # beta testers install a published prerelease without changing code.
        candidates = [item for item in payload if isinstance(item, dict) and not item.get("draft")]
        if not candidates:
            return {"has_release": False, "update_available": False,
                    "current_version": LAUNCHER_VERSION}
        release = candidates[0]
    elif isinstance(payload, dict):
        release = payload
    else:
        raise ValueError("GitHub API вернул неожиданный формат релизов.")
    remote = str(release.get("tag_name", "")).strip().lstrip("vV")
    if not remote:
        raise ValueError("В последнем GitHub Release отсутствует tag_name.")
    try:
        newer = Version(remote) > Version(LAUNCHER_VERSION)
    except InvalidVersion as exc:
        raise ValueError(f"Некорректный номер версии в GitHub Release: {remote}") from exc
    if not newer:
        return {"has_release": True, "update_available": False,
                "latest_version": remote, "current_version": LAUNCHER_VERSION,
                "release_url": release.get("html_url", "")}

    import sys
    frozen = bool(getattr(sys, "frozen", False))
    preferred = ("win64", "windows", "portable") if frozen else ("source", "src")
    assets = release.get("assets", [])
    zip_assets = [item for item in assets if str(item.get("name", "")).lower().endswith(".zip")]
    asset = next((item for term in preferred for item in zip_assets
                  if term in str(item.get("name", "")).lower()), None)
    if asset is None:
        raise ValueError(
            "В релизе нет ZIP-пакета для этого типа установки. Нужен "
            "FiercyLauncher-source.zip для Python или FiercyLauncher-win64.zip для EXE."
        )

    asset_name = str(asset.get("name", ""))
    digest = str(asset.get("digest", ""))
    expected = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        sums_asset = next((item for item in assets
                           if str(item.get("name", "")).lower() == "sha256sums.txt"), None)
        if sums_asset:
            sums_url = str(sums_asset.get("browser_download_url", ""))
            if not sums_url.startswith("https://github.com/"):
                raise ValueError("GitHub вернул неправильную ссылку SHA256SUMS.txt.")
            sums_response = _safe_github_get(
                sums_url, allowed_hosts={"github.com"}, timeout=(5, 20),
                headers={"User-Agent": USER_AGENT},
            )
            sums_response.raise_for_status()
            expected = _sha256_sums(sums_response.text).get(asset_name, "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"Для {asset_name} не найден SHA-256. Опубликуй SHA256SUMS.txt.")
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith("https://github.com/"):
        raise ValueError("GitHub вернул неизвестный адрес пакета обновления.")
    return {
        "has_release": True,
        "update_available": True,
        "current_version": LAUNCHER_VERSION,
        "latest_version": remote,
        "asset_name": asset_name,
        "download_url": url,
        "sha256": expected,
        "size": int(asset.get("size", 0) or 0),
        "notes": str(release.get("body", ""))[:6000],
        "release_url": str(release.get("html_url", "")),
    }


class ReleaseCheckWorker(QThread):
    checked = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, release_url: str = GITHUB_API_LATEST, parent=None):
        super().__init__(parent)
        self.release_url = release_url

    def run(self) -> None:
        try:
            self.checked.emit(check_latest_release(self.release_url))
        except Exception as exc:
            log.exception("Проверка GitHub Releases завершилась ошибкой")
            self.failed.emit(str(exc))


class AssetDownloadWorker(QThread):
    progress = pyqtSignal(int, int, float, str)  # percent, total bytes, bytes/sec, status
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, url: str, expected_sha256: str, filename: str, parent=None):
        super().__init__(parent)
        self.url = str(url)
        self.expected_sha256 = str(expected_sha256).lower()
        self.filename = Path(filename).name
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            folder = Path.home() / ".fiercylauncher" / "updates"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / self.filename

            def report(done: int, total: int, speed: float, status: str) -> None:
                self.progress.emit(int(done * 100 / total) if total else -1,
                                   total, speed, status)

            download_verified_asset(
                self.url, self.expected_sha256, target,
                progress=report, cancel_event=self._cancel, max_workers=6,
            )
            self.completed.emit(str(target))
        except DownloadCancelled:
            self.cancelled.emit()
        except Exception as exc:
            log.exception("Загрузка обновления завершилась ошибкой")
            self.failed.emit(str(exc))


class StaticContentWorker(QThread):
    loaded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self) -> None:
        try:
            result: dict[str, Any] = {}
            for key, url in (("launcher", CONTENT_URL), ("news", NEWS_URL), ("versions", VERSIONS_URL)):
                response = _safe_github_get(
                    url, allowed_hosts={"raw.githubusercontent.com"}, timeout=(5, 15),
                    headers={"User-Agent": USER_AGENT}, stream=True,
                )
                if response.status_code == 404:
                    log.info("Статический файл GitHub пока не опубликован: %s", url)
                    continue
                response.raise_for_status()
                declared_size = int(response.headers.get("Content-Length", "0") or 0)
                if declared_size > 1024 * 1024:
                    raise ValueError(f"Статический JSON {key} превышает лимит 1 МБ.")
                body = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if chunk:
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise ValueError(f"Статический JSON {key} превышает лимит 1 МБ.")
                parsed = json.loads(body.decode("utf-8-sig"))
                if not isinstance(parsed, dict):
                    raise ValueError(f"JSON {key} должен содержать объект верхнего уровня.")
                result[key] = parsed
            self.loaded.emit(result)
        except Exception as exc:
            log.exception("Не удалось загрузить публичный контент GitHub")
            self.failed.emit(str(exc))


class RemoteImageWorker(QThread):
    """Загружает публичный HTTPS-баннер с ограничением размера и MIME-типа."""
    loaded = pyqtSignal(bytes)
    failed = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = str(url).strip()
        parsed = urlsplit(self.url)
        host = (parsed.hostname or "").lower()
        allowed_hosts = {"raw.githubusercontent.com", "github.com"}
        if parsed.scheme != "https" or not (host in allowed_hosts or host.endswith(".github.io")):
            raise ValueError("Баннер должен быть публичным HTTPS-файлом GitHub Raw или GitHub Pages")

    def run(self) -> None:
        try:
            with _safe_github_get(
                self.url, allowed_hosts={"raw.githubusercontent.com", "github.com"},
                stream=True, timeout=(5, 20), headers={"User-Agent": USER_AGENT},
            ) as response:
                response.raise_for_status()
                final_url = urlsplit(str(response.url))
                final_host = (final_url.hostname or "").lower()
                if final_url.scheme != "https" or not (
                    final_host in {"raw.githubusercontent.com", "github.com"}
                    or final_host.endswith(".github.io")
                    or final_host.endswith(".githubusercontent.com")
                ):
                    raise ValueError("Ссылка баннера перенаправила на неподдерживаемый адрес")
                content_type = response.headers.get("Content-Type", "").lower()
                if not content_type.startswith("image/"):
                    raise ValueError("Ссылка баннера не ведёт на изображение")
                length = int(response.headers.get("Content-Length", "0") or 0)
                if length > 8 * 1024 * 1024:
                    raise ValueError("Размер баннера превышает 8 МБ")
                payload = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if chunk:
                        payload.extend(chunk)
                        if len(payload) > 8 * 1024 * 1024:
                            raise ValueError("Размер баннера превышает 8 МБ")
                if not payload:
                    raise ValueError("Файл баннера пуст")
                self.loaded.emit(bytes(payload))
        except Exception as exc:
            log.warning("Не удалось загрузить промо-баннер: %s", exc)
            self.failed.emit(str(exc))
