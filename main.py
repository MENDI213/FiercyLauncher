from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from tls_bootstrap import configure_tls_bundle

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QPropertyAnimation, QEasingCurve, QTimer, QSettings
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication, QDialog, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QProgressBar, QPushButton, QFrame, QGraphicsOpacityEffect,
)

from logging_setup import configure_logging, user_data_dir
from appearance_manager import AppearanceManager
from ui_controls import CompactPopupProxyStyle
from version import LAUNCHER_NAME, LAUNCHER_VERSION
from github_service import check_latest_release
from update_download import DownloadCancelled, download_verified_asset

log = logging.getLogger("fiercy.startup")


class BootstrapWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal()
    restart_requested = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._cancel = threading.Event()
        self.release_result: dict | None = None
        self.release_error = ""
        self.restart_handed_off = False

    def cancel(self):
        self._cancel.set()

    @staticmethod
    def _start_update_helper(archive: Path) -> None:
        if getattr(sys, "frozen", False):
            target = Path(sys.executable).resolve().parent
            helper = target / "FiercyUpdater.exe"
            command = [str(helper)]
            mode, entry = "frozen", "FiercyLauncher.exe"
        else:
            target = Path(__file__).resolve().parent
            helper = target / "update_helper.py"
            command = [sys.executable, str(helper)]
            mode, entry = "source", "main.py"

        if not helper.is_file():
            raise FileNotFoundError(
                "Не найден отдельный помощник обновления: " + str(helper)
            )
        command += [
            "--archive", str(archive),
            "--target", str(target),
            "--parent-pid", str(os.getpid()),
            "--mode", mode,
            "--entry", entry,
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen(
            command,
            cwd=str(target),
            close_fds=(os.name != "nt"),
            creationflags=creation_flags,
        )

    def _check_and_apply_update(self, data_dir: Path) -> bool:
        """Return True when updater helper has taken over and this process must exit."""
        self.progress.emit(38, "Проверка новой версии FiercyLauncher…")
        try:
            release = check_latest_release(timeout=(4, 8))
            self.release_result = release
        except Exception as exc:
            self.release_error = str(exc)
            log.warning("Не удалось проверить обновления при запуске: %s", exc)
            return False

        if not release.get("update_available"):
            self.progress.emit(70, "Установлена последняя версия лаунчера.")
            return False

        settings = QSettings("Fiercy", "FiercyLauncher")
        auto_install = settings.value("updates/auto_install", True, type=bool)
        if not auto_install:
            self.progress.emit(70, f"Доступно обновление {release.get('latest_version')}.")
            return False

        if self._cancel.is_set():
            return False

        filename = Path(str(release.get("asset_name") or "FiercyLauncher-update.zip")).name
        update_dir = data_dir / "updates"
        update_dir.mkdir(parents=True, exist_ok=True)
        archive = update_dir / filename
        latest = str(release.get("latest_version", "новая версия"))
        self.progress.emit(42, f"Подготовка обновления {latest}…")

        def report(done: int, total: int, speed: float, status: str) -> None:
            if self._cancel.is_set():
                return
            ratio = min(1.0, done / total) if total else 0.0
            percent = 44 + int(ratio * 46)
            speed_mbps = speed / (1024 * 1024)
            detail = f"{status} · {speed_mbps:.1f} МБ/с"
            self.progress.emit(percent, detail)

        try:
            archive = download_verified_asset(
                str(release["download_url"]),
                str(release["sha256"]),
                archive,
                expected_size=int(release.get("size", 0) or 0),
                progress=report,
                cancel_event=self._cancel,
                max_workers=8,
            )
        except DownloadCancelled:
            log.info("Пользователь отменил загрузку обновления при запуске")
            return False
        except Exception as exc:
            self.release_error = f"Не удалось скачать обновление {latest}: {exc}"
            log.exception("Не удалось скачать обновление на этапе запуска")
            return False

        if self._cancel.is_set():
            return False
        self.progress.emit(96, "Обновление проверено. Перезапускаем FiercyLauncher…")
        try:
            self._start_update_helper(archive)
            self.restart_handed_off = True
            self.progress.emit(100, f"Устанавливаем обновление {latest}. Лаунчер перезапустится автоматически…")
            self.restart_requested.emit()
            return True
        except Exception as exc:
            self.release_error = f"Архив обновления скачан, но не удалось запустить установщик: {exc}"
            log.exception("Не удалось передать обновление помощнику")
            return False

    def run(self):
        try:
            self.progress.emit(8, "Подготовка пользовательской папки…")
            data = user_data_dir()
            (data / "logs").mkdir(parents=True, exist_ok=True)
            (data / "updates").mkdir(parents=True, exist_ok=True)
            (data / "profiles").mkdir(parents=True, exist_ok=True)
            if self._cancel.is_set():
                return

            self.progress.emit(24, "Проверка файлов лаунчера…")
            root = Path(__file__).resolve().parent
            if not getattr(sys, "frozen", False):
                required = [
                    root / "launcher_window.py", root / "minecraft_service.py",
                    root / "auth_manager.py", root / "appearance_manager.py",
                    root / "toast_notifications.py", root / "path_utils.py",
                    root / "discord_presence.py", root / "github_service.py",
                    root / "update_download.py", root / "update_helper.py",
                    root / "assets" / "themes" / "dark.qss",
                ]
            else:
                required = [root / "assets" / "themes" / "dark.qss"]
            missing = [str(path) for path in required if not path.is_file()]
            if missing:
                raise FileNotFoundError("В комплекте не найдены обязательные файлы:\n" + "\n".join(missing))
            if self._cancel.is_set():
                return

            if self._check_and_apply_update(data):
                return
            if self._cancel.is_set():
                return

            self.progress.emit(94, "Проверка локальных профилей и пользовательских данных…")
            self.progress.emit(100, "Готово. Открываем FiercyLauncher…")
            self.completed.emit()
        except Exception as exc:
            log.exception("Ошибка начальной проверки")
            self.failed.emit(str(exc))


class SplashScreen(QDialog):
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.setObjectName("SplashDialog")
        self.setFixedSize(520, 300)
        appearance = AppearanceManager(QSettings("Fiercy", "FiercyLauncher")).load()
        palette, geometry = appearance["palette"], appearance["geometry"]
        accent_bg = (f"qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {palette['accent']},stop:1 {palette['accent2']})"
                     if geometry.get("gradients", True) else palette["accent"])
        splash_bg = (f"qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {palette['window']},stop:1 {palette['panel']})"
                     if geometry.get("background_gradient", True) else palette["window"])
        self.setStyleSheet(f"""
            QDialog#SplashDialog {{ background: {splash_bg}; border: 1px solid {palette['border']}; border-radius: {geometry['window_radius']}px; }}
            QLabel {{ color: {palette['text']}; font-family: '{geometry['font_family']}'; }}
            QLabel#Mark {{ background: {accent_bg}; color: {palette['button_text']}; border-radius: {geometry['card_radius']}px; font-size: 36pt; font-weight: 900; }}
            QLabel#Brand {{ font-size: 19pt; font-weight: 900; letter-spacing: 1px; }}
            QLabel#Sub {{ color: {palette['muted']}; font-size: 8pt; }}
            QLabel#Status {{ color: {palette['text']}; font-size: 10pt; }}
            QLabel#Percent {{ color: {palette['accent2']}; font-weight: 800; }}
            QProgressBar {{ background: {palette['input']}; border: 1px solid {palette['border']}; border-radius: {geometry['control_radius']}px; height: 13px; text-align: center; color: transparent; }}
            QProgressBar::chunk {{ background: {accent_bg}; border-radius: {geometry['control_radius']}px; }}
            QPushButton {{ background: {palette['panel_alt']}; border: 1px solid {palette['border']}; border-radius: {geometry['button_radius']}px; color: {palette['text']}; padding: 7px 12px; }}
            QPushButton:hover {{ background: {palette['accent']}; color: {palette['button_text']}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 27, 30, 23)
        layout.setSpacing(14)
        head = QHBoxLayout()
        mark = QLabel("F")
        mark.setObjectName("Mark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(78, 78)
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        brand = QLabel(LAUNCHER_NAME.upper())
        brand.setObjectName("Brand")
        sub = QLabel(f"MINECRAFT JAVA EDITION  ·  VERSION {LAUNCHER_VERSION}")
        sub.setObjectName("Sub")
        text_col.addWidget(brand)
        text_col.addWidget(sub)
        head.addWidget(mark)
        head.addSpacing(12)
        head.addLayout(text_col)
        head.addStretch(1)
        layout.addLayout(head)
        layout.addStretch(1)
        self.status = QLabel("Подготовка…")
        self.status.setObjectName("Status")
        layout.addWidget(self.status)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.percent = QLabel("0%")
        self.percent.setObjectName("Percent")
        self.percent.setFixedWidth(42)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.percent)
        layout.addLayout(progress_row)
        bottom = QHBoxLayout()
        self.note_label = QLabel("Проверка выполняется в отдельном потоке — интерфейс не зависает.")
        self.note_label.setObjectName("Sub")
        self.note_label.setWordWrap(True)
        bottom.addWidget(self.note_label, 1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.clicked.connect(self.cancelled.emit)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)

    def set_progress(self, value: int, text: str):
        self.progress.setValue(value)
        self.percent.setText(f"{value}%")
        self.status.setText(text)


class Application:
    def __init__(self, app: QApplication):
        self.app = app
        self.splash = SplashScreen()
        self.worker = BootstrapWorker()
        self.window = None
        self.animation = None
        self.worker.progress.connect(self.splash.set_progress)
        self.worker.completed.connect(self.show_main)
        self.worker.restart_requested.connect(self.exit_for_update)
        self.worker.failed.connect(self.startup_failed)
        self.worker.finished.connect(self._worker_finished)
        self.splash.cancelled.connect(self.cancel_startup)
        self._cancel_requested = False

    def start(self):
        self.splash.show()
        self.worker.start()

    def cancel_startup(self):
        self._cancel_requested = True
        self.worker.cancel()
        self.splash.status.setText("Отмена запуска…")
        self.splash.cancel_button.setEnabled(False)

    def _worker_finished(self):
        if self._cancel_requested:
            self.splash.close()
            self.app.quit()

    def startup_failed(self, message: str):
        # Не показываем системное QMessageBox: ошибка остаётся в том же оформленном splash.
        log.exception("Начальная проверка FiercyLauncher завершилась ошибкой: %s", message)
        self.splash.status.setText("Не удалось подготовить лаунчер. Подробности:")
        self.splash.note_label.setText(str(message)[:600])
        self.splash.progress.setValue(0)
        self.splash.percent.setText("!")
        self.splash.cancel_button.setText("Закрыть")
        try:
            self.splash.cancel_button.clicked.disconnect()
        except TypeError:
            pass
        self.splash.cancel_button.clicked.connect(self.app.quit)
        self.splash.show()

    def exit_for_update(self):
        self.splash.cancel_button.hide()
        self.splash.note_label.setText("Обновление скачано и проверено. Сейчас установится новая версия и лаунчер откроется снова.")
        QTimer.singleShot(350, self.app.quit)

    def show_main(self):
        # Import the large GUI only after bootstrap and update checks have finished.
        from launcher_window import LauncherWindow

        self.window = LauncherWindow()
        self.window.setWindowOpacity(0.0)
        self.window.show()
        self.splash.close()
        self.animation = QPropertyAnimation(self.window, b"windowOpacity", self.window)
        self.animation.setDuration(260)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.start()
        if self.worker.release_error:
            try:
                self.window._release_failed(self.worker.release_error)
            except Exception:
                log.warning("Update check issue: %s", self.worker.release_error)
        elif self.worker.release_result:
            try:
                # When auto-update is disabled this presents the available release
                # without downloading it. When enabled, successful updates have
                # already been handed to the external updater before the GUI opens.
                self.window._release_checked(self.worker.release_result, True)
            except Exception:
                log.debug("Не удалось показать результат проверки обновления в главном окне", exc_info=True)


def main() -> int:
    configure_logging()
    try:
        ca_bundle = configure_tls_bundle()
        log.info("HTTPS CA bundle: %s", ca_bundle)
    except Exception as exc:
        # Fail closed: do not disable certificate verification when the package is broken.
        log.exception("Не удалось подготовить HTTPS-сертификаты")
        from PyQt5.QtWidgets import QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None, "Не удалось запустить FiercyLauncher",
            "В комплекте лаунчера отсутствуют HTTPS-сертификаты. "
            "Переустанови FiercyLauncher из полного официального архива.\n\n" + str(exc)[:1400],
        )
        return 2
    # Qt 5 high-DPI support must be enabled before QApplication is created.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName(LAUNCHER_NAME)
    app.setOrganizationName("Fiercy")
    app.setStyle(CompactPopupProxyStyle("Fusion"))
    app.setFont(QFont("Segoe UI", 10))
    icon = Path(__file__).resolve().parent / "assets" / "icon.ico"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    controller = Application(app)
    controller.start()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
