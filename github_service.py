"""Клиент GitHub Releases и статических JSON-файлов (без собственного сервера)."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

import requests
from packaging.version import InvalidVersion, Version
from PyQt5.QtCore import QThread, pyqtSignal

from version import (GITHUB_API_LATEST, CONTENT_URL, NEWS_URL, VERSIONS_URL,
                     LAUNCHER_VERSION, USER_AGENT)

log = logging.getLogger("fiercy.github")


def _sha256_sums(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match:
            found[match.group(2).strip()] = match.group(1).lower()
    return found


class ReleaseCheckWorker(QThread):
    checked = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, release_url: str = GITHUB_API_LATEST, parent=None):
        super().__init__(parent)
        self.release_url = release_url

    def run(self) -> None:
        try:
            if not self.release_url.startswith("https://api.github.com/"):
                raise ValueError("Проверка обновлений должна идти через HTTPS GitHub API.")
            response = requests.get(
                self.release_url, timeout=(5, 20),
                headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            )
            if response.status_code == 404:
                self.checked.emit({"has_release": False, "update_available": False,
                                   "current_version": LAUNCHER_VERSION})
                return
            response.raise_for_status()
            release = response.json()
            remote = str(release.get("tag_name", "")).strip().lstrip("vV")
            if not remote:
                raise ValueError("В последнем GitHub Release отсутствует tag_name.")
            try:
                newer = Version(remote) > Version(LAUNCHER_VERSION)
            except InvalidVersion as exc:
                raise ValueError(f"Некорректный номер версии в GitHub Release: {remote}") from exc
            if not newer:
                self.checked.emit({"has_release": True, "update_available": False,
                                   "latest_version": remote, "current_version": LAUNCHER_VERSION,
                                   "release_url": release.get("html_url", "")})
                return

            frozen = bool(getattr(__import__("sys"), "frozen", False))
            preferred = ("win64", "windows", "portable") if frozen else ("source", "src")
            assets = release.get("assets", [])
            zip_assets = [item for item in assets if str(item.get("name", "")).lower().endswith(".zip")]
            asset = next((item for term in preferred for item in zip_assets
                          if term in str(item.get("name", "")).lower()), None)
            if asset is None:
                raise ValueError(
                    "В релизе нет ZIP-пакета для этого типа установки. "
                    "Нужен FiercyLauncher-source.zip для запуска из Python или "
                    "FiercyLauncher-win64.zip для EXE."
                )

            asset_name = str(asset.get("name", ""))
            digest = str(asset.get("digest", ""))
            expected = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                sums_asset = next((item for item in assets
                                   if str(item.get("name", "")).lower() == "sha256sums.txt"), None)
                if sums_asset:
                    sums_response = requests.get(sums_asset.get("browser_download_url", ""),
                                                 timeout=(5, 20), headers={"User-Agent": USER_AGENT})
                    sums_response.raise_for_status()
                    expected = _sha256_sums(sums_response.text).get(asset_name, "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"Для {asset_name} не найден SHA-256. Опубликуй SHA256SUMS.txt.")
            url = str(asset.get("browser_download_url", ""))
            if not url.startswith("https://github.com/"):
                raise ValueError("GitHub вернул неизвестный адрес пакета обновления.")
            self.checked.emit({
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
            })
        except Exception as exc:
            log.exception("Проверка GitHub Releases завершилась ошибкой")
            self.failed.emit(str(exc))


class AssetDownloadWorker(QThread):
    progress = pyqtSignal(int, int, float, str)  # done, total, bytes/sec, status
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
        target: Path | None = None
        try:
            if not self.url.startswith("https://github.com/"):
                raise ValueError("Ссылка загрузки должна вести на GitHub по HTTPS.")
            if not re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256):
                raise ValueError("Неверная контрольная сумма SHA-256.")
            folder = Path.home() / ".fiercylauncher" / "updates"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / self.filename
            if target.is_file():
                cached_digest = hashlib.sha256()
                with target.open("rb") as cached_file:
                    for block in iter(lambda: cached_file.read(1024 * 1024), b""):
                        cached_digest.update(block)
                if cached_digest.hexdigest().lower() == self.expected_sha256:
                    size = target.stat().st_size
                    self.progress.emit(size, size, 0.0, f"Используем проверенный кеш: {self.filename}")
                    self.completed.emit(str(target))
                    return
                target.unlink(missing_ok=True)
            digest = hashlib.sha256()
            done = 0
            started = time.monotonic()
            with requests.get(self.url, stream=True, timeout=(10, 90),
                              headers={"User-Agent": USER_AGENT}) as response:
                response.raise_for_status()
                if not str(response.url).startswith("https://"):
                    raise ValueError("GitHub перенаправил загрузку на небезопасный адрес.")
                total = int(response.headers.get("Content-Length", 0) or 0)
                with target.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=512 * 1024):
                        if self._cancel.is_set():
                            output.close()
                            target.unlink(missing_ok=True)
                            self.cancelled.emit()
                            return
                        if not chunk:
                            continue
                        output.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        elapsed = max(0.001, time.monotonic() - started)
                        speed = done / elapsed
                        percent = int(done * 100 / total) if total else -1
                        status = f"Загрузка {done / 1048576:.1f} МБ" + (f" / {total / 1048576:.1f} МБ" if total else "")
                        self.progress.emit(percent, total, speed, status)
            if digest.hexdigest() != self.expected_sha256:
                target.unlink(missing_ok=True)
                raise ValueError("SHA-256 не совпал. Архив удалён, обновление не применено.")
            log.info("Скачано обновление %s (%s байт), SHA-256 проверен", self.filename, done)
            self.completed.emit(str(target))
        except Exception as exc:
            if target and target.exists():
                target.unlink(missing_ok=True)
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
                response = requests.get(url, timeout=(5, 15), headers={"User-Agent": USER_AGENT})
                if response.status_code == 404:
                    log.info("Статический файл GitHub пока не опубликован: %s", url)
                    continue
                response.raise_for_status()
                parsed = response.json()
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
            with requests.get(self.url, stream=True, timeout=(5, 20),
                              headers={"User-Agent": USER_AGENT}) as response:
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
