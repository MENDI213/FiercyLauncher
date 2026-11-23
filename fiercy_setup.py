"""Небольшой Windows bootstrap installer: скачивает portable-пакет последнего GitHub Release."""
from __future__ import annotations

import hashlib
import base64
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import requests
from update_download import download_verified_asset
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QHBoxLayout, QProgressBar, QFileDialog, QCheckBox, QFrame,
)

REPOSITORY = "MENDI213/FiercyLauncher"
API_LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
USER_AGENT = "FiercySetup/1.0"


def _safe_extract(zip_path: Path, output: Path) -> None:
    root = output.resolve()
    with zipfile.ZipFile(zip_path, "r") as archive:
        items = archive.infolist()
        if len(items) > 100_000 or sum(item.file_size for item in items) > 2 * 1024**3:
            raise ValueError("Архив слишком большой или содержит слишком много файлов.")
        for item in items:
            pure = PurePosixPath(item.filename.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
                raise ValueError(f"Небезопасный путь в архиве: {item.filename}")
            destination = (output / Path(*pure.parts)).resolve()
            if destination != root and root not in destination.parents:
                raise ValueError(f"Архив выходит за пределы папки установки: {item.filename}")
        archive.extractall(output)


def _desktop_directory() -> Path:
    """Resolve the actual Windows Desktop folder, including OneDrive redirection."""
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
            if result == 0 and buffer.value:
                return Path(buffer.value)
        except Exception:
            pass
    return Path.home() / "Desktop"


def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _create_desktop_shortcut(executable: Path) -> Path:
    """Create a Windows .lnk using the built-in WScript.Shell COM object."""
    if os.name != "nt":
        raise OSError("Создание ярлыка поддерживается только в Windows.")
    desktop = _desktop_directory()
    desktop.mkdir(parents=True, exist_ok=True)
    shortcut_path = desktop / "FiercyLauncher.lnk"
    script = "\n".join((
        "$shell = New-Object -ComObject WScript.Shell",
        f"$shortcut = $shell.CreateShortcut({_ps_single_quote(str(shortcut_path))})",
        f"$shortcut.TargetPath = {_ps_single_quote(str(executable.resolve()))}",
        f"$shortcut.WorkingDirectory = {_ps_single_quote(str(executable.resolve().parent))}",
        f"$shortcut.IconLocation = {_ps_single_quote(str(executable.resolve()) + ',0')}",
        "$shortcut.Description = 'FiercyLauncher - Minecraft Launcher'",
        "$shortcut.Save()",
    ))
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=25, creationflags=creation_flags,
        check=False,
    )
    if result.returncode != 0 or not shortcut_path.is_file():
        details = (result.stderr or result.stdout or "PowerShell не создал ярлык.").strip()
        raise RuntimeError(details[:800])
    return shortcut_path


class InstallWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, base_dir: str, parent=None, create_desktop_shortcut: bool = True):
        super().__init__(parent)
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.create_desktop_shortcut = bool(create_desktop_shortcut)
        self.shortcut_error = ""
        self.shortcut_path = None

    def run(self):
        temp_root = Path(tempfile.mkdtemp(prefix="fiercy-setup-"))
        try:
            headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
            self.progress.emit(5, "Получение последнего релиза GitHub…")
            response = requests.get(API_LATEST, timeout=(8, 30), headers=headers)
            response.raise_for_status()
            release = response.json()
            assets = release.get("assets", [])
            package = next((a for a in assets if a.get("name") == "FiercyLauncher-win64.zip"), None)
            sums_asset = next((a for a in assets if a.get("name") == "SHA256SUMS.txt"), None)
            if not package or not sums_asset:
                raise RuntimeError("В последнем GitHub Release отсутствует FiercyLauncher-win64.zip или SHA256SUMS.txt.")

            self.progress.emit(12, "Проверка контрольной суммы релиза…")
            sums_response = requests.get(sums_asset["browser_download_url"], timeout=(8, 30), headers=headers)
            sums_response.raise_for_status()
            expected = ""
            for line in sums_response.text.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[1].lstrip("*") == package["name"]:
                    expected = parts[0].lower()
                    break
            if len(expected) != 64:
                digest = str(package.get("digest", ""))
                expected = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
            if len(expected) != 64:
                raise RuntimeError("В релизе нет корректного SHA-256 для Windows-пакета.")

            cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FiercyLauncher" / "cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            release_tag = str(release.get("tag_name", "latest")).replace("/", "_").replace("\\", "_")
            archive_path = cache_root / f"{release_tag}-{package['name']}"

            def file_sha256(path: Path) -> str:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                return digest.hexdigest()

            if archive_path.is_file() and file_sha256(archive_path) == expected:
                self.progress.emit(72, "Используем проверенный архив последней версии из кеша…")
            else:
                archive_path.unlink(missing_ok=True)
                self.progress.emit(18, "Подключение к GitHub CDN и проверка быстрой загрузки…")
                total = int(package.get("size", 0) or 0)

                def report_download(done: int, total_bytes: int, speed: float, detail: str):
                    effective_total = total_bytes or total
                    percent = 18 + int(54 * done / effective_total) if effective_total else 50
                    speed_mb = speed / 1048576
                    size_text = (
                        f"{done / 1048576:.1f} / {effective_total / 1048576:.1f} МБ"
                        if effective_total else f"{done / 1048576:.1f} МБ"
                    )
                    is_parallel = "Быстрая" in detail
                    mode = "Параллельная загрузка" if is_parallel else "Загрузка"
                    streams = "8 потоков" if is_parallel else "1 поток"
                    self.progress.emit(
                        min(72, max(18, percent)),
                        f"{mode}: {size_text} · {speed_mb:.2f} МБ/с · {streams}",
                    )

                # Используем ту же проверенную загрузку с HTTP Range, что и автообновление.
                # Если CDN не поддерживает Range, модуль сам переключится на обычный поток.
                download_verified_asset(
                    str(package["browser_download_url"]),
                    expected,
                    archive_path,
                    expected_size=total,
                    progress=report_download,
                    max_workers=8,
                )
            # Не копим бесконечное число версий установщика: сохраняем текущий и два предыдущих архива.
            cached = sorted(cache_root.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)
            for stale in cached[3:]:
                stale.unlink(missing_ok=True)

            self.progress.emit(75, "Распаковка и проверка файлов…")
            stage = temp_root / "stage"
            stage.mkdir()
            _safe_extract(archive_path, stage)
            candidates = list(stage.rglob("FiercyLauncher.exe"))
            if len(candidates) != 1:
                raise RuntimeError("В архиве не найден единственный FiercyLauncher.exe.")
            package_root = candidates[0].parent
            target = self.base_dir if self.base_dir.name.casefold() == "fiercylauncher" else self.base_dir / "FiercyLauncher"
            target.mkdir(parents=True, exist_ok=True)

            self.progress.emit(84, "Установка файлов лаунчера…")
            for current, dirs, files in os.walk(package_root):
                current_path = Path(current)
                relative = current_path.relative_to(package_root)
                dirs[:] = [d for d in dirs if d not in {"instances", "versions", "libraries", "runtime", "logs", "updates", ".fiercy"}]
                target_dir = target / relative
                target_dir.mkdir(parents=True, exist_ok=True)
                for filename in files:
                    source = current_path / filename
                    destination = target_dir / filename
                    shutil.copy2(source, destination)
            executable = target / "FiercyLauncher.exe"
            if not executable.is_file():
                raise RuntimeError("После установки не найден FiercyLauncher.exe.")
            if self.create_desktop_shortcut:
                self.progress.emit(91, "Создание ярлыка на рабочем столе…")
                try:
                    self.shortcut_path = _create_desktop_shortcut(executable)
                except Exception as shortcut_exc:
                    # A shortcut failure must not roll back a successful application install.
                    self.shortcut_error = str(shortcut_exc)
                    logging.getLogger("fiercy.setup").warning(
                        "Не удалось создать ярлык рабочего стола: %s", shortcut_exc
                    )
            self.progress.emit(96, "Запуск FiercyLauncher…")
            subprocess.Popen([str(executable)], cwd=str(target), close_fds=True)
            self.progress.emit(100, f"Установка завершена: {target}")
            self.completed.emit(str(target))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


