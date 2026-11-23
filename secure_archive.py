"""Strict ZIP validation and extraction helpers used for updates and game imports."""
from __future__ import annotations

import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)


def _normalise_member(name: str) -> tuple[str, bool]:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("В ZIP найдено пустое или некорректное имя файла.")
    normal = name.replace("\\", "/")
    if normal.startswith("/") or normal.startswith("//"):
        raise ValueError(f"Абсолютный путь в ZIP запрещён: {name!r}")
    is_dir = normal.endswith("/")
    pure = PurePosixPath(normal)
    if pure.is_absolute() or not pure.parts or any(p in ("", ".", "..") for p in pure.parts):
        raise ValueError(f"Небезопасный путь в ZIP: {name!r}")
    if ":" in pure.parts[0]:
        raise ValueError(f"Путь с буквой диска в ZIP запрещён: {name!r}")
    for part in pure.parts:
        if part.endswith((" ", ".")) or _WINDOWS_RESERVED.fullmatch(part):
            raise ValueError(f"Недопустимое имя файла для Windows: {name!r}")
        if len(part) > 240:
            raise ValueError(f"Слишком длинное имя файла в ZIP: {name!r}")
    return "/".join(pure.parts), is_dir


def normalise_member_name(name: str) -> tuple[str, bool]:
    """Public path validator for file paths embedded inside archive manifests."""
    return _normalise_member(name)


def validate_zip(source: str | Path | zipfile.ZipFile, *, max_archive_size: int = 1024**3,
                 max_total_size: int = 2 * 1024**3, max_file_size: int = 1024**3,
                 max_entries: int = 100_000, max_ratio: int = 2000) -> list[zipfile.ZipInfo]:
    """Validate paths, links, duplicates, encryption and decompression limits."""
    own = not isinstance(source, zipfile.ZipFile)
    archive = zipfile.ZipFile(source, "r") if own else source
    try:
        if own:
            archive_path = Path(source)
            if archive_path.stat().st_size > max_archive_size:
                raise ValueError("ZIP-архив превышает допустимый размер.")
        entries = archive.infolist()
        if len(entries) > max_entries:
            raise ValueError("В ZIP слишком много элементов.")
        total = 0
        seen: set[str] = set()
        for item in entries:
            name, is_dir = _normalise_member(item.filename)
            key = name.casefold()
            if key in seen:
                raise ValueError(f"В ZIP повторяется путь: {item.filename}")
            seen.add(key)
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"Символические ссылки в ZIP запрещены: {item.filename}")
            if item.flag_bits & 0x1:
                raise ValueError(f"Зашифрованные ZIP-элементы не поддерживаются: {item.filename}")
            if item.file_size < 0 or item.file_size > max_file_size:
                raise ValueError(f"Слишком большой файл в ZIP: {item.filename}")
            total += item.file_size
            if total > max_total_size:
                raise ValueError("Распакованный размер ZIP превышает допустимый лимит.")
            if item.file_size > 1024 * 1024:
                compressed = max(1, item.compress_size)
                if item.file_size / compressed > max_ratio:
                    raise ValueError(f"Подозрительная степень сжатия ZIP: {item.filename}")
            if is_dir and item.file_size:
                raise ValueError(f"Каталог в ZIP содержит данные: {item.filename}")
        return entries
    finally:
        if own:
            archive.close()


def safe_extract_zip(archive: zipfile.ZipFile, destination: str | Path, *,
                     strip_prefix: str = "", max_total_size: int = 2 * 1024**3) -> int:
    """Extract a previously validated archive to an empty, private staging directory."""
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prefix = strip_prefix.replace("\\", "/").strip("/")
    prefix = prefix + "/" if prefix else ""
    total_written = 0
    for item in archive.infolist():
        normal, is_dir = _normalise_member(item.filename)
        if prefix:
            if not normal.startswith(prefix.rstrip("/")):
                continue
            if normal == prefix.rstrip("/"):
                continue
            if not normal.startswith(prefix):
                raise ValueError(f"Некорректная структура ZIP: {item.filename}")
            relative = normal[len(prefix):]
        else:
            relative = normal
        if not relative:
            continue
        target = root.joinpath(*PurePosixPath(relative).parts)
        # Reject any pre-existing symlink in the destination chain.
        cursor = root
        for part in PurePosixPath(relative).parts[:-1]:
            cursor = cursor / part
            if cursor.exists() and cursor.is_symlink():
                raise ValueError("Временная папка распаковки содержит символическую ссылку.")
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Файл ZIP выходит за пределы папки: {item.filename}")
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Повторная запись файла при распаковке запрещена: {relative}")
        written = 0
        with archive.open(item, "r") as src, target.open("xb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                total_written += len(chunk)
                if written > item.file_size or total_written > max_total_size:
                    raise ValueError("ZIP распаковывается за пределы объявленного размера.")
                dst.write(chunk)
        if written != item.file_size:
            raise ValueError(f"Размер файла после распаковки не совпадает: {relative}")
    return total_written
