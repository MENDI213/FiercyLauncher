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
from pathlib import Path, PurePosixPath

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


def safe_extract(archive: Path, destination: Path) -> None:
    base = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        items = zf.infolist()
        if len(items) > 100_000:
            raise ValueError("В архиве слишком много файлов")
        if sum(item.file_size for item in items) > 1_500 * 1024 * 1024:
            raise ValueError("Распакованный размер обновления превышает 1.5 ГБ")
        for item in items:
            name = item.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
                raise ValueError(f"Небезопасный путь в обновлении: {name}")
            target = (base / Path(*pure.parts)).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"Архив выходит за пределы папки: {name}")
        zf.extractall(destination)


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
        dirs[:] = [d for d in dirs if d not in PRESERVE and not d.startswith(".")]
        dest_dir = target / relative
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            # The running standalone updater executable is locked by Windows;
            # it intentionally remains compatible and is replaced in a later
            # dedicated updater release if its protocol ever changes.
            if mode == "frozen" and name.lower() == "fiercyupdater.exe":
                continue
            src = current_path / name
            rel = src.relative_to(source)
            if any(part in PRESERVE or part.startswith(".") for part in rel.parts):
                continue
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
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
    args = parser.parse_args()

    archive = Path(args.archive).resolve()
    target = Path(args.target).resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    wait_for_process(args.parent_pid)

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
