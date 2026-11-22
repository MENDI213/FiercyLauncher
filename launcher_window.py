from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from packaging.version import InvalidVersion, Version
import minecraft_launcher_lib as mll
from PyQt5.QtCore import Qt, QSettings, QTimer, QUrl, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QIcon, QDesktopServices, QColor, QImage, QPixmap, QPainter
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QFrame, QLabel, QPushButton, QToolButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QStackedWidget, QComboBox, QLineEdit, QSpinBox,
    QCheckBox, QProgressBar, QPlainTextEdit, QFileDialog,
    QSizeGrip, QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QScrollArea, QTabWidget,
    QColorDialog, QSlider, QFontComboBox, QListWidget, QListWidgetItem, QInputDialog,
)

from github_service import ReleaseCheckWorker, AssetDownloadWorker, StaticContentWorker, RemoteImageWorker
from logging_setup import configure_logging, log_file, user_data_dir
from minecraft_service import VersionListWorker, InstallMinecraftWorker, LaunchWorker
from profile_manager import ProfileManager, ProfileError
from appearance_manager import AppearanceManager, PRESETS, COLOR_LABELS
from toast_notifications import ToastNotification
from path_utils import migrate_game_data, resolve_launcher_root, selected_base_from_root
from version import LAUNCHER_NAME, LAUNCHER_VERSION, GITHUB_API_LATEST
from ui_controls import (WheelSafeComboBox, WheelSafeFontComboBox, WheelSafeSpinBox, WheelSafeSlider)
from profile_dialog import CreateProfileDialog
from mod_service import (list_mod_files, set_mod_enabled, add_local_mods,
                         ModrinthSearchWorker, ModrinthInstallWorker,
                         CurseForgeSearchWorker, CurseForgeInstallWorker)
from modpack_service import ModpackImportWorker, inspect_modpack
from instance_import import LocalVersionImportWorker
from storage_worker import StorageMigrationWorker
from version_metadata import read_version_metadata

try:
    import keyring
except ImportError:
    keyring = None

log = logging.getLogger("fiercy.ui")
ROOT = Path(__file__).resolve().parent


