from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
import time
import psutil
from pathlib import Path, PurePosixPath

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
from minecraft_service import (VersionListWorker, LoaderVersionListWorker, InstallMinecraftWorker, LaunchWorker)
from profile_manager import ProfileManager, ProfileError
from appearance_manager import AppearanceManager, PRESETS, COLOR_LABELS
from toast_notifications import ToastNotification
from path_utils import migrate_game_data, resolve_launcher_root, selected_base_from_root
from version import LAUNCHER_NAME, LAUNCHER_VERSION, GITHUB_API_LATEST
from ui_controls import (WheelSafeComboBox, WheelSafeFontComboBox, WheelSafeSpinBox, WheelSafeSlider, WheelSafeTabBar)
from profile_dialog import CreateProfileDialog
from mod_service import (list_mod_files, set_mod_enabled, add_local_mods,
                         ModrinthSearchWorker, ModrinthProjectDetailsWorker,
                         ModrinthImageWorker, ModrinthInstallWorker)
from modpack_service import ModpackImportWorker, inspect_modpack
from instance_import import LocalVersionImportWorker
from storage_worker import StorageMigrationWorker
from version_metadata import read_version_metadata
from discord_presence import DiscordPresenceManager
from modrinth_ui import ModrinthCatalogWidget, ModrinthProjectDetailsDialog

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
        saved_profile_id = str(self.settings.value("profiles/active", ""))
        self.active_profile_id = saved_profile_id if self.profile_manager.get(saved_profile_id) else (
            profiles[0]["id"] if profiles else "")

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
        self.mod_details_worker = None
        self.mod_install_worker = None
        self.mod_image_workers = []
        self._mod_image_queue = []
        self._mod_image_active = 0
        self._mod_image_limit = 4
        self._mod_search_offset = 0
        self._mod_search_total = 0
        self._mod_search_filter_key = None
        self._mod_search_pending = False
        self.profile_loader_worker = None
        self._mod_install_project_type = "mod"
        self._pending_modpack_download = ""
        self._launch_button_in_use = None
        self._game_process_pid = 0
        self._discord_game_version = ""
        self._discord_game_nickname = ""
        self._discord_game_started_at = 0
        self.discord_presence = DiscordPresenceManager()
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
        self._configure_discord_presence()
        self.discord_monitor_timer = QTimer(self)
        self.discord_monitor_timer.setInterval(15000)
        self.discord_monitor_timer.timeout.connect(self._monitor_game_process)
        self.discord_monitor_timer.start()

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
            "show_snapshots": self.show_snapshots_check.isChecked() if hasattr(self, "show_snapshots_check") else False,
            "discord_enabled": self.discord_presence_check.isChecked() if hasattr(self, "discord_presence_check") else self.settings.value("discord/enabled", False, type=bool),
            "discord_client_id": self.discord_client_id_edit.text().strip() if hasattr(self, "discord_client_id_edit") else str(self.settings.value("discord/client_id", "")),
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
        hero.setMinimumHeight(190)
        row = QHBoxLayout(hero)
        row.setContentsMargins(30, 22, 28, 22)
        row.setSpacing(22)
        left = QVBoxLayout()
        left.setSpacing(9)
        eyebrow = QLabel("FIERCY GAME PLATFORM  /  YOUR WORLD")
        eyebrow.setObjectName("Eyebrow")
        title = QLabel("Время играть.")
        title.setObjectName("HeroTitle")
        copy = QLabel("Выбирай любую официальную версию Minecraft и запускай её. Для модов и сборок есть отдельные профили.")
        copy.setObjectName("HeroCopy")
        copy.setWordWrap(True)
        left.addWidget(eyebrow)
        left.addWidget(title)
        left.addWidget(copy)
        left.addStretch(1)
        chip = QLabel("●  ОБЫЧНЫЙ MINECRAFT — БЕЗ ПРОФИЛЕЙ")
        chip.setObjectName("Pill")
        left.addWidget(chip, 0, Qt.AlignLeft)
        row.addLayout(left, 1)
        emblem = QLabel("F")
        emblem.setObjectName("HeroEmblem")
        emblem.setAlignment(Qt.AlignCenter)
        emblem.setFixedSize(120, 120)
        row.addWidget(emblem)
        layout.addWidget(hero)

        install_card, install_box = self._section_card(
            "Выбор версии Minecraft",
            "Здесь устанавливается чистая Vanilla-версия. Профиль создавать не нужно. Можно выбрать старый или новый официальный релиз."
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        grid.addWidget(QLabel("Версия Minecraft"), 0, 0)
        version_row = QHBoxLayout()
        self.version_combo = WheelSafeComboBox()
        self.version_combo.setMinimumHeight(46)
        version_row.addWidget(self.version_combo, 1)
        self.refresh_versions_button = self._button("↻")
        self.refresh_versions_button.setFixedWidth(50)
        self.refresh_versions_button.setToolTip("Обновить официальный список релизов Minecraft")
        self.refresh_versions_button.clicked.connect(self.load_versions)
        version_row.addWidget(self.refresh_versions_button)
        grid.addLayout(version_row, 0, 1)
        grid.addWidget(QLabel("Тип установки"), 1, 0)
        vanilla_label = QLabel("Vanilla · без загрузчика модов")
        vanilla_label.setObjectName("StatusText")
        grid.addWidget(vanilla_label, 1, 1)
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
            "Выбери уже установленную версию и нажми «Играть». Этот запуск использует обычную папку игры, отдельно от профилей и сборок."
        )
        nick_row = QHBoxLayout()
        nick_row.addWidget(QLabel("Ник для игры"))
        self.nickname_edit = QLineEdit(str(self.settings.value("profile/nickname", "")))
        self.nickname_edit.setPlaceholderText("Ник Minecraft")
        self.nickname_edit.setMinimumHeight(40)
        nick_row.addWidget(self.nickname_edit, 1)
        play_box.addLayout(nick_row)
        play_row = QHBoxLayout()
        self.installed_combo = WheelSafeComboBox()
        self.installed_combo.setMinimumHeight(52)
        play_row.addWidget(self.installed_combo, 1)
        self.refresh_installed_button = self._button("↻")
        self.refresh_installed_button.setFixedWidth(50)
        self.refresh_installed_button.setToolTip("Обновить список установленных версий")
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

    def _build_profile_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._heading(
            "Instances, mods and custom builds", "Профили и моды",
            "Каждый профиль — отдельная сборка со своими модами, конфигами и мирами. Обычный Minecraft запускается без профиля на вкладке «Игра»."
        ))

        profiles_card, profiles_box = self._section_card(
            "Мои сборки",
            "Создай профиль, чтобы установить загрузчик и собирать собственный набор модов. Общие библиотеки Minecraft скачиваются только один раз."
        )
        row = QHBoxLayout()
        self.profiles_combo = WheelSafeComboBox()
        self.profiles_combo.currentIndexChanged.connect(self._profile_page_combo_changed)
        row.addWidget(self.profiles_combo, 1)
        create_profile_button = self._button("＋ Создать сборку", primary=True)
        create_profile_button.clicked.connect(self.create_profile)
        row.addWidget(create_profile_button)
        rename_profile_button = self._button("Переименовать")
        rename_profile_button.clicked.connect(self.rename_profile)
        row.addWidget(rename_profile_button)
        delete_profile_button = self._button("Удалить", danger=True)
        delete_profile_button.clicked.connect(self.delete_profile)
        row.addWidget(delete_profile_button)
        profiles_box.addLayout(row)
        self.profile_summary_label = QLabel("Сборка не выбрана")
        self.profile_summary_label.setObjectName("StatusText")
        profiles_box.addWidget(self.profile_summary_label)
        self.profile_folder_label = QLabel("")
        self.profile_folder_label.setObjectName("Muted")
        self.profile_folder_label.setWordWrap(True)
        profiles_box.addWidget(self.profile_folder_label)

        setup_grid = QGridLayout()
        setup_grid.setHorizontalSpacing(12)
        setup_grid.setVerticalSpacing(10)
        setup_grid.addWidget(QLabel("Minecraft"), 0, 0)
        self.profile_setup_version_combo = WheelSafeComboBox()
        self.profile_setup_version_combo.setMinimumHeight(40)
        setup_grid.addWidget(self.profile_setup_version_combo, 0, 1)
        setup_grid.addWidget(QLabel("Загрузчик"), 0, 2)
        self.profile_setup_loader_combo = WheelSafeComboBox()
        for label, value in (("Vanilla", "vanilla"), ("Fabric", "fabric"), ("Forge", "forge"),
                             ("NeoForge", "neoforge"), ("Quilt", "quilt")):
            self.profile_setup_loader_combo.addItem(label, value)
        self.profile_setup_loader_combo.setMinimumHeight(40)
        setup_grid.addWidget(self.profile_setup_loader_combo, 0, 3)
        setup_grid.addWidget(QLabel("Версия загрузчика"), 1, 0)
        self.profile_setup_loader_version_combo = WheelSafeComboBox()
        self.profile_setup_loader_version_combo.addItem("Последняя стабильная", "")
        self.profile_setup_loader_version_combo.setMinimumHeight(40)
        setup_grid.addWidget(self.profile_setup_loader_version_combo, 1, 1, 1, 3)
        profiles_box.addLayout(setup_grid)
        self.profile_setup_status = QLabel("Выбери сборку или создай новую.")
        self.profile_setup_status.setObjectName("Muted")
        self.profile_setup_status.setWordWrap(True)
        profiles_box.addWidget(self.profile_setup_status)

        setup_actions = QHBoxLayout()
        open_folder = self._button("Открыть папку сборки")
        open_folder.clicked.connect(self.open_profile_folder)
        setup_actions.addWidget(open_folder)
        self.save_profile_button = self._button("Сохранить параметры")
        self.save_profile_button.clicked.connect(self.save_profile_setup)
        setup_actions.addWidget(self.save_profile_button)
        self.install_profile_button = self._button("Установить / проверить сборку", primary=True)
        self.install_profile_button.clicked.connect(self.install_profile)
        setup_actions.addWidget(self.install_profile_button)
        self.play_profile_button = self._button("▶ Играть в сборку", primary=True)
        self.play_profile_button.clicked.connect(self.launch_active_profile)
        setup_actions.addWidget(self.play_profile_button)
        setup_actions.addStretch(1)
        profiles_box.addLayout(setup_actions)

        import_row = QHBoxLayout()
        import_version = self._button("Импортировать локальную версию")
        import_version.clicked.connect(self.import_local_version)
        import_row.addWidget(import_version)
        import_pack = self._button("Импортировать сборку .mrpack")
        import_pack.clicked.connect(self.import_modpack)
        import_row.addWidget(import_pack)
        import_map = self._button("Импортировать карту .zip")
        import_map.clicked.connect(self.import_map_zip)
        import_row.addWidget(import_map)
        open_versions = self._button("Общая папка versions")
        open_versions.clicked.connect(lambda: self._open_folder(self.game_root / "versions"))
        import_row.addWidget(open_versions)
        import_row.addStretch(1)
        profiles_box.addLayout(import_row)
        layout.addWidget(profiles_card)

        # Управление установленным содержимым и онлайн-каталог разделены по вкладкам:
        # каталог не прячется внизу длинной страницы вместе со списком файлов.
        self.profile_tabs = QTabWidget()
        self.profile_tabs.setObjectName("ProfileContentTabs")
        self.profile_tabs.setDocumentMode(True)
        self.profile_tabs.setTabBar(WheelSafeTabBar(self.profile_tabs))

        mods_page = QWidget()
        mods_page_layout = QVBoxLayout(mods_page)
        mods_page_layout.setContentsMargins(8, 10, 8, 8)
        mods_page_layout.setSpacing(8)
        mods_card, mods_box = self._section_card(
            "Содержимое сборки",
            "Включай, отключай и удаляй установленные моды. Остальные папки доступны из каталога или проводника."
        )
        mods_row = QHBoxLayout()
        self.profile_mod_list = QListWidget()
        self.profile_mod_list.setMinimumHeight(190)
        self.profile_mod_list.setMaximumHeight(260)
        self.profile_mod_list.setSpacing(3)
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
        open_mods = self._button("Папка mods")
        open_mods.clicked.connect(lambda: self._open_profile_subfolder("mods"))
        mod_buttons.addWidget(open_mods)
        open_resources = self._button("Ресурспаки")
        open_resources.clicked.connect(lambda: self._open_profile_subfolder("resourcepacks"))
        mod_buttons.addWidget(open_resources)
        open_shaders = self._button("Шейдеры")
        open_shaders.clicked.connect(lambda: self._open_profile_subfolder("shaderpacks"))
        mod_buttons.addWidget(open_shaders)
        mod_buttons.addStretch(1)
        mods_row.addLayout(mod_buttons)
        mods_box.addLayout(mods_row)
        self.mod_count_label = QLabel("Список модов пуст")
        self.mod_count_label.setObjectName("Muted")
        mods_box.addWidget(self.mod_count_label)
        mods_page_layout.addWidget(mods_card)
        mods_page_layout.addStretch(1)
        self.profile_tabs.addTab(mods_page, "Моя сборка")

        catalog_page = QWidget()
        catalog_page_layout = QVBoxLayout(catalog_page)
        catalog_page_layout.setContentsMargins(8, 10, 8, 8)
        catalog_page_layout.setSpacing(8)
        catalog_card, catalog_box = self._section_card(
            "Каталог Modrinth",
            "Популярные проекты загружаются автоматически. Выбери тип контента или введи название для поиска. API-ключ не нужен."
        )
        filters_row = QHBoxLayout()
        self.mod_content_type_combo = WheelSafeComboBox()
        self.mod_content_type_combo.addItem("Моды", "mod")
        self.mod_content_type_combo.addItem("Ресурспаки", "resourcepack")
        self.mod_content_type_combo.addItem("Шейдеры", "shader")
        self.mod_content_type_combo.addItem("Модпаки", "modpack")
        self.mod_content_type_combo.setMinimumHeight(38)
        filters_row.addWidget(self.mod_content_type_combo)
        self.mod_sort_combo = WheelSafeComboBox()
        self.mod_sort_combo.addItem("По загрузкам", "downloads")
        self.mod_sort_combo.addItem("Популярные", "follows")
        self.mod_sort_combo.addItem("Новинки", "newest")
        self.mod_sort_combo.addItem("Недавно обновлялись", "updated")
        self.mod_sort_combo.setMinimumHeight(38)
        filters_row.addWidget(self.mod_sort_combo)
        catalog_box.addLayout(filters_row)

        search_row = QHBoxLayout()
        self.mod_search_edit = QLineEdit()
        self.mod_search_edit.setPlaceholderText("Найти мод, ресурспак, шейдер или сборку…")
        self.mod_search_edit.setMinimumHeight(40)
        search_row.addWidget(self.mod_search_edit, 1)
        search_button = self._button("Найти", primary=True)
        search_button.clicked.connect(self.search_mods)
        search_row.addWidget(search_button)
        catalog_box.addLayout(search_row)

        self.mod_catalog_widget = ModrinthCatalogWidget()
        self.mod_catalog_widget.projectDetailsRequested.connect(self.open_modrinth_project_details)
        self.mod_catalog_widget.installRequested.connect(self.install_modrinth_project)
        self.mod_catalog_widget.loadMoreRequested.connect(lambda: self.search_mods(load_more=True))
        catalog_box.addWidget(self.mod_catalog_widget, 1)
        self.mod_status_label = QLabel("Выбери профиль, чтобы увидеть совместимые проекты.")
        self.mod_status_label.setObjectName("Muted")
        self.mod_status_label.setWordWrap(True)
        catalog_box.addWidget(self.mod_status_label)
        catalog_page_layout.addWidget(catalog_card, 1)
        self.profile_tabs.addTab(catalog_page, "Каталог модов")
        layout.addWidget(self.profile_tabs, 1)

        self.mod_search_timer = QTimer(self)
        self.mod_search_timer.setSingleShot(True)
        self.mod_search_timer.setInterval(450)
        self.mod_search_timer.timeout.connect(self.search_mods)
        self.mod_search_edit.textChanged.connect(lambda _text: self.mod_search_timer.start())
        self.mod_search_edit.returnPressed.connect(self.search_mods)
        self.mod_content_type_combo.currentIndexChanged.connect(self._catalog_filters_changed)
        self.mod_sort_combo.currentIndexChanged.connect(self._catalog_filters_changed)
        self.profile_tabs.currentChanged.connect(self._profile_content_tab_changed)
        self.profile_setup_version_combo.currentIndexChanged.connect(self._profile_setup_version_changed)
        self.profile_setup_loader_combo.currentIndexChanged.connect(self._profile_setup_loader_changed)
        self.profile_setup_loader_version_combo.currentIndexChanged.connect(self._profile_setup_changed)
        scroll.setWidget(page)
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

        discord_card, discord_box = self._section_card(
            "Discord Rich Presence",
            "Показывает в Discord, что ты используешь FiercyLauncher, а при запуске игры — версию Minecraft. Нужен запущенный Discord для Windows и Application ID приложения из Discord Developer Portal."
        )
        self.discord_presence_check = QCheckBox("Включить отображение статуса в Discord")
        self.discord_presence_check.setChecked(self.settings.value("discord/enabled", False, type=bool))
        discord_box.addWidget(self.discord_presence_check)
        discord_id_row = QHBoxLayout()
        discord_id_row.addWidget(QLabel("Discord Application ID"))
        self.discord_client_id_edit = QLineEdit(str(self.settings.value("discord/client_id", "")))
        self.discord_client_id_edit.setPlaceholderText("Общий Application ID FiercyLauncher")
        discord_id_row.addWidget(self.discord_client_id_edit, 1)
        discord_box.addLayout(discord_id_row)
        discord_hint = QLabel("В Discord включи: Настройки → Конфиденциальность действий → Показывать текущую активность. Веб-версия Discord не поддерживает локальный Rich Presence.")
        discord_hint.setObjectName("Muted")
        discord_hint.setWordWrap(True)
        discord_box.addWidget(discord_hint)
        layout.addWidget(discord_card)

        versions_card, versions_box = self._section_card(
            "Список версий Minecraft",
            "Снапшоты, тестовые сборки и предварительные версии скрыты по умолчанию. Их можно включить здесь, когда понадобится."
        )
        self.show_snapshots_check = QCheckBox("Показывать снапшоты и предварительные версии Minecraft")
        self.show_snapshots_check.setChecked(self.settings.value("versions/show_snapshots", False, type=bool))
        self.show_snapshots_check.toggled.connect(self._version_snapshot_setting_changed)
        versions_box.addWidget(self.show_snapshots_check)
        layout.addWidget(versions_card)

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


    def _build_fiercycity_page(self):
        scroll, layout = self._scroll_page()
        layout.addWidget(self._heading(
            "FiercyCity platform", "FiercyCity",
            "Отдельное пространство для официальных сборок FiercyCity. Каталог можно подключить позднее, когда появится серверная часть."
        ))
        hero, box = self._section_card(
            "FiercyCity — каталог сборок",
            "Раздел подготовлен в интерфейсе. Сейчас сборки можно импортировать из Modrinth .mrpack или локальной папки другого лаунчера."
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
        open_game_logs.clicked.connect(self.open_game_logs_folder)
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
            if self._has_active_profile() and self.profile_tabs.currentIndex() == 1:
                self.search_mods()
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
        self.install_status.setText("Загружаем официальный список версий Minecraft…")
        self._set_status("загрузка версий")
        self.version_worker = VersionListWorker()
        self.version_worker.loaded.connect(self._versions_loaded)
        self.version_worker.failed.connect(self._versions_failed)
        self.version_worker.start()

    def _versions_loaded(self, versions: list):
        self.refresh_versions_button.setEnabled(True)
        self.versions = versions
        self._populate_version_combo()
        hidden_snapshots = not self.show_snapshots_check.isChecked()
        suffix = " Снапшоты скрыты — включи их в настройках." if hidden_snapshots else ""
        self.install_status.setText(
            f"Доступно версий Minecraft: {self.version_combo.count()}.{suffix}"
        )
        self._set_status("список версий загружен")
        if self._has_active_profile():
            self._refresh_profile_setup_controls()
            if self.stack.currentIndex() == 1 and self.profile_tabs.currentIndex() == 1:
                self.search_mods()

    @staticmethod
    def _version_display_name(item: dict) -> str:
        version_id = str(item.get("id", "")).strip()
        labels = {
            "release": "Релиз",
            "snapshot": "Снапшот",
            "old_beta": "Старая Beta",
            "old_alpha": "Старая Alpha",
        }
        kind = labels.get(str(item.get("type", "")).lower(), "Версия")
        return f"{version_id}  ·  {kind}" if version_id else ""

    def _visible_minecraft_versions(self) -> list[dict]:
        show_snapshots = bool(
            self.show_snapshots_check.isChecked()
            if hasattr(self, "show_snapshots_check")
            else self.settings.value("versions/show_snapshots", False, type=bool)
        )
        return [
            item for item in self.versions
            if show_snapshots or str(item.get("type", "")).lower() != "snapshot"
        ]

    def _populate_version_combo(self):
        visible_versions = self._visible_minecraft_versions()
        previous = str(self.version_combo.currentData() or "")
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for item in visible_versions:
            version_id = str(item.get("id", ""))
            if version_id:
                self.version_combo.addItem(self._version_display_name(item), version_id)
        preferred = previous or str(self.remote_versions_config.get("recommended_minecraft_version", ""))
        if not preferred:
            preferred = str(self.remote_launcher_config.get("recommended_minecraft_version", ""))
        index = self.version_combo.findData(preferred)
        if index < 0:
            release = next((item for item in visible_versions
                            if str(item.get("type", "")).lower() == "release"), None)
            fallback = str(release.get("id", "")) if release else (
                str(visible_versions[0].get("id", "")) if visible_versions else "")
            index = self.version_combo.findData(fallback)
        if index >= 0:
            self.version_combo.setCurrentIndex(index)
        elif not self.version_combo.count():
            self.version_combo.addItem("Нет доступных версий", None)
        self.version_combo.blockSignals(False)

        if hasattr(self, "profile_setup_version_combo"):
            profile_previous = str(self.profile_setup_version_combo.currentData() or "")
            self.profile_setup_version_combo.blockSignals(True)
            self.profile_setup_version_combo.clear()
            for item in visible_versions:
                version_id = str(item.get("id", ""))
                if version_id:
                    self.profile_setup_version_combo.addItem(self._version_display_name(item), version_id)
            profile = self._active_profile()
            preferred_profile = str(profile.get("minecraft_version", "") or profile_previous or "")
            profile_pos = self.profile_setup_version_combo.findData(preferred_profile)
            if profile_pos < 0:
                release = next((item for item in visible_versions
                                if str(item.get("type", "")).lower() == "release"), None)
                fallback = str(release.get("id", "")) if release else (
                    str(visible_versions[0].get("id", "")) if visible_versions else "")
                profile_pos = self.profile_setup_version_combo.findData(fallback)
            if profile_pos >= 0:
                self.profile_setup_version_combo.setCurrentIndex(profile_pos)
            elif not self.profile_setup_version_combo.count():
                self.profile_setup_version_combo.addItem("Нет доступных версий", None)
            self.profile_setup_version_combo.blockSignals(False)

    def _version_snapshot_setting_changed(self, checked: bool):
        if not getattr(self, "_settings_tracking_ready", False):
            # The setting widget is created before tracking is connected.
            pass
        if self.versions:
            self._populate_version_combo()
            total = self.version_combo.count()
            if checked:
                self.install_status.setText(f"Доступно версий Minecraft: {total}, включая снапшоты.")
            else:
                self.install_status.setText(f"Доступно стабильных и старых версий: {total}. Снапшоты скрыты.")
            if self._has_active_profile():
                self._profile_setup_changed()
        self._settings_changed()

    def _versions_failed(self, message: str):
        self.refresh_versions_button.setEnabled(True)
        self.install_status.setText(message)
        self._set_status("ошибка сети")
        self._notify("Не удалось загрузить версии", message, "error", 6500)

    def _active_profile(self) -> dict:
        return self.profile_manager.get(self.active_profile_id) or {}

    def _has_active_profile(self) -> bool:
        return bool(self.active_profile_id and self.profile_manager.get(self.active_profile_id))

    def _instance_dir(self) -> Path:
        if not self._has_active_profile():
            return self.game_root / "instances" / ".no-profile-selected"
        return self.profile_manager.instance_dir(self.active_profile_id)

    def _profile_game_root(self) -> Path:
        profile = self._active_profile()
        if not profile or str(profile.get("storage_root", "shared")) == "shared":
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
        """Главный экран показывает версии из обычной общей папки Minecraft."""
        if not hasattr(self, "installed_combo"):
            return
        previous = self.installed_combo.currentData()
        self.installed_combo.blockSignals(True)
        self.installed_combo.clear()
        try:
            items = mll.utils.get_installed_versions(str(self.game_root))
        except Exception:
            items = []
            versions_path = self.game_root / "versions"
            if versions_path.is_dir():
                for folder in versions_path.iterdir():
                    if (folder / f"{folder.name}.json").is_file():
                        items.append({"id": folder.name})
        for item in items:
            version_id = item.get("id") if isinstance(item, dict) else str(item)
            if not version_id:
                continue
            # Каталог главной страницы — только обычные Vanilla-версии.
            # Forge/Fabric/NeoForge/Quilt устанавливаются и запускаются из профилей.
            try:
                metadata = read_version_metadata(self.game_root, str(version_id))
                if metadata.get("loader") != "vanilla":
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                # Не показываем сомнительную запись, если metadata повреждены.
                continue
            self.installed_combo.addItem(str(version_id), str(version_id))
        pos = self.installed_combo.findData(previous)
        if pos >= 0:
            self.installed_combo.setCurrentIndex(pos)
        if not self.installed_combo.count():
            self.installed_combo.addItem("Нет установленных версий", None)
        self.installed_combo.blockSignals(False)

    def _installed_version_changed(self, index: int):
        # Список обычной игры не изменяет выбранный профиль сборки.
        return

    def _start_install(self, minecraft_version: str, loader: str, loader_version: str,
                       profile_id: str = "", instance_dir: Path | None = None):
        if self.install_worker and self.install_worker.isRunning():
            return
        java_path = self.java_edit.text().strip() if hasattr(self, "java_edit") else str(
            self.settings.value("game/java_path", ""))
        auto_java = self.auto_java_check.isChecked() if hasattr(self, "auto_java_check") else self.settings.value(
            "game/auto_java", True, type=bool)
        self.install_button.setEnabled(False)
        if hasattr(self, "install_profile_button"):
            self.install_profile_button.setEnabled(False)
        self.install_progress.setValue(0)
        self.install_progress.show()
        self.install_status.setText("Подготовка к установке…")
        self._set_status("установка Minecraft")
        self._install_profile_id = profile_id
        profile = self.profile_manager.get(profile_id) if profile_id else {"name": "Обычный Minecraft"}
        target_instance = instance_dir or self.game_root
        self.install_worker = InstallMinecraftWorker(
            profile or {}, str(self.game_root), str(target_instance), str(minecraft_version),
            loader or "vanilla", loader_version or "", java_path, auto_java,
        )
        self.install_worker.progress.connect(self._install_progress)
        self.install_worker.completed.connect(self._install_completed)
        self.install_worker.failed.connect(self._install_failed)
        self.install_worker.start()

    def install_selected(self):
        """Ставит чистую Vanilla-версию в обычный каталог игры, без профиля."""
        if self.maintenance_mode or self.minimum_version_block:
            self._notify("Установка временно недоступна", self._policy_message(), "warning", 6500)
            return
        version = self.version_combo.currentData()
        if not version:
            self._notify("Список версий загружается",
                         "Подожди завершения загрузки списка Minecraft и повтори установку.", "info")
            return
        self._start_install(str(version), "vanilla", "", profile_id="", instance_dir=self.game_root)

    def install_profile(self):
        if self.maintenance_mode or self.minimum_version_block:
            self._notify("Установка временно недоступна", self._policy_message(), "warning", 6500)
            return
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Сначала создай или выбери профиль.", "info")
            return
        if self.install_worker and self.install_worker.isRunning():
            return
        self.save_profile_setup(show_notice=False)
        profile = self._active_profile()
        version = str(profile.get("minecraft_version", "")).strip()
        if not version:
            self._notify("Не выбрана версия Minecraft", "Выбери Minecraft в параметрах сборки.", "warning")
            return
        self._start_install(version, str(profile.get("loader", "vanilla")),
                            str(profile.get("loader_version", "")), self.active_profile_id,
                            self._instance_dir())

    def _install_progress(self, percent: int, status: str):
        if percent >= 0:
            self.install_progress.setValue(percent)
        self.install_status.setText(status)
        if hasattr(self, "profile_setup_status") and self._install_profile_id:
            self.profile_setup_status.setText(status)
        self._set_status(status)

    def _install_completed(self, result: dict):
        self.install_button.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        if hasattr(self, "install_profile_button"):
            self.install_profile_button.setEnabled(
                self._has_active_profile() and not (self.maintenance_mode or self.minimum_version_block))
        self.install_progress.setValue(100)
        self.install_status.setText(f"Готово: {result['installed_version_id']}")
        profile_id = self._install_profile_id
        self._install_profile_id = None
        if profile_id and self.profile_manager.get(profile_id):
            self.profile_manager.update(
                profile_id,
                minecraft_version=result["minecraft_version"], loader=result["loader"],
                loader_version=result["loader_version"], installed_version_id=result["installed_version_id"],
                storage_root="shared",
            )
            if hasattr(self, "profile_setup_status") and profile_id == self.active_profile_id:
                self.profile_setup_status.setText(f"Сборка установлена: {result['installed_version_id']}")
        self._refresh_profiles()
        self._refresh_installed_versions()
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        log.info("Установка завершена; profile=%s result=%s", profile_id or "обычная игра", result)
        self._set_status("установка завершена")

    def _install_failed(self, message: str):
        self.install_button.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        if hasattr(self, "install_profile_button"):
            self.install_profile_button.setEnabled(
                self._has_active_profile() and not (self.maintenance_mode or self.minimum_version_block))
        self.install_status.setText("Установка не завершена. Подробности в логах.")
        if hasattr(self, "profile_setup_status") and self._install_profile_id:
            self.profile_setup_status.setText("Установка не завершена. Подробности в логах.")
        self._install_profile_id = None
        self._set_status("ошибка установки")
        self._notify("Не удалось установить Minecraft", message, "error", 8000)

    def _build_launch_account(self) -> dict | None:
        nickname = self.nickname_edit.text().strip() if hasattr(self, "nickname_edit") else ""
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nickname):
            self._notify("Проверь никнейм",
                         "Ник должен содержать 3–16 символов: только латинские буквы, цифры и подчёркивание.",
                         "warning")
            if hasattr(self, "nickname_edit"):
                self.nickname_edit.setFocus()
            return None
        md5 = hashlib.md5(f"OfflinePlayer:{nickname}".encode("utf-8")).digest()
        offline_uuid = str(uuid.UUID(bytes=md5, version=3)).replace("-", "")
        account = {"name": nickname, "id": offline_uuid, "access_token": "0", "user_type": "legacy"}
        self.settings.setValue("profile/nickname", nickname)
        self.settings.sync()
        return account

    def launch_selected(self):
        if self.maintenance_mode or self.minimum_version_block:
            self._notify("Запуск временно недоступен", self._policy_message(), "warning", 6500)
            return
        version_id = self.installed_combo.currentData()
        if not version_id:
            self._notify("Нет установленной версии", "Сначала установи Minecraft без профиля.", "warning")
            return
        account = self._build_launch_account()
        if account:
            self._start_launch(str(version_id), self.game_root, self.game_root, account, self.play_button)

    def launch_active_profile(self):
        if self.maintenance_mode or self.minimum_version_block:
            self._notify("Запуск временно недоступен", self._policy_message(), "warning", 6500)
            return
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Сначала создай или выбери профиль.", "info")
            return
        self.save_profile_setup(show_notice=False)
        profile = self._active_profile()
        version_id = str(profile.get("installed_version_id", "")).strip()
        if not version_id:
            self._notify("Сборка ещё не установлена", "Нажми «Установить / проверить сборку».", "warning")
            return
        account = self._build_launch_account()
        if account:
            self._start_launch(version_id, self._profile_game_root(), self._instance_dir(), account,
                               self.play_profile_button)

    def _start_launch(self, version_id: str, game_root: Path, instance_dir: Path,
                      account: dict, button: QPushButton):
        if self.launch_worker and self.launch_worker.isRunning():
            return
        ram = self.ram_spin.value() if hasattr(self, "ram_spin") else int(self.settings.value("game/ram", 4))
        java_path = self.java_edit.text().strip() if hasattr(self, "java_edit") else str(
            self.settings.value("game/java_path", ""))
        auto_java = self.auto_java_check.isChecked() if hasattr(self, "auto_java_check") else self.settings.value(
            "game/auto_java", True, type=bool)
        jvm_arguments = self.jvm_edit.text().strip() if hasattr(self, "jvm_edit") else str(
            self.settings.value("game/jvm_arguments", ""))
        button.setEnabled(False)
        self._launch_button_in_use = button
        self._discord_game_version = str(version_id)
        self._discord_game_nickname = str(account.get("name", "Minecraft player"))
        self._set_status("запуск Minecraft")
        self.launch_worker = LaunchWorker(
            str(game_root), str(instance_dir), str(version_id), account, ram,
            java_path, auto_java, jvm_arguments,
        )
        self.launch_worker.progress.connect(self._set_status)
        self.launch_worker.started_game.connect(self._game_started)
        self.launch_worker.game_process_started.connect(self._game_process_started)
        self.launch_worker.failed.connect(self._launch_failed)
        self.launch_worker.start()

    def _configure_discord_presence(self):
        enabled = self.discord_presence_check.isChecked() if hasattr(self, "discord_presence_check") else self.settings.value("discord/enabled", False, type=bool)
        client_id = self.discord_client_id_edit.text().strip() if hasattr(self, "discord_client_id_edit") else str(self.settings.value("discord/client_id", "")).strip()
        self.discord_presence.configure(bool(enabled), client_id)
        if self._game_process_pid and psutil.pid_exists(self._game_process_pid):
            self._publish_game_presence()
        else:
            self.discord_presence.set_idle()

    def _game_process_started(self, pid: int):
        self._game_process_pid = int(pid)
        self._discord_game_started_at = int(time.time() * 1000)
        self._publish_game_presence()

    def _publish_game_presence(self):
        if self._game_process_pid and self._discord_game_version:
            self.discord_presence.set_playing(
                self._discord_game_version,
                self._discord_game_nickname,
                self._discord_game_started_at or None,
            )

    def _monitor_game_process(self):
        if self._game_process_pid:
            if psutil.pid_exists(self._game_process_pid):
                self._publish_game_presence()
            else:
                self._game_process_pid = 0
                self._discord_game_version = ""
                self._discord_game_nickname = ""
                self._discord_game_started_at = 0
                self.discord_presence.set_idle()
        else:
            self.discord_presence.set_idle()

    def _game_started(self, message: str):
        if self._launch_button_in_use is not None:
            self._launch_button_in_use.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        self._launch_button_in_use = None
        self._publish_game_presence()
        self._set_status("Minecraft запущен")
        self.install_status.setText(message)
        log.info(message)

    def _launch_failed(self, message: str):
        if self._launch_button_in_use is not None:
            self._launch_button_in_use.setEnabled(not (self.maintenance_mode or self.minimum_version_block))
        self._launch_button_in_use = None
        self._game_process_pid = 0
        self._discord_game_version = ""
        self._discord_game_nickname = ""
        self._discord_game_started_at = 0
        self.discord_presence.set_idle()
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
            self.settings.setValue("versions/show_snapshots", self.show_snapshots_check.isChecked())
            self.settings.setValue("discord/enabled", self.discord_presence_check.isChecked())
            self.settings.setValue("discord/client_id", self.discord_client_id_edit.text().strip())
            self.settings.setValue("profile/nickname", self.nickname_edit.text().strip())
            self.appearance_geometry = self._collect_appearance_geometry()
            mode = str(self.theme_combo.currentData() or "custom")
            self.appearance_manager.save(mode, self.appearance_palette, self.appearance_geometry)
            self.settings.sync()
            self._configure_discord_presence()

            if new_root != old_root:
                previous_profile_id = self.active_profile_id
                self.game_root = new_root
                self.profile_manager = ProfileManager(self.game_root)
                profiles = self.profile_manager.all()
                self.active_profile_id = (previous_profile_id if self.profile_manager.get(previous_profile_id)
                                          else (profiles[0]["id"] if profiles else ""))
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
        combo = getattr(self, "profiles_combo", None)
        if combo is not None:
            combo.blockSignals(True)
            combo.clear()
            for profile in profiles:
                combo.addItem(profile["name"], profile["id"])
            if not profiles:
                combo.addItem("Сборки пока не созданы", None)
            pos = combo.findData(self.active_profile_id)
            if pos >= 0:
                combo.setCurrentIndex(pos)
            combo.blockSignals(False)
        has_profile = self._has_active_profile()
        for name in ("rename_profile_button", "delete_profile_button", "save_profile_button",
                     "install_profile_button", "play_profile_button"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(has_profile)
        if hasattr(self, "create_profile_button"):
            self.create_profile_button.setEnabled(True)
        self._update_profile_folder_label()
        if hasattr(self, "profile_mod_list"):
            self._refresh_mod_list()

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
        self._refresh_profile_setup_controls()
        self._refresh_mod_list()
        if self.stack.currentIndex() == 1:
            self.search_mods()

    @staticmethod
    def _set_combo_value(combo, value: str):
        pos = combo.findData(value)
        if pos < 0 and value:
            combo.addItem(value, value)
            pos = combo.findData(value)
        if pos >= 0:
            combo.setCurrentIndex(pos)

    def _refresh_profile_setup_controls(self):
        if not hasattr(self, "profile_setup_version_combo"):
            return
        profile = self._active_profile()
        self.profile_setup_version_combo.blockSignals(True)
        if not self.profile_setup_version_combo.count():
            for item in self._visible_minecraft_versions():
                version_id = str(item.get("id", ""))
                if version_id:
                    self.profile_setup_version_combo.addItem(self._version_display_name(item), version_id)
        saved_minecraft = str(profile.get("minecraft_version", ""))
        saved_index = self.profile_setup_version_combo.findData(saved_minecraft)
        if saved_index >= 0:
            self.profile_setup_version_combo.setCurrentIndex(saved_index)
        elif self.profile_setup_version_combo.count():
            release = next((item for item in self._visible_minecraft_versions()
                            if str(item.get("type", "")).lower() == "release"), None)
            fallback = str(release.get("id", "")) if release else ""
            fallback_index = self.profile_setup_version_combo.findData(fallback)
            if fallback_index >= 0:
                self.profile_setup_version_combo.setCurrentIndex(fallback_index)
        self.profile_setup_version_combo.blockSignals(False)
        self.profile_setup_loader_combo.blockSignals(True)
        self._set_combo_value(self.profile_setup_loader_combo, str(profile.get("loader", "vanilla")))
        self.profile_setup_loader_combo.blockSignals(False)
        self._load_profile_loader_versions(preserve=str(profile.get("loader_version", "")))
        self._update_profile_folder_label()

    def _profile_setup_version_changed(self, *_args):
        self._load_profile_loader_versions()
        self._profile_setup_changed()

    def _profile_setup_loader_changed(self, *_args):
        self._load_profile_loader_versions()
        self._profile_setup_changed()

    def _profile_setup_changed(self, *_args):
        if not hasattr(self, "profile_setup_status") or not self._has_active_profile():
            return
        profile = self._active_profile()
        selected = {
            "minecraft_version": str(self.profile_setup_version_combo.currentData() or ""),
            "loader": str(self.profile_setup_loader_combo.currentData() or "vanilla"),
            "loader_version": str(self.profile_setup_loader_version_combo.currentData() or ""),
        }
        if any(str(profile.get(key, "")) != value for key, value in selected.items()):
            self.profile_setup_status.setText("Есть несохранённые параметры сборки. Нажми «Сохранить параметры».")

    def _load_profile_loader_versions(self, preserve: str = ""):
        if not hasattr(self, "profile_setup_loader_version_combo"):
            return
        loader = str(self.profile_setup_loader_combo.currentData() or "vanilla")
        minecraft = str(self.profile_setup_version_combo.currentData() or "")
        current = preserve or str(self.profile_setup_loader_version_combo.currentData() or "")
        self.profile_setup_loader_version_combo.blockSignals(True)
        self.profile_setup_loader_version_combo.clear()
        self.profile_setup_loader_version_combo.addItem("Последняя стабильная", "")
        self.profile_setup_loader_version_combo.blockSignals(False)
        self.profile_setup_loader_version_combo.setEnabled(loader != "vanilla")
        if loader == "vanilla":
            self.profile_setup_status.setText("Vanilla не требует отдельной версии загрузчика.")
            return
        if not minecraft:
            self.profile_setup_status.setText("Сначала выбери версию Minecraft.")
            return
        self.profile_setup_status.setText(f"Загружаем версии {loader.title()} для Minecraft {minecraft}…")
        worker = LoaderVersionListWorker(loader, minecraft, self)
        worker.loaded.connect(self._profile_loader_versions_loaded)
        worker.failed.connect(self._profile_loader_versions_failed)
        self._retain_worker(worker)
        self.profile_loader_worker = worker
        worker.start()
        if current:
            # Preserve a value from imported profiles even if the API no longer lists it.
            self.profile_setup_loader_version_combo.addItem(current, current)
            self.profile_setup_loader_version_combo.setCurrentIndex(
                self.profile_setup_loader_version_combo.findData(current))

    def _profile_loader_versions_loaded(self, loader: str, minecraft: str, versions: list):
        if (loader != str(self.profile_setup_loader_combo.currentData() or "vanilla")
                or minecraft != str(self.profile_setup_version_combo.currentData() or "")):
            return
        current = str(self.profile_setup_loader_version_combo.currentData() or "")
        self.profile_setup_loader_version_combo.blockSignals(True)
        self.profile_setup_loader_version_combo.clear()
        self.profile_setup_loader_version_combo.addItem("Последняя стабильная", "")
        for version in versions:
            self.profile_setup_loader_version_combo.addItem(str(version), str(version))
        if current and self.profile_setup_loader_version_combo.findData(current) < 0:
            self.profile_setup_loader_version_combo.addItem(current, current)
        pos = self.profile_setup_loader_version_combo.findData(current)
        self.profile_setup_loader_version_combo.setCurrentIndex(pos if pos >= 0 else 0)
        self.profile_setup_loader_version_combo.blockSignals(False)
        self.profile_setup_loader_version_combo.setEnabled(bool(versions) or bool(current))
        self.profile_setup_status.setText(
            f"Доступно стабильных версий {loader.title()}: {len(versions)}. Можно оставить последнюю стабильную."
            if versions else "Список версий пуст. Попробуй другую версию Minecraft или оставь выбор «последняя стабильная»."
        )

    def _profile_loader_versions_failed(self, loader: str, minecraft: str, message: str):
        if (loader != str(self.profile_setup_loader_combo.currentData() or "vanilla")
                or minecraft != str(self.profile_setup_version_combo.currentData() or "")):
            return
        self.profile_setup_loader_version_combo.setEnabled(True)
        self.profile_setup_status.setText(f"Не удалось загрузить список версий загрузчика: {message}")

    def save_profile_setup(self, show_notice: bool = True) -> bool:
        if not self._has_active_profile():
            if show_notice:
                self._notify("Сборка не выбрана", "Сначала создай профиль сборки.", "info")
            return False
        minecraft = str(self.profile_setup_version_combo.currentData() or "").strip()
        loader = str(self.profile_setup_loader_combo.currentData() or "vanilla").strip().lower()
        loader_version = str(self.profile_setup_loader_version_combo.currentData() or "").strip()
        if not minecraft:
            if show_notice:
                self._notify("Не выбрана версия Minecraft", "Укажи версию Minecraft для сборки.", "warning")
            return False
        try:
            self.profile_manager.update(self.active_profile_id, minecraft_version=minecraft,
                                        loader=loader, loader_version=loader_version)
            self._update_profile_folder_label()
            self.profile_setup_status.setText("Параметры сборки сохранены.")
            if show_notice:
                self._notify("Параметры сохранены", f"Minecraft {minecraft} · {loader.title()} {loader_version or 'latest'}",
                             "success")
            return True
        except Exception as exc:
            self._notify("Не удалось сохранить параметры сборки", str(exc), "error")
            return False

    def _update_profile_folder_label(self):
        profile = self._active_profile()
        has_profile = self._has_active_profile()
        if hasattr(self, "profile_folder_label"):
            if has_profile:
                self.profile_folder_label.setText(
                    f"Папка сборки: {self._instance_dir()}\nОбщие версии и библиотеки: {self._profile_game_root()}")
            else:
                self.profile_folder_label.setText("Профили необязательны. Создай сборку только тогда, когда нужны отдельные моды и настройки.")
        if hasattr(self, "profile_summary_label"):
            if has_profile:
                self.profile_summary_label.setText(
                    f"Сборка: {profile.get('name', 'Без названия')}  ·  "
                    f"Minecraft: {profile.get('minecraft_version') or 'не выбрана'}  ·  "
                    f"Загрузчик: {str(profile.get('loader', 'vanilla')).title()}"
                )
            else:
                self.profile_summary_label.setText("Пока нет пользовательских сборок")
        for name in ("profile_setup_version_combo", "profile_setup_loader_combo",
                     "profile_setup_loader_version_combo"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(has_profile and (name != "profile_setup_loader_version_combo"
                                                    or str(self.profile_setup_loader_combo.currentData()) != "vanilla"))

    def create_profile(self):
        if not self.versions:
            self._notify("Список Minecraft ещё загружается", "Подожди, пока обновится список официальных версий.",
                         "info")
            return
        dialog = CreateProfileDialog(self._visible_minecraft_versions(), self)
        dialog.setStyleSheet(self.styleSheet())
        if dialog.exec_() != dialog.Accepted:
            return
        values = dialog.values()
        try:
            profile = self.profile_manager.create(
                values["name"], description="Игровая сборка FiercyLauncher",
                minecraft_version=values["minecraft_version"], loader=values["loader"],
                loader_version=values["loader_version"], storage_root="shared", kind="custom",
            )
            self._set_active_profile(profile["id"])
            self.profile_setup_status.setText("Сборка создана. Начинается установка выбранной версии и загрузчика…")
            self._notify("Сборка создана", f"«{values['name']}» · Minecraft {values['minecraft_version']} · {values['loader']}",
                         "success", 5000)
            self.install_profile()
        except ProfileError as exc:
            self._notify("Не удалось создать сборку", str(exc), "error")

    # -------------------------- Mods and imports --------------------------
    def _refresh_mod_list(self):
        if not hasattr(self, "profile_mod_list"):
            return
        self.profile_mod_list.clear()
        if not self._has_active_profile():
            self.mod_count_label.setText("Создай или выбери сборку, чтобы управлять её содержимым.")
            return
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
        resource_count = len(list((self._instance_dir() / "resourcepacks").glob("*"))) if (self._instance_dir() / "resourcepacks").exists() else 0
        shader_count = len(list((self._instance_dir() / "shaderpacks").glob("*"))) if (self._instance_dir() / "shaderpacks").exists() else 0
        map_count = len([path for path in (self._instance_dir() / "saves").iterdir() if path.is_dir()]) if (self._instance_dir() / "saves").exists() else 0
        self.mod_count_label.setText(
            f"Моды: {len(mods)} (включено {enabled}, отключено {disabled})  ·  "
            f"Ресурспаки: {resource_count}  ·  Шейдеры: {shader_count}  ·  Карты: {map_count}  ·  "
            f"Папка mods: {self._instance_dir() / 'mods'}")

    def _toggle_mod_item(self, item):
        if item:
            self.profile_mod_list.setCurrentItem(item)
            self.toggle_selected_mod()

    def toggle_selected_mod(self):
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Сначала создай профиль.", "info")
            return
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
        if not self._has_active_profile():
            return
        item = self.profile_mod_list.currentItem()
        if item is None:
            self._notify("Мод не выбран", "Сначала выбери мод.", "info")
            return
        info = item.data(Qt.UserRole) or {}
        path = Path(info.get("path", ""))
        try:
            if path.parent.resolve() != (self._instance_dir() / "mods").resolve() or not path.is_file():
                raise ValueError("Выбранный файл больше не находится в папке mods этой сборки.")
            path.unlink()
            self._notify("Мод удалён", path.name, "success")
            self._refresh_mod_list()
        except Exception as exc:
            self._notify("Не удалось удалить мод", str(exc), "error")

    def add_local_mod_files(self):
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Сначала создай профиль сборки.", "info")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Добавить локальные JAR-моды", "", "Minecraft mods (*.jar)")
        if not files:
            return
        try:
            added = add_local_mods(self._instance_dir(), files)
            self._refresh_mod_list()
            self._notify("Моды добавлены", f"Добавлено файлов: {len(added)}", "success")
        except Exception as exc:
            self._notify("Не удалось добавить моды", str(exc), "error")

    def _open_profile_subfolder(self, name: str):
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Сначала создай профиль сборки.", "info")
            return
        allowed = {"mods", "config", "resourcepacks", "shaderpacks", "saves", "screenshots"}
        if name not in allowed:
            return
        self._open_folder(self._instance_dir() / name)

    def _current_mod_compatibility(self) -> tuple[str, str]:
        profile = self._active_profile()
        return str(profile.get("minecraft_version", "")).strip(), str(profile.get("loader", "vanilla")).lower()

    def _profile_content_tab_changed(self, index: int):
        if index == 1 and self.stack.currentIndex() == 1 and self._has_active_profile():
            self.search_mods()

    def _catalog_filters_changed(self, *_args):
        if self.profile_tabs.currentIndex() == 1:
            self.search_mods()

    def search_mods(self, *_args, load_more: bool = False):
        if not hasattr(self, "mod_catalog_widget"):
            return
        if self.profile_tabs.currentIndex() != 1:
            return
        if not self._has_active_profile():
            self.mod_catalog_widget.clear("Сначала создай или выбери профиль сборки.")
            self.mod_status_label.setText("Каталог показывает проекты для выбранного профиля.")
            return
        if self.mod_search_worker and self.mod_search_worker.isRunning():
            # При изменении фильтра после окончания текущего запроса выполняем
            # новый поиск, а не теряем действие пользователя.
            self._mod_search_pending = not load_more
            return
        game_version, loader = self._current_mod_compatibility()
        if not game_version:
            self.mod_catalog_widget.clear("Сначала выбери и сохрани версию Minecraft в параметрах сборки.")
            self.mod_status_label.setText("Для каталога нужна версия Minecraft в текущем профиле.")
            return

        query = self.mod_search_edit.text().strip()
        project_type = str(self.mod_content_type_combo.currentData() or "mod")
        sort = str(self.mod_sort_combo.currentData() or "downloads")
        filter_key = (query.casefold(), project_type, sort, game_version, loader)
        if load_more and filter_key != self._mod_search_filter_key:
            load_more = False
        if not load_more:
            self._mod_image_queue.clear()
            self._mod_search_offset = 0
            self._mod_search_total = 0
            self._mod_search_filter_key = filter_key
            self.mod_catalog_widget.clear("Загружаем популярные проекты Modrinth…")
        elif self._mod_search_offset >= self._mod_search_total > 0:
            return

        offset = self._mod_search_offset if load_more else 0
        self.mod_catalog_widget.set_loading(True, "Загружаем ещё проекты…" if load_more else
                                            "Загружаем популярные проекты Modrinth…")
        compatibility_label = f"Minecraft {game_version}"
        if project_type == "mod" and loader and loader != "vanilla":
            compatibility_label += f" · совместимость загрузчика проверяется в версиях проекта ({loader})"
        self.mod_status_label.setText(f"{project_type.capitalize()} · {compatibility_label}")
        worker = ModrinthSearchWorker(
            query, game_version, loader, project_type, sort, offset=offset, limit=48, parent=self,
        )
        worker.loaded.connect(
            lambda payload, append=load_more, key=filter_key: self._mod_search_loaded(payload, append, key)
        )
        worker.failed.connect(self._mod_operation_failed)
        worker.finished.connect(self._run_pending_mod_search)
        self._retain_worker(worker)
        self.mod_search_worker = worker
        worker.start()

    def _run_pending_mod_search(self):
        if getattr(self, "_mod_search_pending", False):
            self._mod_search_pending = False
            QTimer.singleShot(0, self.search_mods)

    def _mod_search_loaded(self, payload: dict, append: bool = False, request_key=None):
        current_key = (
            self.mod_search_edit.text().strip().casefold(),
            str(self.mod_content_type_combo.currentData() or "mod"),
            str(self.mod_sort_combo.currentData() or "downloads"),
            *self._current_mod_compatibility(),
        )
        if request_key is not None and request_key != current_key:
            self.mod_catalog_widget.set_loading(False)
            return

        results = list(payload.get("results", []) or [])
        total_hits = int(payload.get("total_hits", len(results)) or len(results))
        offset = int(payload.get("offset", 0) or 0)
        if not append:
            self.mod_catalog_widget.clear()
        if not results:
            if not append:
                self.mod_catalog_widget.set_empty(
                    "Совместимых проектов не найдено. Попробуй очистить поиск или выбрать другой тип контента."
                )
                self._mod_search_offset = 0
                self._mod_search_total = 0
            else:
                self.mod_catalog_widget.set_has_more(False)
                self.mod_catalog_widget.count_label.setText(
                    f"Показано {len(self.mod_catalog_widget.cards)} проектов. Больше результатов нет."
                )
            self.mod_status_label.setText("Попробуй очистить поиск или выбрать другой тип контента.")
            self.mod_catalog_widget.set_loading(False)
            return

        _game_version, active_loader = self._current_mod_compatibility()
        for result in results:
            result["requires_loader"] = (
                str(result.get("project_type", "mod")) == "mod" and active_loader == "vanilla"
            )
        cards = self.mod_catalog_widget.append_projects(results, total_hits)
        for result, card in zip(results, cards):
            icon_url = str(result.get("icon_url", ""))
            if icon_url:
                self._mod_image_queue.append((icon_url, card))
        self._start_next_mod_image_workers()

        self._mod_search_total = total_hits
        self._mod_search_offset = offset + len(results)
        self.mod_catalog_widget.set_loading(False)
        self.mod_catalog_widget.set_has_more(self._mod_search_offset < total_hits and bool(results))
        if results:
            self.mod_status_label.setText(
                "Проекты совместимы с выбранной версией Minecraft. Открой карточку для описания, галереи и версий."
            )

    def _start_next_mod_image_workers(self):
        # Не создаём 48 одновременных QThread/HTTP-соединений: это блокировало
        # прокрутку карточек на слабых ПК и вызывало скачки интерфейса.
        while self._mod_image_queue and self._mod_image_active < self._mod_image_limit:
            icon_url, card = self._mod_image_queue.pop(0)
            if card is None:
                continue
            worker = ModrinthImageWorker(icon_url, self)
            worker.loaded.connect(card.set_icon_bytes)
            worker.finished.connect(lambda current=worker: self._forget_mod_image_worker(current))
            self.mod_image_workers.append(worker)
            self._mod_image_active += 1
            self._retain_worker(worker)
            worker.start()

    def _forget_mod_image_worker(self, worker):
        if worker in self.mod_image_workers:
            self.mod_image_workers.remove(worker)
        self._mod_image_active = max(0, self._mod_image_active - 1)
        worker.deleteLater()
        self._start_next_mod_image_workers()

    def _mod_operation_failed(self, message: str):
        if hasattr(self, "mod_catalog_widget"):
            self.mod_catalog_widget.set_loading(False, message)
        self.mod_status_label.setText(message)
        self._notify("Операция Modrinth не выполнена", message, "error", 7500)

    def open_modrinth_project_details(self, project: dict):
        if not self._has_active_profile():
            return
        if self.mod_details_worker and self.mod_details_worker.isRunning():
            return
        game_version, loader = self._current_mod_compatibility()
        project_type = str(project.get("project_type", "mod"))
        self._pending_details_project = dict(project)
        self.mod_status_label.setText(f"Загружаем описание и версии «{project.get('title', '')}»…")
        worker = ModrinthProjectDetailsWorker(project.get("project_id", ""), game_version, loader,
                                              project_type, self)
        worker.loaded.connect(self._mod_details_loaded)
        worker.failed.connect(self._mod_operation_failed)
        self._retain_worker(worker)
        self.mod_details_worker = worker
        worker.start()

    def _mod_details_loaded(self, payload: dict):
        project = dict(payload.get("project", {}) or {})
        project.update({key: value for key, value in getattr(self, "_pending_details_project", {}).items()
                        if key not in project or project.get(key) in (None, "", 0)})
        versions = payload.get("versions", []) or []
        dialog = ModrinthProjectDetailsDialog(project, versions, self)
        _game_version, active_loader = self._current_mod_compatibility()
        if str(project.get("project_type", "mod")) == "mod" and active_loader == "vanilla":
            dialog.install_button.setEnabled(False)
            dialog.install_button.setText("Сначала выбери загрузчик")
            dialog.install_button.setToolTip(
                "Для установки мода выбери Fabric, Forge, NeoForge или Quilt в параметрах сборки"
            )
        dialog.setStyleSheet(self.styleSheet())
        icon_url = str(project.get("icon_url", ""))
        if icon_url:
            worker = ModrinthImageWorker(icon_url, dialog)
            worker.loaded.connect(dialog.set_project_icon)
            worker.finished.connect(worker.deleteLater)
            worker.start()
        if dialog.exec_() == dialog.Accepted:
            self.install_modrinth_project(project, dialog.selected_version_id())

    def install_modrinth_project(self, project: dict, version_id: str = ""):
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Выбери или создай профиль, куда установить контент.", "warning")
            return
        if self.mod_install_worker and self.mod_install_worker.isRunning():
            return
        game_version, loader = self._current_mod_compatibility()
        if not game_version:
            self._notify("Не выбрана версия Minecraft", "Сохрани версию Minecraft в параметрах сборки.", "warning")
            return
        project_id = str(project.get("project_id", ""))
        project_type = str(project.get("project_type", "mod"))
        if project_type == "mod" and loader == "vanilla":
            self._notify(
                "Для модов нужен загрузчик",
                "Сохрани в параметрах сборки Fabric, Forge, NeoForge или Quilt, затем повтори установку мода.",
                "warning",
            )
            return
        if not project_id:
            self._notify("Modrinth не вернул ID проекта", "Не удалось определить проект для установки.", "error")
            return
        profile_id = self.active_profile_id
        self._mod_install_profile_id = profile_id
        self._mod_install_project_type = project_type
        self._mod_install_project_name = str(project.get("title", project_id))
        worker = ModrinthInstallWorker(project_id, game_version, loader, str(self._instance_dir()),
                                       project_type, version_id, self)
        worker.completed.connect(self._mod_install_completed)
        worker.failed.connect(self._mod_operation_failed)
        worker.progress.connect(self.mod_status_label.setText)
        self._retain_worker(worker)
        self.mod_install_worker = worker
        self.mod_status_label.setText(f"Начинается установка «{self._mod_install_project_name}»…")
        worker.start()

    def _mod_install_completed(self, paths: list):
        profile_id = self._mod_install_profile_id or self.active_profile_id
        profile = self.profile_manager.get(profile_id)
        if self._mod_install_project_type == "modpack" and paths:
            self._start_downloaded_modpack_import(str(paths[0]), profile_id)
            return
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        names = ", ".join(Path(path).name for path in paths)
        profile_name = profile.get("name", "сборки") if profile else "сборки"
        self.mod_status_label.setText(f"Установлено в «{profile_name}»: {names}")
        self._notify("Контент установлен", f"{self._mod_install_project_name}\nСборка: {profile_name}\n{names}", "success")

    def _start_downloaded_modpack_import(self, archive_path: str, profile_id: str):
        if not Path(archive_path).is_file():
            self._mod_operation_failed("Не удалось найти скачанный файл сборки Modrinth.")
            return
        self._pending_modpack_download = archive_path
        self._modpack_profile_id = profile_id
        instance_dir = self.profile_manager.instance_dir(profile_id)
        worker = ModpackImportWorker(
            archive_path, str(self.game_root), str(instance_dir),
            str(self.settings.value("game/java_path", "")),
            self.settings.value("game/auto_java", True, type=bool), "", self,
        )
        worker.progress.connect(self._modpack_progress)
        worker.completed.connect(self._modpack_imported)
        worker.failed.connect(self._modpack_import_failed)
        self.modpack_worker = worker
        self._retain_worker(worker)
        self.mod_status_label.setText("Распаковываем сборку Modrinth и устанавливаем её Minecraft/загрузчик…")
        worker.start()

    def import_local_version(self):
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Сначала создай профиль, в который импортировать версию.", "info")
            return
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
        self._refresh_profile_setup_controls()
        self._refresh_installed_versions()
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        self._notify("Локальная версия импортирована",
                     f"Импортирована {result.get('installed_version_id')}. Моды и миры можно добавить в папку сборки.",
                     "success", 6500)

    def import_modpack(self):
        if self.modpack_worker and self.modpack_worker.isRunning():
            return
        archive_path, _ = QFileDialog.getOpenFileName(
            self, "Импортировать сборку Modrinth", "", "Сборки Modrinth (*.mrpack);;Все файлы (*.*)"
        )
        if not archive_path:
            return
        try:
            metadata = inspect_modpack(archive_path)
            if metadata.get("format") != "modrinth":
                raise ValueError("Поддерживается только формат сборок Modrinth .mrpack.")
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
                self.settings.value("game/auto_java", True, type=bool), "", self,
            )
            worker.progress.connect(self._modpack_progress)
            worker.completed.connect(self._modpack_imported)
            worker.failed.connect(self._modpack_import_failed)
            self.modpack_worker = worker
            self._retain_worker(worker)
            self.mod_status_label.setText("Подготовка импорта сборки Modrinth…")
            worker.start()
        except Exception as exc:
            self._notify("Не удалось прочитать сборку", str(exc), "error", 7000)

    def import_map_zip(self):
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Создай профиль, в который нужно добавить карту.", "info")
            return
        archive_path, _ = QFileDialog.getOpenFileName(
            self, "Импортировать карту Minecraft", "", "Архив карты (*.zip);;Все файлы (*.*)"
        )
        if not archive_path:
            return
        try:
            archive_path = Path(archive_path)
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = archive.infolist()
                total_uncompressed = sum(max(0, int(item.file_size)) for item in members)
                if total_uncompressed > 8 * 1024 * 1024 * 1024:
                    raise ValueError("Размер распакованной карты превышает лимит 8 ГБ.")
                if len(members) > 100000:
                    raise ValueError("В архиве слишком много файлов.")
                level_entry = next((item for item in members
                                    if PurePosixPath(item.filename.replace("\\", "/")).name.lower() == "level.dat"), None)
                if level_entry is None:
                    raise ValueError("В архиве не найден level.dat. Выбери ZIP с папкой готового мира Minecraft.")
                level_path = PurePosixPath(level_entry.filename.replace("\\", "/"))
                prefix = str(level_path.parent)
                if prefix == ".":
                    prefix = ""
                world_name = (PurePosixPath(prefix).name if prefix else archive_path.stem).strip() or archive_path.stem
                world_name = re.sub(r"[^A-Za-z0-9._ -]", "_", world_name)[:64]
                saves_dir = self._instance_dir() / "saves"
                target_root = (saves_dir / world_name).resolve()
                saves_resolved = saves_dir.resolve()
                if target_root.parent != saves_resolved:
                    raise ValueError("Некорректное имя папки мира.")
                target_root.mkdir(parents=True, exist_ok=True)
                extracted = 0
                for item in members:
                    raw_name = item.filename.replace("\\", "/")
                    if prefix and not raw_name.startswith(prefix + "/"):
                        continue
                    relative = raw_name[len(prefix) + 1:] if prefix else raw_name
                    pure = PurePosixPath(relative)
                    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                        continue
                    # ZIP symbolic links are not extracted.
                    if (item.external_attr >> 16) & 0o170000 == 0o120000:
                        continue
                    target = (target_root / Path(*pure.parts)).resolve()
                    if target != target_root and target_root not in target.parents:
                        raise ValueError("Архив содержит путь за пределами папки карты.")
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item, "r") as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    extracted += 1
                    if extracted > 100000:
                        raise ValueError("В архиве слишком много файлов.")
            self._refresh_mod_list()
            self._notify("Карта добавлена", f"Мир «{world_name}» установлен в сохранения сборки.", "success")
        except Exception as exc:
            log.exception("Map import failed")
            self._notify("Не удалось импортировать карту", str(exc), "error", 7500)

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
        if self._pending_modpack_download:
            Path(self._pending_modpack_download).unlink(missing_ok=True)
            self._pending_modpack_download = ""
        self._refresh_profiles()
        self._refresh_profile_setup_controls()
        self._refresh_installed_versions()
        if profile_id == self.active_profile_id:
            self._refresh_mod_list()
        self.mod_status_label.setText(f"Сборка «{metadata['name']}» готова к запуску.")
        self._notify("Сборка установлена",
                     f"{metadata['name']} · Minecraft {metadata['minecraft_version']} · {metadata['loader']}",
                     "success", 6500)

    def _modpack_import_failed(self, message: str):
        if self._pending_modpack_download:
            Path(self._pending_modpack_download).unlink(missing_ok=True)
            self._pending_modpack_download = ""
        self.mod_status_label.setText(message)
        self._notify("Импорт сборки не завершён", message, "error", 8500)

    def rename_profile(self):
        if not self._has_active_profile():
            return
        profile = self._active_profile()
        new_name, accepted = QInputDialog.getText(
            self, "Переименовать сборку", "Новое название сборки:",
            QLineEdit.Normal, profile.get("name", ""),
        )
        if not accepted:
            return
        new_name = " ".join(new_name.split()).strip()
        if not new_name:
            self._notify("Название не изменено", "Название сборки не может быть пустым.", "warning")
            return
        if any(item["id"] != profile["id"] and item["name"].casefold() == new_name.casefold()
               for item in self.profile_manager.all()):
            self._notify("Название занято", "Сборка с таким названием уже существует.", "warning")
            return
        try:
            self.profile_manager.update(profile["id"], name=new_name[:48])
            self._refresh_profiles()
            self._notify("Сборка переименована", new_name[:48], "success")
        except Exception as exc:
            self._notify("Не удалось переименовать сборку", str(exc), "error")

    def delete_profile(self):
        if not self._has_active_profile():
            return
        profile = self._active_profile()
        from PyQt5.QtWidgets import QDialog, QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle("Удалить сборку")
        dialog.setModal(True)
        dialog.setMinimumWidth(390)
        box = QVBoxLayout(dialog)
        title = QLabel(f"Удалить сборку «{profile['name']}»?")
        title.setStyleSheet("font-size: 14pt; font-weight: 800;")
        body = QLabel("Сборка будет удалена из списка. Её моды, миры и файлы останутся на диске.")
        body.setWordWrap(True)
        body.setObjectName("Muted")
        box.addWidget(title)
        box.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        buttons.button(QDialogButtonBox.Yes).setText("Удалить сборку")
        buttons.button(QDialogButtonBox.No).setText("Отмена")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        dialog.setStyleSheet(self.styleSheet())
        if dialog.exec_() != QDialog.Accepted:
            return
        try:
            old_id = self.active_profile_id
            profiles_before = self.profile_manager.all()
            self.profile_manager.delete(old_id, delete_files=False)
            remaining = [item for item in profiles_before if item["id"] != old_id]
            self.active_profile_id = remaining[0]["id"] if remaining else ""
            self.settings.setValue("profiles/active", self.active_profile_id)
            self.settings.sync()
            self._refresh_profiles()
            self._refresh_profile_setup_controls()
            self._refresh_mod_list()
            self._notify("Сборка удалена из списка", "Файлы сохранены на диске.", "success")
        except ProfileError as exc:
            self._notify("Не удалось удалить сборку", str(exc), "error")

    def open_profile_folder(self):
        if not self._has_active_profile():
            self._notify("Сборка не выбрана", "Сначала создай профиль сборки.", "info")
            return
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
        if hasattr(self, "install_profile_button"):
            self.install_profile_button.setEnabled(
                not blocked and self._has_active_profile()
                and not (self.install_worker and self.install_worker.isRunning())
            )
        if self.versions:
            self._populate_version_combo()
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
    def _game_logs_directory(self) -> Path:
        # Обычная игра пишет логи в корень .minecraft, сборка — в свою папку.
        return self._instance_dir() if self._has_active_profile() else self.game_root

    def open_game_logs_folder(self):
        self._open_folder(self._game_logs_directory() / "logs")

    def _refresh_logs(self):
        if not hasattr(self, "log_view") or self.stack.currentIndex() != 4:
            return
        launcher_path = log_file()
        game_path = self._game_logs_directory() / "logs" / "latest.log"
        for path, viewer, empty_text in (
                (launcher_path, self.log_view, "Лог лаунчера пока пуст."),
                (game_path, self.game_log_view, "Minecraft ещё не создавал latest.log в этой папке игры."),
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
            self.discord_presence.shutdown()
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
        self.discord_presence.shutdown()
        event.accept()