"""Wait for FiercyLauncher to exit, verify/extract a release ZIP and replace files."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import hashlib
import re
from pathlib import Path
from secure_archive import safe_extract_zip, validate_zip

PRESERVE = {".venv", ".git", "__pycache__", "logs", "instances", "saves", "mods", "config", "resourcepacks", "shaderpacks"}


def wait_for_process(pid: int, timeout: int = 600) -> None:
    if pid <= 0:
        return
    deadline = time.time() + timeout
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            try:
                WAIT_TIMEOUT = 0x00000102
                INFINITE = 0xFFFFFFFF
                remaining = max(0, int((deadline - time.time()) * 1000))
                result = ctypes.windll.kernel32.WaitForSingleObject(handle, min(remaining, INFINITE))
                if result == WAIT_TIMEOUT:
                    raise TimeoutError("Ожидание закрытия FiercyLauncher превысило 10 минут")
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            return
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.4)
        except OSError:
            return
    raise TimeoutError("Не удалось дождаться закрытия FiercyLauncher")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        validate_zip(zf, max_archive_size=2 * 1024**3,
                     max_total_size=1536 * 1024**2, max_file_size=1024 * 1024**3)
        safe_extract_zip(zf, destination, max_total_size=1536 * 1024**2)


def find_package_root(stage: Path, mode: str) -> Path:
    marker = "main.py" if mode == "source" else "FiercyLauncher.exe"
    if (stage / marker).is_file():
        return stage
    candidates = [p for p in stage.iterdir() if p.is_dir() and (p / marker).is_file()]
    if len(candidates) == 1:
        return candidates[0]
    # PyInstaller archives may contain a named app folder plus metadata.
    for candidate in stage.rglob(marker):
        return candidate.parent
    raise FileNotFoundError(f"В ZIP не найден {marker}. Это не подходящий пакет обновления.")


def copy_package(source: Path, target: Path, backup: Path, mode: str) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    changed: list[Path] = []
    for current, dirs, files in os.walk(source):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        safe_dirs = []
        for dirname in dirs:
            candidate = current_path / dirname
            if dirname in PRESERVE or dirname.startswith("."):
                continue
            if candidate.is_symlink():
                raise ValueError(f"В пакете обновления обнаружена символическая ссылка: {candidate}")
            safe_dirs.append(dirname)
        dirs[:] = safe_dirs
        dest_dir = target / relative
        if dest_dir.is_symlink() or getattr(dest_dir, "is_junction", lambda: False)():
            raise ValueError(f"Каталог приложения является ссылкой: {dest_dir}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        resolved_dest_dir = dest_dir.resolve()
        if resolved_dest_dir != target and target not in resolved_dest_dir.parents:
            raise ValueError(f"Путь обновления выходит за папку приложения: {relative}")
        for name in files:
            # The running standalone updater executable is locked by Windows;
            # it intentionally remains compatible and is replaced in a later
            # dedicated updater release if its protocol ever changes.
            if mode == "frozen" and name.lower() == "fiercyupdater.exe":
                continue
            src = current_path / name
            if src.is_symlink() or not src.is_file():
                raise ValueError(f"Небезопасный файл в пакете обновления: {src}")
            rel = src.relative_to(source)
            if any(part in PRESERVE or part.startswith(".") for part in rel.parts):
                continue
            dst = target / rel
            cursor = target
            for part in rel.parts[:-1]:
                cursor = cursor / part
                if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
                    raise ValueError(f"Путь обновления проходит через ссылку или junction: {cursor}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or getattr(dst, "is_junction", lambda: False)():
                raise ValueError(f"Обновление не будет перезаписывать ссылку: {dst}")
            resolved_parent = dst.parent.resolve()
            if resolved_parent != target and target not in resolved_parent.parents:
                raise ValueError(f"Путь обновления выходит за папку приложения: {rel}")
            if dst.exists():
                old = backup / rel
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, old)
            else:
                changed.append(rel)  # Newly created file; remove on rollback.
            temp = dst.with_suffix(dst.suffix + ".new")
            shutil.copy2(src, temp)
            os.replace(temp, dst)
            changed.append(rel)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--mode", choices=("source", "frozen"), required=True)
    parser.add_argument("--entry", default="main.py")
    parser.add_argument("--sha256", default="", help="Optional SHA-256; older launchers use the already-verified download flow.")
    args = parser.parse_args()

    archive = Path(args.archive).resolve()
    target = Path(args.target).resolve()
    expected_hash = str(args.sha256).lower().strip()
    sidecar = archive.with_name(archive.name + ".sha256")
    if not expected_hash and sidecar.is_file():
        expected_hash = sidecar.read_text(encoding="ascii", errors="ignore").strip().split()[0]
    if expected_hash and not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("Некорректная контрольная сумма обновления.")
    if not archive.is_file():
        raise FileNotFoundError(archive)
    # The launcher verifies the GitHub Release SHA-256 before hand-off. A sidecar
    # allows newer helpers to repeat verification without breaking older installed
    # FiercyUpdater.exe builds, whose command line does not know --sha256.
    if expected_hash and sha256_file(archive) != expected_hash:
        raise ValueError("SHA-256 архива не совпадает. Файлы приложения не изменены.")
    if not target.is_dir() or target.is_symlink():
        raise ValueError("Недопустимая папка установленного лаунчера.")
    marker = "main.py" if args.mode == "source" else "FiercyLauncher.exe"
    if not (target / marker).is_file():
        raise ValueError("В целевой папке не найдено установленное приложение FiercyLauncher.")
    wait_for_process(args.parent_pid)
    if expected_hash and sha256_file(archive) != expected_hash:
        raise ValueError("Архив изменился во время ожидания закрытия лаунчера.")

    work = Path(tempfile.mkdtemp(prefix="fiercy-update-"))
    try:
        stage = work / "extract"
        stage.mkdir()
        safe_extract(archive, stage)
        package = find_package_root(stage, args.mode)
        backup = work / "backup"
        changed = []
        try:
            # Update is an overlay, not a destructive mirror: user data, settings,
            # virtual environments and game instances remain untouched. Existing
            # files are backed up and rolled back if any copy step fails.
            changed = copy_package(package, target, backup, args.mode)
        except Exception:
            for rel in reversed(changed):
                dst = target / rel
                old = backup / rel
                if old.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(old, dst)
                else:
                    dst.unlink(missing_ok=True)
            raise
        if args.mode == "source":
            entry = target / args.entry
            command = [sys.executable, str(entry)]
        else:
            entry = target / args.entry
            command = [str(entry)]
        if not entry.exists():
            raise FileNotFoundError(f"После обновления не найден файл запуска: {entry}")
        subprocess.Popen(command, cwd=str(target), close_fds=True)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log = Path(tempfile.gettempdir()) / "FiercyLauncher-update-error.txt"
        log.write_text(f"Update failed: {exc}\n", encoding="utf-8")
        raise
