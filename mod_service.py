"""Моды Modrinth/CurseForge и локальные операции с папкой mods профиля."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import requests
from PyQt5.QtCore import QThread, pyqtSignal

from version import USER_AGENT

log = logging.getLogger("fiercy.mods")
MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"


def mods_directory(instance_dir: str | Path) -> Path:
    path = Path(instance_dir).expanduser().resolve() / "mods"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_mod_files(instance_dir: str | Path) -> list[dict]:
    folder = mods_directory(instance_dir)
    output = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or not (path.name.lower().endswith(".jar") or path.name.lower().endswith(".jar.disabled")):
            continue
        disabled = path.name.lower().endswith(".disabled")
        output.append({"name": path.name, "path": str(path), "enabled": not disabled,
                       "size": path.stat().st_size})
    return output


def set_mod_enabled(instance_dir: str | Path, filename: str, enabled: bool) -> Path:
    folder = mods_directory(instance_dir)
    source = (folder / Path(filename).name).resolve()
    if source.parent != folder or not source.is_file():
        raise ValueError("Файл мода не найден в папке текущего профиля.")
    if enabled and source.name.lower().endswith(".jar.disabled"):
        target = source.with_name(source.name[:-9])
    elif not enabled and source.name.lower().endswith(".jar"):
        target = source.with_name(source.name + ".disabled")
    else:
        return source
    if target.exists():
        raise FileExistsError(f"Файл уже существует: {target.name}")
    source.rename(target)
    return target


def add_local_mods(instance_dir: str | Path, files: list[str]) -> list[Path]:
    folder = mods_directory(instance_dir)
    added: list[Path] = []
    for raw in files:
        source = Path(raw).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() != ".jar":
            continue
        target = folder / source.name
        if target.exists():
            target = folder / f"{source.stem}-{int(time.time())}{source.suffix}"
        shutil.copy2(source, target)
        added.append(target)
    return added


def _safe_filename(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._+() -]", "_", name).strip(" .")
    if not name or name in (".", "..") or len(name) > 180:
        raise ValueError("Сервис модов вернул некорректное имя файла.")
    return name


def _download_verified(url: str, destination: Path, hashes: dict | None = None,
                       max_size: int = 500 * 1024 * 1024) -> Path:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Мод должен загружаться по защищённой HTTPS-ссылке.")
    allowed = ("cdn.modrinth.com", "mediafilez.forgecdn.net", "edge.forgecdn.net", "curseforge.com")
    if not any(parsed.hostname == host or parsed.hostname.endswith("." + host) for host in allowed):
        raise ValueError(f"Неизвестный домен загрузки мода: {parsed.hostname}")
    digesters = {}
    if isinstance(hashes, dict):
        for algorithm in ("sha512", "sha1", "sha256"):
            expected = str(hashes.get(algorithm, "")).lower()
            if expected:
                digesters[algorithm] = (hashlib.new(algorithm), expected)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".download")
    written = 0
    try:
        with requests.get(url, stream=True, timeout=(10, 90), headers={"User-Agent": USER_AGENT}) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length", "0") or 0)
            if total > max_size:
                raise ValueError("Файл мода превышает лимит 500 МБ.")
            with temp.open("wb") as output:
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_size:
                        raise ValueError("Файл мода превышает лимит 500 МБ.")
                    output.write(chunk)
                    for digest, _expected in digesters.values():
                        digest.update(chunk)
        for algorithm, (digest, expected) in digesters.items():
            if digest.hexdigest().lower() != expected:
                raise ValueError(f"Контрольная сумма {algorithm.upper()} не совпала для {destination.name}.")
        if written == 0:
            raise ValueError("Сервис вернул пустой файл мода.")
        os.replace(temp, destination)
        return destination
    except Exception:
        temp.unlink(missing_ok=True)
        raise


class ModrinthSearchWorker(QThread):
    """Постраничный каталог проектов Modrinth; пустой запрос = популярные проекты."""
    loaded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, query: str = "", game_version: str = "", loader: str = "",
                 project_type: str = "mod", sort: str = "downloads", offset: int = 0,
                 limit: int = 48, parent=None):
        super().__init__(parent)
        self.query = query.strip()
        self.game_version = game_version.strip()
        self.loader = loader.strip().lower()
        self.project_type = project_type.strip().lower()
        self.sort = sort if sort in {"relevance", "downloads", "follows", "newest", "updated"} else "downloads"
        self.offset = max(0, int(offset))
        self.limit = max(1, min(100, int(limit)))

    def run(self):
        try:
            supported_types = {"mod", "resourcepack", "shader", "modpack"}
            if self.project_type not in supported_types:
                raise ValueError("Неизвестный тип контента Modrinth.")
            facets = [[f"project_type:{self.project_type}"]]
            if self.game_version:
                facets.append([f"versions:{self.game_version}"])
            # Для модов ограничиваем выдачу одновременно версией Minecraft и
            # загрузчиком активной сборки. Modrinth индексирует loader как category.
            if self.project_type == "mod" and self.loader in {"fabric", "forge", "neoforge", "quilt"}:
                facets.append([f"categories:{self.loader}"])
            params = {
                "facets": json.dumps(facets),
                "limit": self.limit,
                "offset": self.offset,
                "index": self.sort,
            }
            if self.query:
                params["query"] = self.query
            response = requests.get(
                f"{MODRINTH_API}/search", params=params,
                timeout=(8, 25), headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            data = response.json()
            hits = data.get("hits", []) if isinstance(data, dict) else []
            results = []
            for item in hits:
                project_id = str(item.get("project_id", "")).strip()
                if not project_id:
                    continue
                results.append({
                    "project_id": project_id,
                    "title": str(item.get("title", "Без названия")),
                    "description": str(item.get("description", ""))[:500],
                    "downloads": int(item.get("downloads", 0) or 0),
                    "follows": int(item.get("follows", 0) or 0),
                    "author": str(item.get("author", "")),
                    "icon_url": str(item.get("icon_url", "") or ""),
                    "categories": list(item.get("display_categories", []) or item.get("categories", [])),
                    "slug": str(item.get("slug", "")),
                    "project_type": self.project_type,
                    "versions": list(item.get("versions", []) or []),
                    "date_modified": str(item.get("date_modified", "") or ""),
                })
            total_hits = int(data.get("total_hits", len(results)) or len(results)) if isinstance(data, dict) else len(results)
            self.loaded.emit({
                "results": results,
                "total_hits": total_hits,
                "offset": self.offset,
                "limit": self.limit,
            })
        except Exception as exc:
            log.exception("Modrinth search failed")
            self.failed.emit(f"Modrinth не смог загрузить каталог: {exc}")


class ModrinthProjectDetailsWorker(QThread):
    """Загружает полную карточку проекта, галерею и совместимые версии."""
    loaded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, project_id: str, game_version: str = "", loader: str = "",
                 project_type: str = "mod", parent=None):
        super().__init__(parent)
        self.project_id = str(project_id).strip()
        self.game_version = str(game_version).strip()
        self.loader = str(loader).strip().lower()
        self.project_type = str(project_type).strip().lower()

    def run(self):
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", self.project_id):
                raise ValueError("Некорректный ID проекта Modrinth.")
            headers = {"User-Agent": USER_AGENT}
            response = requests.get(f"{MODRINTH_API}/project/{self.project_id}",
                                    timeout=(8, 25), headers=headers)
            response.raise_for_status()
            project = response.json()
            params = {}
            if self.game_version:
                params["game_versions"] = json.dumps([self.game_version])
            if self.project_type == "mod" and self.loader and self.loader != "vanilla":
                params["loaders"] = json.dumps([self.loader])
            versions_response = requests.get(
                f"{MODRINTH_API}/project/{self.project_id}/version", params=params,
                timeout=(8, 25), headers=headers,
            )
            versions_response.raise_for_status()
            versions = versions_response.json()
            if not isinstance(versions, list):
                versions = []
            self.loaded.emit({"project": project, "versions": versions})
        except Exception as exc:
            log.exception("Modrinth project details failed")
            self.failed.emit(f"Не удалось открыть страницу проекта Modrinth: {exc}")


class ModrinthImageWorker(QThread):
    """Загружает только изображения с CDN Modrinth для карточек и галереи."""
    loaded = pyqtSignal(bytes)
    failed = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = str(url).strip()

    @staticmethod
    def _allowed_host(host: str) -> bool:
        host = (host or "").lower().rstrip(".")
        return host == "modrinth.com" or host.endswith(".modrinth.com")

    def run(self):
        try:
            parsed = urlsplit(self.url)
            if parsed.scheme != "https" or not self._allowed_host(parsed.hostname or ""):
                raise ValueError("Изображение должно находиться на HTTPS-CDN Modrinth.")
            with requests.get(self.url, stream=True, timeout=(5, 20),
                              headers={"User-Agent": USER_AGENT}) as response:
                response.raise_for_status()
                final_url = urlsplit(str(response.url))
                if final_url.scheme != "https" or not self._allowed_host(final_url.hostname or ""):
                    raise ValueError("Ссылка изображения перенаправила на посторонний домен.")
                if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    raise ValueError("Modrinth вернул не изображение.")
                length = int(response.headers.get("Content-Length", "0") or 0)
                if length > 8 * 1024 * 1024:
                    raise ValueError("Изображение превышает 8 МБ.")
                payload = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if chunk:
                        payload.extend(chunk)
                        if len(payload) > 8 * 1024 * 1024:
                            raise ValueError("Изображение превышает 8 МБ.")
                if not payload:
                    raise ValueError("Изображение пустое.")
                self.loaded.emit(bytes(payload))
        except Exception as exc:
            self.failed.emit(str(exc))


class ModrinthInstallWorker(QThread):
    completed = pyqtSignal(list)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, project_id: str, game_version: str, loader: str,
                 instance_dir: str, project_type: str = "mod", version_id: str = "", parent=None):
        super().__init__(parent)
        self.project_id = str(project_id).strip()
        self.game_version = str(game_version).strip()
        self.loader = str(loader).strip().lower()
        self.instance_dir = Path(instance_dir)
        self.project_type = str(project_type or "mod").strip().lower()
        self.version_id = str(version_id or "").strip()

    def _request_version(self, dependency: dict) -> dict:
        version_id = str(dependency.get("version_id") or "").strip()
        project_id = str(dependency.get("project_id") or "").strip()
        headers = {"User-Agent": USER_AGENT}
        if version_id:
            response = requests.get(
                f"{MODRINTH_API}/version/{version_id}", timeout=(8, 25), headers=headers,
            )
            response.raise_for_status()
            version = response.json()
            if self.game_version and self.game_version not in version.get("game_versions", []):
                raise RuntimeError("Выбранная версия или обязательная зависимость не поддерживает Minecraft этой сборки.")
            loaders = [str(item).lower() for item in version.get("loaders", [])]
            if self.project_type == "mod" and self.loader and self.loader != "vanilla" and loaders and self.loader not in loaders:
                raise RuntimeError("Выбранная версия или обязательная зависимость не поддерживает загрузчик этой сборки.")
            return version
        if not project_id:
            raise RuntimeError("Modrinth указал обязательную зависимость без project_id/version_id.")
        params = {"game_versions": json.dumps([self.game_version])} if self.game_version else {}
        if self.project_type == "mod" and self.loader and self.loader != "vanilla":
            params["loaders"] = json.dumps([self.loader])
        response = requests.get(
            f"{MODRINTH_API}/project/{project_id}/version", params=params,
            timeout=(8, 25), headers=headers,
        )
        response.raise_for_status()
        versions = response.json()
        if not versions:
            raise RuntimeError(f"Не найдена обязательная зависимость Modrinth: {project_id}.")
        return next((item for item in versions if item.get("version_type") == "release"), versions[0])

    def _install_single_file(self, version: dict) -> list[str]:
        files = [f for f in version.get("files", []) if isinstance(f, dict) and f.get("url")]
        if not files:
            raise RuntimeError("У выбранной версии Modrinth нет файлов для установки.")
        primary = next((f for f in files if f.get("primary")), files[0])
        filename = _safe_filename(primary.get("filename", "download"))
        if self.project_type == "mod":
            if not filename.lower().endswith(".jar"):
                primary = next((f for f in files if str(f.get("filename", "")).lower().endswith(".jar")), None)
                if not primary:
                    raise RuntimeError("У выбранной версии мода нет JAR-файла.")
                filename = _safe_filename(primary.get("filename", "mod.jar"))
            target_dir = mods_directory(self.instance_dir)
        elif self.project_type == "resourcepack":
            target_dir = self.instance_dir / "resourcepacks"
        elif self.project_type == "shader":
            target_dir = self.instance_dir / "shaderpacks"
        elif self.project_type == "modpack":
            target_dir = self.instance_dir / ".fiercy-downloads"
            if not filename.lower().endswith(".mrpack"):
                raise RuntimeError("Modrinth-сборка должна быть файлом .mrpack.")
        else:
            raise RuntimeError("Этот тип проекта пока не поддерживается для установки.")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        if target.exists():
            target = target_dir / f"{target.stem}-{str(version.get('id', 'version'))[:8]}{target.suffix}"
        self.progress.emit(f"Скачивание {filename}…")
        saved = _download_verified(primary["url"], target, primary.get("hashes", {}),
                                   max_size=1024 * 1024 * 1024)
        return [str(saved)]

    def run(self):
        try:
            if self.project_type not in {"mod", "resourcepack", "shader", "modpack"}:
                raise RuntimeError("Неподдерживаемый тип проекта Modrinth.")
            if self.project_type == "mod":
                queue = [{"version_id": self.version_id}] if self.version_id else [{"project_id": self.project_id}]
                seen: set[str] = set()
                installed: list[str] = []
                while queue:
                    if len(seen) >= 32:
                        raise RuntimeError("У мода слишком много обязательных зависимостей (лимит 32).")
                    dependency = queue.pop(0)
                    identity = str(dependency.get("version_id") or dependency.get("project_id") or "").strip()
                    if not identity or identity in seen:
                        continue
                    seen.add(identity)
                    version = self._request_version(dependency)
                    version_id = str(version.get("id", ""))
                    if version_id:
                        seen.add(version_id)
                    installed.extend(self._install_single_file(version))
                    for child in version.get("dependencies", []):
                        if not isinstance(child, dict) or child.get("dependency_type") != "required":
                            continue
                        child_id = str(child.get("version_id") or child.get("project_id") or "").strip()
                        if child_id and child_id not in seen:
                            queue.append(child)
                if not installed:
                    raise RuntimeError("Modrinth не вернул ни одного JAR-файла.")
                self.completed.emit(installed)
            else:
                version = self._request_version({"version_id": self.version_id} if self.version_id else
                                                {"project_id": self.project_id})
                installed = self._install_single_file(version)
                self.completed.emit(installed)
        except Exception as exc:
            log.exception("Modrinth install failed")
            self.failed.emit(str(exc))


class CurseForgeSearchWorker(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, query: str, game_version: str, loader: str, api_key: str, parent=None):
        super().__init__(parent)
        self.query, self.game_version, self.loader, self.api_key = query.strip(), game_version.strip(), loader.lower(), api_key.strip()

    def run(self):
        try:
            if not self.api_key:
                raise RuntimeError("Для CurseForge нужен собственный API key. Его можно сохранить в настройках интеграции.")
            loader_types = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
            params = {"gameId": 432, "classId": 6, "searchFilter": self.query,
                      "pageSize": 30, "sortField": 2, "sortOrder": "desc"}
            if self.game_version:
                params["gameVersion"] = self.game_version
            if self.loader in loader_types:
                params["modLoaderType"] = loader_types[self.loader]
            response = requests.get(f"{CURSEFORGE_API}/mods/search", params=params,
                                    headers={"x-api-key": self.api_key, "Accept": "application/json",
                                             "User-Agent": USER_AGENT}, timeout=(8, 25))
            response.raise_for_status()
            results = []
            for item in response.json().get("data", []):
                results.append({"project_id": str(item.get("id", "")),
                                "title": str(item.get("name", "Без названия")),
                                "description": str(item.get("summary", ""))[:240],
                                "downloads": int(item.get("downloadCount", 0) or 0),
                                "source": "curseforge"})
            self.loaded.emit(results)
        except Exception as exc:
            log.exception("CurseForge search failed")
            self.failed.emit(str(exc))


class CurseForgeInstallWorker(QThread):
    completed = pyqtSignal(list)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, project_id: str, game_version: str, loader: str,
                 instance_dir: str, api_key: str, parent=None):
        super().__init__(parent)
        self.project_id, self.game_version, self.loader = str(project_id), game_version, loader.lower()
        self.instance_dir, self.api_key = Path(instance_dir), api_key.strip()

    def run(self):
        try:
            if not self.api_key:
                raise RuntimeError("Не настроен API key CurseForge.")
            loader_types = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
            params = {"gameVersion": self.game_version, "pageSize": 20, "sortField": 4, "sortOrder": "desc"}
            if self.loader in loader_types:
                params["modLoaderType"] = loader_types[self.loader]
            headers = {"x-api-key": self.api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
            response = requests.get(f"{CURSEFORGE_API}/mods/{self.project_id}/files", params=params,
                                    headers=headers, timeout=(8, 25))
            response.raise_for_status()
            files = response.json().get("data", [])
            if not files:
                raise RuntimeError("CurseForge не нашёл совместимый файл для этой версии.")
            file_info = files[0]
            file_id = str(file_info.get("id", ""))
            url_response = requests.get(f"{CURSEFORGE_API}/mods/{self.project_id}/files/{file_id}/download-url",
                                        headers=headers, timeout=(8, 20))
            url_response.raise_for_status()
            download_url = url_response.json().get("data")
            if not download_url:
                raise RuntimeError("CurseForge не вернул публичный URL файла. Возможно, файл недоступен для сторонней загрузки.")
            filename = _safe_filename(file_info.get("fileName", "curseforge-mod.jar"))
            if not filename.lower().endswith(".jar"):
                raise RuntimeError("Выбранный файл CurseForge не является JAR-модом.")
            hashes = {}
            for item in file_info.get("hashes", []):
                algo = item.get("algo")
                value = item.get("value")
                if algo == 1:
                    hashes["sha1"] = value
                elif algo == 2:
                    hashes["md5"] = value
            self.progress.emit(f"Загрузка {filename} с CurseForge…")
            target = mods_directory(self.instance_dir) / filename
            saved = _download_verified(download_url, target, hashes)
            self.completed.emit([str(saved)])
        except Exception as exc:
            log.exception("CurseForge install failed")
            self.failed.emit(str(exc))
