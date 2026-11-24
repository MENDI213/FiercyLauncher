"""Create source and portable Windows release archives plus SHA256SUMS.txt."""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

from release_packaging import should_exclude_pem

ROOT = Path(__file__).resolve().parent
EXCLUDE_DIRS = {
    ".git", ".venv", "__pycache__", "build", "dist", "release_assets",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "admin_panel", "control", "node_modules",
    "logs", "instances", "runtime", "saves", "modpack-downloads", "updates",
    ".fiercy", ".minecraft", "profiles", "temp", "tmp", "crash-reports",
    "screenshots", "resourcepacks", "shaderpacks", "mods", "config", ".idea", ".vscode",
}
EXCLUDE_FILES = {
    ".env", ".env.local", "desktop.ini", "profiles.json", "credentials.json",
    "launcher_profiles.json", "accounts.json", "auth.json", "tokens.json",
}


def add_tree(archive: zipfile.ZipFile, folder: Path, arc_prefix: str,
             include_hidden: bool = True) -> None:
    folder = Path(folder)
    for current, dirs, files in os.walk(folder):
        current_path = Path(current)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and (include_hidden or not d.startswith("."))]
        for filename in files:
            lowered = filename.lower()
            if (filename in EXCLUDE_FILES or filename.endswith((".pyc", ".pyo", ".log", ".download", ".bak"))
                    or lowered.endswith(("-installer.jar.log", ".key", ".sqlite", ".sqlite3", ".db"))
                    or any(token in lowered for token in ("token", "credential", "secret", "account"))):
                continue
            if filename.startswith(".") and not include_hidden:
                continue
            full = current_path / filename
            if full.is_symlink() or not full.is_file():
                continue
            rel = full.relative_to(folder)
            # Keep exactly the public root-CA list needed by requests/certifi.
            # Arbitrary PEM files (including private keys) remain excluded.
            if should_exclude_pem(rel, full):
                continue
            archive.write(full, (Path(arc_prefix) / rel).as_posix())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release_archives(root: Path = ROOT) -> list[Path]:
    root = Path(root).resolve()
    out = root / "release_assets"
    out.mkdir(exist_ok=True)
    created: list[Path] = []

    source_zip = out / "FiercyLauncher-source.zip"
    with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        add_tree(archive, root, "FiercyLauncher")
    created.append(source_zip)
    print(f"Created {source_zip}")

    portable = root / "dist" / "FiercyLauncher"
    portable_zip = out / "FiercyLauncher-win64.zip"
    if (portable / "FiercyLauncher.exe").is_file():
        with zipfile.ZipFile(portable_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            add_tree(archive, portable, "FiercyLauncher", include_hidden=True)
        ca_member = "FiercyLauncher/_internal/certifi/cacert.pem"
        with zipfile.ZipFile(portable_zip) as archive:
            if ca_member not in archive.namelist():
                raise RuntimeError(
                    "Portable ZIP создан без certifi/cacert.pem. Публикация остановлена: "
                    "игроки не смогут использовать HTTPS. Проверь PyInstaller и фильтр PEM."
                )
        created.append(portable_zip)
        print(f"Created {portable_zip}")
    else:
        print("Windows package skipped (run build_windows.bat first).")

    assets = sorted([path for path in out.glob("*.zip")] + ([out / "FiercySetup.exe"] if (out / "FiercySetup.exe").is_file() else []))
    sums = out / "SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in assets), encoding="utf-8")
    created.append(sums)
    print(f"Created {sums}")
    return created


if __name__ == "__main__":
    build_release_archives()
