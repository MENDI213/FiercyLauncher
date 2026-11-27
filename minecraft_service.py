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
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from typing import Any

import minecraft_launcher_lib as mll
import psutil
from PyQt5.QtCore import QThread, pyqtSignal
from update_download import DownloadCancelled

from version import LAUNCHER_NAME, LAUNCHER_VERSION
from version_metadata import read_version_metadata
from neoforge_support import (
    neoforge_maven_coordinates, filter_stable_neoforge_versions,
    neoforge_installer_url, neoforge_bmclapi_metadata_url, neoforge_bmclapi_list_url,
    neoforge_bmclapi_installer_url, extract_neoforge_versions,
    BMCLAPI_BASE, BMCLAPI_MAVEN_MIRROR, ensure_launcher_profiles, find_installed_neoforge_version,
)

log = logging.getLogger("fiercy.minecraft")

_LOADER_VERSION_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}
_LOADER_VERSION_CACHE_LOCK = threading.Lock()
_LOADER_VERSION_CACHE_TTL = 600.0


def _callback(worker: QThread, rate_limiter=None):
    state = {
        "current": 0.0, "maximum": 0.0, "status": "Подготовка…",
        "sample_current": 0.0, "sample_time": time.monotonic(), "speed": 0.0,
        "last_download_current": 0.0,
    }

    def check_cancelled():
        control = getattr(worker, "download_control", None)
        if control is not None:
            control.checkpoint(0, rate_limiter)
            return
        cancel_event = getattr(worker, "cancel_event", None)
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Загрузка отменена пользователем.")

    def emit_progress(percent: int = -1):
        signal = getattr(worker, "progress", None)
        if signal is None or worker.__class__.__name__ == "LaunchWorker":
            return
        maximum = state["maximum"]
        current_value = state["current"]
        now = time.monotonic()
        elapsed = now - state["sample_time"]
        if elapsed >= 0.15:
            delta = current_value - state["sample_current"]
            if delta >= 0:
                instantaneous = delta / max(elapsed, 0.001)
                state["speed"] = instantaneous if state["speed"] <= 0 else (
                    state["speed"] * 0.55 + instantaneous * 0.45
                )
            state["sample_current"] = current_value
            state["sample_time"] = now

        message = state["status"]
        # Fast/small files may complete before the 150 ms sampling interval.
        # Show an average rate in that case instead of a misleading 0.00 МБ/с.
        speed = state["speed"]
        if speed <= 0 and elapsed >= 0.01 and current_value > 0:
            speed = current_value / elapsed
        if maximum > 0:
            file_percent = max(0, min(100, int(current_value * 100 / maximum)))
            message = (
                f"{message} · текущий файл {file_percent}% · "
                f"{current_value / 1048576:.1f}/{maximum / 1048576:.1f} МБ · "
                f"{speed / 1048576:.2f} МБ/с"
            )
            if percent < 0:
                percent = file_percent
        elif speed > 0:
            message = f"{message} · {speed / 1048576:.2f} МБ/с"
        signal.emit(max(-1, min(100, int(percent))), message)

    def status(value: Any) -> None:
        check_cancelled()
        state["status"] = str(value)
        signal = getattr(worker, "progress", None)
        if signal is not None:
            if worker.__class__.__name__ == "LaunchWorker":
                signal.emit(state["status"])
            else:
                emit_progress(-1)

    def current(value: Any) -> None:
        check_cancelled()
        try:
            new_current = float(value or 0)
        except (TypeError, ValueError):
            new_current = 0.0
        delta = max(0.0, new_current - state["last_download_current"])
        state["last_download_current"] = new_current
        control = getattr(worker, "download_control", None)
        if control is not None and delta > 0:
            control.checkpoint(int(delta), rate_limiter)
        state["current"] = new_current
        maximum = state["maximum"]
        percent = int(state["current"] * 100 / maximum) if maximum else 0
        emit_progress(max(0, min(100, percent)) if maximum else -1)

    def maximum(value: Any) -> None:
        check_cancelled()
        try:
            state["maximum"] = float(value or 0)
        except (TypeError, ValueError):
            state["maximum"] = 0
        state["current"] = 0.0
        state["last_download_current"] = 0.0
        state["sample_current"] = 0.0
        state["sample_time"] = time.monotonic()
        state["speed"] = 0.0
        current(state["current"])

    return {"setStatus": status, "setProgress": current, "setMax": maximum}


