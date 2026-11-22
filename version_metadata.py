"""Извлечение метаданных из локального Minecraft version JSON."""
from __future__ import annotations

import json
import re
from pathlib import Path


_LOADER_PATTERNS = (
    ("neoforge", re.compile(r"neoforge[-_](\d+(?:\.\d+)+)", re.IGNORECASE)),
    ("forge", re.compile(r"forge[-_](\d+(?:\.\d+)+)", re.IGNORECASE)),
    ("fabric", re.compile(r"fabric-loader[-_](\d+(?:\.\d+)+)", re.IGNORECASE)),
    ("quilt", re.compile(r"quilt-loader[-_](\d+(?:\.\d+)+)", re.IGNORECASE)),
)


def read_version_metadata(game_root: str | Path, version_id: str) -> dict[str, str]:
    """Возвращает Minecraft version и loader из versions/<id>/<id>.json.

    Если версия наследуется от Vanilla, minecraft_version берётся из inheritsFrom.
    Loader version определяется из ID профиля, если он содержит стандартный формат.
    """
    version_id = str(version_id).strip()
    if not version_id or any(part in version_id for part in ("/", "\\", "..")):
        raise ValueError("Некорректный ID версии Minecraft.")
    root = Path(game_root).expanduser().resolve()
    json_path = root / "versions" / version_id / f"{version_id}.json"
    if not json_path.is_file():
        raise FileNotFoundError(f"Не найден JSON установленной версии: {json_path}")
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("JSON версии Minecraft должен содержать объект.")
    json_id = str(raw.get("id", version_id)).strip()
    if json_id != version_id:
        raise ValueError("ID версии в JSON не совпадает с именем папки versions.")

    loader = "vanilla"
    loader_version = ""
    for name, pattern in _LOADER_PATTERNS:
        match = pattern.search(version_id)
        if match:
            loader = name
            loader_version = match.group(1)
            break

    minecraft_version = str(raw.get("inheritsFrom") or "").strip()
    if not minecraft_version:
        minecraft_version = version_id
        for name, pattern in _LOADER_PATTERNS:
            if name in loader:
                # Для IDs вида forge-1.20.1-47.2.0 пробуем найти версию MC,
                # а для стандартных loader IDs используем указанного родителя.
                parts = version_id.split("-")
                candidates = [part for part in parts if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", part)]
                if candidates:
                    minecraft_version = candidates[-1]
                break
    return {
        "installed_version_id": version_id,
        "minecraft_version": minecraft_version,
        "loader": loader,
        "loader_version": loader_version,
    }
