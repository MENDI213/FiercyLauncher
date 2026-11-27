"""Small, testable helpers for installing official NeoForge client profiles.

The official LegacyInstaller expects a launcher-style game directory containing
launcher_profiles.json. Third-party launchers can provide a minimal profile file;
the installer's own processor pipeline still creates the version JSON and libraries.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from packaging.version import InvalidVersion, Version

NEOFORGED_MAVEN = "https://maven.neoforged.net/releases/net/neoforged"
BMCLAPI_BASE = "https://bmclapi2.bangbang93.com"
BMCLAPI_MAVEN_MIRROR = f"{BMCLAPI_BASE}/maven/"


def neoforge_bmclapi_list_url(minecraft_version: str) -> str:
    """BMCLAPI's documented list endpoint, scoped to one Minecraft version."""
    from urllib.parse import quote
    value = str(minecraft_version or "").strip()
    neoforge_maven_coordinates(value)  # validate before building a request URL
    return f"{BMCLAPI_BASE}/neoforge/list/{quote(value, safe='.') }"


def neoforge_bmclapi_metadata_url(minecraft_version: str) -> str:
    """BMCLAPI's Maven metadata endpoint for the correct NeoForge coordinate family."""
    artifact, _prefix = neoforge_maven_coordinates(minecraft_version)
    return (f"{BMCLAPI_BASE}/neoforge/meta/api/maven/details/releases/"
            f"net/neoforged/{artifact}")


def neoforge_bmclapi_installer_url(minecraft_version: str, loader_version: str) -> str:
    """BMCLAPI's official NeoForge installer redirect endpoint."""
    neoforge_installer_url(minecraft_version, loader_version)  # validates exact MC/version pairing
    from urllib.parse import quote
    return f"{BMCLAPI_BASE}/neoforge/version/{quote(str(loader_version), safe='.-')}/download/installer.jar"


def extract_neoforge_versions(payload: str | bytes, minecraft_version: str) -> list[str]:
    """Extract exact version strings from BMCLAPI's JSON metadata response."""
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = str(payload or "")
    candidates: set[str] = set()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None

    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str):
                    candidates.add(key)
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, str):
            candidates.add(value.strip())

    if parsed is not None:
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                mc = str(item.get("mcversion") or item.get("minecraftVersion") or "").strip()
                if mc and mc != str(minecraft_version).strip():
                    continue
                version = str(item.get("version") or "").strip()
                raw_version = str(item.get("rawVersion") or "").strip()
                if version:
                    candidates.add(version)
                if raw_version:
                    # Some legacy objects encode `1.20.1-47.1.12` in rawVersion.
                    prefix = str(minecraft_version).strip() + "-"
                    candidates.add(raw_version[len(prefix):] if raw_version.startswith(prefix) else raw_version)
        else:
            collect(parsed)
    # Some BMCLAPI metadata shapes embed version coordinates in strings or keys.
    candidates.update(re.findall(
        r"(?<![A-Za-z0-9])(?:1\.20\.1-)?[0-9]+\.[0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9.]+)?",
        text,
    ))
    return filter_stable_neoforge_versions(minecraft_version, candidates)


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
    """Create a launcher_profiles.json containing a usable vanilla bootstrap profile.

    LegacyInstaller checks for at least one vanilla launcher profile. An empty
    ``profiles`` object is not enough and produces the familiar "run the launcher
    first" error. Existing user profiles are preserved; a temporary Fiercy profile
    is added only when no usable profile exists.
    """
    root = Path(game_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "launcher_profiles.json"
    created = not path.exists()
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
    else:
        data = {}

    profiles = data.get("profiles")
    if profiles is None:
        profiles = {}
        data["profiles"] = profiles
    elif not isinstance(profiles, dict):
        raise RuntimeError(f"В файле {path} поле 'profiles' имеет неверный формат. Файл не изменён.")

    has_usable_profile = any(
        isinstance(profile, dict)
        and str(profile.get("name") or "").strip()
        and (str(profile.get("lastVersionId") or "").strip()
             or str(profile.get("type") or "").strip())
        for profile in profiles.values()
    )
    if not has_usable_profile:
        profile_id = "fiercylauncher-bootstrap"
        suffix = 2
        while profile_id in profiles:
            profile_id = f"fiercylauncher-bootstrap-{suffix}"
            suffix += 1
        now = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
        profiles[profile_id] = {
            "name": "FiercyLauncher",
            "type": "custom",
            "created": now,
            "lastUsed": now,
            "lastVersionId": "latest-release",
            "gameDir": str(root),
            "javaArgs": "",
        }
        data["selectedProfile"] = profile_id
    else:
        selected = str(data.get("selectedProfile") or "")
        if selected not in profiles:
            selected = next((key for key, value in profiles.items()
                             if isinstance(value, dict) and value.get("name")), "")
            if selected:
                data["selectedProfile"] = selected

    data.setdefault("settings", {})
    if not isinstance(data["settings"], dict):
        data["settings"] = {}
    data["version"] = max(3, int(data.get("version", 0) or 0))
    _atomic_json_write(path, data)
    return path, created


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
