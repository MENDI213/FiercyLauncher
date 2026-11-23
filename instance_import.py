"""Импорт локальных Minecraft-версий в общий game store FiercyLauncher."""
from __future__ import annotations

import filecmp
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from PyQt5.QtCore import QThread, pyqtSignal
from version_metadata import read_version_metadata

log = logging.getLogger("fiercy.import")


def _copy_missing_tree(source: Path, target: Path, progress=None) -> int:
    copied = 0
    if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
        raise ValueError(f"Исходный каталог импорта является ссылкой: {source}")
    target_root = target.expanduser().resolve()
    if not source.is_dir():
        return copied
    for current, dirs, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not (
            (current_path / name).is_symlink()
            or getattr(current_path / name, "is_junction", lambda: False)()
        )]
        rel = current_path.relative_to(source)
        target_dir = target_root / rel
        if target_dir.is_symlink() or getattr(target_dir, "is_junction", lambda: False)():
            raise ValueError(f"Целевой каталог импорта является ссылкой: {target_dir}")
        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = target_dir.resolve()
        if resolved_dir != target_root and target_root not in resolved_dir.parents:
            raise ValueError(f"Целевой путь импорта выходит за пределы папки: {target_dir}")
        if target_dir.is_symlink() or getattr(target_dir, "is_junction", lambda: False)():
            raise ValueError(f"Целевой каталог импорта является ссылкой: {target_dir}")
        for name in files:
            src = current_path / name
            dst = target_dir / name
            try:
                if src.is_symlink() or not src.is_file():
                    log.warning("Пропущена символическая ссылка при импорте: %s", src)
                    continue
                if dst.is_symlink() or getattr(dst, "is_junction", lambda: False)():
                    raise ValueError(f"Целевой путь является ссылкой: {dst}")
                if dst.is_file() and getattr(dst.stat(), "st_nlink", 1) > 1:
                    raise ValueError(f"Целевой файл связан жёсткой ссылкой: {dst}")
                if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
                    continue
                fd, temp_name = tempfile.mkstemp(prefix=".fiercy-import-", suffix=".part", dir=str(target_dir))
                os.close(fd)
                temp_path = Path(temp_name)
                try:
                    shutil.copy2(src, temp_path)
                    os.replace(temp_path, dst)
                finally:
                    temp_path.unlink(missing_ok=True)
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
    version_id = str(version_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._+\-]{1,128}", version_id) or version_id in {".", ".."}:
        raise ValueError("Некорректный идентификатор локальной версии Minecraft.")
    version_folder = source / "versions" / version_id
    version_json = version_folder / f"{version_id}.json"
    if version_folder.is_symlink() or getattr(version_folder, "is_junction", lambda: False)():
        raise ValueError("Папка локальной версии является ссылкой и не может быть импортирована.")
    if version_json.is_symlink() or not version_json.is_file():
        raise FileNotFoundError(f"Не найден файл версии {version_json}")
    metadata = read_version_metadata(source, version_id)
    raw = json.loads(version_json.read_text(encoding="utf-8"))

    # Импортируем всю цепочку inheritsFrom — Forge/NeoForge часто опираются на родителя.
    current = version_id
    seen: set[str] = set()
    while current:
        if not re.fullmatch(r"[A-Za-z0-9._+\-]{1,128}", current) or current in {".", ".."}:
            raise ValueError(f"Некорректный ID родительской версии: {current!r}")
        if current in seen:
            raise ValueError(f"В цепочке inheritsFrom обнаружен цикл: {current}")
        seen.add(current)
        folder = source / "versions" / current
        json_path = folder / f"{current}.json"
        if folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
            raise ValueError(f"Папка родительской версии является ссылкой: {folder}")
        if json_path.is_symlink() or not json_path.is_file():
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
