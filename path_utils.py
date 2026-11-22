"""Единые правила выбора корневой папки FiercyLauncher."""
from pathlib import Path
import shutil


def resolve_launcher_root(selected_directory: str | Path) -> Path:
    """Возвращает <выбранная папка>/FiercyLauncher без двойного вложения.

    Если пользователь уже выбрал папку с именем FiercyLauncher, она используется
    как корень. Иначе одноимённая папка создаётся внутри выбранного каталога.
    """
    raw = str(selected_directory).strip()
    if not raw:
        raise ValueError("Не выбрана папка для хранения FiercyLauncher.")
    selected = Path(raw).expanduser()
    if selected.name.casefold() == "fiercylauncher":
        root = selected
    else:
        root = selected / "FiercyLauncher"
    return root.resolve()


def selected_base_from_root(root: str | Path) -> Path:
    """Показывает пользователю каталог-родитель, который он выбирал в диалоге."""
    path = Path(root).expanduser().resolve()
    if path.name.casefold() == "fiercylauncher":
        return path.parent
    return path


def migrate_game_data(source_directory: str | Path, destination_directory: str | Path) -> tuple[Path, Path]:
    """Безопасно объединяет старую папку игры с новым корнем.

    Уже существующие файлы в месте назначения не удаляются: copytree с
    dirs_exist_ok=True перезаписывает только файлы с теми же относительными
    именами, сохраняя чужие папки и дополнительные файлы пользователя.
    Нельзя переносить папку внутрь самой себя или в её родительскую папку.
    """
    source = Path(source_directory).expanduser().resolve()
    destination = Path(destination_directory).expanduser().resolve()
    if source == destination:
        return source, destination
    if source in destination.parents or destination in source.parents:
        raise ValueError(
            "Нельзя переносить игровые данные во вложенную папку или в родительский каталог "
            "текущей установки. Выбери отдельную папку, например D:\\Games, чтобы лаунчер "
            "создал D:\\Games\\FiercyLauncher."
        )
    if not source.exists():
        destination.mkdir(parents=True, exist_ok=True)
        return source, destination
    if not source.is_dir():
        raise NotADirectoryError(f"Исходный путь не является папкой: {source}")

    excluded_dirs = {
        ".git", ".venv", "venv", "__pycache__", "build", "dist",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
    }
    excluded_files = {"desktop.ini", "Thumbs.db"}

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in excluded_dirs or name in excluded_files}
        # Защита от самокопирования, если папка назначения уже существует
        # внутри обхода (например, её создали вручную до вызова функции).
        current = Path(directory).resolve()
        for name in names:
            candidate = (current / name).resolve()
            if candidate == destination or destination in candidate.parents:
                ignored.add(name)
        return ignored

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source, destination, dirs_exist_ok=True, copy_function=shutil.copy2,
        ignore=ignore, symlinks=True,
    )
    return source, destination
