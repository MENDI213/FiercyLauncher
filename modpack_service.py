"""Импорт Modrinth .mrpack и CurseForge export .zip в игровой профиль."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

import requests
from PyQt5.QtCore import QThread, pyqtSignal
import minecraft_launcher_lib as mll

from minecraft_service import _callback, resolve_java, install_neoforge_profile
from update_download import DownloadCancelled
from mod_service import _download_verified, _safe_filename
from version import USER_AGENT
from secure_archive import validate_zip, normalise_member_name

log = logging.getLogger("fiercy.modpack")


def _safe_member_path(base: Path, relative: str) -> Path:
    normal, is_dir = normalise_member_name(str(relative))
    if is_dir:
        raise ValueError(f"Ожидался путь файла, получен каталог: {relative}")
    pure = PurePosixPath(normal)
    root = base.expanduser().resolve()
    target = root.joinpath(*pure.parts)
    cursor = root
    for part in pure.parts[:-1]:
        cursor = cursor / part
        is_junction = getattr(cursor, "is_junction", lambda: False)()
        if cursor.exists() and (cursor.is_symlink() or is_junction):
            raise ValueError(f"Путь модпака проходит через ссылку или junction: {relative}")
    is_junction = getattr(target, "is_junction", lambda: False)()
    if target.exists() and (target.is_symlink() or is_junction):
        raise ValueError(f"Нельзя перезаписывать ссылку или junction: {relative}")
    if target.exists() and target.is_file() and getattr(target.stat(), "st_nlink", 1) > 1:
        raise ValueError(f"Нельзя перезаписывать файл с жёсткими ссылками: {relative}")
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Файл модпака выходит за пределы профиля: {relative}")
    return target


def inspect_modpack(archive_path: str | Path) -> dict:
    path = Path(archive_path)
    with zipfile.ZipFile(path, "r") as archive:
        validate_zip(archive, max_archive_size=1024**3, max_total_size=4 * 1024**3,
                     max_file_size=1024**3, max_entries=100_000)
        names = {item.filename.replace("\\", "/").lstrip("/") for item in archive.infolist()}
        if "modrinth.index.json" in names:
            index = json.loads(archive.read("modrinth.index.json").decode("utf-8"))
            if not isinstance(index, dict) or index.get("formatVersion") != 1:
                raise ValueError("Неподдерживаемый формат modrinth.index.json")
            deps = index.get("dependencies", {})
            if not isinstance(deps, dict) or not isinstance(index.get("files", []), list):
                raise ValueError("Манифест Modrinth имеет неверную структуру.")
            if len(index.get("files", [])) > 50_000:
                raise ValueError("В сборке Modrinth слишком много файлов.")
            for entry in index.get("files", []):
                if not isinstance(entry, dict):
                    raise ValueError("Элемент списка файлов Modrinth имеет неверный формат.")
                normalise_member_name(str(entry.get("path", "")))
                hashes = entry.get("hashes")
                downloads = entry.get("downloads")
                if not isinstance(hashes, dict) or not any(hashes.get(key) for key in ("sha512", "sha1", "sha256")):
                    raise ValueError("В манифесте Modrinth для файла отсутствует контрольная сумма.")
                if not isinstance(downloads, list) or any(not isinstance(link, str) for link in downloads):
                    raise ValueError("Список ссылок Modrinth имеет неверный формат.")
            loader_id, loader_version = "vanilla", ""
            for dependency_name, dependency_version in deps.items():
                key = str(dependency_name).lower()
                if key.endswith("-loader"):
                    loader_id = key[:-7]
                    loader_version = str(dependency_version or "")
                    break
                if key in ("neoforge", "forge", "fabric", "quilt"):
                    loader_id = key
                    loader_version = str(dependency_version or "")
                    break
            return {"format": "modrinth", "name": str(index.get("name") or path.stem),
                    "minecraft_version": str(deps.get("minecraft", "")),
                    "loader": loader_id, "loader_version": loader_version}
        if "manifest.json" in names:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files", []), list):
                raise ValueError("Манифест CurseForge имеет неверную структуру.")
            mc = manifest.get("minecraft", {})
            if not isinstance(mc, dict) or len(manifest.get("files", [])) > 50_000:
                raise ValueError("Манифест CurseForge имеет неверную структуру.")
            loader_id, loader_version = "vanilla", ""
            for item in mc.get("modLoaders", []):
                loader_string = str(item.get("id", ""))
                for loader in ("neoforge", "forge", "fabric", "quilt"):
                    if loader_string.startswith(loader + "-"):
                        loader_id = loader
                        loader_version = loader_string[len(loader) + 1:]
                        break
            return {"format": "curseforge", "name": str(manifest.get("name") or path.stem),
                    "minecraft_version": str(mc.get("version", "")),
                    "loader": loader_id, "loader_version": loader_version}
    raise ValueError("Это не Modrinth .mrpack и не экспортированный CurseForge .zip.")


class ModpackImportWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, archive_path: str, game_root: str, instance_dir: str,
                 java_path: str = "", auto_java: bool = True,
                 curseforge_api_key: str = "", parent=None,
                 download_control=None, rate_limiter=None):
        super().__init__(parent)
        self.archive_path = Path(archive_path)
        self.game_root = Path(game_root)
        self.instance_dir = Path(instance_dir)
        self.java_path = java_path
        self.auto_java = auto_java
        self.curseforge_api_key = curseforge_api_key.strip()
        self.download_control = download_control
        self.rate_limiter = rate_limiter
        self.cancel_event = (download_control.cancel_event if download_control is not None
                             else threading.Event())

    def cancel(self) -> None:
        if self.download_control is not None:
            self.download_control.cancel()
        else:
            self.cancel_event.set()

    def pause(self) -> None:
        if self.download_control is not None:
            self.download_control.pause()

    def resume(self) -> None:
        if self.download_control is not None:
            self.download_control.resume()

    def _check_cancelled(self) -> None:
        if self.download_control is not None:
            self.download_control.checkpoint(0, self.rate_limiter)
        elif self.cancel_event.is_set():
            raise DownloadCancelled("Импорт сборки остановлен пользователем.")

    def run(self):
        try:
            self._check_cancelled()
            metadata = inspect_modpack(self.archive_path)
            if not metadata["minecraft_version"]:
                raise RuntimeError("В манифесте сборки не указана версия Minecraft.")
            self.game_root.mkdir(parents=True, exist_ok=True)
            self.instance_dir.mkdir(parents=True, exist_ok=True)
            self.progress.emit(4, "Проверка формата сборки…")
            callback = _callback(self, self.rate_limiter)
            mll.install.install_minecraft_version(metadata["minecraft_version"], str(self.game_root), callback=callback)
            self.progress.emit(35, "Подготовка Java…")
            java = resolve_java(metadata["minecraft_version"], self.game_root, self.java_path, self.auto_java, callback)
            installed_id = metadata["minecraft_version"]
            if metadata["loader"] != "vanilla":
                self.progress.emit(45, f"Установка {metadata['loader'].title()}…")
                if metadata["loader"] == "neoforge":
                    installed_id = install_neoforge_profile(
                        metadata["minecraft_version"], metadata["loader_version"],
                        self.game_root, java, callback=callback,
                        progress=lambda message: self.progress.emit(45, message),
                        cancel_event=self.cancel_event, download_control=self.download_control,
                        rate_limiter=self.rate_limiter,
                    )
                else:
                    loader = mll.mod_loader.get_mod_loader(metadata["loader"])
                    installed_id = loader.install(metadata["minecraft_version"], str(self.game_root),
                                                   loader_version=metadata["loader_version"] or None,
                                                   callback=callback, java=java)
                resolve_java(str(installed_id), self.game_root, self.java_path, self.auto_java, callback)

            if metadata["format"] == "modrinth":
                self._install_modrinth_pack()
            else:
                self._install_curseforge_pack()
            for folder in ("mods", "config", "saves", "resourcepacks", "shaderpacks", "screenshots"):
                self._check_cancelled()
                (self.instance_dir / folder).mkdir(parents=True, exist_ok=True)
            self._check_cancelled()
            metadata["installed_version_id"] = installed_id
            self.progress.emit(100, "Сборка импортирована и готова к запуску")
            self.completed.emit(metadata)
        except DownloadCancelled:
            log.info("Импорт сборки отменён пользователем: %s", self.archive_path)
            self.cancelled.emit()
        except Exception as exc:
            log.exception("Modpack import failed")
            self.failed.emit(str(exc))

    def _install_modrinth_pack(self):
        with zipfile.ZipFile(self.archive_path, "r") as archive:
            index = json.loads(archive.read("modrinth.index.json").decode("utf-8"))
            files = index.get("files", [])
            total = max(1, len(files))
            for number, item in enumerate(files, 1):
                self._check_cancelled()
                env = item.get("env", {})
                if env.get("client") == "unsupported":
                    continue
                relative = str(item.get("path", ""))
                target = _safe_member_path(self.instance_dir, relative)
                downloads = item.get("downloads", [])
                if not downloads:
                    log.warning("У модпака отсутствует ссылка для файла %s", relative)
                    continue
                hashes = item.get("hashes", {})
                url = str(downloads[0])
                overall = 50 + int((number - 1) * 35 / total)
                _download_verified(
                    url, target, hashes, max_size=1024 * 1024 * 1024,
                    cancel_event=self.cancel_event,
                    download_control=self.download_control, rate_limiter=self.rate_limiter,
                    progress=lambda message, n=number, t=total, base=overall: self.progress.emit(
                        base, f"Файлы сборки {n}/{t} · {target.name}: {message}"
                    ),
                )
                self.progress.emit(50 + int(number * 35 / total), f"Загружен файл сборки: {number}/{total}")
            self._extract_overrides(archive, "overrides/")

    def _install_curseforge_pack(self):
        if not self.curseforge_api_key:
            raise RuntimeError("Для сборки CurseForge нужен API key. Открой раздел модов и укажи личный ключ CurseForge.")
        with zipfile.ZipFile(self.archive_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            files = manifest.get("files", [])
            total = max(1, len(files))
            headers = {"x-api-key": self.curseforge_api_key, "Accept": "application/json",
                       "User-Agent": USER_AGENT}
            for number, item in enumerate(files, 1):
                self._check_cancelled()
                if item.get("required", True) is False:
                    continue
                mod_id, file_id = str(item.get("projectID", "")), str(item.get("fileID", ""))
                if not mod_id or not file_id:
                    continue
                response = requests.get(
                    f"https://api.curseforge.com/v1/mods/{mod_id}/files/{file_id}/download-url",
                    headers=headers, timeout=(8, 25),
                )
                response.raise_for_status()
                url = response.json().get("data")
                if not url:
                    raise RuntimeError(f"CurseForge не выдал ссылку на файл {file_id}.")
                file_info = requests.get(
                    f"https://api.curseforge.com/v1/mods/{mod_id}/files/{file_id}",
                    headers=headers, timeout=(8, 25),
                )
                file_info.raise_for_status()
                filename = _safe_filename(file_info.json().get("data", {}).get("fileName", f"{mod_id}-{file_id}.jar"))
                if filename.lower().endswith(".jar"):
                    target = self.instance_dir / "mods" / filename
                    _download_verified(
                        url, target, {}, max_size=1024 * 1024 * 1024,
                        cancel_event=self.cancel_event,
                        download_control=self.download_control, rate_limiter=self.rate_limiter,
                        progress=lambda message, n=number, t=total: self.progress.emit(
                            50 + int((n - 1) * 35 / t), f"CurseForge {n}/{t} · {filename}: {message}"
                        ),
                    )
                self.progress.emit(50 + int(number * 35 / total), f"Загрузка CurseForge: {number}/{total}")
            self._extract_overrides(archive, "overrides/")

    def _extract_overrides(self, archive: zipfile.ZipFile, prefix: str):
        for info in archive.infolist():
            self._check_cancelled()
            name = info.filename.replace("\\", "/")
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            relative = name[len(prefix):]
            if not relative:
                continue
            target = _safe_member_path(self.instance_dir, relative)
            if info.file_size > 1024 * 1024 * 1024:
                raise ValueError(f"Файл в overrides слишком большой: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".fiercy-override-", suffix=".part", dir=str(target.parent))
            temp_target = Path(temp_name)
            try:
                with archive.open(info, "r") as source, os.fdopen(fd, "wb") as output:
                    fd = -1
                    while True:
                        self._check_cancelled()
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                os.replace(temp_target, target)
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                temp_target.unlink(missing_ok=True)
