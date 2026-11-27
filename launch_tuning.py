"""Safe JVM presets and lightweight local-install verification cache."""
from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path
from typing import Iterable


def recommended_jvm_arguments(minecraft_version: str, loader: str, java_major: int | None) -> list[str]:
    """Return conservative G1GC options supported by mainstream Java runtimes.

    Loader-specific launch arguments are still taken from the installed version
    metadata by minecraft-launcher-lib; this function only adds generic JVM flags.
    """
    if java_major is None or int(java_major) < 8:
        return []
    loader_name = str(loader or "vanilla").lower()
    if loader_name not in {"vanilla", "fabric", "forge", "neoforge", "quilt"}:
        loader_name = "vanilla"
    # Deliberately conservative: no forced heap sizes, experimental collector,
    # or flags that depend on a particular Forge/NeoForge release.
    return [
        "-XX:+UseG1GC",
        "-XX:+ParallelRefProcEnabled",
        "-XX:MaxGCPauseMillis=100",
        "-XX:+DisableExplicitGC",
    ]


def merge_jvm_arguments(defaults: Iterable[str], custom_text: str) -> list[str]:
    """Merge user options over defaults without duplicate -XX/-D keys."""
    try:
        custom = shlex.split(str(custom_text or ""), posix=True)
    except ValueError as exc:
        raise ValueError(f"Не удалось разобрать дополнительные JVM-аргументы: {exc}") from exc

    def option_key(argument: str) -> str:
        arg = str(argument)
        if arg.startswith("-XX:"):
            body = arg[4:]
            body = body.lstrip("+-")
            return "-XX:" + body.split("=", 1)[0]
        if arg.startswith("-D"):
            return arg.split("=", 1)[0]
        if arg.startswith("-Xms") or arg.startswith("-Xmx"):
            return arg[:4]
        return arg.split("=", 1)[0]

    merged: dict[str, str] = {}
    order: list[str] = []
    for item in list(defaults or []) + custom:
        if item.lower().startswith(("-xms", "-xmx")):
            raise ValueError("Параметры -Xms и -Xmx задаются ползунком памяти.")
        key = option_key(item)
        if key not in merged:
            order.append(key)
        merged[key] = item
    return [merged[key] for key in order]


def _verification_cache_path(game_root: str | Path) -> Path:
    return Path(game_root).expanduser().resolve() / ".fiercy" / "launch_verification.json"


def _local_version_files_exist(game_root: str | Path, version_id: str) -> bool:
    root = Path(game_root).expanduser().resolve()
    version_dir = root / "versions" / str(version_id)
    manifest = version_dir / f"{version_id}.json"
    if not manifest.is_file() or manifest.stat().st_size <= 0:
        return False
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    jar_id = str(metadata.get("jar") or metadata.get("inheritsFrom") or version_id)
    jar_path = root / "versions" / jar_id / f"{jar_id}.jar"
    # A child loader version normally inherits the vanilla client JAR.
    return jar_path.is_file() and jar_path.stat().st_size > 0


def should_verify_before_launch(
    game_root: str | Path, version_id: str, max_age_seconds: int = 30 * 24 * 60 * 60,
    now: float | None = None,
) -> bool:
    """Run the library's full repair check when files are missing or cache is stale."""
    if not _local_version_files_exist(game_root, version_id):
        return True
    path = _verification_cache_path(game_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or str(version_id) not in payload:
            # Existing installations from older launcher versions have no cache.
            # Do not force a network round-trip on every first launch: the quick
            # manifest/JAR preflight passed, and the user has an explicit Verify action.
            return False
        checked_at = float(payload.get(str(version_id), 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    current = time.time() if now is None else float(now)
    return checked_at <= 0 or current - checked_at >= max(60, int(max_age_seconds))


def mark_version_verified(game_root: str | Path, version_ids: Iterable[str], now: float | None = None) -> None:
    path = _verification_cache_path(game_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        payload = {}
    stamp = time.time() if now is None else float(now)
    for version_id in version_ids:
        if str(version_id or "").strip():
            payload[str(version_id)] = stamp
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def prefer_gui_java(java_path: str, windows: bool | None = None) -> str:
    """Use javaw.exe on Windows when a sibling exists, avoiding a console window."""
    path = Path(java_path).expanduser()
    is_windows = (os.name == "nt") if windows is None else bool(windows)
    if is_windows and path.name.lower() == "java.exe":
        candidate = path.with_name("javaw.exe")
        if candidate.is_file():
            return str(candidate.resolve())
    return str(path.resolve())
