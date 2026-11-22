"""Фоновые операции с корнем хранения FiercyLauncher."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from path_utils import migrate_game_data

log = logging.getLogger("fiercy.storage")


class StorageMigrationWorker(QThread):
    """Переносит игровые данные и, при необходимости, portable-приложение.

    Копирование выполняется в отдельном потоке: перенос большого каталога с
    библиотеками/миром не блокирует интерфейс и прокрутку настроек.
    """

    progress = pyqtSignal(str)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, source_root: str, destination_root: str,
                 application_root: str = "", parent=None):
        super().__init__(parent)
        self.source_root = Path(source_root).expanduser().resolve()
        self.destination_root = Path(destination_root).expanduser().resolve()
        self.application_root = Path(application_root).expanduser().resolve() if application_root else None

    @staticmethod
    def _copy_application(source: Path, destination: Path) -> None:
        if source == destination:
            return
        if not source.is_dir():
            raise FileNotFoundError(f"Не найдена папка установленного приложения: {source}")
        destination.mkdir(parents=True, exist_ok=True)
        excluded = {
            ".git", ".venv", "venv", "__pycache__", "build", "dist", "release_assets",
            "instances", ".fiercy", "runtime", "logs", "updates", "profiles",
            "versions", "libraries",
        }

        def ignore(current: str, names: list[str]) -> set[str]:
            current_path = Path(current).resolve()
            ignored = {name for name in names if name in excluded}
            # Если каталог назначения находится внутри исходной папки приложения,
            # исключаем ветку, в которой он находится, чтобы избежать рекурсии.
            for name in names:
                candidate = (current_path / name).resolve()
                if candidate == destination or candidate in destination.parents:
                    ignored.add(name)
            return ignored

        shutil.copytree(source, destination, dirs_exist_ok=True, copy_function=shutil.copy2,
                        ignore=ignore)

    def run(self) -> None:
        try:
            self.destination_root.mkdir(parents=True, exist_ok=True)
            if self.source_root != self.destination_root:
                self.progress.emit("Перенос миров, профилей, версий и библиотек…")
                migrate_game_data(self.source_root, self.destination_root)
            if self.application_root and self.application_root != self.destination_root:
                self.progress.emit("Копирование файлов FiercyLauncher…")
                self._copy_application(self.application_root, self.destination_root)
            self.progress.emit("Проверка новой папки FiercyLauncher…")
            self.completed.emit(str(self.destination_root))
        except Exception as exc:
            log.exception("Ошибка переноса каталога FiercyLauncher")
            self.failed.emit(str(exc))
