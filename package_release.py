"""Создаёт ZIP исходников, portable Windows ZIP и SHA256SUMS.txt."""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "release_assets"
OUT.mkdir(exist_ok=True)
EXCLUDE_DIRS = {
    ".git", ".venv", "__pycache__", "build", "dist", "release_assets",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "admin_panel", "control", "node_modules",
}
EXCLUDE_FILES = {".env", ".env.local", "desktop.ini"}


def add_tree(archive: zipfile.ZipFile, folder: Path, arc_prefix: str,
             include_hidden: bool = True) -> None:
    for current, dirs, files in os.walk(folder):
        current_path = Path(current)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and (include_hidden or not d.startswith("."))]
        for filename in files:
            if filename in EXCLUDE_FILES or filename.endswith((".pyc", ".pyo")):
                continue
            if filename.startswith(".") and not include_hidden:
                continue
            full = current_path / filename
            rel = full.relative_to(folder)
            archive.write(full, (Path(arc_prefix) / rel).as_posix())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


source_zip = OUT / "FiercyLauncher-source.zip"
with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
    add_tree(archive, ROOT, "FiercyLauncher")
print(f"Created {source_zip}")

portable = ROOT / "dist" / "FiercyLauncher"
portable_zip = OUT / "FiercyLauncher-win64.zip"
if (portable / "FiercyLauncher.exe").is_file():
    with zipfile.ZipFile(portable_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        add_tree(archive, portable, "FiercyLauncher", include_hidden=True)
    print(f"Created {portable_zip}")
else:
    print("Windows package skipped (run build_windows.bat first).")

assets = sorted([path for path in OUT.glob("*.zip")] + ([OUT / "FiercySetup.exe"] if (OUT / "FiercySetup.exe").is_file() else []))
(OUT / "SHA256SUMS.txt").write_text(
    "".join(f"{sha256(path)}  {path.name}\n" for path in assets), encoding="utf-8"
)
print(f"Created {OUT / 'SHA256SUMS.txt'}")
