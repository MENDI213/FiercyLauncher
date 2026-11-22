"""Небольшой карточный каталог Modrinth и окно подробностей проекта."""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QTextBrowser, QTextEdit, QScrollArea, QWidget, QDialogButtonBox, QSizePolicy,
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
        self._column_count = 0
        self.count_label.setText(message)
        self.load_more_button.hide()
        self.content.adjustSize()

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
        added = []
        first_index = len(self.cards)
        for project in projects:
            card = ModrinthProjectCard(project, self.content)
            card.detailsRequested.connect(self.projectDetailsRequested)
            card.installRequested.connect(self.installRequested)
            self.cards.append(card)
            added.append(card)
        self._total_hits = max(self._total_hits, int(total_hits or 0))
        if not self._column_count:
            self._reflow()
        else:
            columns = self._column_count
            for index in range(first_index, len(self.cards)):
                self.grid.addWidget(self.cards[index], index // columns, index % columns)
            self.content.updateGeometry()
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
        width = max(1, self.scroll.viewport().width())
        if width >= 1180:
            columns = 4
        elif width >= 800:
            columns = 3
        elif width >= 520:
            columns = 2
        else:
            columns = 1
        if columns == self._column_count and self.grid.count() == len(self.cards):
            return
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(self.content)
        self._column_count = columns
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
        self.grid.invalidate()
        self.content.updateGeometry()


class ClickableGalleryImage(QLabel):
    clicked = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.payload = b""
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setText("Загрузка изображения…")
        self.setFixedSize(260, 148)
        self.setObjectName("ModrinthGalleryImage")

    def set_image_bytes(self, payload: bytes):
        pixmap = QPixmap()
        if payload and pixmap.loadFromData(payload):
            self.payload = bytes(payload)
            self.setPixmap(pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            self.setText("")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.payload:
            self.clicked.emit(self.payload)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ModrinthImagePreviewDialog(QDialog):
    def __init__(self, payload: bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Просмотр изображения Modrinth")
        self.resize(1050, 720)
        self.setMinimumSize(560, 400)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap()
        if payload and pixmap.loadFromData(payload):
            image.setPixmap(pixmap)
            image.setMinimumSize(pixmap.size())
        else:
            image.setText("Не удалось открыть изображение")
        scroll.setWidget(image)
        root.addWidget(scroll, 1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignRight)


class ModrinthProjectDetailsDialog(QDialog):
    """Аккуратная страница проекта, галерея с полноразмерным просмотром и выбор версии."""

    def __init__(self, project: dict, versions: list[dict], parent=None):
        super().__init__(parent)
        self.project = dict(project)
        self.versions = list(versions or [])
        self.setWindowTitle(str(project.get("title", "Проект Modrinth")))
        self.setObjectName("ModrinthDetailsDialog")
        self.setModal(True)
        self.resize(900, 760)
        self.setMinimumSize(720, 600)
        self._image_workers = []
        self._gallery_queue = []
        self._gallery_active = 0
        self._gallery_limit = 3

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        scroll = QScrollArea()
        scroll.setObjectName("ModrinthDetailsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 10, 12)
        content_layout.setSpacing(12)

        header_card = QFrame()
        header_card.setObjectName("ModrinthDetailsHeader")
        header = QHBoxLayout(header_card)
        header.setContentsMargins(16, 14, 16, 14)
        header.setSpacing(14)
        self.icon = QLabel("F")
        self.icon.setObjectName("ModrinthProjectIcon")
        self.icon.setFixedSize(80, 80)
        self.icon.setAlignment(Qt.AlignCenter)
        header.addWidget(self.icon, 0, Qt.AlignTop)
        identity = QVBoxLayout()
        identity.setSpacing(5)
        title = QLabel(str(project.get("title", "Без названия")))
        title.setObjectName("PageHeading")
        title.setWordWrap(True)
        identity.addWidget(title)
        author = str(project.get("author", "Неизвестный автор"))
        stats = (f"Автор: {author}   ·   Загрузки: {_number(project.get('downloads'))}"
                 f"   ·   В избранном: {_number(project.get('follows'))}")
        self.stats_label = QLabel(stats)
        self.stats_label.setObjectName("Muted")
        self.stats_label.setWordWrap(True)
        identity.addWidget(self.stats_label)
        short = QLabel(str(project.get("description", "Описание пока не добавлено.")))
        short.setObjectName("Muted")
        short.setWordWrap(True)
        identity.addWidget(short)
        header.addLayout(identity, 1)
        content_layout.addWidget(header_card)

        gallery = project.get("gallery", []) or []
        if gallery:
            gallery_title = QLabel("Галерея проекта")
            gallery_title.setObjectName("SectionTitle")
            content_layout.addWidget(gallery_title)
            self.gallery_scroll = QScrollArea()
            self.gallery_scroll.setObjectName("ModrinthGalleryScroll")
            self.gallery_scroll.setWidgetResizable(True)
            self.gallery_scroll.setFrameShape(QFrame.NoFrame)
            self.gallery_scroll.setFixedHeight(174)
            self.gallery_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.gallery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.gallery_widget = QWidget()
            self.gallery_layout = QHBoxLayout(self.gallery_widget)
            self.gallery_layout.setContentsMargins(2, 2, 2, 4)
            self.gallery_layout.setSpacing(10)
            self.gallery_scroll.setWidget(self.gallery_widget)
            content_layout.addWidget(self.gallery_scroll)
            self._build_gallery(gallery)

        body_title = QLabel("Описание проекта")
        body_title.setObjectName("SectionTitle")
        content_layout.addWidget(body_title)
        self.body = QTextBrowser()
        self.body.setObjectName("ModrinthProjectBody")
        self.body.setOpenExternalLinks(True)
        self.body.setOpenLinks(True)
        self.body.setReadOnly(True)
        self.body.setFrameShape(QFrame.NoFrame)
        self.body.setMinimumHeight(320)
        self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.body.setLineWrapMode(QTextEdit.WidgetWidth)
        self.body.setMarkdown(self._clean_markdown(
            str(project.get("body", "") or project.get("description", "Описание отсутствует."))
        ))
        content_layout.addWidget(self.body)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QFrame()
        footer.setObjectName("ModrinthDetailsFooter")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(10, 8, 10, 8)
        footer_row.addWidget(QLabel("Совместимая версия"))
        self.version_combo = WheelSafeComboBox()
        self.version_combo.setMinimumHeight(42)
        self.version_combo.setMinimumWidth(280)
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
            self.version_combo.addItem("Совместимые версии не найдены", None)
            self.version_combo.setEnabled(False)
        footer_row.addWidget(self.version_combo, 1)
        self.install_button = QPushButton("Установить выбранную версию")
        self.install_button.setObjectName("PrimaryButton")
        self.install_button.setMinimumHeight(42)
        self.install_button.setEnabled(self.version_combo.isEnabled())
        self.install_button.clicked.connect(self.accept)
        footer_row.addWidget(self.install_button)
        close_button = QPushButton("Закрыть")
        close_button.setObjectName("SecondaryButton")
        close_button.setMinimumHeight(42)
        close_button.clicked.connect(self.reject)
        footer_row.addWidget(close_button)
        root.addWidget(footer)

    @staticmethod
    def _clean_markdown(markdown: str) -> str:
        """Remove embedded remote/HTML images that Qt QTextDocument displays as broken glyphs.

        Gallery images are displayed in the dedicated gallery above where they can
        be opened at native resolution. Keep link labels and standard Markdown text.
        """
        import re
        text = str(markdown or "")
        text = re.sub(r"(?is)<(script|style|svg|picture)\b.*?</\1\s*>", "", text)
        text = re.sub(r"(?is)<img\b[^>]*?/?>", "", text)
        text = re.sub(r"(?is)<!--.*?-->", "", text)
        # QTextBrowser's Markdown renderer cannot fetch remote images and some
        # Modrinth descriptions contain nested HTML/image blocks. Remove those
        # blocks here; gallery images are shown separately and are clickable.
        text = re.sub(r"(?is)<(div|section|figure|table)\b[^>]*>.*?</\1\s*>", "", text)
        text = re.sub(r"!\[([^]]*)\]\(.*?\)", r"\1", text)
        text = re.sub(r"(?m)^\s*<[^>]+>\s*$", "", text)
        text = re.sub(r"(?m)^\s*[\u25a0\u25a1\ufffd]+\s*$", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        if len(text) > 90000:
            text = text[:90000].rstrip() + "\n\n*Описание обрезано в интерфейсе, чтобы не перегружать окно.*"
        return text or "Описание проекта пока не добавлено."

    def _build_gallery(self, gallery):
        urls = []
        for item in gallery:
            url = item.get("url", "") if isinstance(item, dict) else item
            url = str(url or "").strip()
            if url.startswith("https://"):
                urls.append(url)
        for url in urls[:12]:
            label = ClickableGalleryImage(self.gallery_widget)
            label.clicked.connect(self._show_image_preview)
            self.gallery_layout.addWidget(label)
            self._gallery_queue.append((url, label))
        self.gallery_layout.addStretch(1)
        self._start_next_gallery_images()

    def _start_next_gallery_images(self):
        while self._gallery_queue and self._gallery_active < self._gallery_limit:
            url, label = self._gallery_queue.pop(0)
            owner = self.parent() if self.parent() is not None else self
            worker = ModrinthImageWorker(url, owner)
            worker.loaded.connect(label.set_image_bytes)
            worker.failed.connect(lambda _message, target=label: target.setText("Изображение недоступно"))
            worker.finished.connect(lambda current=worker: self._forget_image_worker(current))
            if owner is not self and hasattr(owner, "mod_image_workers"):
                owner.mod_image_workers.append(worker)
            self._image_workers.append(worker)
            self._gallery_active += 1
            worker.start()

    def _show_image_preview(self, payload: bytes):
        dialog = ModrinthImagePreviewDialog(payload, self)
        dialog.setStyleSheet(self.styleSheet())
        dialog.exec_()

    def _forget_image_worker(self, worker):
        if worker in self._image_workers:
            self._image_workers.remove(worker)
        owner = self.parent() if self.parent() is not None else None
        if owner is not None and hasattr(owner, "mod_image_workers"):
            try:
                if worker in owner.mod_image_workers:
                    owner.mod_image_workers.remove(worker)
            except RuntimeError:
                pass
        self._gallery_active = max(0, self._gallery_active - 1)
        worker.deleteLater()
        # If the dialog was accepted/rejected, no more image widgets need updating.
        if self.isVisible():
            self._start_next_gallery_images()

    def selected_version_id(self) -> str:
        return str(self.version_combo.currentData() or "")

    def set_project_icon(self, payload: bytes):
        pixmap = QPixmap()
        if payload and pixmap.loadFromData(payload):
            self.icon.setPixmap(pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.icon.setText("")