class SetupWindow(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Установка FiercyLauncher")
        self.setFixedSize(600, 390)
        self.setStyleSheet("""
            QDialog { background:#0b1020; color:#f4f6ff; border:1px solid #2c3a58; border-radius:18px; }
            QLabel { color:#f4f6ff; font-family:'Segoe UI'; }
            QLabel#Title { font-size:20pt; font-weight:900; }
            QLabel#Muted { color:#aebbd2; font-size:9pt; }
            QLabel#ShortcutHint { color:#bdc9de; font-size:9pt; padding-left:39px; }
            QLineEdit { background:#111b30; border:1px solid #30415f; border-radius:9px; padding:10px; color:#fff; font-size:10pt; }
            QPushButton { background:#182641; border:1px solid #344966; border-radius:9px; color:#fff; padding:10px 15px; font-size:10pt; font-weight:700; }
            QPushButton#Primary { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #884dff,stop:1 #19bfcf); border:0; }
            QFrame#ShortcutCard { background:#17243a; border:1px solid #4b6387; border-radius:12px; }
            QCheckBox { color:#ffffff; font-family:'Segoe UI'; font-size:12pt; font-weight:800; spacing:12px; padding:10px 12px 4px 12px; }
            QCheckBox::indicator { width:23px; height:23px; }
            QCheckBox::indicator:unchecked { background:#0c1527; border:2px solid #c1cde0; border-radius:5px; }
            QCheckBox::indicator:checked { background:#19bfcf; border:2px solid #19bfcf; border-radius:5px; }
            QCheckBox::indicator:hover { border:2px solid #ffffff; }
            QProgressBar { background:#10192a; border:1px solid #30415f; border-radius:7px; height:15px; text-align:center; color:transparent; }
            QProgressBar::chunk { background:#8e5bff; border-radius:6px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 23, 26, 22)
        layout.setSpacing(13)
        title = QLabel("FIERCY LAUNCHER")
        title.setObjectName("Title")
        subtitle = QLabel("Установка последней версии напрямую из GitHub Releases")
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        path_row = QHBoxLayout()
        default = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs"
        self.path_edit = QLineEdit(str(default))
        browse = QPushButton("Обзор")
        browse.clicked.connect(self.choose_path)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addLayout(path_row)
        shortcut_card = QFrame()
        shortcut_card.setObjectName("ShortcutCard")
        shortcut_layout = QVBoxLayout(shortcut_card)
        shortcut_layout.setContentsMargins(8, 7, 8, 9)
        shortcut_layout.setSpacing(2)
        self.desktop_shortcut_check = QCheckBox("Создать ярлык на рабочем столе")
        self.desktop_shortcut_check.setChecked(True)
        self.desktop_shortcut_check.setCursor(Qt.PointingHandCursor)
        self.desktop_shortcut_check.setToolTip("Сними галочку, если ярлык на рабочем столе не нужен.")
        shortcut_hint = QLabel("На рабочем столе появится значок для быстрого запуска FiercyLauncher.")
        shortcut_hint.setObjectName("ShortcutHint")
        shortcut_hint.setWordWrap(True)
        shortcut_layout.addWidget(self.desktop_shortcut_check)
        shortcut_layout.addWidget(shortcut_hint)
        layout.addWidget(shortcut_card)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = QLabel("Выбери папку и нажми «Установить».")
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.install_button = QPushButton("Установить FiercyLauncher")
        self.install_button.setObjectName("Primary")
        self.install_button.clicked.connect(self.install)
        bottom.addWidget(self.install_button)
        layout.addLayout(bottom)
        self.worker = None

    def choose_path(self):
        path = QFileDialog.getExistingDirectory(self, "Выбери папку установки", self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def install(self):
        if not self.path_edit.text().strip():
            self.status.setText("Укажи папку установки.")
            return
        self.install_button.setEnabled(False)
        self.worker = InstallWorker(
            self.path_edit.text().strip(), self,
            create_desktop_shortcut=self.desktop_shortcut_check.isChecked(),
        )
        self.worker.progress.connect(self.on_progress)
        self.worker.completed.connect(self.on_complete)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def on_progress(self, value: int, text: str):
        self.progress.setValue(value)
        self.status.setText(text)

    def on_complete(self, path: str):
        message = f"Готово! FiercyLauncher установлен в:\n{path}"
        if self.worker and self.worker.shortcut_path:
            message += "\nЯрлык создан на рабочем столе."
        elif self.worker and self.worker.shortcut_error:
            message += "\nНо ярлык не удалось создать: " + self.worker.shortcut_error[:220]
        elif self.worker and not self.worker.create_desktop_shortcut:
            message += "\nСоздание ярлыка пропущено по твоему выбору."
        self.status.setText(message)
        self.install_button.setText("Установлено")
        self.install_button.setEnabled(False)

    def on_error(self, message: str):
        self.status.setText("Не удалось установить лаунчер:\n" + message)
        self.install_button.setEnabled(True)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FiercySetup")
    window = SetupWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
