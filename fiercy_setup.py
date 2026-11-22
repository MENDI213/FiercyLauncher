"""Небольшой Windows bootstrap installer: скачивает portable-пакет последнего GitHub Release."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import requests
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QHBoxLayout, QProgressBar, QFileDialog,
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


class InstallWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, base_dir: str, parent=None):
        super().__init__(parent)
        self.base_dir = Path(base_dir).expanduser().resolve()

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
                temporary_archive = temp_root / package["name"]
                self.progress.emit(18, "Загрузка FiercyLauncher…")
                digest = hashlib.sha256()
                total = int(package.get("size", 0) or 0)
                downloaded = 0
                with requests.get(package["browser_download_url"], stream=True, timeout=(10, 90), headers=headers) as stream:
                    stream.raise_for_status()
                    with temporary_archive.open("wb") as output:
                        for chunk in stream.iter_content(512 * 1024):
                            if not chunk:
                                continue
                            output.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            percent = 18 + int(54 * downloaded / total) if total else 50
                            self.progress.emit(percent, f"Загрузка: {downloaded / 1048576:.1f} МБ / {max(total, 1) / 1048576:.1f} МБ")
                if digest.hexdigest() != expected:
                    raise RuntimeError("SHA-256 не совпал. Загрузка отклонена, файлы не установлены.")
                shutil.copy2(temporary_archive, archive_path.with_suffix(archive_path.suffix + ".tmp"))
                os.replace(archive_path.with_suffix(archive_path.suffix + ".tmp"), archive_path)
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
        self.setFixedSize(560, 300)
        self.setStyleSheet("""
            QDialog { background:#0b1020; color:#f4f6ff; border:1px solid #2c3a58; border-radius:18px; }
            QLabel { color:#f4f6ff; font-family:'Segoe UI'; }
            QLabel#Title { font-size:20pt; font-weight:900; }
            QLabel#Muted { color:#96a5c2; }
            QLineEdit { background:#111b30; border:1px solid #30415f; border-radius:9px; padding:10px; color:#fff; }
            QPushButton { background:#182641; border:1px solid #344966; border-radius:9px; color:#fff; padding:9px 14px; font-weight:700; }
            QPushButton#Primary { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #884dff,stop:1 #19bfcf); border:0; }
            QProgressBar { background:#10192a; border:1px solid #30415f; border-radius:7px; height:13px; text-align:center; color:transparent; }
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
        self.worker = InstallWorker(self.path_edit.text().strip(), self)
        self.worker.progress.connect(self.on_progress)
        self.worker.completed.connect(self.on_complete)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def on_progress(self, value: int, text: str):
        self.progress.setValue(value)
        self.status.setText(text)

    def on_complete(self, path: str):
        self.status.setText(f"Готово! FiercyLauncher установлен в:\n{path}")
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
