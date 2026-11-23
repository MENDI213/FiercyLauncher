"""Small, testable helpers for installing official NeoForge client profiles.

The official LegacyInstaller expects a launcher-style game directory containing
launcher_profiles.json. Third-party launchers can provide a minimal profile file;
the installer's own processor pipeline still creates the version JSON and libraries.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from packaging.version import InvalidVersion, Version

NEOFORGED_MAVEN = "https://maven.neoforged.net/releases/net/neoforged"


def neoforge_maven_coordinates(minecraft_version: str) -> tuple[str, str]:
    """Return (artifact id, stable Maven version prefix) for a Minecraft version.

    NeoForge 1.20.1 was published under net.neoforged:forge with versions like
    1.20.1-47.1.106. Starting with MC 1.20.2 the coordinates use neoforge and
    the Minecraft major/minor pair (20.2, 21.1, 26.3, ...).
    """
    value = str(minecraft_version or "").strip()
    match = re.fullmatch(r"(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?", value)
    if not match:
        raise ValueError(f"Неподдерживаемый формат версии Minecraft: {value or '(пусто)'}")

    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = match.group("patch")

    if (major, minor, patch) == (1, 20, "1"):
        return "forge", "1.20.1-"

    # Legacy Minecraft numbering: 1.21.1 -> NeoForge 21.1.x.
    # Modern Minecraft numbering: 26.3 -> NeoForge 26.3.x.
    if major == 1:
        neo_major = minor
        neo_minor = int(patch) if patch is not None else 0
    else:
        neo_major = major
        neo_minor = minor
    return "neoforge", f"{neo_major}.{neo_minor}."


def filter_stable_neoforge_versions(minecraft_version: str, versions: Iterable[str]) -> list[str]:
    artifact, prefix = neoforge_maven_coordinates(minecraft_version)
    result: list[str] = []
    for raw in versions:
        version = str(raw or "").strip()
        if artifact == "forge":
            # The hyphen is the coordinate separator for 1.20.1, not a prerelease.
            if re.fullmatch(r"1\.20\.1-\d+(?:\.\d+)+", version):
                result.append(version)
        elif version.startswith(prefix) and "-" not in version and "+" not in version:
            result.append(version)

    def sort_key(value: str):
        numeric = value[len(prefix):] if artifact == "forge" else value
        try:
            return Version(numeric)
        except InvalidVersion:
            return Version("0")

    return sorted(set(result), key=sort_key, reverse=True)


def neoforge_installer_url(minecraft_version: str, loader_version: str) -> str:
    artifact, prefix = neoforge_maven_coordinates(minecraft_version)
    version = str(loader_version or "").strip()
    valid = (version.startswith(prefix) and bool(re.fullmatch(r"1\.20\.1-\d+(?:\.\d+)+", version))) if artifact == "forge" else (
        version.startswith(prefix) and "-" not in version and "+" not in version
    )
    if not valid:
        raise ValueError(f"NeoForge {version or '(не выбрана)'} не подходит для Minecraft {minecraft_version}.")
    from urllib.parse import quote
    safe_version = quote(version, safe=".-")
    return f"{NEOFORGED_MAVEN}/{artifact}/{safe_version}/{artifact}-{safe_version}-installer.jar"


def ensure_launcher_profiles(game_root: str | Path) -> tuple[Path, bool]:
    """Ensure the official installer has a launcher_profiles.json to inject into.

    Existing valid profile data is preserved. An existing malformed file is not
    overwritten: this avoids destroying a user's launcher configuration.
    Returns (path, created_by_us).
    """
    root = Path(game_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "launcher_profiles.json"

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Файл {path} существует, но повреждён. Он не был перезаписан. "
                "Сделай резервную копию и исправь JSON, затем повтори установку NeoForge."
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Файл {path} должен содержать JSON-объект. Он не был изменён.")
        profiles = data.get("profiles")
        if profiles is None:
            data["profiles"] = {}
        elif not isinstance(profiles, dict):
            raise RuntimeError(f"В файле {path} поле 'profiles' имеет неверный формат. Файл не изменён.")
        # Do not rewrite an already valid file unnecessarily.
        if profiles is None:
            _atomic_json_write(path, data)
        return path, False

    # LegacyInstaller only requires a JSON object with a profiles object; it adds
    # the NeoForge entry itself after processor execution.
    minimal = {
        "profiles": {},
        "settings": {},
        "version": 3,
    }
    _atomic_json_write(path, minimal)
    return path, True


def _atomic_json_write(path: Path, payload: dict) -> None:
    temp = path.with_name(path.name + ".fiercy-tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def find_installed_neoforge_version(
    game_root: str | Path, minecraft_version: str, loader_version: str
) -> str:
    """Find and validate the version JSON produced by the official installer."""
    versions_root = Path(game_root).expanduser().resolve() / "versions"
    expected_id = f"neoforge-{loader_version}"
    candidate_paths = []
    expected = versions_root / expected_id / f"{expected_id}.json"
    if expected.is_file():
        candidate_paths.append(expected)
    if versions_root.is_dir():
        discovered = []
        for json_path in versions_root.glob("*/*.json"):
            if json_path == expected or json_path in candidate_paths:
                continue
            folder_id = json_path.parent.name.lower()
            if ("neoforge" in folder_id or folder_id.startswith("forge-")) and (not loader_version or loader_version in folder_id):
                discovered.append(json_path)
        discovered.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
        candidate_paths.extend(discovered)

    errors = []
    for path in candidate_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{path.parent.name}: повреждён JSON ({exc})")
            continue
        actual_id = str(payload.get("id") or path.parent.name)
        if actual_id != path.parent.name:
            errors.append(f"{path.parent.name}: id в JSON не совпадает с именем папки")
            continue
        inherited = str(payload.get("inheritsFrom") or "")
        main_class = str(payload.get("mainClass") or "")
        libraries = payload.get("libraries")
        if inherited != minecraft_version:
            errors.append(f"{actual_id}: inheritsFrom={inherited!r}, ожидалась {minecraft_version!r}")
            continue
        if not main_class or not isinstance(libraries, list):
            errors.append(f"{actual_id}: отсутствуют mainClass или libraries")
            continue
        return actual_id

    detail = "\n".join(errors[-5:])
    raise RuntimeError(
        f"NeoForge installer завершился, но корректный профиль для Minecraft {minecraft_version} "
        f"и NeoForge {loader_version} не найден в {versions_root}."
        + (f"\nПроверка: {detail}" if detail else "")
    )