def _download_neoforge_file(url: str | list[str] | tuple[str, ...], destination: Path, progress=None,
                            cancel_event: threading.Event | None = None,
                            download_control=None, rate_limiter=None) -> Path:
    """Download a NeoForge installer from BMCLAPI first, then the official Maven.

    The user's logs show that maven.neoforged.net does not establish TCP
    connections from their network. BMCLAPI mirrors the NeoForge Maven layout
    and exposes a dedicated installer endpoint. A downloaded JAR is kept only
    after ZIP validation; partial files are deleted on every failure.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    urls = [url] if isinstance(url, str) else [str(item) for item in url if item]
    if not urls:
        raise RuntimeError("Не задан адрес установщика NeoForge.")
    errors: list[str] = []
    headers = {"User-Agent": LAUNCHER_NAME, "Accept": "application/java-archive, */*"}
    for source_url in urls:
        source_name = "BMCLAPI" if "bmclapi" in source_url.lower() else "официальный Maven NeoForged"
        for attempt in range(2):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("Загрузка NeoForge отменена.")
            temporary.unlink(missing_ok=True)
            try:
                if progress:
                    progress(f"Установщик NeoForge · источник {source_name} · попытка {attempt + 1}/2…")
                with requests.get(source_url, stream=True, timeout=(12, 120), headers=headers) as response:
                    response.raise_for_status()
                    final_url = str(response.url or source_url)
                    if not final_url.startswith("https://"):
                        raise RuntimeError("Источник установщика перенаправил на незащищённый HTTP.")
                    content_type = str(response.headers.get("Content-Type", "")).lower()
                    if "text/html" in content_type:
                        raise RuntimeError("Вместо установщика сервер вернул HTML-страницу.")
                    expected = int(response.headers.get("Content-Length", "0") or 0)
                    if expected and expected > 256 * 1024 * 1024:
                        raise RuntimeError("Размер установщика NeoForge неожиданно велик.")
                    written = 0
                    started = time.monotonic()
                    last_report = started
                    with temporary.open("wb") as output:
                        for chunk in response.iter_content(256 * 1024):
                            if cancel_event is not None and cancel_event.is_set():
                                raise DownloadCancelled("Загрузка NeoForge отменена.")
                            if not chunk:
                                continue
                            if download_control is not None:
                                download_control.checkpoint(len(chunk), rate_limiter)
                            written += len(chunk)
                            if written > 256 * 1024 * 1024:
                                raise RuntimeError("Установщик NeoForge превысил лимит 256 МБ.")
                            output.write(chunk)
                            now = time.monotonic()
                            if progress and (now - last_report >= 0.2 or (expected and written >= expected)):
                                speed = written / max(now - started, 0.001) / 1048576
                                amount = (f"{written / 1048576:.1f}/{expected / 1048576:.1f} МБ"
                                          if expected else f"{written / 1048576:.1f} МБ")
                                pct_label = f"{int(written * 100 / expected)}% · " if expected else ""
                                attribution = " · источник BMCLAPI" if source_name == "BMCLAPI" else ""
                                progress(f"NeoForge · {pct_label}{amount} · {speed:.2f} МБ/с{attribution}")
                                last_report = now
                    if written < 1024 or not zipfile.is_zipfile(temporary):
                        raise RuntimeError("Скачанный установщик NeoForge повреждён или это не JAR-файл.")
                temporary.replace(destination)
                if progress:
                    progress(f"Установщик NeoForge загружен · {source_name} · {written / 1048576:.1f} МБ")
                return destination
            except DownloadCancelled:
                temporary.unlink(missing_ok=True)
                raise
            except (requests.RequestException, OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{source_name}: {exc}")
                temporary.unlink(missing_ok=True)
                log.warning("NeoForge installer source %s attempt %s/2 failed: %s", source_name, attempt + 1, exc)
                if attempt == 0:
                    time.sleep(1.5)
    raise RuntimeError(
        "Не удалось скачать установщик NeoForge ни с зеркала BMCLAPI, ни с официального Maven.\n"
        + "\n".join(errors[-6:])
    )


def _fetch_neoforge_versions(minecraft_version: str) -> tuple[list[str], list[str]]:
    """Try BMCLAPI metadata first, then the official NeoForged Maven XML."""
    errors: list[str] = []
    # This is BMCLAPI's dedicated endpoint for a Minecraft version. Its objects
    # include both `version` and `rawVersion`, so we can filter against MC exactly.
    list_url = neoforge_bmclapi_list_url(minecraft_version)
    try:
        response = requests.get(list_url, timeout=(8, 20), headers={"User-Agent": LAUNCHER_NAME})
        response.raise_for_status()
        versions = extract_neoforge_versions(response.content, minecraft_version)
        if versions:
            return versions, errors
        errors.append("BMCLAPI list: ответ не содержит совместимых стабильных версий")
    except (requests.RequestException, ValueError, TypeError) as exc:
        errors.append(f"BMCLAPI list: {exc}")

    # Older BMCLAPI endpoint mirrors the upstream Maven metadata JSON.
    api_url = neoforge_bmclapi_metadata_url(minecraft_version)
    try:
        response = requests.get(api_url, timeout=(8, 20), headers={"User-Agent": LAUNCHER_NAME})
        response.raise_for_status()
        versions = extract_neoforge_versions(response.content, minecraft_version)
        if versions:
            return versions, errors
        errors.append("BMCLAPI metadata: ответ не содержит совместимых стабильных версий")
    except (requests.RequestException, ValueError, TypeError) as exc:
        errors.append(f"BMCLAPI metadata: {exc}")

    artifact_id, _prefix = neoforge_maven_coordinates(minecraft_version)
    metadata_url = (
        f"https://maven.neoforged.net/releases/net/neoforged/{artifact_id}/maven-metadata.xml"
    )
    try:
        response = requests.get(metadata_url, timeout=(8, 20), headers={"User-Agent": LAUNCHER_NAME})
        response.raise_for_status()
        metadata_root = ET.fromstring(response.content)
        remote_versions = [node.text.strip() for node in metadata_root.findall(".//version") if node.text]
        versions = filter_stable_neoforge_versions(minecraft_version, remote_versions)
        if versions:
            return versions, errors
        errors.append("Официальный Maven: список не содержит совместимых стабильных версий")
    except (requests.RequestException, ET.ParseError, ValueError, TypeError) as exc:
        errors.append(f"Официальный Maven: {exc}")
    return [], errors


def _patch_neoforge_maven_urls(game_root: Path, installed_id: str) -> int:
    """Point NeoForge version-library downloads at BMCLAPI when the official Maven is unreachable."""
    version_json = game_root / "versions" / installed_id / f"{installed_id}.json"
    if not version_json.is_file():
        return 0
    try:
        payload = json.loads(version_json.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось прочитать JSON установленного NeoForge: {exc}") from exc

    official_prefixes = (
        "https://maven.neoforged.net/releases/",
        "https://maven.minecraftforge.net/",
    )
    replacements = 0

    def visit(value):
        nonlocal replacements
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if isinstance(child, str) and key.lower() == "url":
                    for prefix in official_prefixes:
                        if child.startswith(prefix):
                            value[key] = BMCLAPI_MAVEN_MIRROR + child[len(prefix):]
                            replacements += 1
                            break
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    if replacements:
        temp = version_json.with_suffix(".json.fiercy-tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(version_json)
        log.info("NeoForge %s: switched %s library URLs to BMCLAPI Maven mirror", installed_id, replacements)
    return replacements


def install_neoforge_client(minecraft_version: str, loader_version: str, game_root: str | Path,
                            java_path: str, progress=None,
                            cancel_event: threading.Event | None = None,
                            download_control=None, rate_limiter=None) -> str:
    """Install NeoForge using its official LegacyInstaller and BMCLAPI fallback."""
    game_root = Path(game_root).expanduser().resolve()
    game_root.mkdir(parents=True, exist_ok=True)
    version = str(loader_version or "").strip()

    if not version:
        versions, errors = _fetch_neoforge_versions(minecraft_version)
        if not versions:
            raise RuntimeError(
                f"Не удалось получить версии NeoForge для Minecraft {minecraft_version}.\n"
                + "\n".join(errors)
            )
        version = versions[0]

    # Validate the selected exact build before any network or filesystem changes.
    official_installer_url = neoforge_installer_url(minecraft_version, version)
    artifact_id, _prefix = neoforge_maven_coordinates(minecraft_version)
    installer_name = official_installer_url.rsplit("/", 1)[-1]
    installer_dir = game_root / ".fiercy" / "installers"
    installer = installer_dir / installer_name
    if not installer.is_file() or not zipfile.is_zipfile(installer):
        mirror_urls = [
            neoforge_bmclapi_installer_url(minecraft_version, version),
            f"{BMCLAPI_MAVEN_MIRROR}net/neoforged/{artifact_id}/{version}/{installer_name}",
            official_installer_url,
        ]
        _download_neoforge_file(mirror_urls, installer, progress, cancel_event,
                                 download_control, rate_limiter)

    # LegacyInstaller refuses to run without a real launcher profile. Ensure one
    # exists without replacing the user's existing profiles/settings.
    launcher_profiles, created_profile_file = ensure_launcher_profiles(game_root)
    if progress:
        progress(f"Подготовка NeoForge {version} · Minecraft {minecraft_version}…")
        progress("Источник зависимостей: BMCLAPI Maven с резервом на официальный Maven")

    installer_java = str(java_path)
    if Path(installer_java).name.lower() == "javaw.exe":
        console_java = Path(installer_java).with_name("java.exe")
        if console_java.is_file():
            installer_java = str(console_java)

    command = [
        installer_java, "-Djava.awt.headless=true", "-Djava.net.preferIPv4Stack=true",
        "-jar", str(installer), "--install-client", str(game_root),
        "--mirror", BMCLAPI_MAVEN_MIRROR,
    ]
    log.info("Запуск официального NeoForge installer: mc=%s loader=%s root=%s mirror=%s bootstrap_profile=%s",
             minecraft_version, version, game_root, BMCLAPI_MAVEN_MIRROR, created_profile_file)
    try:
        completed = subprocess.run(
            command, cwd=str(game_root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1200,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Установка NeoForge {version} не завершилась за 20 минут. "
            f"Проверь лог установщика {installer.name}.log"
        ) from exc

    installer_log = game_root / (installer.name + ".log")
    if not installer_log.is_file():
        installer_log = installer.with_name(installer.name + ".log")
    if completed.returncode != 0:
        details_parts = [completed.stderr or "", completed.stdout or ""]
        if installer_log.is_file():
            try:
                details_parts.append(installer_log.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        details = "\n".join(part.strip() for part in details_parts if part.strip())[-8000:]
        if "maven.neoforged.net" in details.lower() or "connecttimeout" in details.lower():
            reason = (
                "NeoForge установщик всё ещё обращается к недоступному официальному Maven, "
                "несмотря на параметр зеркала. Это сетевой сбой upstream; зеркало BMCLAPI было подключено, "
                "но текущая сборка установщика не применила его к этому шагу.\n"
            )
        elif "launcher profile" in details.lower():
            reason = "NeoForge не принял bootstrap-профиль лаунчера. Сохранился диагностический лог установщика.\n"
        else:
            reason = "Официальный установщик NeoForge завершился с ошибкой.\n"
        raise RuntimeError(reason + details)

    installed_id = find_installed_neoforge_version(game_root, minecraft_version, version)
    patched_urls = _patch_neoforge_maven_urls(game_root, installed_id)

    # The installer may create the profile JSON and its processor outputs, while
    # minecraft-launcher-lib completes Mojang assets/natives and remaining files.
    # The generated NeoForge Maven URLs are patched to BMCLAPI before this step.
    if progress:
        progress(f"Завершаем файлы NeoForge {version}…")
    # The caller's download callback is used by the completion pass in
    # install_neoforge_profile(); this function only runs the official installer.
    log.info("NeoForge install completed: %s (BMCLAPI Maven URLs patched: %s)", installed_id, patched_urls)
    if progress:
        progress(f"NeoForge {version} установлен. Проверяем библиотеки…")
    return installed_id


def install_neoforge_profile(
    minecraft_version: str,
    loader_version: str,
    game_root: str | Path,
    java_path: str,
    callback: dict | None = None,
    progress=None,
    cancel_event: threading.Event | None = None,
    download_control=None,
    rate_limiter=None,
) -> str:
    """Install every NeoForge line through LegacyInstaller, then complete via minecraft-launcher-lib.

    We intentionally do not call minecraft-launcher-lib's NeoForge adapter: its
    metadata lookup hits maven.neoforged.net directly (the host that timed out in
    the user's logs). The official installer is used for all NeoForge lines;
    minecraft-launcher-lib remains responsible for vanilla libraries/assets and
    launching the generated version JSON.
    """
    root = Path(game_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    ensure_launcher_profiles(root)
    installed_id = install_neoforge_client(
        str(minecraft_version).strip(), str(loader_version or "").strip(), root,
        str(java_path), progress, cancel_event, download_control, rate_limiter,
    )
    if progress:
        progress("Проверяем библиотеки, natives и ресурсы Minecraft…")
    # _patch_neoforge_maven_urls ran immediately after installer success. The
    # normal library installer can now fetch any remaining Maven artifacts via mirror.
    try:
        mll.install.install_minecraft_version(
            installed_id, str(root), callback=callback,
        )
    except Exception as exc:
        raise RuntimeError(
            f"NeoForge {loader_version} установлен, но не удалось завершить зависимости Minecraft: {exc}"
        ) from exc
    return find_installed_neoforge_version(root, str(minecraft_version), str(loader_version or ""))


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
        if callback and callback.get("setMax") and total:
            callback["setMax"](total)
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
    """Получает совместимые версии загрузчиков с кэшированием NeoForge."""
    loaded = pyqtSignal(str, str, list)
    failed = pyqtSignal(str, str, str)

    def __init__(self, loader_id: str, minecraft_version: str, parent=None, cache_root=None):
        super().__init__(parent)
        self.loader_id = str(loader_id).strip().lower()
        self.minecraft_version = str(minecraft_version).strip()
        self.cache_root = Path(cache_root).expanduser().resolve() if cache_root else (
            Path.home() / ".fiercylauncher" / "cache"
        )

    def _neoforge_cache_file(self) -> Path:
        safe_mc = re.sub(r"[^A-Za-z0-9._-]", "_", self.minecraft_version)
        return self.cache_root / "neoforge-versions" / f"{safe_mc}.json"

    def _read_neoforge_cache(self) -> list[str]:
        path = self._neoforge_cache_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            versions = data.get("versions", []) if isinstance(data, dict) else []
            return filter_stable_neoforge_versions(self.minecraft_version, versions)
        except (OSError, ValueError, TypeError):
            return []

    def _write_neoforge_cache(self, versions: list[str], source: str) -> None:
        path = self._neoforge_cache_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({
                "minecraft_version": self.minecraft_version,
                "versions": versions,
                "source": source,
                "updated_at": time.time(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except OSError:
            log.warning("Не удалось сохранить кэш версий NeoForge: %s", path, exc_info=True)

    def run(self):
        try:
            if self.loader_id == "vanilla":
                self.loaded.emit(self.loader_id, self.minecraft_version, [])
                return
            cache_key = (self.loader_id, self.minecraft_version)
            now = time.monotonic()
            with _LOADER_VERSION_CACHE_LOCK:
                cached = _LOADER_VERSION_CACHE.get(cache_key)
                if cached and now - cached[0] < _LOADER_VERSION_CACHE_TTL:
                    self.loaded.emit(self.loader_id, self.minecraft_version, list(cached[1]))
                    return
            if self.loader_id == "neoforge":
                cleaned: list[str] = []
                errors: list[str] = []
                versions, errors = _fetch_neoforge_versions(self.minecraft_version)
                if versions:
                    cleaned = versions
                    source = "official" if any(error.startswith("Официальный Maven:") for error in errors) else "BMCLAPI"
                    self._write_neoforge_cache(cleaned, source)
                else:
                    cleaned = self._read_neoforge_cache()
                    if cleaned:
                        log.warning("NeoForge metadata unavailable; using cached versions for %s: %s",
                                    self.minecraft_version, " | ".join(errors))
                    else:
                        raise RuntimeError("Не удалось получить версии NeoForge ни с BMCLAPI, ни с официального Maven. "
                                           + " | ".join(errors))
            else:
                loader = mll.mod_loader.get_mod_loader(self.loader_id)
                try:
                    versions = loader.get_loader_versions(self.minecraft_version, True)
                except TypeError:
                    versions = loader.get_loader_versions(self.minecraft_version)
                cleaned = [str(version) for version in (versions or []) if str(version).strip()]
            with _LOADER_VERSION_CACHE_LOCK:
                _LOADER_VERSION_CACHE[cache_key] = (time.monotonic(), list(cleaned))
            self.loaded.emit(self.loader_id, self.minecraft_version, cleaned)
        except Exception as exc:
            log.exception("Не удалось загрузить версии загрузчика %s для Minecraft %s",
                          self.loader_id, self.minecraft_version)
            self.failed.emit(self.loader_id, self.minecraft_version, str(exc))


class InstallMinecraftWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, profile: dict, game_root: str, instance_dir: str, minecraft_version: str,
                 loader: str = "vanilla", loader_version: str = "", java_path: str = "",
                 auto_java: bool = True, download_control=None, rate_limiter=None):
        super().__init__()
        self.profile = dict(profile)
        self.game_root = Path(game_root).expanduser().resolve()
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self.minecraft_version = minecraft_version.strip()
        self.loader = (loader or "vanilla").lower().strip()
        self.loader_version = loader_version.strip()
        self.java_path = java_path.strip()
        self.auto_java = bool(auto_java)
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
            raise DownloadCancelled("Загрузка отменена пользователем.")

    def run(self) -> None:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self._check_cancelled()
                if not self.minecraft_version:
                    raise RuntimeError("Сначала выбери версию Minecraft.")
                self.game_root.mkdir(parents=True, exist_ok=True)
                self.instance_dir.mkdir(parents=True, exist_ok=True)
                for folder in ("mods", "config", "saves", "resourcepacks", "shaderpacks", "screenshots"):
                    (self.instance_dir / folder).mkdir(parents=True, exist_ok=True)

                callback = _callback(self, self.rate_limiter)
                self.progress.emit(0, "Проверка общей библиотеки Minecraft…")
                mll.install.install_minecraft_version(self.minecraft_version, str(self.game_root), callback=callback)

                self._check_cancelled()
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

                        installed_id = install_neoforge_profile(
                            self.minecraft_version, self.loader_version, self.game_root,
                            java, callback=callback, progress=_neo_progress,
                            cancel_event=self.cancel_event,
                            download_control=self.download_control,
                            rate_limiter=self.rate_limiter,
                        )
                    else:
                        loader = mll.mod_loader.get_mod_loader(self.loader)
                        installed_id = loader.install(
                            self.minecraft_version, str(self.game_root),
                            loader_version=self.loader_version or None, callback=callback, java=java,
                        )
                    java = resolve_java(str(installed_id), self.game_root, self.java_path,
                                        self.auto_java, callback=callback)

                resolved_loader_version = self.loader_version
                if self.loader == "neoforge" and not resolved_loader_version:
                    try:
                        resolved_loader_version = read_version_metadata(
                            self.game_root, str(installed_id),
                        ).get("loader_version", "")
                    except Exception:
                        log.debug("Не удалось извлечь установленную версию NeoForge из JSON", exc_info=True)
                result = {
                    "minecraft_version": self.minecraft_version,
                    "loader": self.loader,
                    "loader_version": resolved_loader_version,
                    "installed_version_id": str(installed_id),
                }
                self._check_cancelled()
                log.info("Установка готова: %s (%s)", self.minecraft_version, installed_id)
                self.completed.emit(result)
                return # Выходим из цикла, если успешно

            except DownloadCancelled:
                log.info("Установка Minecraft отменена пользователем: %s", self.minecraft_version)
                self.cancelled.emit()
                return
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
                    if self.cancel_event.wait(2):
                        self.cancelled.emit()
                        return
                    continue
                elif isinstance(exc, requests.exceptions.RequestException) and attempt < max_retries - 1:
                    log.warning("Сетевая ошибка установки (попытка %s/%s): %s", attempt + 1, max_retries, exc)
                    self.progress.emit(-1, f"Проблема сети. Повторяем установку ({attempt + 2}/{max_retries})…")
                    if self.cancel_event.wait(2 ** (attempt + 1)):
                        self.cancelled.emit()
                        return
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