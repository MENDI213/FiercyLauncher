"""Установка и запуск Minecraft через minecraft-launcher-lib 8.x.

Библиотека Mojang-совместимо читает version JSON, включая наследование,
собирает classpath, natives, assets и параметры Forge/Fabric/NeoForge/Quilt.
Не дублируем эту хрупкую логику вручную.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import platform
import tempfile
import zipfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from typing import Any

import minecraft_launcher_lib as mll
import psutil
from PyQt5.QtCore import QThread, pyqtSignal

from version import LAUNCHER_NAME, LAUNCHER_VERSION

log = logging.getLogger("fiercy.minecraft")


def _callback(worker: QThread):
    state = {"current": 0.0, "maximum": 0.0, "status": "Подготовка…"}

    def status(value: Any) -> None:
        state["status"] = str(value)
        signal = getattr(worker, "progress", None)
        if signal is not None:
            if worker.__class__.__name__ == "LaunchWorker":
                signal.emit(state["status"])
            else:
                signal.emit(-1, state["status"])

    def current(value: Any) -> None:
        try:
            state["current"] = float(value or 0)
        except (TypeError, ValueError):
            state["current"] = 0
        maximum = state["maximum"]
        percent = int(state["current"] * 100 / maximum) if maximum else 0
        signal = getattr(worker, "progress", None)
        if signal is not None and worker.__class__.__name__ != "LaunchWorker":
            signal.emit(max(0, min(100, percent)), state["status"])

    def maximum(value: Any) -> None:
        try:
            state["maximum"] = float(value or 0)
        except (TypeError, ValueError):
            state["maximum"] = 0
        current(state["current"])

    return {"setStatus": status, "setProgress": current, "setMax": maximum}


def _download_neoforge_file(url: str, destination: Path, progress=None) -> Path:
    """Downloads a NeoForge installer atomically, retries transient network errors.

    The reported error in earlier logs was a TCP connect timeout, not an invalid
    Maven path. Use a longer connect timeout, bounded retries, and atomic file
    replacement so a failed transfer never leaves a partial JAR.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error = None
    headers = {"User-Agent": LAUNCHER_NAME, "Accept": "application/java-archive, */*"}
    for attempt in range(3):
        temporary.unlink(missing_ok=True)
        try:
            if progress:
                progress(f"Скачиваем NeoForge: попытка {attempt + 1}/3…")
            with requests.get(url, stream=True, timeout=(25, 180), headers=headers) as response:
                response.raise_for_status()
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text/html" in content_type:
                    raise RuntimeError("Вместо установщика Maven вернул HTML-страницу.")
                expected = int(response.headers.get("Content-Length", "0") or 0)
                if expected and expected > 256 * 1024 * 1024:
                    raise RuntimeError("Размер установщика NeoForge неожиданно велик.")
                written = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > 256 * 1024 * 1024:
                            raise RuntimeError("Установщик NeoForge превысил лимит 256 МБ.")
                        output.write(chunk)
                if written < 1024 or not zipfile.is_zipfile(temporary):
                    raise RuntimeError("Скачанный установщик NeoForge повреждён или это не JAR-файл.")
            temporary.replace(destination)
            return destination
        except (requests.RequestException, OSError, RuntimeError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            log.warning("NeoForge installer download attempt %s/3 failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
    if isinstance(last_error, requests.exceptions.ConnectTimeout):
        raise RuntimeError(
            "Соединение с официальным сервером NeoForge не устанавливается (ConnectTimeout). "
            "Это не ошибка версии и не отсутствие файла: проверь доступ к maven.neoforged.net, "
            "VPN/прокси, DNS и сетевой экран. Можно скачать JAR в браузере по официальной ссылке "
            f"{url} и положить его в папку {destination}. Затем снова нажми «Установить / проверить». "
            f"Последняя ошибка: {last_error}"
        )
    raise RuntimeError(
        "Не удалось скачать установщик NeoForge с официального Maven после 3 попыток. "
        "Проверь доступ к maven.neoforged.net, VPN/прокси и настройки антивируса. "
        f"Официальная ссылка: {url}. Кэш установщика: {destination}. Последняя ошибка: {last_error}"
    )


def install_neoforge_client(minecraft_version: str, loader_version: str, game_root: str | Path,
                            java_path: str, progress=None) -> str:
    """Install official NeoForge client into the shared versions directory."""
    game_root = Path(game_root).expanduser().resolve()
    version = str(loader_version or "").strip()
    metadata_url = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
    headers = {"User-Agent": LAUNCHER_NAME}
    if not version:
        match = re.fullmatch(r"1\.(\d+)(?:\.(\d+))?", str(minecraft_version).strip())
        if not match:
            raise RuntimeError("Не удалось определить совместимую версию NeoForge для этой версии Minecraft.")
        prefix = f"{match.group(1)}.{match.group(2) or '0'}."
        response = None
        last_error = None
        for attempt in range(4):
            try:
                response = requests.get(metadata_url, timeout=(35, 60), headers=headers)
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
        if response is None:
            raise RuntimeError(f"Не удалось получить список версий NeoForge с официального Maven: {last_error}")
        metadata_root = ET.fromstring(response.content)
        candidates = [node.text.strip() for node in metadata_root.findall(".//version")
                      if node.text and node.text.strip().startswith(prefix) and "-" not in node.text]
        from packaging.version import Version
        candidates.sort(key=Version, reverse=True)
        if not candidates:
            raise RuntimeError(f"Для Minecraft {minecraft_version} не найден стабильный NeoForge.")
        version = candidates[0]

    # Protect against accidentally selecting a loader build for another MC line.
    mc_match = re.fullmatch(r"1\.(\d+)(?:\.(\d+))?", str(minecraft_version).strip())
    loader_match = re.match(r"^(\d+)\.(\d+)\.", version)
    if mc_match and loader_match:
        expected_prefix = (mc_match.group(1), mc_match.group(2) or "0")
        if loader_match.groups() != expected_prefix:
            raise RuntimeError(f"NeoForge {version} не относится к Minecraft {minecraft_version}.")

    installer_dir = game_root / ".fiercy" / "installers"
    installer = installer_dir / f"neoforge-{version}-installer.jar"
    if not installer.is_file() or not zipfile.is_zipfile(installer):
        from urllib.parse import quote
        url = ("https://maven.neoforged.net/releases/net/neoforged/neoforge/"
               f"{quote(version)}/neoforge-{quote(version)}-installer.jar")
        _download_neoforge_file(url, installer, progress)

    if progress:
        progress(f"Устанавливаем NeoForge {version}…")
    installer_java = str(java_path)
    if Path(installer_java).name.lower() == "javaw.exe":
        console_java = Path(installer_java).with_name("java.exe")
        if console_java.is_file():
            installer_java = str(console_java)
    completed = subprocess.run(
        [installer_java, "-Djava.awt.headless=true", "-jar", str(installer),
         "--installClient", str(game_root)],
        capture_output=True, text=True, timeout=900,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "")[-4500:]
        raise RuntimeError("Установщик NeoForge завершился с ошибкой.\n" + details)

    expected_id = f"neoforge-{version}"
    expected_json = game_root / "versions" / expected_id / f"{expected_id}.json"
    if expected_json.is_file():
        return expected_id
    versions_dir = game_root / "versions"
    candidates = [folder for folder in versions_dir.iterdir()
                  if folder.is_dir() and folder.name.lower().startswith("neoforge")
                  and (folder / f"{folder.name}.json").is_file()] if versions_dir.is_dir() else []
    if not candidates:
        raise RuntimeError("NeoForge installer завершился, но JSON установленной версии не найден.")
    return max(candidates, key=lambda folder: folder.stat().st_mtime).name


def find_system_java() -> str:
    """Ищет Java в PATH и JAVA_HOME, включая javaw.exe на Windows."""
    candidates: list[str] = []
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        home = Path(java_home)
        candidates.extend([str(home / "bin" / "javaw.exe"), str(home / "bin" / "java.exe"),
                           str(home / "bin" / "java")])
    for name in ("javaw", "java"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return ""


def java_major_version(java_path: str) -> int | None:
    """Читает версию выбранной Java; не полагается на имя файла."""
    try:
        probe_path = java_path
        if os.name == "nt" and Path(java_path).name.lower() == "javaw.exe":
            console_java = Path(java_path).with_name("java.exe")
            if console_java.is_file():
                probe_path = str(console_java)
        result = subprocess.run(
            [probe_path, "-version"], capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        output = (result.stderr or "") + "\n" + (result.stdout or "")
        match = re.search(r'version\s+"?([0-9]+)(?:\.([0-9]+))?', output, re.IGNORECASE)
        if match:
            first = int(match.group(1))
            second = int(match.group(2) or 0)
            return second if first == 1 and second else first
        executable = Path(java_path).resolve()
        java_home = executable.parent.parent
        release_file = java_home / "release"
        if release_file.is_file():
            release_text = release_file.read_text(encoding="utf-8", errors="replace")
            release_match = re.search(r'^JAVA_VERSION="?([0-9]+)(?:\.([0-9]+))?',
                                      release_text, re.MULTILINE)
            if release_match:
                first = int(release_match.group(1))
                second = int(release_match.group(2) or 0)
                return second if first == 1 and second else first
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _version_chain(version_id: str, minecraft_dir: Path) -> list[str]:
    """Возвращает ID версии и её родителей из локальных version JSON."""
    result: list[str] = []
    current = str(version_id).strip()
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        result.append(current)
        path = minecraft_dir / "versions" / current / f"{current}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            current = str(data.get("inheritsFrom", "")).strip()
        except (OSError, ValueError, TypeError):
            break
    return result


def required_java_major(version_id: str, minecraft_dir: Path) -> int | None:
    """Определяет Java из runtime metadata/version JSON и проверенных правил Mojang."""
    for candidate in _version_chain(version_id, minecraft_dir):
        try:
            info = mll.runtime.get_version_runtime_information(candidate, str(minecraft_dir))
            major = info.get("javaMajorVersion") if isinstance(info, dict) else None
            if major:
                return int(major)
        except Exception:
            log.debug("Runtime metadata недоступна для %s", candidate, exc_info=True)
        version_json = minecraft_dir / "versions" / candidate / f"{candidate}.json"
        try:
            raw = json.loads(version_json.read_text(encoding="utf-8"))
            java_version = raw.get("javaVersion", {})
            if isinstance(java_version, dict) and java_version.get("majorVersion"):
                return int(java_version["majorVersion"])
        except (OSError, ValueError, TypeError) as exc:
            log.debug("Не удалось прочитать javaVersion для %s: %s", candidate, exc)

    base_id = _version_chain(version_id, minecraft_dir)[-1]
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", base_id)
    if match:
        first, second, patch = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
        if first >= 26:
            return 25
        if first == 1:
            if second < 17:
                return 8
            if second == 17:
                return 16
            if second in (18, 19):
                return 17
            if second == 20:
                return 21 if patch >= 5 else 17
            if second >= 21:
                return 21
    return None


def _adoptium_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "windows", "aarch64" if "arm" in machine else "x64"
    if system == "darwin":
        return "mac", "aarch64" if machine in ("arm64", "aarch64") else "x64"
    if system == "linux":
        return "linux", "aarch64" if "arm" in machine or "aarch64" in machine else "x64"
    raise RuntimeError(f"Автоустановка Java пока не поддерживает платформу {platform.system()}.")


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    total = 0
    for item in archive.infolist():
        target = (destination / item.filename).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError("Архив Java содержит небезопасный путь.")
        total += item.file_size
        if total > 1_500_000_000:
            raise RuntimeError("Архив Java превышает допустимый размер 1.5 ГБ.")
    archive.extractall(destination)


def _install_adoptium_runtime(major: int, minecraft_dir: Path, callback: dict | None = None) -> str:
    """Загружает подходящий Java runtime из Adoptium, если runtime Mojang недоступен."""
    os_name, architecture = _adoptium_platform()
    runtime_root = minecraft_dir / "runtime" / f"adoptium-{major}-{os_name}-{architecture}"
    runtime_root.mkdir(parents=True, exist_ok=True)
    java_names = ("javaw.exe", "java.exe") if os.name == "nt" else ("java",)
    for java_name in java_names:
        existing = next(runtime_root.rglob(java_name), None)
        if existing and existing.is_file():
            actual = java_major_version(str(existing))
            if actual == major:
                return str(existing.resolve())

    base_url = f"https://api.adoptium.net/v3/binary/latest/{major}/ga/{os_name}/{architecture}"
    archive_path = runtime_root / "runtime-download.zip"
    if callback and callback.get("setStatus"):
        callback["setStatus"](f"Загрузка Java {major} (Adoptium)…")
    response = None
    last_status = 0
    for image_type in ("jre", "jdk"):
        url = f"{base_url}/{image_type}/hotspot/normal/eclipse"
        candidate_response = requests.get(
            url, stream=True, timeout=(15, 60),
            headers={"User-Agent": f"{LAUNCHER_NAME}/{LAUNCHER_VERSION}"},
        )
        if candidate_response.status_code < 400:
            response = candidate_response
            break
        last_status = candidate_response.status_code
        candidate_response.close()
    if response is None:
        raise RuntimeError(f"Adoptium не смог выдать Java {major} (HTTP {last_status}).")
    total = int(response.headers.get("Content-Length", "0") or 0)
    downloaded = 0
    try:
        with archive_path.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if downloaded > 1_000_000_000:
                    raise RuntimeError("Загрузка Java превысила 1 ГБ — файл отклонён.")
                if callback and callback.get("setProgress") and total:
                    callback["setProgress"](downloaded)
                    if callback.get("setMax"):
                        callback["setMax"](total)
    finally:
        response.close()
    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        raise RuntimeError("Adoptium вернул пустой архив Java.")
    if callback and callback.get("setStatus"):
        callback["setStatus"](f"Распаковка Java {major}…")
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            _safe_extract_zip(archive, runtime_root)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Архив Java повреждён или не является ZIP.") from exc
    finally:
        archive_path.unlink(missing_ok=True)
    for java_name in java_names:
        executable = next(runtime_root.rglob(java_name), None)
        if executable and executable.is_file():
            actual = java_major_version(str(executable))
            if actual == major:
                log.info("Установлен Java runtime %s: %s", major, executable)
                return str(executable.resolve())
    raise RuntimeError(f"Java {major} скачана, но исполняемый файл не найден после распаковки.")


def resolve_java(version_id: str, minecraft_dir: Path, custom_java: str, auto_java: bool,
                 callback: dict | None = None) -> str:
    """Подбирает совместимую Java: путь пользователя → Mojang runtime → Adoptium → системная."""
    required = required_java_major(version_id, minecraft_dir)
    selected = custom_java.strip()
    if selected:
        java = Path(selected).expanduser()
        if not java.is_file():
            raise RuntimeError(f"Указанный файл Java не найден:\n{java}\n\nПроверь путь в настройках.")
        major = java_major_version(str(java))
        if major is None:
            raise RuntimeError("Не удалось определить версию выбранной Java. Укажи java.exe/javaw.exe.")
        if required and (major is None or major < required):
            raise RuntimeError(
                f"Эта сборка требует Java {required} или новее, а выбрана Java {major}. "
                "Очисти ручной путь Java или включи автоматическую установку."
            )
        return str(java.resolve())

    if auto_java:
        for candidate in _version_chain(version_id, minecraft_dir):
            try:
                runtime = mll.runtime.get_version_runtime_information(candidate, str(minecraft_dir))
                runtime_name = runtime.get("name") if isinstance(runtime, dict) else None
                if not runtime_name:
                    continue
                executable = mll.runtime.get_executable_path(runtime_name, str(minecraft_dir))
                if not executable or not Path(executable).is_file():
                    if callback and callback.get("setStatus"):
                        callback["setStatus"](f"Установка встроенной Java ({runtime_name})…")
                    mll.runtime.install_jvm_runtime(runtime_name, str(minecraft_dir), callback=callback)
                    executable = mll.runtime.get_executable_path(runtime_name, str(minecraft_dir))
                if executable and Path(executable).is_file():
                    detected = java_major_version(str(executable))
                    if not required or (detected is not None and detected >= required):
                        return str(Path(executable).resolve())
                    log.warning("Mojang runtime %s имеет Java %s, ожидается %s", runtime_name, detected, required)
            except Exception as exc:
                log.warning("Mojang runtime недоступен для %s: %s", candidate, exc)

        if required:
            try:
                return _install_adoptium_runtime(required, minecraft_dir, callback)
            except Exception as exc:
                log.exception("Не удалось установить Java %s через Adoptium", required)
                system_java = find_system_java()
                if system_java and (java_major_version(system_java) or 0) >= required:
                    log.warning("Используется установленная в системе Java %s", required)
                    return system_java
                raise RuntimeError(
                    f"Не удалось автоматически установить Java {required}. Проверь подключение к интернету "
                    f"и повтори установку.\n\nПодробности: {exc}"
                ) from exc

    system_java = find_system_java()
    if system_java:
        major = java_major_version(system_java)
        if required and (major is None or major < required):
            raise RuntimeError(
                f"Для этой версии Minecraft требуется Java {required} или новее, а найдена Java {major or 'неизвестно'}. "
                "Включи автоматическую установку Java или укажи совместимый java.exe."
            )
        return system_java
    raise RuntimeError("Java не найдена. Включи «Автоматически загружать Java» в настройках лаунчера.")


class VersionListWorker(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def run(self) -> None:
        try:
            versions = mll.utils.get_version_list()
            # Показываем все официальные типы Minecraft: релизы, снапшоты,
            # старые Beta и Alpha. Это список для обычной игры; доступность
            # конкретного загрузчика отдельно проверяется его API.
            supported_types = {"release", "snapshot", "old_beta", "old_alpha"}
            available = [dict(item) for item in versions if item.get("type") in supported_types]
            self.loaded.emit(available)
        except Exception as exc:
            log.exception("Не удалось загрузить список версий Minecraft")
            self.failed.emit(f"Не удалось получить список официальных версий. Проверь интернет.\n{exc}")


class LoaderVersionListWorker(QThread):
    """Получает официальные стабильные версии Forge/NeoForge/Fabric/Quilt."""
    loaded = pyqtSignal(str, str, list)
    failed = pyqtSignal(str, str, str)

    def __init__(self, loader_id: str, minecraft_version: str, parent=None):
        super().__init__(parent)
        self.loader_id = str(loader_id).strip().lower()
        self.minecraft_version = str(minecraft_version).strip()

    def run(self):
        try:
            if self.loader_id == "vanilla":
                self.loaded.emit(self.loader_id, self.minecraft_version, [])
                return
            if self.loader_id == "neoforge":
                # minecraft-launcher-lib не во всех выпусках 8.x корректно
                # получает NeoForge. Берём список напрямую из официального Maven.
                match = re.fullmatch(r"1\.(\d+)(?:\.(\d+))?", self.minecraft_version)
                if not match:
                    raise RuntimeError(f"Неизвестный формат версии Minecraft: {self.minecraft_version}")
                mc_prefix = f"{match.group(1)}.{match.group(2) or '0'}"
                metadata = requests.get(
                    "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml",
                    timeout=(8, 25), headers={"User-Agent": LAUNCHER_NAME},
                )
                metadata.raise_for_status()
                root = ET.fromstring(metadata.content)
                cleaned = [node.text.strip() for node in root.findall(".//version")
                           if node.text and node.text.strip().startswith(mc_prefix + ".")
                           and "-" not in node.text]
                from packaging.version import Version
                cleaned.sort(key=Version, reverse=True)
            else:
                loader = mll.mod_loader.get_mod_loader(self.loader_id)
                try:
                    versions = loader.get_loader_versions(self.minecraft_version, True)
                except TypeError:
                    versions = loader.get_loader_versions(self.minecraft_version)
                cleaned = [str(version) for version in (versions or []) if str(version).strip()]
            self.loaded.emit(self.loader_id, self.minecraft_version, cleaned)
        except Exception as exc:
            log.exception("Не удалось загрузить версии загрузчика %s для Minecraft %s",
                          self.loader_id, self.minecraft_version)
            self.failed.emit(self.loader_id, self.minecraft_version, str(exc))


class InstallMinecraftWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, profile: dict, game_root: str, instance_dir: str, minecraft_version: str,
                 loader: str = "vanilla", loader_version: str = "", java_path: str = "",
                 auto_java: bool = True):
        super().__init__()
        self.profile = dict(profile)
        self.game_root = Path(game_root).expanduser().resolve()
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self.minecraft_version = minecraft_version.strip()
        self.loader = (loader or "vanilla").lower().strip()
        self.loader_version = loader_version.strip()
        self.java_path = java_path.strip()
        self.auto_java = bool(auto_java)

    def run(self) -> None:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.minecraft_version:
                    raise RuntimeError("Сначала выбери версию Minecraft.")
                self.game_root.mkdir(parents=True, exist_ok=True)
                self.instance_dir.mkdir(parents=True, exist_ok=True)
                for folder in ("mods", "config", "saves", "resourcepacks", "shaderpacks", "screenshots"):
                    (self.instance_dir / folder).mkdir(parents=True, exist_ok=True)

                callback = _callback(self)
                self.progress.emit(0, "Проверка общей библиотеки Minecraft…")
                mll.install.install_minecraft_version(self.minecraft_version, str(self.game_root), callback=callback)

                installed_id = self.minecraft_version
                java = resolve_java(self.minecraft_version, self.game_root, self.java_path,
                                    self.auto_java, callback=callback)
                if self.loader != "vanilla":
                    supported = {"fabric", "forge", "neoforge", "quilt"}
                    if self.loader not in supported:
                        raise RuntimeError(f"Неизвестный загрузчик: {self.loader}")
                    self.progress.emit(-1, f"Установка {self.loader.title()}…")
                    if self.loader == "neoforge":
                        def _neo_progress(message: str):
                            self.progress.emit(-1, message)
                        installed_id = install_neoforge_client(
                            self.minecraft_version, self.loader_version, self.game_root, java, _neo_progress,
                        )
                    else:
                        loader = mll.mod_loader.get_mod_loader(self.loader)
                        installed_id = loader.install(
                            self.minecraft_version, str(self.game_root),
                            loader_version=self.loader_version or None, callback=callback, java=java,
                        )
                    java = resolve_java(str(installed_id), self.game_root, self.java_path,
                                        self.auto_java, callback=callback)

                result = {
                    "minecraft_version": self.minecraft_version,
                    "loader": self.loader,
                    "loader_version": self.loader_version,
                    "installed_version_id": str(installed_id),
                }
                log.info("Установка готова: %s (%s)", self.minecraft_version, installed_id)
                self.completed.emit(result)
                return # Выходим из цикла, если успешно

            except Exception as exc:
                # Если это ошибка контрольной суммы (битый файл)
                if "InvalidChecksum" in str(exc) or "wrong Checksum" in str(exc):
                    log.warning(f"Попытка {attempt+1} провалена (битый файл). Удаляем и качаем заново...")
                    self.progress.emit(-1, f"Ошибка скачивания файла. Повторная попытка {attempt+1}/{max_retries}...")

                    # Вытаскиваем путь битого файла из текста ошибки
                    match = re.search(r"([A-Za-z]:\\[^\s]+)\s+has the wrong Checksum", str(exc))
                    if match:
                        bad_file = Path(match.group(1))
                        if bad_file.exists():
                            try:
                                bad_file.unlink() # Удаляем
                            except Exception:
                                pass
                    import time
                    time.sleep(2) # Ждем и пробуем снова
                    continue
                elif isinstance(exc, requests.exceptions.RequestException) and attempt < max_retries - 1:
                    log.warning("Сетевая ошибка установки (попытка %s/%s): %s", attempt + 1, max_retries, exc)
                    self.progress.emit(-1, f"Проблема сети. Повторяем установку ({attempt + 2}/{max_retries})…")
                    time.sleep(2 ** (attempt + 1))
                    continue
                else:
                    log.exception("Ошибка установки Minecraft")
                    self.failed.emit(self._friendly_error(exc))
                    return

            # Если все попытки исчерпаны и мы здесь
        self.failed.emit("Не удалось скачать файлы игры после 3 попыток. Проверь интернет или отключи антивирус.\nПопробуй нажать «Установить» еще раз.")

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        message = str(exc)
        lower = message.lower()
        if "version" in lower and ("not found" in lower or "unsupported" in lower):
            return "Эта версия Minecraft не найдена или не поддерживается выбранным загрузчиком.\nВыбери другой релиз/версию загрузчика."
        if "java" in lower:
            return message
        if "timeout" in lower or "connection" in lower:
            return "Не удалось скачать файлы игры. Проверь интернет, VPN/прокси и попробуй ещё раз.\n\n" + message
        return "Не удалось установить выбранную сборку. Проверь место на диске и лог лаунчера.\n\n" + message


class LaunchWorker(QThread):
    started_game = pyqtSignal(str)
    game_process_started = pyqtSignal(int)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, game_root: str, instance_dir: str, version_id: str, account: dict, ram_gb: int,
                 java_path: str = "", auto_java: bool = True, jvm_arguments: str = ""):
        super().__init__()
        self.game_root = Path(game_root).expanduser().resolve()
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self.version_id = version_id.strip()
        self.account = dict(account)
        self.ram_gb = int(ram_gb)
        self.java_path = java_path.strip()
        self.auto_java = bool(auto_java)
        self.jvm_arguments = jvm_arguments.strip()

    def run(self) -> None:
        try:
            self._validate_account()
            version_json = self.game_root / "versions" / self.version_id / f"{self.version_id}.json"
            if not version_json.is_file():
                raise RuntimeError(
                    f"Версия {self.version_id} не установлена в выбранном профиле. "
                    "Открой экран игры и установи её заново."
                )

            callback = _callback(self)
            self.progress.emit("Проверяем игровые файлы и докачиваем недостающее…")
            try:
                mll.install.install_minecraft_version(
                    self.version_id, str(self.game_root), callback=callback,
                )
            except Exception as install_error:
                if version_json.is_file():
                    log.warning("Онлайн-проверка версии недоступна; пробуем локальные файлы: %s", install_error)
                    self.progress.emit("Сеть недоступна — используем локальные файлы Minecraft…")
                else:
                    raise

            self.progress.emit("Проверяем Java и её совместимость…")
            java = resolve_java(self.version_id, self.game_root, self.java_path,
                                self.auto_java, callback=callback)
            required = required_java_major(self.version_id, self.game_root)
            detected = java_major_version(java)
            if required and detected and detected < required:
                raise RuntimeError(f"Minecraft требует Java {required}, но выбрана Java {detected}.")

            total_ram_gb = max(1, int(psutil.virtual_memory().total / (1024 ** 3)))
            ram = max(2, min(self.ram_gb, max(2, total_ram_gb - 1)))
            jvm_args = ["-Xms512M", f"-Xmx{ram}G"]
            custom = shlex.split(self.jvm_arguments, posix=True) if self.jvm_arguments else []
            if any(arg.lower().startswith(("-xms", "-xmx")) for arg in custom):
                raise RuntimeError("Параметры -Xms и -Xmx задаются ползунком памяти. Удали их из дополнительных JVM-аргументов.")
            jvm_args.extend(custom)

            options = {
                "username": str(self.account["name"]),
                "uuid": str(self.account["id"]),
                "token": str(self.account["access_token"]),
                "executablePath": java,
                "defaultExecutablePath": java,
                "jvmArguments": jvm_args,
                "launcherName": LAUNCHER_NAME,
                "launcherVersion": LAUNCHER_VERSION,
                "gameDirectory": str(self.instance_dir),
            }
            self.progress.emit("Формируем команду запуска из version JSON…")
            command = mll.command.get_minecraft_command(self.version_id, str(self.game_root), options)
            if not command or not Path(command[0]).exists():
                raise RuntimeError("Java executable не найден. Проверь путь в настройках.")

            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            game_process = subprocess.Popen(
                command,
                cwd=str(self.instance_dir),
                creationflags=creation_flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                close_fds=(os.name != "nt"),
            )
            log.info("Minecraft стартовал: pid=%s version=%s sharedRoot=%s gameDir=%s java=%s ram=%sG",
                     game_process.pid, self.version_id, self.game_root, self.instance_dir, java, ram)
            self.game_process_started.emit(int(game_process.pid))
            self.started_game.emit(f"Minecraft запущен · версия {self.version_id}")
        except Exception as exc:
            log.exception("Ошибка запуска Minecraft")
            message = str(exc)
            lower = message.lower()
            if isinstance(exc, FileNotFoundError) or "no such file" in lower:
                message = "Не найден Java или игровой файл. Открой настройки Java и выполни восстановление версии.\n\n" + message
            elif "memory" in lower or "ram" in lower:
                message = "Недостаточно оперативной памяти. Уменьши выделенную память в настройках лаунчера.\n\n" + message
            elif "versionnotfound" in lower or "version not found" in lower:
                message = "Установленная версия повреждена или отсутствует. Нажми «Установить/проверить файлы» и повтори запуск.\n\n" + message
            elif "access token" in lower or "invalid token" in lower or "minecraft profile" in lower:
                message = "Сессия Microsoft/Minecraft истекла. Выйди из аккаунта и войди снова.\n\n" + message
            self.failed.emit(message)

    def _validate_account(self) -> None:
        required = ("name", "id", "access_token")
        missing = [key for key in required if not self.account.get(key)]
        if missing:
            raise RuntimeError(
                "Нужен действующий Microsoft-аккаунт с доступом к Minecraft Java Edition. "
                "Нажми «Войти через Microsoft» и заверши вход в браузере."
            )