class TitleBar(QFrame):
    """Собственная верхняя панель окна без системной рамки."""

    def __init__(self, window: "LauncherWindow"):
        super().__init__(window)
        self.window = window
        self.setObjectName("TitleBar")
        self.setFixedHeight(66)
        self._drag_offset = None
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 0, 14, 0)
        row.setSpacing(12)
        mark = QLabel("F")
        mark.setObjectName("BrandMark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(38, 38)
        row.addWidget(mark)
        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        title = QLabel("FIERCY LAUNCHER")
        title.setObjectName("WindowTitle")
        subtitle = QLabel(f"MINECRAFT JAVA EDITION  ·  {LAUNCHER_VERSION}")
        subtitle.setObjectName("WindowSubtitle")
        name_col.addWidget(title)
        name_col.addWidget(subtitle)
        row.addLayout(name_col)
        row.addStretch(1)
        self.status = QLabel("●  ГОТОВ")
        self.status.setObjectName("StatusText")
        row.addWidget(self.status)
        for symbol, tooltip, callback, object_name in (
                ("−", "Свернуть", window.showMinimized, "WindowControl"),
                ("▢", "Развернуть", window.toggle_maximized, "WindowControl"),
                ("×", "Закрыть", window.close, "CloseControl"),
        ):
            button = QToolButton()
            button.setObjectName(object_name)
            button.setText(symbol)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            row.addWidget(button)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.window.isMaximized():
            self._drag_offset = event.globalPos() - self.window.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window.move(event.globalPos() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.window.toggle_maximized()
        super().mouseDoubleClickEvent(event)


class LauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Fiercy", "FiercyLauncher")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(1000, 680)
        self.resize(1220, 800)
        icon_path = ROOT / "assets" / "icon.ico"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        default_root = Path.home() / "FiercyLauncher"
        stored_root = self.settings.value("game/directory", str(default_root))
        self.game_root = Path(str(stored_root)).expanduser().resolve()
        self.game_root.mkdir(parents=True, exist_ok=True)
        self.profile_manager = ProfileManager(self.game_root)
        profiles = self.profile_manager.all()
        self.active_profile_id = str(self.settings.value("profiles/active", profiles[0]["id"]))
        if not self.profile_manager.get(self.active_profile_id):
            self.active_profile_id = profiles[0]["id"]

        self.versions: list[dict] = []
        self.remote_launcher_config: dict = {}
        self.remote_versions_config: dict = {}
        self.maintenance_mode = False
        self.minimum_version_block = False
        self.workers: list = []
        self.version_worker = None
        self.install_worker = None
        self.launch_worker = None
        self.release_worker = None
        self.download_worker = None
        self.content_worker = None
        self.banner_worker = None
        self.mod_search_worker = None
        self.mod_install_worker = None
        self.modpack_worker = None
        self.local_import_worker = None
        self.storage_worker = None
        self._install_profile_id = None
        self._local_import_profile_id = None
        self._modpack_profile_id = None
        self._mod_install_profile_id = None
        self._settings_dirty = False
        self._settings_committed = None
        self._settings_tracking_ready = False
        self._banner_pixmap = QPixmap()
        self._update_archive = ""
        self._allow_update_close = False
        self._page_animation = None
        self.appearance_manager = AppearanceManager(self.settings)
        self.appearance_data = self.appearance_manager.load()
        self.appearance_palette = dict(self.appearance_data["palette"])
        self.appearance_geometry = dict(self.appearance_data["geometry"])
        self._build_ui()
        self.toast = ToastNotification(self)
        self._connect_settings_tracking()
        self._settings_committed = self._settings_snapshot()
        self.apply_theme()
        self._refresh_profiles()
        self._refresh_installed_versions()

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self._refresh_logs)
        self.log_timer.start(7000)
        QTimer.singleShot(250, self._startup)

    # -------------------------- Basic layout --------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("WindowRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)
        self.window_root = root
        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        self.sidebar = sidebar
        sidebar.setFixedWidth(self.appearance_geometry.get("sidebar_width", 236))
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(15, 20, 15, 16)
        side.setSpacing(9)
        kicker = QLabel("РАБОЧЕЕ ПРОСТРАНСТВО")
        kicker.setObjectName("Eyebrow")
        side.addWidget(kicker)
        self.nav_buttons = []
        for title, icon in (("Игра", "▣"), ("Профили и моды", "◉"), ("FiercyCity", "✦"),
                            ("Настройки", "⚙"), ("Обновления и логи", "⌁")):
            button = QPushButton(f"{icon}    {title}")
            button.setObjectName("NavButton")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, idx=len(self.nav_buttons): self.navigate(idx))
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch(1)

        foot = QLabel(f"FIERCY  ·  {LAUNCHER_VERSION}\nТема и настройки персонализируются")
        foot.setObjectName("SmallNote")
        side.addWidget(foot)
        body.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 18, 24, 12)
        content_layout.setSpacing(12)
        body.addWidget(content, 1)
        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_game_page())  # 0 — игра
        self.stack.addWidget(self._build_profile_page())  # 1 — профили и моды
        self.stack.addWidget(self._build_fiercycity_page())  # 2 — FiercyCity
        self.stack.addWidget(self._build_settings_page())  # 3 — настройки
        self.stack.addWidget(self._build_updates_page())  # 4 — обновления и логи
        self.settings_page_index = 3
        self.settings_page_widget = self.stack.widget(self.settings_page_index)
        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(root))
        content_layout.addLayout(grip_row)
        self.setCentralWidget(root)
        self.navigate(0)

    def _card_widget(self, hero: bool = False) -> QFrame:
        frame = QFrame()
        frame.setObjectName("HeroCard" if hero else "Card")
        if self.appearance_geometry.get("shadows", True):
            shadow = QGraphicsDropShadowEffect(frame)
            shadow.setBlurRadius(25 if hero else 19)
            shadow.setOffset(0, 7 if hero else 4)
            shadow.setColor(QColor(0, 0, 0, 85 if hero else 58))
            frame.setGraphicsEffect(shadow)
        return frame

    def _scroll_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 4, 5, 10)
        layout.setSpacing(15)
        scroll.setWidget(page)
        return scroll, layout

    def _heading(self, eyebrow: str, title: str, description: str = "") -> QWidget:
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 4)
        box.setSpacing(5)
        a = QLabel(eyebrow.upper())
        a.setObjectName("Eyebrow")
        b = QLabel(title)
        b.setObjectName("PageHeading")
        box.addWidget(a)
        box.addWidget(b)
        if description:
            c = QLabel(description)
            c.setObjectName("Muted")
            c.setWordWrap(True)
            box.addWidget(c)
        return holder

    def _section_card(self, title: str, description: str = ""):
        card = self._card_widget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        label = QLabel(title)
        label.setStyleSheet("font-size: 12pt; font-weight: 800;")
        layout.addWidget(label)
        if description:
            copy = QLabel(description)
            copy.setObjectName("Muted")
            copy.setWordWrap(True)
            layout.addWidget(copy)
        return card, layout

    def _button(self, text: str, primary: bool = False, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("PrimaryButton" if primary else ("DangerButton" if danger else "SecondaryButton"))
        button.setCursor(Qt.PointingHandCursor)
        return button

    def _settings_snapshot(self) -> dict:
        appearance = {
            "mode": str(self.theme_combo.currentData() or "custom") if hasattr(self, "theme_combo") else "dark",
            "palette": dict(self.appearance_palette),
            "geometry": self._collect_appearance_geometry() if hasattr(self, "appearance_sliders") else dict(
                self.appearance_geometry),
        }
        return {
            "directory": self.game_dir_edit.text().strip() if hasattr(self, "game_dir_edit") else str(self.game_root),
            "java_path": self.java_edit.text().strip() if hasattr(self, "java_edit") else "",
            "ram": self.ram_spin.value() if hasattr(self, "ram_spin") else 4,
            "auto_java": self.auto_java_check.isChecked() if hasattr(self, "auto_java_check") else True,
            "jvm_arguments": self.jvm_edit.text().strip() if hasattr(self, "jvm_edit") else "",
            "auto_update": self.auto_update_check.isChecked() if hasattr(self, "auto_update_check") else True,
            "appearance": appearance,
        }

    def _connect_settings_tracking(self):
        page = getattr(self, "settings_page_widget", None)
        if page is None:
            return
        for widget in page.findChildren(QWidget):
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._settings_changed)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self._settings_changed)
            elif isinstance(widget, QSlider):
                widget.valueChanged.connect(self._settings_changed)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._settings_changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._settings_changed)
                if isinstance(widget, QFontComboBox):
                    widget.currentFontChanged.connect(self._settings_changed)
        self._settings_tracking_ready = True

    def _settings_changed(self, *_args):
        if not self._settings_tracking_ready or self._settings_committed is None:
            return
        try:
            self._settings_dirty = self._settings_snapshot() != self._settings_committed
        except Exception:
            self._settings_dirty = True
        if hasattr(self, "settings_dirty_label"):
            if self._settings_dirty:
                self.settings_dirty_label.setText("● Есть несохранённые изменения — сохранение обязательно")
                self.settings_dirty_label.setProperty("dirty", True)
            else:
                self.settings_dirty_label.setText("Все настройки сохранены")
                self.settings_dirty_label.setProperty("dirty", False)
            self.settings_dirty_label.style().unpolish(self.settings_dirty_label)
            self.settings_dirty_label.style().polish(self.settings_dirty_label)

    def _build_game_page(self):
        scroll, layout = self._scroll_page()
        hero = self._card_widget(hero=True)
        hero.setMinimumHeight(205)
        row = QHBoxLayout(hero)
        row.setContentsMargins(30, 24, 28, 24)
        row.setSpacing(22)
        left = QVBoxLayout()
        left.setSpacing(9)
        eyebrow = QLabel("FIERCY GAME PLATFORM  /  YOUR WORLD")
        eyebrow.setObjectName("Eyebrow")
        title = QLabel("Время играть.")
        title.setObjectName("HeroTitle")
        copy = QLabel("Твои сборки, твои моды и твои правила. Всё нужное — в одном месте.")
        copy.setObjectName("HeroCopy")
        copy.setWordWrap(True)
        left.addWidget(eyebrow)
        left.addWidget(title)
        left.addWidget(copy)
        left.addStretch(1)
        chip = QLabel("●  ГОТОВ К ЗАПУСКУ")
        chip.setObjectName("Pill")
        left.addWidget(chip, 0, Qt.AlignLeft)
        row.addLayout(left, 1)
        emblem = QLabel("F")
        emblem.setObjectName("HeroEmblem")
        emblem.setAlignment(Qt.AlignCenter)
        emblem.setFixedSize(132, 132)
        row.addWidget(emblem)
        layout.addWidget(hero)

        install_card, install_box = self._section_card(
            "Установка сборки",
            "Выбери версию Minecraft и загрузчик. Подходящая Java будет установлена автоматически вместе с выбранной сборкой."
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        grid.addWidget(QLabel("Игровой профиль"), 0, 0)
        self.game_profile_combo = WheelSafeComboBox()
        self.game_profile_combo.currentIndexChanged.connect(self._profile_combo_changed)
        grid.addWidget(self.game_profile_combo, 0, 1)
        grid.addWidget(QLabel("Версия Minecraft"), 1, 0)
        version_row = QHBoxLayout()
        self.version_combo = WheelSafeComboBox()
        self.version_combo.setMinimumHeight(46)
        version_row.addWidget(self.version_combo, 1)
        self.refresh_versions_button = self._button("↻")
        self.refresh_versions_button.setFixedWidth(50)
        self.refresh_versions_button.setToolTip("Обновить официальный список релизов Minecraft")
        self.refresh_versions_button.clicked.connect(self.load_versions)
        version_row.addWidget(self.refresh_versions_button)
        grid.addLayout(version_row, 1, 1)
        grid.addWidget(QLabel("Загрузчик"), 2, 0)
        self.loader_combo = WheelSafeComboBox()
        for label, value in (("Vanilla", "vanilla"), ("Fabric", "fabric"), ("Forge", "forge"),
                             ("NeoForge", "neoforge"), ("Quilt", "quilt")):
            self.loader_combo.addItem(label, value)
        grid.addWidget(self.loader_combo, 2, 1)
        grid.addWidget(QLabel("Версия загрузчика"), 3, 0)
        self.loader_version_edit = QLineEdit()
        self.loader_version_edit.setPlaceholderText("Автоматически выбрать совместимую версию")
        grid.addWidget(self.loader_version_edit, 3, 1)
        install_box.addLayout(grid)
        self.install_progress = QProgressBar()
        self.install_progress.setRange(0, 100)
        self.install_progress.hide()
        install_box.addWidget(self.install_progress)
        self.install_status = QLabel("Готов к установке. Java будет подготовлена автоматически.")
        self.install_status.setObjectName("StatusText")
        install_box.addWidget(self.install_status)
        install_row = QHBoxLayout()
        self.install_button = self._button("↓  УСТАНОВИТЬ / ПРОВЕРИТЬ", primary=True)
        self.install_button.setMinimumHeight(48)
        self.install_button.clicked.connect(self.install_selected)
        install_row.addWidget(self.install_button)
        install_row.addStretch(1)
        install_box.addLayout(install_row)
        layout.addWidget(install_card)

        play_card, play_box = self._section_card(
            "Запуск Minecraft",
            "Введи свой ник и выбери установленную сборку. Все параметры запуска, библиотеки и аргументы берутся из version JSON."
        )
        nick_row = QHBoxLayout()
        nick_row.addWidget(QLabel("Ник для игры"))
        self.nickname_edit = QLineEdit(str(self.settings.value("profile/nickname", "")))
        self.nickname_edit.setPlaceholderText("Ник Minecraft (3–16 символов)")
        self.nickname_edit.setMinimumHeight(40)
        nick_row.addWidget(self.nickname_edit, 1)
        play_box.addLayout(nick_row)

        play_row = QHBoxLayout()
        self.installed_combo = WheelSafeComboBox()
        self.installed_combo.setMinimumHeight(52)
        self.installed_combo.currentIndexChanged.connect(self._installed_version_changed)
        play_row.addWidget(self.installed_combo, 1)
        self.refresh_installed_button = self._button("↻")
        self.refresh_installed_button.setFixedWidth(50)
        self.refresh_installed_button.clicked.connect(self._refresh_installed_versions)
        play_row.addWidget(self.refresh_installed_button)
        self.play_button = self._button("▶    ИГРАТЬ", primary=True)
        self.play_button.setMinimumHeight(54)
        self.play_button.setMinimumWidth(205)
        self.play_button.clicked.connect(self.launch_selected)
        play_row.addWidget(self.play_button)
        play_box.addLayout(play_row)
        layout.addWidget(play_card)
        layout.addStretch(1)
        return scroll

    def _build_settings_page(self):
        scroll, layout = self._scroll_page()
        layout.addWidget(self._heading(
            "Launcher preferences", "Настройки",
            "Изменения можно предварительно смотреть сразу. Чтобы выйти из настроек, сначала сохрани их — иначе они останутся черновиком."
        ))

        card, box = self._section_card("Папка данных игры и лаунчера",
                                       "Выбери родительскую папку. Внутри автоматически будет создана FiercyLauncher — например D:\\dodo\\FiercyLauncher.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(11)
        grid.addWidget(QLabel("Родительская папка"), 0, 0)
        self.game_dir_edit = QLineEdit(str(selected_base_from_root(self.game_root)))
        self.game_dir_edit.setPlaceholderText(r"Например D:\Games")
        grid.addWidget(self.game_dir_edit, 0, 1)
        browse_game = self._button("Выбрать папку")
        browse_game.clicked.connect(self._browse_game_dir)
        grid.addWidget(browse_game, 0, 2)
        self.resolved_root_label = QLabel(f"Будет использоваться: {self.game_root}")
        self.resolved_root_label.setObjectName("Muted")
        self.resolved_root_label.setWordWrap(True)
        grid.addWidget(self.resolved_root_label, 1, 1, 1, 2)
        launcher_location = QLabel(
            "В portable EXE-версии файлы приложения будут скопированы в эту же FiercyLauncher и откроется новая копия. Существующие игровые профили, миры и библиотеки сначала переносятся в новый корень. При запуске из Python создаётся только папка данных.")
        launcher_location.setObjectName("Muted")
        launcher_location.setWordWrap(True)
        grid.addWidget(launcher_location, 2, 1, 1, 2)
        grid.addWidget(QLabel("Java (необязательно)"), 3, 0)
        self.java_edit = QLineEdit(str(self.settings.value("game/java_path", "")))
        self.java_edit.setPlaceholderText("Пусто — лаунчер сам установит нужную Java")
        grid.addWidget(self.java_edit, 3, 1)
        browse_java = self._button("Обзор")
        browse_java.clicked.connect(self._browse_java)
        grid.addWidget(browse_java, 3, 2)
        grid.addWidget(QLabel("Оперативная память (ГБ)"), 4, 0)
        self.ram_spin = WheelSafeSpinBox()
        self.ram_spin.setRange(2, 64)
        self.ram_spin.setValue(int(self.settings.value("game/ram", 4)))
        grid.addWidget(self.ram_spin, 4, 1)
        self.auto_java_check = QCheckBox("Автоматически устанавливать Java нужной версии при установке каждой сборки")
        self.auto_java_check.setChecked(self.settings.value("game/auto_java", True, type=bool))
        grid.addWidget(self.auto_java_check, 5, 1, 1, 2)
        box.addLayout(grid)
        layout.addWidget(card)

        jvm_card, jvm_box = self._section_card("Дополнительные JVM-аргументы",
                                               "Память регулируется выше. Сюда добавляй только дополнительные параметры JVM.")
        self.jvm_edit = QLineEdit(str(self.settings.value("game/jvm_arguments", "")))
        self.jvm_edit.setPlaceholderText("-XX:+UseG1GC -XX:+UnlockExperimentalVMOptions")
        jvm_box.addWidget(self.jvm_edit)
        layout.addWidget(jvm_card)

        updates_card, updates_box = self._section_card(
            "Обновления FiercyLauncher",
            "При включённой настройке лаунчер проверяет GitHub Releases при запуске, загружает обновление, проверяет SHA-256 и применяет его отдельным помощником после закрытия главного окна."
        )
        self.auto_update_check = QCheckBox("Автоматически скачивать и устанавливать обновления GitHub")
        self.auto_update_check.setChecked(self.settings.value("updates/auto_install", True, type=bool))
        updates_box.addWidget(self.auto_update_check)
        layout.addWidget(updates_card)

        appearance_card, appearance_box = self._section_card(
            "Полная персонализация интерфейса",
            "Цвета и размеры применяются как предварительный просмотр. В постоянные настройки они попадут только после нажатия «Сохранить все настройки»."
        )
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Готовый стиль"))
        self.theme_combo = WheelSafeComboBox()
        for label, value in (("Глубокая ночь", "dark"), ("Светлая", "light"), ("Neon", "neon"),
                             ("Emerald", "emerald"), ("Crimson", "crimson"), ("Ocean", "ocean"),
                             ("Мой стиль", "custom")):
            self.theme_combo.addItem(label, value)
        current_mode = str(self.settings.value("appearance/theme", "dark"))
        pos = self.theme_combo.findData(current_mode)
        self.theme_combo.setCurrentIndex(max(0, pos))
        self.theme_combo.currentIndexChanged.connect(self._appearance_preset_changed)
        self._refresh_appearance_color_buttons()
        preset_row.addWidget(self.theme_combo)
        preset_row.addStretch(1)
        appearance_box.addLayout(preset_row)

        self.appearance_color_buttons = {}
        color_grid = QGridLayout()
        color_grid.setHorizontalSpacing(14)
        color_grid.setVerticalSpacing(9)
        for index, (key, label) in enumerate(COLOR_LABELS.items()):
            row, col = divmod(index, 2)
            holder = QWidget()
            h = QHBoxLayout(holder)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(QLabel(label), 1)
            button = QPushButton(self.appearance_palette.get(key, "#000000"))
            button.setObjectName("ColorButton")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, token=key: self._choose_appearance_color(token))
            self.appearance_color_buttons[key] = button
            h.addWidget(button)
            color_grid.addWidget(holder, row, col)
        appearance_box.addLayout(color_grid)

        self.appearance_sliders = {}
        sliders_grid = QGridLayout()
        sliders_grid.setHorizontalSpacing(14)
        sliders_grid.setVerticalSpacing(8)
        slider_specs = (
            ("window_radius", "Скругление окна", 0, 36),
            ("card_radius", "Скругление карточек", 0, 32),
            ("button_radius", "Скругление кнопок", 0, 28),
            ("control_radius", "Скругление полей", 0, 24),
            ("density", "Плотность интерфейса (%)", 75, 130),
            ("sidebar_width", "Ширина боковой панели", 190, 330),
            ("titlebar_height", "Высота верхней панели", 48, 100),
            ("shadow_strength", "Сила теней (%)", 0, 100),
        )
        for index, (key, label, minimum, maximum) in enumerate(slider_specs):
            row, col = divmod(index, 2)
            holder = QWidget()
            v = QVBoxLayout(holder)
            v.setContentsMargins(0, 0, 0, 0)
            line = QHBoxLayout()
            line.addWidget(QLabel(label), 1)
            value_label = QLabel(str(self.appearance_geometry.get(key, minimum)))
            value_label.setObjectName("Muted")
            line.addWidget(value_label)
            slider = WheelSafeSlider(Qt.Horizontal)
            slider.setRange(minimum, maximum)
            slider.setValue(int(self.appearance_geometry.get(key, minimum)))
            slider.valueChanged.connect(
                lambda value, k=key, output=value_label: self._appearance_slider_changed(k, value, output))
            slider.sliderReleased.connect(self._appearance_adjusted)
            v.addLayout(line)
            v.addWidget(slider)
            self.appearance_sliders[key] = slider
            sliders_grid.addWidget(holder, row, col)
        appearance_box.addLayout(sliders_grid)

        options_row = QHBoxLayout()
        self.gradient_check = QCheckBox("Градиентные кнопки")
        self.gradient_check.setChecked(bool(self.appearance_geometry.get("gradients", True)))
        self.gradient_check.toggled.connect(self._appearance_adjusted)
        options_row.addWidget(self.gradient_check)
        self.background_gradient_check = QCheckBox("Градиент фона")
        self.background_gradient_check.setChecked(bool(self.appearance_geometry.get("background_gradient", True)))
        self.background_gradient_check.toggled.connect(self._appearance_adjusted)
        options_row.addWidget(self.background_gradient_check)
        self.shadows_check = QCheckBox("Тени карточек")
        self.shadows_check.setChecked(bool(self.appearance_geometry.get("shadows", True)))
        self.shadows_check.toggled.connect(self._appearance_adjusted)
        options_row.addWidget(self.shadows_check)
        self.low_effects_check = QCheckBox("Режим слабого ПК (меньше анимаций и эффектов)")
        self.low_effects_check.setChecked(bool(self.appearance_geometry.get("low_effects", True)))
        self.low_effects_check.toggled.connect(self._appearance_adjusted)
        options_row.addWidget(self.low_effects_check)
        options_row.addStretch(1)
        appearance_box.addLayout(options_row)
        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Шрифт интерфейса"))
        self.font_combo = WheelSafeFontComboBox()
        self.font_combo.setCurrentFont(__import__('PyQt5.QtGui', fromlist=['QFont']).QFont(
            str(self.appearance_geometry.get("font_family", "Segoe UI"))))
        self.font_combo.currentFontChanged.connect(lambda _font: self._appearance_adjusted())
        font_row.addWidget(self.font_combo)
        appearance_box.addLayout(font_row)
        action_row = QHBoxLayout()
        reset_appearance = self._button("Сбросить стиль")
        reset_appearance.clicked.connect(self._reset_appearance)
        action_row.addWidget(reset_appearance)
        action_row.addStretch(1)
        appearance_box.addLayout(action_row)
        layout.addWidget(appearance_card)

        save_row = QHBoxLayout()
        self.settings_dirty_label = QLabel("Все изменения будут применены после сохранения")
        self.settings_dirty_label.setObjectName("Muted")
        save_row.addWidget(self.settings_dirty_label)
        save_row.addStretch(1)
        self.save_settings_button = self._button("Сохранить все настройки", primary=True)
        self.save_settings_button.clicked.connect(self.save_settings)
        save_row.addWidget(self.save_settings_button)
        layout.addLayout(save_row)
        layout.addStretch(1)
        return scroll

    def _refresh_appearance_color_buttons(self):
        for key, button in getattr(self, "appearance_color_buttons", {}).items():
            color = self.appearance_palette.get(key, "#000000")
            button.setText(color.upper())
            button.setStyleSheet(
                f"QPushButton#ColorButton {{ background: {color}; color: {'#111111' if QColor(color).lightness() > 150 else '#FFFFFF'}; }}"
            )

    def _choose_appearance_color(self, key: str):
        initial = QColor(self.appearance_palette.get(key, "#FFFFFF"))
        selected = QColorDialog.getColor(initial, self, f"Цвет: {COLOR_LABELS.get(key, key)}")
        if selected.isValid():
            self.appearance_palette[key] = selected.name()
            self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData("custom")))
            self._refresh_appearance_color_buttons()
            self._appearance_adjusted()

    def _appearance_preset_changed(self, index: int):
        mode = str(self.theme_combo.itemData(index) or "dark")
        if mode in PRESETS:
            self.appearance_palette = dict(PRESETS[mode])
            self._refresh_appearance_color_buttons()
            self._appearance_adjusted()
        else:
            self._appearance_adjusted()

    def _appearance_slider_changed(self, key: str, value: int, output: QLabel):
        self.appearance_geometry[key] = int(value)
        output.setText(str(value))
        self._settings_changed()

    def _collect_appearance_geometry(self) -> dict:
        geometry = dict(self.appearance_geometry)
        geometry["gradients"] = self.gradient_check.isChecked()
        geometry["background_gradient"] = self.background_gradient_check.isChecked()
        geometry["shadows"] = self.shadows_check.isChecked()
        geometry["low_effects"] = self.low_effects_check.isChecked()
        geometry["font_family"] = self.font_combo.currentFont().family()
        for key, slider in self.appearance_sliders.items():
            geometry[key] = slider.value()
        return geometry

    def _appearance_adjusted(self, *_args):
        self.appearance_geometry = self._collect_appearance_geometry()
        self.apply_theme()
        self._settings_changed()

    def _reset_appearance(self):
        self.appearance_palette = dict(PRESETS["dark"])
        self.appearance_geometry = dict(self.appearance_manager.load()["geometry"])
        for key, slider in self.appearance_sliders.items():
            slider.setValue(int(self.appearance_geometry[key]))
        self.gradient_check.setChecked(True)
        self.background_gradient_check.setChecked(True)
        self.shadows_check.setChecked(False)
        self.low_effects_check.setChecked(True)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData("dark"))
        self._refresh_appearance_color_buttons()
        self._appearance_adjusted()
        self._notify("Оформление сброшено", "Возвращена стандартная тёмная тема Fiercy.", "success")

    def _build_profile_page(self):
        scroll, layout = self._scroll_page()
        layout.addWidget(self._heading(
            "Instances, mods and imported builds", "Профили и моды",
            "Создавай отдельные игровые окружения. Версии Minecraft и библиотеки хранятся общими файлами, а моды, конфиги и миры — отдельно для каждого профиля."
        ))

        profiles_card, profiles_box = self._section_card(
            "Игровые профили",
            "При создании выбираются имя, версия Minecraft и мод-загрузчик. Установка запускается отдельно в фоне; общие библиотеки не скачиваются повторно для каждого нового профиля."
        )
        row = QHBoxLayout()
        self.profiles_combo = WheelSafeComboBox()
        self.profiles_combo.currentIndexChanged.connect(self._profile_page_combo_changed)
        row.addWidget(self.profiles_combo, 1)
        create_button = self._button("＋ Создать профиль", primary=True)
        create_button.clicked.connect(self.create_profile)
        row.addWidget(create_button)
        rename_button = self._button("Переименовать")
        rename_button.clicked.connect(self.rename_profile)
        row.addWidget(rename_button)
        delete_button = self._button("Удалить", danger=True)
        delete_button.clicked.connect(self.delete_profile)
        row.addWidget(delete_button)
        profiles_box.addLayout(row)
        self.profile_summary_label = QLabel("Версия: —  ·  Загрузчик: —")
        self.profile_summary_label.setObjectName("StatusText")
        profiles_box.addWidget(self.profile_summary_label)
        self.profile_folder_label = QLabel("")
        self.profile_folder_label.setObjectName("Muted")
        self.profile_folder_label.setWordWrap(True)
        profiles_box.addWidget(self.profile_folder_label)
        folder_row = QHBoxLayout()
        open_folder = self._button("Открыть папку профиля")
        open_folder.clicked.connect(self.open_profile_folder)
        folder_row.addWidget(open_folder)
        import_version = self._button("Импортировать локальную версию")
        import_version.clicked.connect(self.import_local_version)
        folder_row.addWidget(import_version)
        import_pack = self._button("Импортировать сборку (.mrpack / .zip)")
        import_pack.clicked.connect(self.import_modpack)
        folder_row.addWidget(import_pack)
        open_versions = self._button("Открыть общую папку versions")
        open_versions.clicked.connect(lambda: self._open_folder(self._profile_game_root() / "versions"))
        folder_row.addWidget(open_versions)
        folder_row.addStretch(1)
        profiles_box.addLayout(folder_row)
        layout.addWidget(profiles_card)

        mods_card, mods_box = self._section_card(
            "Моды активного профиля",
            "Каждый профиль использует собственную папку mods. Дважды нажми по моду, чтобы включить или отключить его. Файлы .jar.disabled не загружаются Minecraft."
        )
        mods_row = QHBoxLayout()
        self.profile_mod_list = QListWidget()
        self.profile_mod_list.setMinimumHeight(170)
        self.profile_mod_list.itemDoubleClicked.connect(self._toggle_mod_item)
        mods_row.addWidget(self.profile_mod_list, 1)
        mod_buttons = QVBoxLayout()
        refresh_mods = self._button("Обновить список")
        refresh_mods.clicked.connect(self._refresh_mod_list)
        mod_buttons.addWidget(refresh_mods)
        add_mods = self._button("Добавить .jar")
        add_mods.clicked.connect(self.add_local_mod_files)
        mod_buttons.addWidget(add_mods)
        toggle_mod = self._button("Включить / отключить")
        toggle_mod.clicked.connect(self.toggle_selected_mod)
        mod_buttons.addWidget(toggle_mod)
        remove_mod = self._button("Удалить мод", danger=True)
        remove_mod.clicked.connect(self.remove_selected_mod)
        mod_buttons.addWidget(remove_mod)
        open_mods = self._button("Открыть папку mods")
        open_mods.clicked.connect(lambda: self._open_folder(self._instance_dir() / "mods"))
        mod_buttons.addWidget(open_mods)
        mod_buttons.addStretch(1)
        mods_row.addLayout(mod_buttons)
        mods_box.addLayout(mods_row)
        self.mod_count_label = QLabel("Моды не найдены")
        self.mod_count_label.setObjectName("Muted")
        mods_box.addWidget(self.mod_count_label)
        layout.addWidget(mods_card)

        catalog_card, catalog_box = self._section_card(
            "Каталог модов: Modrinth и CurseForge",
            "Modrinth работает без ключа. Для официального CurseForge API потребуется личный API key; лаунчер не встраивает общий секрет в клиент."
        )
        search_row = QHBoxLayout()
        self.mod_provider_combo = WheelSafeComboBox()
        self.mod_provider_combo.addItem("Modrinth", "modrinth")
        self.mod_provider_combo.addItem("CurseForge", "curseforge")
        search_row.addWidget(self.mod_provider_combo)
        self.mod_search_edit = QLineEdit()
        self.mod_search_edit.setPlaceholderText("Название мода, например Sodium или JEI")
        search_row.addWidget(self.mod_search_edit, 1)
        search_button = self._button("Найти моды", primary=True)
        search_button.clicked.connect(self.search_mods)
        search_row.addWidget(search_button)
        catalog_box.addLayout(search_row)
        self.mod_results_list = QListWidget()
        self.mod_results_list.setMinimumHeight(150)
        catalog_box.addWidget(self.mod_results_list)
        catalog_actions = QHBoxLayout()
        download_mod = self._button("Установить выбранный мод", primary=True)
        download_mod.clicked.connect(self.install_selected_mod_from_catalog)
        catalog_actions.addWidget(download_mod)
        curse_key = self._button("Настроить API key CurseForge")
        curse_key.clicked.connect(self.configure_curseforge_key)
        catalog_actions.addWidget(curse_key)
        catalog_actions.addStretch(1)
        catalog_box.addLayout(catalog_actions)
        self.mod_status_label = QLabel("Выбери сборку, найди мод и добавь его в активный профиль.")
        self.mod_status_label.setObjectName("Muted")
        self.mod_status_label.setWordWrap(True)
        catalog_box.addWidget(self.mod_status_label)
        layout.addWidget(catalog_card)
        layout.addStretch(1)
        return scroll

    def _build_fiercycity_page(self):
        scroll, layout = self._scroll_page()
        layout.addWidget(self._heading(
            "FiercyCity platform", "FiercyCity",
            "Отдельное пространство для официальных сборок FiercyCity. Каталог можно подключить позднее, когда появится серверная часть."
        ))
        hero, box = self._section_card(
            "FiercyCity — каталог сборок",
            "Раздел подготовлен в интерфейсе. Сейчас сборки можно импортировать из .mrpack, CurseForge export ZIP или локальной папки другого лаунчера."
        )
        status = QLabel(
            "F  ·  FIERCYCITY\n\nСКОРО\n\nПосле появления VPS/API здесь появятся опубликованные сборки, их версии, описание, список модов, обновления и управление установками.")
        status.setAlignment(Qt.AlignCenter)
        status.setMinimumHeight(250)
        status.setObjectName("HeroTitle")
        status.setWordWrap(True)
        box.addWidget(status)
        row = QHBoxLayout()
        import_pack = self._button("Импортировать свою сборку", primary=True)
        import_pack.clicked.connect(self.import_modpack)
        row.addWidget(import_pack)
        repo = self._button("Открыть GitHub проекта")
        repo.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/MENDI213/FiercyLauncher")))
        row.addWidget(repo)
        row.addStretch(1)
        box.addLayout(row)
        layout.addWidget(hero)
        layout.addStretch(1)
        return scroll

    def _build_updates_page(self):
        scroll, layout = self._scroll_page()
        layout.addWidget(self._heading("GitHub services", "Обновления и логи",
                                       "Обновления лаунчера, новости и конфигурация публикуются как публичные GitHub Releases и JSON."))
        update_card, update_box = self._section_card("Обновление лаунчера")
        self.update_status = QLabel("Проверка ещё не выполнялась.")
        self.update_status.setObjectName("StatusText")
        update_box.addWidget(self.update_status)
        self.update_progress = QProgressBar()
        self.update_progress.setRange(0, 100)
        self.update_progress.hide()
        update_box.addWidget(self.update_progress)
        update_buttons = QHBoxLayout()
        self.check_updates_button = self._button("Проверить GitHub Releases", primary=True)
        self.check_updates_button.clicked.connect(lambda: self.check_updates(silent=False))
        update_buttons.addWidget(self.check_updates_button)
        self.cancel_download_button = self._button("Отменить загрузку", danger=True)
        self.cancel_download_button.clicked.connect(self.cancel_update_download)
        self.cancel_download_button.hide()
        update_buttons.addWidget(self.cancel_download_button)
        self.release_page_button = self._button("Открыть Releases")
        self.release_page_button.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(f"https://github.com/{__import__('version').GITHUB_REPOSITORY}/releases")))
        update_buttons.addWidget(self.release_page_button)
        update_box.addLayout(update_buttons)
        layout.addWidget(update_card)

        news_card, news_box = self._section_card("Новости и объявления")
        self.news_label = QLabel("Новости загружаются из content/news.json репозитория GitHub.")
        self.news_label.setObjectName("Muted")
        self.news_label.setWordWrap(True)
        news_box.addWidget(self.news_label)
        self.promo_banner = QLabel()
        self.promo_banner.setObjectName("PromoBanner")
        self.promo_banner.setAlignment(Qt.AlignCenter)
        self.promo_banner.setMinimumHeight(110)
        self.promo_banner.setMaximumHeight(170)
        self.promo_banner.hide()
        news_box.addWidget(self.promo_banner)
        self.refresh_content_button = self._button("Обновить новости")
        self.refresh_content_button.clicked.connect(self.load_static_content)
        news_box.addWidget(self.refresh_content_button, 0, Qt.AlignLeft)
        layout.addWidget(news_card)

        logs_card, logs_box = self._section_card("Последние строки лога")
        self.log_tabs = QTabWidget()
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.game_log_view = QPlainTextEdit()
        self.game_log_view.setReadOnly(True)
        for viewer in (self.log_view, self.game_log_view):
            viewer.setMinimumHeight(210)
        self.log_tabs.addTab(self.log_view, "Лаунчер")
        self.log_tabs.addTab(self.game_log_view, "Minecraft")
        logs_box.addWidget(self.log_tabs)
        log_buttons = QHBoxLayout()
        open_logs = self._button("Логи лаунчера")
        open_logs.clicked.connect(self.open_log_folder)
        log_buttons.addWidget(open_logs)
        open_game_logs = self._button("Логи Minecraft")
        open_game_logs.clicked.connect(lambda: self._open_folder(self._instance_dir() / "logs"))
        log_buttons.addWidget(open_game_logs)
        clear_view = self._button("Очистить просмотр")
        clear_view.clicked.connect(lambda: (self.log_view.clear(), self.game_log_view.clear()))
        log_buttons.addWidget(clear_view)
        log_buttons.addStretch(1)
        logs_box.addLayout(log_buttons)
        layout.addWidget(logs_card)
        layout.addStretch(1)
        return scroll

    def _notify(self, title: str, message: str, kind: str = "info", duration: int = 4500):
        log_method = logging.error if kind == "error" else (logging.warning if kind == "warning" else logging.info)
        log_method("%s: %s", title, message.replace("\n", " | "))
        if hasattr(self, "toast"):
            self.toast.show_message(title, message, kind, duration)

    # -------------------------- Navigation / theme --------------------------
    def navigate(self, index: int):
        if index < 0 or index >= self.stack.count():
            return
        current = self.stack.currentIndex()
        if current == getattr(self, "settings_page_index", 3) and index != current and self._settings_dirty:
            self._notify(
                "Настройки ещё не сохранены",
                "Сначала нажми «Сохранить все настройки». Пока изменения не сохранены, выйти из этого раздела нельзя.",
                "warning", 6500,
            )
            return
        page = self.stack.widget(index)
        self.stack.setCurrentIndex(index)
        low_effects = bool(self.appearance_geometry.get("low_effects", True))
        if not low_effects:
            effect = QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(effect)
            self._page_animation = QPropertyAnimation(effect, b"opacity", self)
            self._page_animation.setDuration(150)
            self._page_animation.setStartValue(0.82)
            self._page_animation.setEndValue(1.0)
            self._page_animation.setEasingCurve(QEasingCurve.OutCubic)
            self._page_animation.finished.connect(lambda p=page: p.setGraphicsEffect(None))
            self._page_animation.start()
        for i, button in enumerate(self.nav_buttons):
            button.setProperty("active", i == index)
            button.style().unpolish(button)
            button.style().polish(button)
        if index == 1:
            self._refresh_mod_list()
        elif index == 4:
            self._refresh_logs()

    def apply_theme(self):
        try:
            data = {
                "mode": str(self.theme_combo.currentData() or "custom") if hasattr(self, "theme_combo") else "dark",
                "palette": dict(self.appearance_palette),
                "geometry": dict(self.appearance_geometry),
            }
            self.setStyleSheet(self.appearance_manager.stylesheet(data))
            if hasattr(self, "sidebar"):
                self.sidebar.setFixedWidth(int(self.appearance_geometry.get("sidebar_width", 236)))
            if hasattr(self, "title_bar"):
                self.title_bar.setFixedHeight(int(self.appearance_geometry.get("titlebar_height", 66)))
            self._update_card_shadows()
            self._refresh_appearance_color_buttons()
            if hasattr(self, "toast") and self.toast.isVisible():
                self.toast.reposition()
        except Exception:
            log.exception("Не удалось применить пользовательскую тему")

    def _update_card_shadows(self):
        enabled = bool(self.appearance_geometry.get("shadows", False))
        strength = int(self.appearance_geometry.get("shadow_strength", 20))
        enabled = enabled and strength > 0 and not bool(self.appearance_geometry.get("low_effects", True))
        root = getattr(self, "window_root", None)
        if root is not None:
            effect = root.graphicsEffect()
            if enabled:
                if not isinstance(effect, QGraphicsDropShadowEffect):
                    effect = QGraphicsDropShadowEffect(root)
                    root.setGraphicsEffect(effect)
                effect.setBlurRadius(8 + strength * 0.24)
                effect.setOffset(0, 2 + strength * 0.055)
                effect.setColor(QColor(0, 0, 0, int(strength * 1.5)))
            elif isinstance(effect, QGraphicsDropShadowEffect):
                root.setGraphicsEffect(None)
        for frame in self.findChildren(QFrame):
            if frame.objectName() not in ("Card", "HeroCard"):
                continue
            effect = frame.graphicsEffect()
            if enabled:
                if not isinstance(effect, QGraphicsDropShadowEffect):
                    effect = QGraphicsDropShadowEffect(frame)
                    frame.setGraphicsEffect(effect)
                is_hero = frame.objectName() == "HeroCard"
                effect.setBlurRadius((10 if is_hero else 6) + strength * (0.25 if is_hero else 0.19))
                effect.setOffset(0, (2 if is_hero else 1) + strength * (0.055 if is_hero else 0.04))
                effect.setColor(QColor(0, 0, 0, int(strength * (1.2 if is_hero else 0.75))))
            elif isinstance(effect, QGraphicsDropShadowEffect):
                frame.setGraphicsEffect(None)

    def toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _set_status(self, text: str):
        self.title_bar.status.setText("●  " + text.upper())

    # -------------------------- Startup --------------------------
    def _startup(self):
        self.load_versions()
        QTimer.singleShot(450, self.load_static_content)
        QTimer.singleShot(1100, lambda: self.check_updates(silent=True))

    def _retain_worker(self, worker):
        if worker not in self.workers:
            self.workers.append(worker)
        worker.finished.connect(lambda current=worker: self.workers.remove(current)
        if current in self.workers else None)

    # -------------------------- Minecraft versions/install/launch --------------------------
    def load_versions(self):
        if self.version_worker and self.version_worker.isRunning():
            return
        self.refresh_versions_button.setEnabled(False)
        self.install_status.setText("Загружаем список официальных релизов…")
        self._set_status("загрузка версий")
        self.version_worker = VersionListWorker()
        self.version_worker.loaded.connect(self._versions_loaded)
        self.version_worker.failed.connect(self._versions_failed)
        self.version_worker.start()

    def _versions_loaded(self, versions: list):
        self.refresh_versions_button.setEnabled(True)
        self.versions = versions
        self._populate_version_combo()
        self.install_status.setText(f"Доступно официальных релизов: {self.version_combo.count()}. Snapshots скрыты.")
        self._set_status("список версий загружен")

    def _populate_version_combo(self):
        previous = self.version_combo.currentData()
        allowed = self.remote_versions_config.get("supported_minecraft_versions", [])
        if not isinstance(allowed, list):
            allowed = []
        allowed_set = {str(item) for item in allowed if str(item).strip()}
        items = [item for item in self.versions if not allowed_set or str(item.get("id", "")) in allowed_set]
        self.version_combo.clear()
        for item in items:
            version_id = str(item.get("id", ""))
            if version_id:
                self.version_combo.addItem(version_id, version_id)
        preferred = previous or str(self.remote_versions_config.get("recommended_minecraft_version", ""))
        if not preferred:
            preferred = str(self.remote_launcher_config.get("recommended_minecraft_version", ""))
        index = self.version_combo.findData(preferred)
        if index >= 0:
            self.version_combo.setCurrentIndex(index)
        elif self.version_combo.count():
            self.version_combo.setCurrentIndex(0)

    def _versions_failed(self, message: str):
        self.refresh_versions_button.setEnabled(True)
        self.install_status.setText(message)
        self._set_status("ошибка сети")
        self._notify("Не удалось загрузить версии", message, "error", 6500)

    def _active_profile(self) -> dict:
        return self.profile_manager.get(self.active_profile_id) or self.profile_manager.all()[0]

    def _instance_dir(self) -> Path:
        return self.profile_manager.instance_dir(self.active_profile_id)

    def _profile_game_root(self) -> Path:
        profile = self._active_profile()
        if str(profile.get("storage_root", "legacy")) == "shared":
            return self.game_root
        legacy = self._instance_dir()
        versions = legacy / "versions"
        if versions.is_dir() and any(
                (folder / f"{folder.name}.json").is_file() for folder in versions.iterdir() if folder.is_dir()):
            return legacy
        try:
            self.profile_manager.update(self.active_profile_id, storage_root="shared")
        except Exception:
            log.exception("Не удалось обновить storage_root профиля")
        return self.game_root

    def _refresh_installed_versions(self):
        if not hasattr(self, "installed_combo"):
            return
        previous = self.installed_combo.currentData()
        self.installed_combo.blockSignals(True)
        self.installed_combo.clear()
        try:
            items = mll.utils.get_installed_versions(str(self._profile_game_root()))
        except Exception:
            items = []
            versions_path = self._profile_game_root() / "versions"
            if versions_path.is_dir():
                for folder in versions_path.iterdir():
                    if (folder / f"{folder.name}.json").is_file():
                        items.append({"id": folder.name})
        for item in items:
            version_id = item.get("id") if isinstance(item, dict) else str(item)
            if version_id:
                self.installed_combo.addItem(str(version_id), str(version_id))
        profile = self._active_profile()
        preferred = profile.get("installed_version_id", "") or previous
        pos = self.installed_combo.findData(preferred)
        if pos >= 0:
            self.installed_combo.setCurrentIndex(pos)
        if not self.installed_combo.count():
            self.installed_combo.addItem("Нет установленных версий", None)
        self.installed_combo.blockSignals(False)

    def _installed_version_changed(self, index: int):
        if index < 0 or not hasattr(self, "installed_combo"):
            return
        version_id = self.installed_combo.itemData(index)
        if not version_id:
            return
        profile = self._active_profile()
        if str(profile.get("installed_version_id", "")) != str(version_id):
            try:
                metadata = read_version_metadata(self._profile_game_root(), str(version_id))
                self.profile_manager.update(self.active_profile_id, **metadata)
            except Exception:
                log.exception("Не удалось прочитать метаданные локальной версии %s", version_id)
                self.profile_manager.update(self.active_profile_id, installed_version_id=str(version_id))
            self._update_profile_folder_label()

    def install_selected(self):
        if self.maintenance_mode or self.minimum_version_block:
            self._notify("Установка временно недоступна", self._policy_message(), "warning", 6500)
            return
        if self.install_worker and self.install_worker.isRunning():
            return
        version = self.version_combo.currentData()
        if not version:
            self._notify("Список версий загружается",
                         "Подожди завершения загрузки списка Minecraft и повтори установку.", "info")
            return
        profile = self._active_profile()
        loader = self.loader_combo.currentData() or "vanilla"
        java_path = self.java_edit.text().strip() if hasattr(self, "java_edit") else str(
            self.settings.value("game/java_path", ""))
        auto_java = self.auto_java_check.isChecked() if hasattr(self, "auto_java_check") else self.settings.value(
            "game/auto_java", True, type=bool)
        self.install_button.setEnabled(False)
        self.install_progress.setValue(0)
        self.install_progress.show()
        self.install_status.setText("Подготовка к установке…")
        self._set_status("установка Minecraft")
        self._install_profile_id = self.active_profile_id
        self.install_worker = InstallMinecraftWorker(
            profile, str(self._profile_game_root()), str(self._instance_dir()), str(version), loader,
            self.loader_version_edit.text().strip(), java_path, auto_java,
        )
        self.install_worker.progress.connect(self._install_progress)
        self.install_worker.completed.connect(self._install_completed)
        self.install_worker.failed.connect(self._install_failed)
        self.install_worker.start()

    def _install_progress(self, percent: int, status: str):
        if percent >= 0:
            self.install_progress.setValue(percent)
        self.install_status.setText(status)
        self._set_status(status)

    def _install_completed(self, result: dict):
        self.install_button.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        self.install_progress.setValue(100)
        self.install_status.setText(f"Готово: {result['installed_version_id']}")
        profile_id = self._install_profile_id or self.active_profile_id
        profile = self.profile_manager.get(profile_id)
        if profile:
            self.profile_manager.update(
                profile_id,
                minecraft_version=result["minecraft_version"], loader=result["loader"],
                loader_version=result["loader_version"], installed_version_id=result["installed_version_id"],
                storage_root="shared" if profile.get("storage_root") == "shared" else "legacy",
            )
        self._refresh_profiles()
        self._refresh_installed_versions()
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        log.info("Версия установлена для профиля %s: %s", profile_id, result)
        self._set_status("установка завершена")

    def _install_failed(self, message: str):
        self.install_button.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        self.install_status.setText("Установка не завершена. Подробности в логах.")
        self._set_status("ошибка установки")
        self._notify("Не удалось установить Minecraft", message, "error", 8000)

    def launch_selected(self):
        if self.maintenance_mode or self.minimum_version_block:
            self._notify("Запуск временно недоступен", self._policy_message(), "warning", 6500)
            return
        nickname = self.nickname_edit.text().strip() if hasattr(self, "nickname_edit") else ""
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nickname):
            self._notify("Проверь никнейм",
                         "Ник должен содержать 3–16 символов: только латинские буквы, цифры и подчёркивание.",
                         "warning")
            self.nickname_edit.setFocus()
            return

        # Оффлайн режим
        md5 = hashlib.md5(f"OfflinePlayer:{nickname}".encode('utf-8')).digest()
        offline_uuid = str(uuid.UUID(bytes=md5, version=3)).replace("-", "")
        account = {
            "name": nickname,
            "id": offline_uuid,
            "access_token": "0",
            "user_type": "legacy"
        }

        version_id = self.installed_combo.currentData()
        if not version_id:
            self._notify("Нет установленной версии", "Сначала установи Minecraft в активный профиль.", "warning")
            return
        ram = self.ram_spin.value() if hasattr(self, "ram_spin") else int(self.settings.value("game/ram", 4))
        java_path = self.java_edit.text().strip() if hasattr(self, "java_edit") else str(
            self.settings.value("game/java_path", ""))
        auto_java = self.auto_java_check.isChecked() if hasattr(self, "auto_java_check") else self.settings.value(
            "game/auto_java", True, type=bool)
        jvm_arguments = self.jvm_edit.text().strip() if hasattr(self, "jvm_edit") else str(
            self.settings.value("game/jvm_arguments", ""))
        self.settings.setValue("profile/nickname", nickname)
        self.settings.sync()

        self.play_button.setEnabled(False)
        self._set_status("запуск Minecraft")

        self.launch_worker = LaunchWorker(
            str(self._profile_game_root()), str(self._instance_dir()), str(version_id), account, ram,
            java_path, auto_java, jvm_arguments,
        )
        self.launch_worker.progress.connect(self._set_status)
        self.launch_worker.started_game.connect(self._game_started)
        self.launch_worker.failed.connect(self._launch_failed)
        self.launch_worker.start()

    def _game_started(self, message: str):
        self.play_button.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        self._set_status("Minecraft запущен")
        self.install_status.setText(message)
        log.info(message)

    def _launch_failed(self, message: str):
        self.play_button.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        self._set_status("ошибка запуска")
        self._notify("Не удалось запустить Minecraft", message, "error", 8000)

    # -------------------------- Settings --------------------------
    def _browse_game_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Выбери место, где создать FiercyLauncher",
                                                self.game_dir_edit.text())
        if path:
            self.game_dir_edit.setText(path)
            try:
                target = resolve_launcher_root(path)
                self.resolved_root_label.setText(f"Будет использоваться: {target}")
            except ValueError:
                self.resolved_root_label.setText("Выбери корректную папку.")

    def _browse_java(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбери Java executable", "",
                                              "Java executable (java.exe javaw.exe);;All files (*.*)")
        if path:
            self.java_edit.setText(path)

    def _install_launcher_copy_if_needed(self, target_root: Path) -> bool:
        if not getattr(sys, "frozen", False):
            return False
        source = Path(sys.executable).resolve().parent
        target = target_root.resolve()
        if source == target:
            return False
        executable_name = Path(sys.executable).name
        target_executable = target / executable_name
        if not target_executable.is_file():
            raise FileNotFoundError(f"После переноса не найден {target_executable}")
        subprocess.Popen([str(target_executable), "--installed-copy"], cwd=str(target),
                         close_fds=(os.name != "nt"))
        self._allow_update_close = True
        QTimer.singleShot(300, self.close)
        self._notify("FiercyLauncher перенесён",
                     f"Файлы приложения и игровые данные находятся в {target}. Запускается новая копия.", "success",
                     3000)
        return True

    def _storage_migration_progress(self, message: str):
        self.settings_dirty_label.setText(message)
        self._set_status(message)

    def _storage_migration_failed(self, message: str):
        self.save_settings_button.setEnabled(True)
        self.settings_dirty_label.setText("Перенос не завершён. Изменения не сохранены — можно повторить.")
        self._set_status("ошибка переноса папки")
        self._notify("Не удалось перенести FiercyLauncher", message, "error", 8500)

    def _storage_migration_completed(self, destination: str):
        self.save_settings_button.setEnabled(True)
        self._commit_settings(Path(destination).resolve())

    def _commit_settings(self, new_root: Path):
        old_root = self.game_root.resolve()
        try:
            new_root.mkdir(parents=True, exist_ok=True)
            self.settings.setValue("game/directory", str(new_root))
            self.settings.setValue("launcher/storage_directory", str(new_root))
            self.settings.setValue("game/java_path", self.java_edit.text().strip())
            self.settings.setValue("game/ram", self.ram_spin.value())
            self.settings.setValue("game/auto_java", self.auto_java_check.isChecked())
            self.settings.setValue("game/jvm_arguments", self.jvm_edit.text().strip())
            self.settings.setValue("updates/auto_install", self.auto_update_check.isChecked())
            self.settings.setValue("profile/nickname", self.nickname_edit.text().strip())
            self.appearance_geometry = self._collect_appearance_geometry()
            mode = str(self.theme_combo.currentData() or "custom")
            self.appearance_manager.save(mode, self.appearance_palette, self.appearance_geometry)
            self.settings.sync()

            if new_root != old_root:
                previous_profile_id = self.active_profile_id
                self.game_root = new_root
                self.profile_manager = ProfileManager(self.game_root)
                profiles = self.profile_manager.all()
                self.active_profile_id = previous_profile_id if self.profile_manager.get(previous_profile_id) else \
                profiles[0]["id"]
                self.settings.setValue("profiles/active", self.active_profile_id)
                self.settings.sync()
                self._refresh_profiles()
                self._refresh_installed_versions()

            self.resolved_root_label.setText(f"Используется: {self.game_root}")
            self.apply_theme()
            self._settings_committed = self._settings_snapshot()
            self._settings_dirty = False
            self._settings_changed()
            self._set_status("настройки сохранены")
            if new_root != old_root and self._install_launcher_copy_if_needed(new_root):
                return
            self._notify("Настройки сохранены",
                         f"Папка игровых данных: {self.game_root}\nОформление и параметры Java сохранены.", "success")
        except Exception as exc:
            log.exception("Не удалось сохранить настройки")
            self._notify("Ошибка сохранения настроек", str(exc), "error", 7500)

    def save_settings(self):
        if self.storage_worker and self.storage_worker.isRunning():
            self._notify("Перенос уже выполняется", "Дождись окончания копирования игровых данных.", "info")
            return
        selected_text = self.game_dir_edit.text().strip()
        if not selected_text:
            self._notify("Не указана папка", "Выбери место, внутри которого будет создана FiercyLauncher.", "warning")
            return
        try:
            selected_base = Path(selected_text).expanduser()
            current_base = selected_base_from_root(self.game_root)
            new_root = self.game_root if selected_base.resolve() == current_base.resolve() else resolve_launcher_root(
                selected_base)
            if new_root.resolve() == self.game_root.resolve():
                self._commit_settings(new_root.resolve())
                return

            application_root = ""
            if getattr(sys, "frozen", False):
                source_app = Path(sys.executable).resolve().parent
                if source_app != new_root.resolve():
                    application_root = str(source_app)
            self.save_settings_button.setEnabled(False)
            self.settings_dirty_label.setText("Подготовка переноса… Не закрывай лаунчер.")
            self._set_status("перенос файлов в новую папку")
            self.storage_worker = StorageMigrationWorker(
                str(self.game_root), str(new_root), application_root, self,
            )
            self.storage_worker.progress.connect(self._storage_migration_progress)
            self.storage_worker.completed.connect(self._storage_migration_completed)
            self.storage_worker.failed.connect(self._storage_migration_failed)
            self.storage_worker.start()
        except Exception as exc:
            log.exception("Не удалось подготовить перенос игровых данных")
            self._notify("Ошибка сохранения настроек", str(exc), "error", 7500)

    # -------------------------- Profiles --------------------------
    def _refresh_profiles(self):
        profiles = self.profile_manager.all()
        for combo in (getattr(self, "game_profile_combo", None), getattr(self, "profiles_combo", None)):
            if combo is None:
                continue
            combo.blockSignals(True)
            combo.clear()
            for profile in profiles:
                combo.addItem(profile["name"], profile["id"])
            pos = combo.findData(self.active_profile_id)
            if pos >= 0:
                combo.setCurrentIndex(pos)
            combo.blockSignals(False)
        self._update_profile_folder_label()
        if hasattr(self, "profile_mod_list"):
            self._refresh_mod_list()

    def _profile_combo_changed(self, index: int):
        if index < 0:
            return
        profile_id = self.game_profile_combo.itemData(index)
        if profile_id:
            self._set_active_profile(str(profile_id))

    def _profile_page_combo_changed(self, index: int):
        if index < 0:
            return
        profile_id = self.profiles_combo.itemData(index)
        if profile_id:
            self._set_active_profile(str(profile_id))

    def _set_active_profile(self, profile_id: str):
        if not self.profile_manager.get(profile_id):
            return
        self.active_profile_id = profile_id
        self.settings.setValue("profiles/active", profile_id)
        self.settings.sync()
        self._refresh_profiles()
        self._refresh_installed_versions()
        profile = self._active_profile()
        if hasattr(self, "version_combo"):
            mc_version = str(profile.get("minecraft_version", ""))
            if mc_version:
                pos = self.version_combo.findData(mc_version)
                if pos >= 0:
                    self.version_combo.setCurrentIndex(pos)
        if hasattr(self, "loader_combo"):
            loader_pos = self.loader_combo.findData(str(profile.get("loader", "vanilla")))
            if loader_pos >= 0:
                self.loader_combo.setCurrentIndex(loader_pos)
        if hasattr(self, "loader_version_edit"):
            self.loader_version_edit.setText(str(profile.get("loader_version", "")))
        if hasattr(self, "profile_mod_list"):
            self._refresh_mod_list()

    def _update_profile_folder_label(self):
        profile = self._active_profile()
        if hasattr(self, "profile_folder_label"):
            self.profile_folder_label.setText(
                f"Папка профиля: {self._instance_dir()}\nОбщие версии/библиотеки: {self._profile_game_root()}")
        if hasattr(self, "profile_summary_label"):
            self.profile_summary_label.setText(
                f"Minecraft: {profile.get('minecraft_version') or 'не выбрана'}  ·  "
                f"Загрузчик: {profile.get('loader', 'vanilla')}  ·  "
                f"Установленная версия: {profile.get('installed_version_id') or 'нет'}"
            )
        if hasattr(self, "game_profile_combo"):
            self.game_profile_combo.blockSignals(True)
            pos = self.game_profile_combo.findData(self.active_profile_id)
            if pos >= 0:
                self.game_profile_combo.setCurrentIndex(pos)
            self.game_profile_combo.blockSignals(False)

    def create_profile(self):
        if not self.versions:
            self._notify("Список Minecraft ещё загружается", "Подожди, пока обновится список официальных версий.",
                         "info")
            return
        dialog = CreateProfileDialog(self.versions, self)
        dialog.setStyleSheet(self.styleSheet())
        if dialog.exec_() != dialog.Accepted:
            return
        values = dialog.values()
        try:
            profile = self.profile_manager.create(
                values["name"], description="Игровой профиль FiercyLauncher",
                minecraft_version=values["minecraft_version"], loader=values["loader"],
                loader_version=values["loader_version"], storage_root="shared",
            )
            self._set_active_profile(profile["id"])
            version_pos = self.version_combo.findData(values["minecraft_version"])
            if version_pos < 0:
                self.version_combo.addItem(values["minecraft_version"], values["minecraft_version"])
                version_pos = self.version_combo.findData(values["minecraft_version"])
            self.version_combo.setCurrentIndex(version_pos)
            loader_pos = self.loader_combo.findData(values["loader"])
            if loader_pos >= 0:
                self.loader_combo.setCurrentIndex(loader_pos)
            self.loader_version_edit.setText(values["loader_version"])
            self._notify("Профиль создан",
                         f"Профиль «{values['name']}» создан. Начинается установка Minecraft и загрузчика.", "success",
                         5000)
            self.navigate(0)
            self.install_selected()
        except ProfileError as exc:
            self._notify("Не удалось создать профиль", str(exc), "error")

    # -------------------------- Mods and imports --------------------------
    def _refresh_mod_list(self):
        if not hasattr(self, "profile_mod_list"):
            return
        self.profile_mod_list.clear()
        try:
            mods = list_mod_files(self._instance_dir())
        except Exception as exc:
            self.mod_count_label.setText(f"Не удалось прочитать папку mods: {exc}")
            return
        for mod in mods:
            state = "ВКЛЮЧЁН" if mod["enabled"] else "ОТКЛЮЧЁН"
            size_mb = mod["size"] / (1024 * 1024)
            item = QListWidgetItem(
                f"{'●' if mod['enabled'] else '○'}  {mod['name']}   ·   {state}   ·   {size_mb:.1f} МБ")
            item.setData(Qt.UserRole, mod)
            self.profile_mod_list.addItem(item)
        enabled = sum(1 for item in mods if item["enabled"])
        disabled = len(mods) - enabled
        self.mod_count_label.setText(
            f"Всего: {len(mods)}  ·  Включено: {enabled}  ·  Отключено: {disabled}  ·  Папка: {self._instance_dir() / 'mods'}")

    def _toggle_mod_item(self, item):
        if item:
            self.profile_mod_list.setCurrentItem(item)
            self.toggle_selected_mod()

    def toggle_selected_mod(self):
        item = self.profile_mod_list.currentItem()
        if item is None:
            self._notify("Мод не выбран", "Выбери мод из списка.", "info")
            return
        info = item.data(Qt.UserRole) or {}
        try:
            new_path = set_mod_enabled(self._instance_dir(), info.get("name", ""), not bool(info.get("enabled")))
            self._notify("Состояние мода изменено", f"Файл: {new_path.name}", "success")
            self._refresh_mod_list()
        except Exception as exc:
            self._notify("Не удалось изменить мод", str(exc), "error")

    def remove_selected_mod(self):
        item = self.profile_mod_list.currentItem()
        if item is None:
            self._notify("Мод не выбран", "Сначала выбери мод.", "info")
            return
        info = item.data(Qt.UserRole) or {}
        path = Path(info.get("path", ""))
        try:
            if path.parent.resolve() != (self._instance_dir() / "mods").resolve() or not path.is_file():
                raise ValueError("Выбранный файл больше не находится в папке mods этого профиля.")
            path.unlink()
            self._notify("Мод удалён", path.name, "success")
            self._refresh_mod_list()
        except Exception as exc:
            self._notify("Не удалось удалить мод", str(exc), "error")

    def add_local_mod_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Добавить локальные JAR-моды", "", "Minecraft mods (*.jar)")
        if not files:
            return
        try:
            added = add_local_mods(self._instance_dir(), files)
            self._refresh_mod_list()
            self._notify("Моды добавлены", f"Добавлено файлов: {len(added)}", "success")
        except Exception as exc:
            self._notify("Не удалось добавить моды", str(exc), "error")

    def _current_mod_compatibility(self) -> tuple[str, str]:
        profile = self._active_profile()
        game_version = str(profile.get("minecraft_version") or "").strip()
        if not game_version:
            game_version = str(self.version_combo.currentData() or "").strip()
        loader = str(profile.get("loader") or "vanilla").lower()
        return game_version, loader

    def _curseforge_api_key(self) -> str:
        if keyring is None:
            return ""
        try:
            return keyring.get_password("FiercyLauncher", "curseforge_api_key") or ""
        except Exception:
            log.exception("Не удалось прочитать CurseForge API key из Credential Manager")
            return ""

    def configure_curseforge_key(self):
        if keyring is None:
            self._notify("Нужно системное хранилище",
                         "Установи зависимости из requirements.txt. Ключ CurseForge хранится через Windows Credential Manager, а не в открытом JSON.",
                         "warning", 7000)
            return
        key, accepted = QInputDialog.getText(self, "CurseForge API key", "Вставь личный API key CurseForge:",
                                             QLineEdit.Password)
        if not accepted:
            return
        key = key.strip()
        if len(key) < 12:
            self._notify("Ключ не сохранён", "Проверь, что скопировал полный API key CurseForge.", "warning")
            return
        try:
            keyring.set_password("FiercyLauncher", "curseforge_api_key", key)
            self._notify("CurseForge настроен", "Ключ сохранён в системном хранилище Windows.", "success")
        except Exception as exc:
            self._notify("Не удалось сохранить ключ", str(exc), "error")

    def search_mods(self):
        if self.mod_search_worker and self.mod_search_worker.isRunning():
            return
        query = self.mod_search_edit.text().strip()
        if not query:
            self._notify("Укажи название мода", "Напиши, что нужно найти.", "info")
            return
        game_version, loader = self._current_mod_compatibility()
        if not game_version:
            self._notify("Не выбрана версия Minecraft", "Сначала создай профиль с версией Minecraft.", "warning")
            return
        provider = str(self.mod_provider_combo.currentData() or "modrinth")
        if provider == "curseforge":
            api_key = self._curseforge_api_key()
            if not api_key:
                self._notify("CurseForge API key",
                             "Сначала нажми «Настроить API key CurseForge». Modrinth можно использовать без ключа.",
                             "warning", 7000)
                return
            worker = CurseForgeSearchWorker(query, game_version, loader, api_key, self)
        else:
            worker = ModrinthSearchWorker(query, game_version, loader, self)
        self.mod_results_list.clear()
        self.mod_status_label.setText(f"Поиск {query} для Minecraft {game_version} / {loader}…")
        self._retain_worker(worker)
        worker.loaded.connect(self._mod_search_loaded)
        worker.failed.connect(self._mod_operation_failed)
        self.mod_search_worker = worker
        worker.start()

    def _mod_search_loaded(self, results: list):
        self.mod_results_list.clear()
        for result in results:
            downloads = result.get("downloads", 0)
            item = QListWidgetItem(
                f"{result.get('title', 'Мод')}   ·   {downloads:,} загрузок\n{result.get('description', '')}")
            item.setData(Qt.UserRole, result)
            self.mod_results_list.addItem(item)
        self.mod_status_label.setText(
            f"Найдено результатов: {len(results)}. Выбери мод и нажми «Установить выбранный мод».")

    def _mod_operation_failed(self, message: str):
        self.mod_status_label.setText(message)
        self._notify("Операция с модом не выполнена", message, "error", 7500)

    def install_selected_mod_from_catalog(self):
        item = self.mod_results_list.currentItem()
        if item is None:
            self._notify("Мод не выбран", "Выбери результат поиска.", "info")
            return
        if self.mod_install_worker and self.mod_install_worker.isRunning():
            return
        data = item.data(Qt.UserRole) or {}
        game_version, loader = self._current_mod_compatibility()
        provider = str(self.mod_provider_combo.currentData() or "modrinth")
        profile_id = self.active_profile_id
        instance_dir = self._instance_dir()
        self._mod_install_profile_id = profile_id
        if provider == "curseforge":
            worker = CurseForgeInstallWorker(data.get("project_id", ""), game_version, loader,
                                             str(instance_dir), self._curseforge_api_key(), self)
        else:
            worker = ModrinthInstallWorker(data.get("project_id", ""), game_version, loader,
                                           str(instance_dir), self)
        worker.completed.connect(self._mod_install_completed)
        worker.failed.connect(self._mod_operation_failed)
        worker.progress.connect(self.mod_status_label.setText)
        self._retain_worker(worker)
        self.mod_install_worker = worker
        self.mod_status_label.setText("Начинается загрузка мода…")
        worker.start()

    def _mod_install_completed(self, paths: list):
        profile_id = self._mod_install_profile_id or self.active_profile_id
        profile = self.profile_manager.get(profile_id)
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        names = ", ".join(Path(path).name for path in paths)
        profile_name = profile.get("name", "профиля") if profile else "профиля"
        if hasattr(self, "mod_status_label"):
            self.mod_status_label.setText(f"Установлено в «{profile_name}»: {names}")
        self._notify("Мод установлен", f"Профиль: {profile_name}\n{names}", "success")

    def import_local_version(self):
        source_root = QFileDialog.getExistingDirectory(self, "Выбери папку .minecraft или папку другого лаунчера",
                                                       str(Path.home()))
        if not source_root:
            return
        versions_dir = Path(source_root) / "versions"
        if not versions_dir.is_dir():
            self._notify("Папка версий не найдена",
                         "В выбранной папке нет каталога versions. Выбери корень .minecraft.", "warning")
            return
        versions = sorted(folder.name for folder in versions_dir.iterdir()
                          if folder.is_dir() and (folder / f"{folder.name}.json").is_file())
        if not versions:
            self._notify("Локальные версии не найдены", "В каталоге versions нет корректных JSON-версий.", "warning")
            return
        version_id, accepted = QInputDialog.getItem(self, "Импорт версии", "Какую локальную версию импортировать?",
                                                    versions, 0, False)
        if not accepted:
            return
        try:
            profile = self.profile_manager.create(
                version_id, description=f"Импортировано из {source_root}", storage_root="shared",
                minecraft_version=version_id, loader="vanilla", kind="imported",
            )
            self._set_active_profile(profile["id"])
            self._local_import_profile_id = profile["id"]
            worker = LocalVersionImportWorker(source_root, version_id, str(self.game_root),
                                              str(self._instance_dir()), self)
            worker.progress.connect(lambda message: self._set_status(message))
            worker.completed.connect(self._local_version_imported)
            worker.failed.connect(self._mod_operation_failed)
            self.local_import_worker = worker
            self._retain_worker(worker)
            self.mod_status_label.setText(f"Импорт локальной версии {version_id}…")
            worker.start()
        except Exception as exc:
            self._notify("Не удалось создать импортированный профиль", str(exc), "error")

    def _local_version_imported(self, result: dict):
        profile_id = self._local_import_profile_id or self.active_profile_id
        if self.profile_manager.get(profile_id):
            self.profile_manager.update(
                profile_id,
                minecraft_version=result.get("minecraft_version", ""), loader=result.get("loader", "vanilla"),
                loader_version=result.get("loader_version", ""),
                installed_version_id=result.get("installed_version_id", ""),
                storage_root="shared", kind="imported",
            )
        self._refresh_profiles()
        self._refresh_installed_versions()
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        self._notify("Локальная версия импортирована",
                     f"Импортирована {result.get('installed_version_id')}. Моды и миры можно добавить в папку профиля.",
                     "success", 6500)

    def import_modpack(self):
        archive_path, _ = QFileDialog.getOpenFileName(
            self, "Импортировать сборку Minecraft", "",
            "Сборки Minecraft (*.mrpack *.zip);;Все файлы (*.*)"
        )
        if not archive_path:
            return
        if self.modpack_worker and self.modpack_worker.isRunning():
            return
        try:
            metadata = inspect_modpack(archive_path)
            if not metadata.get("minecraft_version"):
                raise ValueError("В манифесте сборки не указана версия Minecraft.")
            profile = self.profile_manager.create(
                metadata["name"], description=f"Импорт сборки: {Path(archive_path).name}",
                minecraft_version=metadata["minecraft_version"], loader=metadata["loader"],
                loader_version=metadata["loader_version"], storage_root="shared",
                modpack_source=metadata["format"], modpack_name=metadata["name"], kind="modpack",
            )
            self._set_active_profile(profile["id"])
            self._modpack_profile_id = profile["id"]
            worker = ModpackImportWorker(
                archive_path, str(self.game_root), str(self._instance_dir()),
                str(self.settings.value("game/java_path", "")),
                self.settings.value("game/auto_java", True, type=bool),
                self._curseforge_api_key(), self,
            )
            worker.progress.connect(lambda value, message: self._modpack_progress(value, message))
            worker.completed.connect(self._modpack_imported)
            worker.failed.connect(self._modpack_import_failed)
            self.modpack_worker = worker
            self._retain_worker(worker)
            self.mod_status_label.setText("Подготовка импорта сборки…")
            worker.start()
        except Exception as exc:
            self._notify("Не удалось прочитать сборку", str(exc), "error", 7000)

    def _modpack_progress(self, value: int, message: str):
        self.mod_status_label.setText(message)
        self._set_status(message)

    def _modpack_imported(self, metadata: dict):
        profile_id = self._modpack_profile_id or self.active_profile_id
        if self.profile_manager.get(profile_id):
            self.profile_manager.update(
                profile_id, minecraft_version=metadata["minecraft_version"],
                loader=metadata["loader"], loader_version=metadata["loader_version"],
                installed_version_id=metadata["installed_version_id"], storage_root="shared", kind="modpack",
            )
        self._refresh_profiles()
        self._refresh_installed_versions()
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        self.mod_status_label.setText(f"Сборка «{metadata['name']}» готова к запуску.")
        self._notify("Сборка импортирована",
                     f"{metadata['name']} · Minecraft {metadata['minecraft_version']} · {metadata['loader']}",
                     "success", 6500)

    def _modpack_import_failed(self, message: str):
        self.mod_status_label.setText(message)
        self._notify("Импорт сборки не завершён", message, "error", 8500)

    def rename_profile(self):
        profile = self._active_profile()
        new_name, accepted = QInputDialog.getText(
            self, "Переименовать профиль", "Новое название профиля:",
            QLineEdit.Normal, profile.get("name", ""),
        )
        if not accepted:
            return
        new_name = " ".join(new_name.split()).strip()
        if not new_name:
            self._notify("Название не изменено", "Название профиля не может быть пустым.", "warning")
            return
        if any(item["id"] != profile["id"] and item["name"].casefold() == new_name.casefold()
               for item in self.profile_manager.all()):
            self._notify("Название занято", "Профиль с таким названием уже существует.", "warning")
            return
        try:
            self.profile_manager.update(profile["id"], name=new_name[:48])
            self._refresh_profiles()
            self._notify("Профиль переименован", new_name[:48], "success")
        except Exception as exc:
            self._notify("Не удалось переименовать профиль", str(exc), "error")

    def delete_profile(self):
        if len(self.profile_manager.all()) <= 1:
            self._notify("Игровые профили", "Нельзя удалить последний профиль.", "warning")
            return
        profile = self._active_profile()
        from PyQt5.QtWidgets import QDialog, QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle("Удалить игровой профиль")
        dialog.setModal(True)
        dialog.setMinimumWidth(390)
        box = QVBoxLayout(dialog)
        title = QLabel(f"Удалить профиль «{profile['name']}»?")
        title.setStyleSheet("font-size: 14pt; font-weight: 800;")
        body = QLabel("Профиль будет удалён из списка. Его моды, миры и файлы останутся на диске.")
        body.setWordWrap(True)
        body.setObjectName("Muted")
        box.addWidget(title)
        box.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        buttons.button(QDialogButtonBox.Yes).setText("Удалить профиль")
        buttons.button(QDialogButtonBox.No).setText("Отмена")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        dialog.setStyleSheet(self.styleSheet())
        if dialog.exec_() != QDialog.Accepted:
            return
        try:
            old_id = self.active_profile_id
            self.profile_manager.delete(old_id, delete_files=False)
            self.active_profile_id = self.profile_manager.all()[0]["id"]
            self.settings.setValue("profiles/active", self.active_profile_id)
            self.settings.sync()
            self._refresh_profiles()
            self._refresh_installed_versions()
        except ProfileError as exc:
            self._notify("Не удалось удалить профиль", str(exc), "error")

    def open_profile_folder(self):
        self._open_folder(self._instance_dir())

    def _open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # -------------------------- GitHub updates and content --------------------------
    def check_updates(self, silent: bool = False):
        if self.release_worker and self.release_worker.isRunning():
            return
        self.check_updates_button.setEnabled(False)
        self.update_status.setText("Проверяем последний GitHub Release…")
        self._set_status("проверка обновлений")
        self.release_worker = ReleaseCheckWorker(GITHUB_API_LATEST, self)
        self.release_worker.checked.connect(lambda data, quiet=silent: self._release_checked(data, quiet))
        self.release_worker.failed.connect(self._release_failed)
        self.release_worker.start()

    def _release_checked(self, data: dict, silent: bool):
        self.check_updates_button.setEnabled(True)
        if not data.get("has_release"):
            self.update_status.setText("На GitHub ещё не опубликован ни один релиз.")
            self._set_status("релизов пока нет")
            return
        if not data.get("update_available"):
            self.update_status.setText(
                f"Установлена актуальная версия {LAUNCHER_VERSION}. Последняя: {data.get('latest_version', LAUNCHER_VERSION)}.")
            self._set_status("лаунчер обновлён")
            if not silent:
                self._notify("Обновления", "Установлена последняя версия FiercyLauncher.", "success")
            return
        latest = data.get("latest_version", "")
        self.update_status.setText(f"Доступна версия {latest}. Текущая: {LAUNCHER_VERSION}.")
        self._set_status(f"доступно обновление {latest}")
        if silent:
            auto_install = self.settings.value("updates/auto_install", True, type=bool)
            if auto_install:
                self._notify(
                    "Обновление FiercyLauncher найдено",
                    f"Версия {latest} будет загружена и установлена автоматически. SHA-256 проверяется перед заменой файлов.",
                    "info", 8000,
                )
                self._start_update_download(data)
            else:
                self._notify("Доступно обновление FiercyLauncher",
                             f"Версия {latest} доступна в GitHub Releases. Открой раздел «Обновления и логи» и нажми «Проверить GitHub Releases», чтобы скачать её.",
                             "info", 8000)
            return
        self._notify("Начинается обновление",
                     f"Скачиваем FiercyLauncher {latest}. После проверки SHA-256 приложение перезапустится.", "info",
                     6000)
        self._start_update_download(data)

    def _release_failed(self, message: str):
        self.check_updates_button.setEnabled(True)
        self.update_status.setText("Не удалось проверить GitHub Releases. Лаунчер продолжит работать без обновления.")
        self._set_status("ошибка проверки GitHub")
        log.warning("GitHub update check: %s", message)

    def _start_update_download(self, data: dict):
        self.download_worker = AssetDownloadWorker(
            data["download_url"], data["sha256"], data["asset_name"], self,
        )
        self.download_worker.progress.connect(self._update_download_progress)
        self.download_worker.completed.connect(self._update_downloaded)
        self.download_worker.failed.connect(self._update_download_failed)
        self.download_worker.cancelled.connect(self._update_cancelled)
        self.update_progress.show()
        self.update_progress.setValue(0)
        self.cancel_download_button.show()
        self.check_updates_button.setEnabled(False)
        self.update_status.setText(f"Загрузка FiercyLauncher {data.get('latest_version')}…")
        self.download_worker.start()

    def _update_download_progress(self, percent: int, total: int, speed: float, status: str):
        if percent >= 0:
            self.update_progress.setValue(percent)
        speed_mbps = speed / 1048576
        self.update_status.setText(f"{status} · {speed_mbps:.2f} МБ/с")
        self._set_status("загрузка обновления")

    def cancel_update_download(self):
        if self.download_worker and self.download_worker.isRunning():
            self.download_worker.cancel()
            self.cancel_download_button.setEnabled(False)
            self.update_status.setText("Отменяем загрузку…")

    def _update_cancelled(self):
        self.cancel_download_button.hide()
        self.cancel_download_button.setEnabled(True)
        self.check_updates_button.setEnabled(True)
        self.update_status.setText("Загрузка обновления отменена.")
        self._set_status("обновление отменено")

    def _update_downloaded(self, archive_path: str):
        self._update_archive = archive_path
        self.cancel_download_button.hide()
        self.update_progress.setValue(100)
        self.update_status.setText("Архив загружен и SHA-256 проверен. Готовим безопасную замену файлов…")
        QTimer.singleShot(800, self._apply_update)

    def _update_download_failed(self, message: str):
        self.cancel_download_button.hide()
        self.cancel_download_button.setEnabled(True)
        self.check_updates_button.setEnabled(True)
        self.update_status.setText("Не удалось скачать обновление. Текущая версия не изменена.")
        self._set_status("ошибка загрузки обновления")
        self._notify("Ошибка обновления", message, "error", 7500)

    def _apply_update(self):
        if not self._update_archive:
            return
        workers = list(self.workers) + [self.version_worker, self.install_worker, self.launch_worker,
                                        self.release_worker, self.download_worker, self.content_worker,
                                        self.banner_worker]
        if any(worker is not None and worker.isRunning() for worker in workers):
            QTimer.singleShot(500, self._apply_update)
            return
        frozen = bool(getattr(sys, "frozen", False))
        if frozen:
            target = Path(sys.executable).resolve().parent
            helper = target / "FiercyUpdater.exe"
            command = [str(helper)]
            mode, entry = "frozen", "FiercyLauncher.exe"
        else:
            target = ROOT
            helper = target / "update_helper.py"
            command = [sys.executable, str(helper)]
            mode, entry = "source", "main.py"
        if not helper.is_file():
            self._update_download_failed(
                "Не найден помощник обновления. Для EXE положи FiercyUpdater.exe рядом с FiercyLauncher.exe; для исходников нужен update_helper.py.")
            return
        command += ["--archive", self._update_archive, "--target", str(target),
                    "--parent-pid", str(os.getpid()), "--mode", mode, "--entry", entry]
        try:
            subprocess.Popen(command, cwd=str(target), close_fds=(os.name != "nt"))
            self._allow_update_close = True
            log.info("Запущен помощник применения обновления: %s", self._update_archive)
            QTimer.singleShot(250, self.close)
        except Exception as exc:
            self._update_download_failed(str(exc))

    def load_static_content(self):
        if self.content_worker and self.content_worker.isRunning():
            return
        self.refresh_content_button.setEnabled(False)
        self.content_worker = StaticContentWorker(self)
        self.content_worker.loaded.connect(self._content_loaded)
        self.content_worker.failed.connect(self._content_failed)
        self.content_worker.start()

    def _content_loaded(self, data: dict):
        self.refresh_content_button.setEnabled(True)
        launcher_data = data.get("launcher", {})
        self.remote_launcher_config = launcher_data.get("launcher", {}) if isinstance(launcher_data, dict) else {}
        self.remote_versions_config = data.get("versions", {}) if isinstance(data.get("versions", {}), dict) else {}
        self.maintenance_mode = bool(self.remote_launcher_config.get("maintenance_mode", False))
        self.minimum_version_block = False
        minimum = str(self.remote_launcher_config.get("minimum_version", "")).strip()
        if minimum:
            try:
                self.minimum_version_block = Version(LAUNCHER_VERSION) < Version(minimum)
            except InvalidVersion:
                log.warning("В launcher.json указана некорректная minimum_version: %s", minimum)
        blocked = self.maintenance_mode or self.minimum_version_block
        self.install_button.setEnabled(not blocked and not (self.install_worker and self.install_worker.isRunning()))
        self.play_button.setEnabled(not blocked and not (self.launch_worker and self.launch_worker.isRunning()))
        if self.versions:
            self._populate_version_combo()
        allowed_loaders = self.remote_versions_config.get("supported_loaders", [])
        if isinstance(allowed_loaders, list) and allowed_loaders:
            allowed_loader_set = {str(item).lower() for item in allowed_loaders}
            loader_items = [("Vanilla", "vanilla"), ("Fabric", "fabric"), ("Forge", "forge"),
                            ("NeoForge", "neoforge"), ("Quilt", "quilt")]
            selected_loader = self.loader_combo.currentData()
            self.loader_combo.clear()
            for label, value in loader_items:
                if value in allowed_loader_set:
                    self.loader_combo.addItem(label, value)
            selected_pos = self.loader_combo.findData(selected_loader)
            if selected_pos < 0:
                recommended = str(self.remote_launcher_config.get("recommended_loader", ""))
                selected_pos = self.loader_combo.findData(recommended)
            if selected_pos >= 0:
                self.loader_combo.setCurrentIndex(selected_pos)
        ui_config = launcher_data.get("ui", {}) if isinstance(launcher_data, dict) else {}
        banner_url = str(ui_config.get("banner_url", "")) if isinstance(ui_config, dict) else ""
        self._load_promo_banner(banner_url.strip())
        if self.maintenance_mode:
            announcement = str(self.remote_launcher_config.get("announcement", "Техническое обслуживание"))
            self.news_label.setText("ТЕХНИЧЕСКИЕ РАБОТЫ\n" + announcement)
            self._set_status("объявлено обслуживание")
            return
        if self.minimum_version_block:
            self.news_label.setText(
                "ТРЕБУЕТСЯ ОБНОВЛЕНИЕ ЛАУНЧЕРА\n"
                f"Для продолжения нужна версия {minimum}. Открой раздел «Обновления и логи»."
            )
            self._set_status("требуется обновление")
            return
        news = data.get("news", {})
        posts = news.get("items", []) if isinstance(news, dict) else []
        if posts:
            lines = []
            announcement = str(self.remote_launcher_config.get("announcement", "")).strip()
            if announcement:
                lines.append("ОБЪЯВЛЕНИЕ\n" + announcement)
            for item in posts[:8]:
                title = str(item.get("title", "Новость"))[:120]
                body = str(item.get("text", ""))[:500]
                date = str(item.get("date", ""))[:24]
                lines.append(f"• {title}" + (f"  ·  {date}" if date else "") + (f"\n  {body}" if body else ""))
            self.news_label.setText("\n\n".join(lines))
        else:
            self.news_label.setText("Пока нет объявлений. Добавь новости в content/news.json репозитория GitHub.")
        self._set_status("контент GitHub обновлён")

    def _policy_message(self) -> str:
        if self.maintenance_mode:
            announcement = str(self.remote_launcher_config.get("announcement", "Технические работы."))
            return "Игровые операции временно отключены публикацией launcher.json.\n\n" + announcement
        minimum = str(self.remote_launcher_config.get("minimum_version", "последняя версия"))
        return f"Для продолжения требуется FiercyLauncher {minimum}. Открой «Обновления и логи» и установи обновление."

    def _load_promo_banner(self, url: str):
        if not url:
            self.promo_banner.hide()
            self.promo_banner.clear()
            self._banner_pixmap = QPixmap()
            return
        try:
            self.banner_worker = RemoteImageWorker(url, self)
            self._retain_worker(self.banner_worker)
            self.banner_worker.loaded.connect(self._promo_banner_loaded)
            self.banner_worker.failed.connect(lambda message: log.debug("Промо-баннер не загружен: %s", message))
            self.banner_worker.start()
        except ValueError as exc:
            self.promo_banner.hide()
            log.warning("Неверная ссылка промо-баннера: %s", exc)

    def _promo_banner_loaded(self, raw: bytes):
        image = QImage()
        if not image.loadFromData(raw):
            log.warning("GitHub content banner: неподдерживаемый формат изображения")
            return
        self._banner_pixmap = QPixmap.fromImage(image)
        self._resize_promo_banner()

    def _resize_promo_banner(self):
        if self._banner_pixmap.isNull():
            return
        size = self.promo_banner.size()
        if size.width() < 10 or size.height() < 10:
            return
        scaled = self._banner_pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.promo_banner.setPixmap(scaled)
        self.promo_banner.show()

    def _content_failed(self, message: str):
        self.refresh_content_button.setEnabled(True)
        self.news_label.setText(
            "Не удалось обновить новости. Проверь подключение к GitHub или открой репозиторий позже.")
        log.warning("Static content: %s", message)

    # -------------------------- Logs / close --------------------------
    def _refresh_logs(self):
        if not hasattr(self, "log_view") or self.stack.currentIndex() != 4:
            return
        launcher_path = log_file()
        game_path = self._instance_dir() / "logs" / "latest.log"
        for path, viewer, empty_text in (
                (launcher_path, self.log_view, "Лог лаунчера пока пуст."),
                (game_path, self.game_log_view, "Minecraft ещё не создавал latest.log в этом профиле."),
        ):
            try:
                with path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(max(0, size - 512 * 1024), os.SEEK_SET)
                    tail = handle.read().decode("utf-8", errors="replace")
                lines = tail.splitlines()[-180:]
                text = "\n".join(lines) if lines else empty_text
                if viewer.toPlainText() != text:
                    viewer.setPlainText(text)
                    cursor = viewer.textCursor()
                    cursor.movePosition(cursor.End)
                    viewer.setTextCursor(cursor)
            except OSError:
                viewer.setPlainText(empty_text)

    def open_log_folder(self):
        self._open_folder(user_data_dir() / "logs")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "promo_banner"):
            self._resize_promo_banner()
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast.reposition()

    def closeEvent(self, event):
        if self._allow_update_close:
            event.accept()
            return
        if self.stack.currentIndex() == getattr(self, "settings_page_index", 3) and self._settings_dirty:
            self._notify("Настройки не сохранены",
                         "Сначала нажми «Сохранить все настройки». Закрыть лаунчер с несохранёнными изменениями нельзя.",
                         "warning", 7000)
            event.ignore()
            return
        if self.download_worker and self.download_worker.isRunning():
            self.download_worker.cancel()
            self._notify("Отмена загрузки", "Загрузка отменяется. Закрой лаунчер после завершения операции.", "info",
                         6000)
            event.ignore()
            return
        active = [worker for worker in self.workers + [self.version_worker, self.install_worker,
                                                       self.launch_worker, self.release_worker, self.content_worker,
                                                       self.banner_worker,
                                                       self.mod_search_worker, self.mod_install_worker,
                                                       self.modpack_worker,
                                                       self.local_import_worker, self.storage_worker]
                  if worker is not None and worker.isRunning()]
        if active:
            names = []
            for worker in active:
                if worker is self.install_worker:
                    names.append("установка Minecraft")
                elif worker is self.launch_worker:
                    names.append("проверка/запуск Minecraft")
                else:
                    names.append("сетевой запрос")
            self._notify(
                "Операция ещё выполняется",
                "Сначала дождись завершения: " + ", ".join(sorted(set(names))) +
                ". Это нужно, чтобы не повредить файлы игры.", "warning", 6500
            )
            event.ignore()
            return
        event.accept()