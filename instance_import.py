"""Импорт локальных Minecraft-версий в общий game store FiercyLauncher."""
from __future__ import annotations

import filecmp
import json
import logging
import os
import shutil
from pathlib import Path
from PyQt5.QtCore import QThread, pyqtSignal
from version_metadata import read_version_metadata

log = logging.getLogger("fiercy.import")


def _copy_missing_tree(source: Path, target: Path, progress=None) -> int:
    copied = 0
    if not source.is_dir():
        return copied
    for current, dirs, files in os.walk(source):
        current_path = Path(current)
        rel = current_path.relative_to(source)
        target_dir = target / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = current_path / name
            dst = target_dir / name
            try:
                if dst.is_file() and src.is_file() and filecmp.cmp(src, dst, shallow=False):
                    continue
                shutil.copy2(src, dst)
                copied += 1
                if progress and copied % 100 == 0:
                    progress(f"Скопировано файлов: {copied}")
            except OSError as exc:
                log.warning("Не удалось импортировать %s: %s", src, exc)
                raise
    return copied


def import_local_version(source_game_root: str | Path, version_id: str,
                         target_game_root: str | Path, target_instance_dir: str | Path | None = None,
                         progress=None) -> dict:
    source = Path(source_game_root).expanduser().resolve()
    target = Path(target_game_root).expanduser().resolve()
    version_folder = source / "versions" / version_id
    version_json = version_folder / f"{version_id}.json"
    if not version_json.is_file():
        raise FileNotFoundError(f"Не найден файл версии {version_json}")
    metadata = read_version_metadata(source, version_id)
    raw = json.loads(version_json.read_text(encoding="utf-8"))

    # Импортируем всю цепочку inheritsFrom — Forge/NeoForge часто опираются на родителя.
    current = version_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        folder = source / "versions" / current
        json_path = folder / f"{current}.json"
        if not json_path.is_file():
            raise FileNotFoundError(f"Не найден родительский JSON версии: {json_path}")
        if progress:
            progress(f"Импорт версии {current}…")
        _copy_missing_tree(folder, target / "versions" / current, progress)
        parent_data = json.loads(json_path.read_text(encoding="utf-8"))
        current = str(parent_data.get("inheritsFrom", "")).strip()

    # Общие библиотеки/ассеты/Java копируются один раз; файлы одинакового размера
    # не переписываются повторно, поэтому импорт не удваивает уже существующие данные.
    for folder in ("libraries", "assets", "runtime"):
        if progress:
            progress(f"Импорт общей папки {folder}…")
        _copy_missing_tree(source / folder, target / folder, progress)

    # Импорт локальной сборки — это не только JSON версии: переносим профильные
    # данные из выбранного game root в новую папку instances/<id>.
    if target_instance_dir:
        target_instance = Path(target_instance_dir).expanduser().resolve()
        target_instance.mkdir(parents=True, exist_ok=True)
        for folder in ("mods", "config", "resourcepacks", "shaderpacks", "saves"):
            if progress:
                progress(f"Импорт папки профиля {folder}…")
            _copy_missing_tree(source / folder, target_instance / folder, progress)
        for filename in ("options.txt", "servers.dat", "servers.dat_old"):
            src = source / filename
            dst = target_instance / filename
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)

    metadata.update({"source": str(source)})
    return metadata


class LocalVersionImportWorker(QThread):
    progress = pyqtSignal(str)
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, source_root: str, version_id: str, target_root: str,
                 target_instance_dir: str, parent=None):
        super().__init__(parent)
        self.source_root = source_root
        self.version_id = version_id
        self.target_root = target_root
        self.target_instance_dir = target_instance_dir

    def run(self):
        try:
            result = import_local_version(
                self.source_root, self.version_id, self.target_root,
                target_instance_dir=self.target_instance_dir,
                progress=self.progress.emit,
            )
            self.completed.emit(result)
        except Exception as exc:
            log.exception("Local version import failed")
            self.failed.emit(str(exc))
