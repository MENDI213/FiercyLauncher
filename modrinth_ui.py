"""Небольшой карточный каталог Modrinth и окно подробностей проекта."""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QTextBrowser, QScrollArea, QWidget, QDialogButtonBox, QSizePolicy,
)
from ui_controls import WheelSafeComboBox


def _number(value) -> str:
    try:
        amount = max(0, int(value or 0))
    except (TypeError, ValueError):
        amount = 0
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f} млн".replace(".0 млн", " млн")
    if amount >= 1_000:
        return f"{amount / 1_000:.1f} тыс.".replace(".0 тыс.", " тыс.")
    return str(amount)


class ModrinthProjectCard(QFrame):
    detailsRequested = pyqtSignal(dict)
    installRequested = pyqtSignal(dict)

    def __init__(self, project: dict, parent=None):
        super().__init__(parent)
        self.project = dict(project)
        self.setObjectName("ModrinthProjectCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(205)
        self.setMinimumHeight(178)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(11, 10, 11, 10)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.icon = QLabel("F")
        self.icon.setObjectName("ModrinthProjectIcon")
        self.icon.setFixedSize(48, 48)
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header.addWidget(self.icon, 0, Qt.AlignTop)

        identity = QVBoxLayout()
        identity.setSpacing(3)
        self.title = QLabel(str(project.get("title", "Без названия")))
        self.title.setObjectName("ModrinthProjectTitle")
        self.title.setWordWrap(True)
        self.title.setMaximumHeight(40)
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        identity.addWidget(self.title)
        author = str(project.get("author", ""))
        project_type = str(project.get("project_type", "mod")).lower()
        type_names = {
            "mod": "МОД",
            "resourcepack": "РЕСУРСПАК",
            "shader": "ШЕЙДЕР",
            "modpack": "СБОРКА",
        }
        subtitle = " · ".join(part for part in (author, type_names.get(project_type, "MODRINTH")) if part)
        self.author_label = QLabel(subtitle)
        self.author_label.setObjectName("ModrinthProjectMeta")
        self.author_label.setWordWrap(True)
        self.author_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        identity.addWidget(self.author_label)
        header.addLayout(identity, 1)
        root.addLayout(header)

        description = str(project.get("description", "")).strip() or "Описание пока не добавлено."
        self.description = QLabel(description)
        self.description.setObjectName("ModrinthProjectDescription")
        self.description.setWordWrap(True)
        self.description.setMaximumHeight(42)
        self.description.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        root.addWidget(self.description, 1)

        categories = [str(value).replace("_", " ").strip() for value in (project.get("categories", []) or [])]
        self.categories = QLabel("  ·  ".join(categories[:3]))
        self.categories.setObjectName("ModrinthProjectTags")
        self.categories.setMaximumHeight(20)
        self.categories.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        root.addWidget(self.categories)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        stats = f"↓ {_number(project.get('downloads'))}    ♥ {_number(project.get('follows'))}"
        self.stats = QLabel(stats)
        self.stats.setObjectName("ModrinthProjectMeta")
        self.stats.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        footer.addWidget(self.stats, 1)
        self.install_button = QPushButton("Установить")
        self.install_button.setObjectName("PrimaryButton")
        self.install_button.setCursor(Qt.PointingHandCursor)
        self.install_button.setMinimumWidth(104)
        self.install_button.setMaximumHeight(34)
        if project.get("requires_loader"):
            self.install_button.setText("Нужен загрузчик")
            self.install_button.setEnabled(False)
            self.install_button.setToolTip("Выбери Fabric, Forge, NeoForge или Quilt в параметрах сборки")
        self.install_button.clicked.connect(lambda: self.installRequested.emit(dict(self.project)))
        footer.addWidget(self.install_button)
        root.addLayout(footer)

    def set_icon_bytes(self, payload: bytes):
        pixmap = QPixmap()
        if payload and pixmap.loadFromData(payload):
            self.icon.setPixmap(pixmap.scaled(48, 48, Qt.KeepAspectRatioByExpanding, Qt.FastTransformation))
            self.icon.setText("")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.detailsRequested.emit(dict(self.project))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ModrinthCatalogWidget(QWidget):
    """Адаптивная сетка карточек с отдельной прокруткой и пагинацией."""

    projectDetailsRequested = pyqtSignal(dict)
    installRequested = pyqtSignal(dict)
    loadMoreRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("ModrinthCatalogScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setMinimumHeight(520)
        self.scroll.setMaximumHeight(16777215)
        self.scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.content = QWidget()
        self.content.setObjectName("ModrinthCatalogContent")
        self.grid = QGridLayout(self.content)
        self.grid.setContentsMargins(3, 3, 8, 8)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(10)
        self.grid.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        self.count_label = QLabel("Популярные проекты Modrinth")
        self.count_label.setObjectName("Muted")
        footer.addWidget(self.count_label, 1)
        self.load_more_button = QPushButton("Показать ещё")
        self.load_more_button.setObjectName("SecondaryButton")
        self.load_more_button.setCursor(Qt.PointingHandCursor)
        self.load_more_button.setFocusPolicy(Qt.NoFocus)
        self.load_more_button.clicked.connect(self.loadMoreRequested.emit)
        self.load_more_button.hide()
        footer.addWidget(self.load_more_button)
        root.addLayout(footer)

        self.cards: list[ModrinthProjectCard] = []
        self._loading = False
        self._total_hits = 0
        self._reflow_pending = False
        self._column_count = 0

    def clear(self, message: str = "Загружаем рекомендации Modrinth…"):
        for card in self.cards:
            self.grid.removeWidget(card)
            card.deleteLater()
        self.cards.clear()
        self._total_hits = 0
        self.count_label.setText(message)
        self.load_more_button.hide()
        self._reflow()

    def set_loading(self, loading: bool, message: str | None = None):
        self._loading = bool(loading)
        self.load_more_button.setEnabled(not self._loading)
        if message:
            self.count_label.setText(message)
        elif self._loading:
            self.count_label.setText("Загружаем проекты Modrinth…")

    def append_projects(self, projects: list[dict], total_hits: int = 0) -> list[ModrinthProjectCard]:
        # При пагинации сохраняем положение внутренней прокрутки: Qt не должен
        # переносить пользователя к началу страницы после добавления карточек.
        previous_scroll = self.scroll.verticalScrollBar().value()
        added = []
        for project in projects:
            card = ModrinthProjectCard(project, self.content)
            card.detailsRequested.connect(self.projectDetailsRequested)
            card.installRequested.connect(self.installRequested)
            self.cards.append(card)
            added.append(card)
        self._total_hits = max(self._total_hits, int(total_hits or 0))
        self._reflow()
        QTimer.singleShot(0, lambda value=previous_scroll: self.scroll.verticalScrollBar().setValue(value))
        shown = len(self.cards)
        if self._total_hits:
            self.count_label.setText(f"Показано {shown} из {self._total_hits} проектов")
        else:
            self.count_label.setText(f"Найдено проектов: {shown}")
        self.load_more_button.setVisible(shown < self._total_hits and bool(projects))
        return added

    def set_empty(self, message: str):
        self.clear(message)

    def set_has_more(self, has_more: bool):
        self.load_more_button.setVisible(bool(has_more))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._reflow_pending:
            self._reflow_pending = True
            QTimer.singleShot(0, self._reflow)

    def _reflow(self):
        self._reflow_pending = False
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(self.content)
        width = max(1, self.scroll.viewport().width())
        if width >= 1180:
            columns = 4
        elif width >= 800:
            columns = 3
        elif width >= 520:
            columns = 2
        else:
            columns = 1
        self._column_count = columns
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
        self.grid.invalidate()
        self.content.updateGeometry()


class ModrinthProjectDetailsDialog(QDialog):
    """Подробности проекта, галерея скриншотов и выбор версии перед установкой."""

    def __init__(self, project: dict, versions: list[dict], parent=None):
        super().__init__(parent)
        self.project = dict(project)
        self.versions = list(versions or [])
        self.setWindowTitle(str(project.get("title", "Проект Modrinth")))
        self.setObjectName("ModrinthDetailsDialog")
        self.setModal(True)
        self.resize(790, 660)
        self.setMinimumSize(650, 540)
        self._image_workers = []

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        self.icon = QLabel("F")
        self.icon.setObjectName("ModrinthProjectIcon")
        self.icon.setFixedSize(76, 76)
        self.icon.setAlignment(Qt.AlignCenter)
        header.addWidget(self.icon)
        identity = QVBoxLayout()
        identity.setSpacing(4)
        title = QLabel(str(project.get("title", "Без названия")))
        title.setObjectName("PageHeading")
        title.setWordWrap(True)
        identity.addWidget(title)
        author = str(project.get("author", ""))
        stats = (f"Автор: {author}   ·   Загрузки: {_number(project.get('downloads'))}"
                 f"   ·   В избранном: {_number(project.get('follows'))}")
        self.stats_label = QLabel(stats)
        self.stats_label.setObjectName("Muted")
        self.stats_label.setWordWrap(True)
        identity.addWidget(self.stats_label)
        header.addLayout(identity, 1)
        root.addLayout(header)

        short = QLabel(str(project.get("description", "")))
        short.setObjectName("Muted")
        short.setWordWrap(True)
        root.addWidget(short)

        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.gallery_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.gallery_scroll.setFixedHeight(150)
        self.gallery_widget = QWidget()
        self.gallery_layout = QHBoxLayout(self.gallery_widget)
        self.gallery_layout.setContentsMargins(2, 2, 2, 2)
        self.gallery_layout.setSpacing(10)
        self.gallery_scroll.setWidget(self.gallery_widget)
        root.addWidget(self.gallery_scroll)
        self._build_gallery(project.get("gallery", []) or [])

        body_title = QLabel("Описание проекта")
        body_title.setStyleSheet("font-weight: 800; font-size: 11pt;")
        root.addWidget(body_title)
        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setMarkdown(str(project.get("body", "") or project.get("description", "Описание отсутствует.")))
        self.body.setMinimumHeight(190)
        root.addWidget(self.body, 1)

        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("Версия для установки"))
        self.version_combo = WheelSafeComboBox()
        self.version_combo.setMinimumHeight(40)
        for version in self.versions:
            version_id = str(version.get("id", ""))
            if not version_id:
                continue
            number = str(version.get("version_number", version.get("name", version_id)))
            loaders = ", ".join(version.get("loaders", []) or [])
            game_versions = ", ".join(version.get("game_versions", []) or [])
            label_parts = [number]
            if loaders:
                label_parts.append(loaders)
            if game_versions:
                label_parts.append(game_versions)
            self.version_combo.addItem("  ·  ".join(label_parts), version_id)
        if not self.version_combo.count():
            self.version_combo.addItem("Нет совместимых версий", None)
            self.version_combo.setEnabled(False)
        version_row.addWidget(self.version_combo, 1)
        root.addLayout(version_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.install_button = QPushButton("Установить выбранную версию")
        self.install_button.setObjectName("PrimaryButton")
        self.install_button.setEnabled(self.version_combo.isEnabled())
        self.install_button.clicked.connect(self.accept)
        buttons.addButton(self.install_button, QDialogButtonBox.AcceptRole)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_gallery(self, gallery):
        urls = []
        for item in gallery:
            url = item.get("url", "") if isinstance(item, dict) else item
            url = str(url or "").strip()
            if url.startswith("https://"):
                urls.append(url)
        if not urls:
            placeholder = QLabel("У проекта пока нет изображений в галерее.")
            placeholder.setObjectName("Muted")
            self.gallery_layout.addWidget(placeholder)
            self.gallery_layout.addStretch(1)
            return
        from mod_service import ModrinthImageWorker
        for url in urls[:12]:
            label = QLabel("Загрузка изображения…")
            label.setObjectName("ModrinthGalleryImage")
            label.setFixedSize(240, 136)
            label.setAlignment(Qt.AlignCenter)
            self.gallery_layout.addWidget(label)
            worker = ModrinthImageWorker(url, self)
            worker.loaded.connect(lambda payload, target=label: self._set_gallery_image(target, payload))
            worker.failed.connect(lambda _message, target=label: target.setText("Изображение недоступно"))
            worker.finished.connect(lambda current=worker: self._forget_image_worker(current))
            self._image_workers.append(worker)
            worker.start()
        self.gallery_layout.addStretch(1)

    def _forget_image_worker(self, worker):
        if worker in self._image_workers:
            self._image_workers.remove(worker)
        worker.deleteLater()

    @staticmethod
    def _set_gallery_image(label: QLabel, payload: bytes):
        pixmap = QPixmap()
        if payload and pixmap.loadFromData(payload):
            label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatioByExpanding, Qt.FastTransformation))
            label.setText("")

    def selected_version_id(self) -> str:
        return str(self.version_combo.currentData() or "")

    def set_project_icon(self, payload: bytes):
        pixmap = QPixmap()
        if payload and pixmap.loadFromData(payload):
            self.icon.setPixmap(pixmap.scaled(76, 76, Qt.KeepAspectRatio, Qt.FastTransformation))
            self.icon.setText("")
