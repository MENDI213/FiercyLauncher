"""Настройка ротационных логов лаунчера и Minecraft."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def user_data_dir() -> Path:
    """Пользовательские данные не хранятся рядом с EXE или внутри PyInstaller."""
    root = Path.home() / ".fiercylauncher"
    root.mkdir(parents=True, exist_ok=True)
    return root


def log_file() -> Path:
    folder = user_data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "launcher.log"


def configure_logging() -> Path:
    path = log_file()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(getattr(handler, "baseFilename", None) == str(path) for handler in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        root.addHandler(handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return path
