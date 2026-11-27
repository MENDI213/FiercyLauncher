"""Небольшой карточный каталог Modrinth и окно подробностей проекта."""
from __future__ import annotations

import re

from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QByteArray, QBuffer, QIODevice, QSize
from PyQt5.QtWidgets import QScroller
from PyQt5.QtGui import QPixmap, QImageReader, QFontMetrics
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QTextBrowser, QTextEdit, QScrollArea, QWidget, QDialogButtonBox, QSizePolicy, QProgressBar,
)
from ui_controls import WheelSafeComboBox, SmoothScrollArea


def _safe_pixmap(payload: bytes, max_pixels: int = 24_000_000, max_edge: int = 4096) -> QPixmap:
    """Decode remote image data only after checking its declared dimensions."""
    if not payload or len(payload) > 8 * 1024 * 1024:
        return QPixmap()
    try:
        buffer = QBuffer()
        buffer.setData(QByteArray(payload))
        if not buffer.open(QIODevice.ReadOnly):
            return QPixmap()
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            width, height = size.width(), size.height()
            if width <= 0 or height <= 0 or width * height > max_pixels:
                buffer.close()
                return QPixmap()
            # A preview should not allocate a giant full-resolution texture.
            scale = min(1.0, max_edge / max(width, height))
            if scale < 1.0:
                reader.setScaledSize(QSize(max(1, int(width * scale)), max(1, int(height * scale))))
        image = reader.read()
        buffer.close()
        return QPixmap.fromImage(image) if not image.isNull() else QPixmap()
    except Exception:
        return QPixmap()


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
    """Compact, colourful project card with a fixed layout for every download state."""

    detailsRequested = pyqtSignal(dict)
    installRequested = pyqtSignal(dict)
    removeRequested = pyqtSignal(dict)
    cancelRequested = pyqtSignal(dict)

    TYPE_META = {
        "mod": ("МОД", "mod"),
        "modpack": ("СБОРКА", "modpack"),
        "resourcepack": ("РЕСУРСПАК", "resourcepack"),
        "shader": ("ШЕЙДЕР", "shader"),
    }

    def __init__(self, project: dict, parent=None):
        super().__init__(parent)
        self.project = dict(project)
        self._requires_loader = bool(project.get("requires_loader"))
        self._state = "idle"
        self._left_press_inside = False
        self._full_status_message = ""
        project_type = str(project.get("project_type", "mod")).lower()
        type_label, type_property = self.TYPE_META.get(project_type, ("ПРОЕКТ", "mod"))
        self.setObjectName("ModrinthProjectCard")
        self.setProperty("projectType", type_property)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(264)
        self.setFixedHeight(268)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(13, 12, 13, 12)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(11)
        self.icon = QLabel("F")
        self.icon.setObjectName("ModrinthProjectIcon")
        self.icon.setFixedSize(60, 60)
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header.addWidget(self.icon, 0, Qt.AlignTop)

        identity = QVBoxLayout()
        identity.setSpacing(3)
        self.title = QLabel(str(project.get("title", "Без названия")))
        self.title.setObjectName("ModrinthProjectTitle")
        self.title.setWordWrap(True)
        self.title.setFixedHeight(46)
        self.title.setToolTip(str(project.get("title", "Без названия")))
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        identity.addWidget(self.title)

        author = str(project.get("author", "Неизвестный автор")).strip() or "Неизвестный автор"
        self.author_label = QLabel(author)
        self.author_label.setObjectName("ModrinthProjectAuthor")
        self.author_label.setWordWrap(False)
        self.author_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._full_author = author
        self.author_label.setToolTip(author)

        self.type_badge = QLabel(type_label)
        self.type_badge.setObjectName("ModrinthProjectTypeBadge")
        self.type_badge.setAlignment(Qt.AlignCenter)
        self.type_badge.setFixedHeight(19)
        self.type_badge.setMaximumWidth(102)
        identity_meta = QHBoxLayout()
        identity_meta.setSpacing(7)
        identity_meta.addWidget(self.author_label, 1)
        identity_meta.addWidget(self.type_badge, 0, Qt.AlignRight)
        identity.addLayout(identity_meta)
        header.addLayout(identity, 1)
        root.addLayout(header)

        full_description = str(project.get("description", "")).strip() or "Описание проекта пока не добавлено."
        short_description = full_description
        if len(short_description) > 190:
            short_description = short_description[:187].rstrip() + "…"
        self.description = QLabel(short_description)
        self.description.setObjectName("ModrinthProjectDescription")
        self.description.setWordWrap(True)
        self.description.setMinimumHeight(62)
        self.description.setMaximumHeight(70)
        self.description.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.description.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.description.setToolTip(full_description)
        root.addWidget(self.description, 1)

        categories = [str(value).replace("_", " ").strip() for value in (project.get("categories", []) or [])]
        self.categories = QLabel("  ·  ".join(value for value in categories[:3] if value))
        self.categories.setObjectName("ModrinthProjectTags")
        self.categories.setWordWrap(True)
        self.categories.setMinimumHeight(22)
        self.categories.setMaximumHeight(28)
        self.categories.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.categories.setToolTip(" · ".join(categories))
        root.addWidget(self.categories)

        self.status_label = QLabel("")
        self.status_label.setObjectName("ModrinthProjectState")
        self.status_label.setFixedHeight(16)
        self.status_label.setWordWrap(False)
        self.status_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        root.addWidget(self.status_label)
        # Always reserve the same 5px row. A progress state never changes card height.
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("ModrinthCardProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        root.addWidget(self.progress_bar)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.stats = QLabel(f"↓ {_number(project.get('downloads'))}   ♥ {_number(project.get('follows'))}")
        self.stats.setObjectName("ModrinthProjectMeta")
        self.stats.setMinimumWidth(0)
        self.stats.setWordWrap(False)
        self.stats.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        footer.addWidget(self.stats, 1)

        self.action_button = QPushButton("Скачать")
        self.action_button.setObjectName("ModrinthActionButton")
        self.action_button.setCursor(Qt.PointingHandCursor)
        self.action_button.setFixedSize(112, 39)
        self.action_button.setFocusPolicy(Qt.NoFocus)
        self.action_button.clicked.connect(self._dispatch_action)
        footer.addWidget(self.action_button, 0, Qt.AlignRight)
        root.addLayout(footer)

        # Compatibility aliases: callers that update the previous state-specific
        # buttons still address the same fixed-size action widget.
        self.install_button = self.action_button
        self.remove_button = self.action_button
        self.cancel_button = self.action_button
        self.set_install_state("idle")

    def _dispatch_action(self):
        project = dict(self.project)
        if self._state == "installed":
            self.removeRequested.emit(project)
        elif self._state in {"queued", "downloading", "cancelling"}:
            self.cancelRequested.emit(project)
        else:
            self.installRequested.emit(project)

    def _elide_status(self, message: str):
        self._full_status_message = str(message or "")
        clean = self._full_status_message.replace("\n", " ").strip()
        # Never show a generated JAR filename inside a compact card. It can be
        # longer than the whole footer and makes the layout look broken.
        if re.search(r"\.jar(?:\b|$|[.])", clean, flags=re.IGNORECASE):
            clean = "Скачивание файла" if self._state == "downloading" else "Подготовка файла"
            percent = re.search(r"(?<!\d)(\d{1,3})%", self._full_status_message)
            if percent and self._state == "downloading":
                clean += f" · {min(100, int(percent.group(1)))}%"
        if self._state == "idle":
            clean = "Открыть описание · версии · галерею"
        elif self._state == "queued":
            clean = "В очереди на установку"
        elif self._state == "installed":
            clean = "Установлено в выбранную сборку"
        elif self._state == "cancelling":
            clean = "Остановка загрузки…"
        elif self._state == "failed" and not clean:
            clean = "Не удалось установить · нажми «Повторить»"
        metrics = QFontMetrics(self.status_label.font())
        self.status_label.setText(metrics.elidedText(clean, Qt.ElideRight, max(80, self.status_label.width())))
        self.status_label.setToolTip(self._full_status_message or clean)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if all(hasattr(self, name) for name in ("status_label", "author_label", "stats")):
            self._elide_status(self._full_status_message)
            author_metrics = QFontMetrics(self.author_label.font())
            self.author_label.setText(author_metrics.elidedText(
                self._full_author, Qt.ElideRight, max(48, self.author_label.width())))
            full_stats = f"↓ {_number(self.project.get('downloads'))}   ♥ {_number(self.project.get('follows'))}"
            self.stats.setText(QFontMetrics(self.stats.font()).elidedText(
                full_stats, Qt.ElideRight, max(30, self.stats.width())))
            self.stats.setToolTip(full_stats)

    def set_install_state(self, state: str, message: str = ""):
        state = str(state or "idle")
        self._state = state
        labels = {
            "idle": "Скачать",
            "queued": "Убрать",
            "downloading": "Отменить",
            "cancelling": "Остановка…",
            "installed": "Удалить",
            "failed": "Повторить",
        }
        object_names = {
            "idle": "ModrinthActionButton",
            "queued": "ModrinthQueuedButton",
            "downloading": "ModrinthDownloadingButton",
            "cancelling": "ModrinthQueuedButton",
            "installed": "ModrinthInstalledButton",
            "failed": "ModrinthRetryButton",
        }
        self.action_button.setText(labels.get(state, "Скачать"))
        self.action_button.setObjectName(object_names.get(state, "ModrinthActionButton"))
        self.action_button.setEnabled(state != "cancelling" and not (state == "idle" and self._requires_loader))
        self.action_button.setToolTip(
            message or ("Для мода сначала выбери Fabric, Forge, NeoForge или Quilt." if self._requires_loader else "")
        )
        self.status_label.setProperty("state", state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.action_button.style().unpolish(self.action_button)
        self.action_button.style().polish(self.action_button)

        percent_match = re.search(r"(?<!\d)(\d{1,3})%", str(message or ""))
        if state == "installed":
            self.progress_bar.setValue(100)
        elif percent_match and state in {"queued", "downloading"}:
            self.progress_bar.setValue(max(0, min(100, int(percent_match.group(1)))))
        else:
            self.progress_bar.setValue(0)
        self.progress_bar.setProperty("state", state)
        self.progress_bar.style().unpolish(self.progress_bar)
        self.progress_bar.style().polish(self.progress_bar)

        if state == "idle":
            self._elide_status("")
        elif state == "failed" and not message:
            self._elide_status("Не удалось установить · нажми «Повторить»")
        else:
            self._elide_status(message)

    def set_icon_bytes(self, payload: bytes):
        pixmap = _safe_pixmap(payload, max_pixels=8_000_000, max_edge=512)
        if not pixmap.isNull():
            self.icon.setPixmap(pixmap.scaled(60, 60, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            self.icon.setText("")

    def mousePressEvent(self, event):
        self._left_press_inside = event.button() == Qt.LeftButton and self.rect().contains(event.pos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        should_open = (
            event.button() == Qt.LeftButton and self._left_press_inside
            and self.rect().contains(event.pos())
        )
        self._left_press_inside = False
        if should_open:
            child = self.childAt(event.pos())
            if child is self.action_button or (child and self.action_button.isAncestorOf(child)):
                super().mouseReleaseEvent(event)
                return
            self.detailsRequested.emit(dict(self.project))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ModrinthCatalogWidget(QWidget):
    """Адаптивная сетка карточек с отдельной прокруткой и пагинацией."""

    projectDetailsRequested = pyqtSignal(dict)
    installRequested = pyqtSignal(dict)
    removeRequested = pyqtSignal(dict)
    cancelRequested = pyqtSignal(dict)
    loadMoreRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.scroll = SmoothScrollArea()
        self.scroll.setObjectName("ModrinthCatalogScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setMinimumHeight(420)
        self.scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.scroll.verticalScrollBar().setSingleStep(22)
        self.scroll.verticalScrollBar().valueChanged.connect(self._check_auto_load)
        # Кинетическая прокрутка делает длинный каталог заметно плавнее на тачпаде и мыши.
        QScroller.grabGesture(self.scroll.viewport(), QScroller.TouchGesture)
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
        self.load_more_button = QPushButton("Загрузка по прокрутке")
        self.load_more_button.setObjectName("SecondaryButton")
        self.load_more_button.setCursor(Qt.PointingHandCursor)
        self.load_more_button.setFocusPolicy(Qt.NoFocus)
        self.load_more_button.setEnabled(False)
        self.load_more_button.hide()
        footer.addWidget(self.load_more_button)
        root.addLayout(footer)

        self.cards: list[ModrinthProjectCard] = []
        self._loading = False
        self._total_hits = 0
        self._reflow_pending = False
        self._column_count = 0
        self._has_more = False
        self._auto_load_pending = False

    def set_project_state(self, project_id: str, state: str, message: str = ""):
        project_id = str(project_id or "")
        for card in self.cards:
            if str(card.project.get("project_id", "")) == project_id:
                card.set_install_state(state, message)

    def set_installed_projects(self, states: dict[str, bool]):
        # Backward-compatible helper used by older call sites.
        self.set_project_states({
            project_id: (("installed", "Уже в сборке") if installed else ("idle", ""))
            for project_id, installed in states.items()
        })

    def set_project_states(self, states: dict[str, tuple[str, str]]):
        """Обновляет все карточки относительно выбранной целевой сборки."""
        for card in self.cards:
            project_id = str(card.project.get("project_id", ""))
            state, message = states.get(project_id, ("idle", ""))
            card.set_install_state(state, message)

    def _check_auto_load(self, value: int):
        if not self._has_more or self._loading or self._auto_load_pending:
            return
        bar = self.scroll.verticalScrollBar()
        if bar.maximum() > 0 and value < bar.maximum() - max(260, self.scroll.viewport().height() // 2):
            return
        self._auto_load_pending = True
        QTimer.singleShot(320 if bar.maximum() <= 0 else 120, self._emit_auto_load)

    def _emit_auto_load(self):
        self._auto_load_pending = False
        if self._has_more and not self._loading:
            self.loadMoreRequested.emit()

    def clear(self, message: str = "Загружаем рекомендации Modrinth…"):
        for card in self.cards:
            self.grid.removeWidget(card)
            card.deleteLater()
        self.cards.clear()
        self._total_hits = 0
        self._column_count = 0
        self._has_more = False
        self._auto_load_pending = False
        self.count_label.setText(message)
        self.load_more_button.hide()
        self.content.adjustSize()

    def set_loading(self, loading: bool, message: str | None = None):
        self._loading = bool(loading)
        self.load_more_button.setEnabled(False)
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
            card.removeRequested.connect(self.removeRequested)
            card.cancelRequested.connect(self.cancelRequested)
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
        self.set_has_more(shown < self._total_hits and bool(projects))
        return added

    def set_empty(self, message: str):
        self.clear(message)

    def set_has_more(self, has_more: bool):
        self._has_more = bool(has_more)
        # Никакой кнопки «Показать ещё»: следующая страница подгружается сама.
        self.load_more_button.hide()
        if self._has_more and not self._loading:
            QTimer.singleShot(360, lambda: self._check_auto_load(self.scroll.verticalScrollBar().value()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._reflow_pending:
            self._reflow_pending = True
            QTimer.singleShot(0, self._reflow)

    def _reflow(self):
        self._reflow_pending = False
        width = max(1, self.scroll.viewport().width())
        # Keep card text readable in compact windows. Prefer two roomy columns
        # over three compressed cards when the catalogue viewport is narrow.
        if width >= 1460:
            columns = 4
        elif width >= 1000:
            columns = 3
        elif width >= 640:
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
    imageReady = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.payload = b""
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setText("Загрузка изображения…")
        self.setFixedSize(260, 148)
        self.setObjectName("ModrinthGalleryImage")

    def set_image_bytes(self, payload: bytes):
        pixmap = _safe_pixmap(payload, max_pixels=24_000_000, max_edge=2048)
        if not pixmap.isNull():
            self.payload = bytes(payload)
            self._failed = False
            self.setPixmap(pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            self.setText("")
            self.imageReady.emit()

    def set_image_failed(self, _message: str = ""):
        self._failed = True
        self.setText("Изображение недоступно")

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
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        pixmap = _safe_pixmap(payload, max_pixels=24_000_000, max_edge=4096)
        if not pixmap.isNull():
            image.setPixmap(pixmap)
            image.setMinimumSize(pixmap.size())
        else:
            image.setText("Не удалось открыть изображение")
        scroll.setWidget(image)
        root.addWidget(scroll, 1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignRight)


class ModrinthProjectDetailsDialog(QWidget):
    """Встроенная страница проекта Modrinth с галереей и полноэкранным просмотром."""

    backRequested = pyqtSignal()
    installRequested = pyqtSignal(str)

    def __init__(self, project: dict, versions: list[dict], parent=None):
        super().__init__(parent)
        self.project = dict(project)
        self.versions = list(versions or [])
        self.setObjectName("ModrinthDetailsDialog")
        self._image_workers = []
        self._gallery_queue = []
        self._gallery_active = 0
        self._gallery_limit = 3
        self._gallery_labels = []
        self._preview_overlay = None
        self._preview_image = None
        self._preview_index = 0
        self._preview_labels = []

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(8)

        scroll = SmoothScrollArea()
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
        self.icon.setFixedSize(88, 88)
        self.icon.setAlignment(Qt.AlignCenter)
        header.addWidget(self.icon, 0, Qt.AlignTop)
        identity = QVBoxLayout()
        identity.setSpacing(5)
        title = QLabel(str(project.get("title", "Без названия"))[:160])
        title.setObjectName("PageHeading")
        title.setWordWrap(True)
        identity.addWidget(title)
        author = str(project.get("author", "Неизвестный автор"))[:120]
        stats = (f"Автор: {author}   ·   Загрузки: {_number(project.get('downloads'))}"
                 f"   ·   В избранном: {_number(project.get('follows'))}")
        self.stats_label = QLabel(stats)
        self.stats_label.setObjectName("Muted")
        self.stats_label.setWordWrap(True)
        identity.addWidget(self.stats_label)
        short = QLabel(str(project.get("description", "Описание пока не добавлено."))[:1000])
        short.setObjectName("Muted")
        short.setWordWrap(True)
        identity.addWidget(short)
        header.addLayout(identity, 1)
        content_layout.addWidget(header_card)

        gallery = project.get("gallery", []) or []
        if not isinstance(gallery, list):
            gallery = []
        if gallery:
            gallery_title = QLabel("Галерея проекта  ·  нажми на изображение, чтобы развернуть")
            gallery_title.setObjectName("SectionTitle")
            content_layout.addWidget(gallery_title)
            self.gallery_scroll = SmoothScrollArea()
            self.gallery_scroll.setObjectName("ModrinthGalleryScroll")
            self.gallery_scroll.setWidgetResizable(True)
            self.gallery_scroll.setFrameShape(QFrame.NoFrame)
            self.gallery_scroll.setFixedHeight(174)
            self.gallery_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
        self.body.setMinimumHeight(280)
        self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.body.setLineWrapMode(QTextEdit.WidgetWidth)
        markdown = self._clean_markdown(
            str(project.get("body", "") or project.get("description", "Описание отсутствует."))
        )
        try:
            self.body.setMarkdown(markdown)
        except Exception:
            self.body.setPlainText(self._markdown_fallback_text(markdown))
        content_layout.addWidget(self.body)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QFrame()
        footer.setObjectName("ModrinthDetailsFooter")
        footer_outer = QVBoxLayout(footer)
        footer_outer.setContentsMargins(10, 8, 10, 8)
        footer_outer.setSpacing(7)
        footer_top = QHBoxLayout()
        back_button = QPushButton("←  Назад к каталогу")
        back_button.setObjectName("SecondaryButton")
        back_button.setMinimumHeight(36)
        back_button.clicked.connect(self.backRequested.emit)
        footer_top.addWidget(back_button)
        footer_top.addStretch(1)
        footer_outer.addLayout(footer_top)
        footer_row = QHBoxLayout()
        footer_row.addWidget(QLabel("Совместимая версия"))
        self.version_combo = WheelSafeComboBox()
        self.version_combo.setMinimumHeight(40)
        self.version_combo.setMinimumWidth(140)
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
        self.install_button.clicked.connect(lambda: self.installRequested.emit(self.selected_version_id()))
        footer_row.addWidget(self.install_button)
        footer_outer.addLayout(footer_row)
        root.addWidget(footer)

    @staticmethod
    def _clean_markdown(markdown: str) -> str:
        """Sanitize untrusted Modrinth Markdown before rendering it in QTextBrowser."""
        import re
        text = str(markdown or "")[:100000]
        text = re.sub(r"(?is)<(script|style|svg|picture)\b.*?</\1\s*>", "", text)
        text = re.sub(r"(?is)<img\b[^>]*?/?>", "", text)
        text = re.sub(r"(?is)<!--.*?-->", "", text)
        text = re.sub(r"(?is)<(div|section|figure|table)\b[^>]*>.*?</\1\s*>", "", text)
        text = re.sub(r"!\[([^]]*)\]\(.*?\)", r"\1", text)
        text = re.sub(r"(?is)<[^>]{1,2000}>", " ", text)
        text = re.sub(r"(?m)^\s*[\u25a0\u25a1\ufffd]+\s*$", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > 40000:
            text = text[:40000].rstrip() + "\n\n*Описание обрезано в интерфейсе, чтобы не перегружать окно.*"
        return text or "Описание проекта пока не добавлено."

    @staticmethod
    def _markdown_fallback_text(markdown: str) -> str:
        import re
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", str(markdown or "")[:40000])
        text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
        text = re.sub(r"[*_`~]", "", text)
        return text[:40000] or "Описание проекта не удалось отобразить."

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
            label.imageReady.connect(self._gallery_preview_maybe_refresh)
            self.gallery_layout.addWidget(label)
            self._gallery_labels.append(label)
            self._gallery_queue.append((url, label))
        self.gallery_layout.addStretch(1)
        self._start_next_gallery_images()

    def _start_next_gallery_images(self):
        # Import locally: modrinth_ui is also used by the launcher, so the worker
        # is resolved only when the gallery starts, avoiding a circular import.
        try:
            from mod_service import ModrinthImageWorker
        except Exception as exc:
            for _url, label in self._gallery_queue:
                label.setText("Не удалось загрузить изображения")
            self._gallery_queue.clear()
            return
        owner = self.window()
        while self._gallery_queue and self._gallery_active < self._gallery_limit:
            url, label = self._gallery_queue.pop(0)
            worker_parent = owner if owner is not self else self
            worker = ModrinthImageWorker(url, worker_parent)
            worker.loaded.connect(label.set_image_bytes)
            worker.failed.connect(label.set_image_failed)
            import weakref
            page_ref = weakref.ref(self)
            def cleanup(current=worker, page_ref=page_ref, main_window=owner):
                if hasattr(main_window, "mod_image_workers"):
                    try:
                        if current in main_window.mod_image_workers:
                            main_window.mod_image_workers.remove(current)
                    except RuntimeError:
                        pass
                current.deleteLater()
                page = page_ref()
                if page is not None:
                    try:
                        if current in page._image_workers:
                            page._image_workers.remove(current)
                        page._gallery_active = max(0, page._gallery_active - 1)
                        if page.isVisible():
                            page._start_next_gallery_images()
                    except RuntimeError:
                        pass
            worker.finished.connect(cleanup)
            if hasattr(owner, "mod_image_workers"):
                owner.mod_image_workers.append(worker)
            self._image_workers.append(worker)
            self._gallery_active += 1
            worker.start()

    def _show_image_preview(self, payload: bytes):
        self._preview_labels = list(self._gallery_labels)
        self._preview_index = next(
            (index for index, label in enumerate(self._preview_labels) if label.payload == payload), 0
        )
        if not self._preview_labels:
            return
        if self._preview_overlay is None:
            owner = self.window()
            overlay_parent = getattr(owner, "window_root", self)
            overlay = QFrame(overlay_parent)
            overlay.setObjectName("ModrinthImageOverlay")
            overlay.setStyleSheet("QFrame#ModrinthImageOverlay { background: rgba(4, 8, 16, 248); border-radius: 14px; }")
            layout = QVBoxLayout(overlay)
            layout.setContentsMargins(18, 14, 18, 18)
            layout.setSpacing(12)
            top = QHBoxLayout()
            title = QLabel("Галерея проекта")
            title.setObjectName("PageHeading")
            top.addWidget(title, 1)
            close = QPushButton("Закрыть  ×")
            close.setObjectName("SecondaryButton")
            close.clicked.connect(self.close_preview)
            top.addWidget(close)
            layout.addLayout(top)
            self._preview_image = QLabel("Загрузка изображения…")
            self._preview_image.setAlignment(Qt.AlignCenter)
            self._preview_image.setMinimumSize(240, 180)
            self._preview_image.setStyleSheet("background: #050912; border-radius: 10px;")
            layout.addWidget(self._preview_image, 1)
            bottom = QHBoxLayout()
            self._preview_counter = QLabel("")
            self._preview_counter.setObjectName("Muted")
            bottom.addWidget(self._preview_counter, 1)
            previous = QPushButton("‹  Предыдущее")
            previous.setObjectName("SecondaryButton")
            previous.clicked.connect(lambda: self._step_preview(-1))
            bottom.addWidget(previous)
            following = QPushButton("Следующее  ›")
            following.setObjectName("SecondaryButton")
            following.clicked.connect(lambda: self._step_preview(1))
            bottom.addWidget(following)
            layout.addLayout(bottom)
            self._preview_prev = previous
            self._preview_next = following
            self._preview_overlay = overlay
        overlay_parent = self._preview_overlay.parentWidget()
        self._preview_overlay.setGeometry(overlay_parent.rect() if overlay_parent is not None else self.rect())
        self._preview_overlay.show()
        self._preview_overlay.raise_()
        self._update_preview_image()

    def _step_preview(self, direction: int):
        if not self._preview_labels:
            return
        self._preview_index = (self._preview_index + direction) % len(self._preview_labels)
        self._update_preview_image()

    def _gallery_preview_maybe_refresh(self):
        if self._preview_overlay is not None and self._preview_overlay.isVisible():
            self._update_preview_image()

    def _update_preview_image(self):
        if not self._preview_overlay or not self._preview_image or not self._preview_labels:
            return
        label = self._preview_labels[self._preview_index]
        if not label.payload:
            self._preview_image.setPixmap(QPixmap())
            self._preview_image.setText(
                "Не удалось загрузить изображение" if getattr(label, "_failed", False)
                else "Изображение загружается…"
            )
        else:
            pixmap = _safe_pixmap(label.payload, max_pixels=24_000_000, max_edge=4096)
            if pixmap.isNull():
                self._preview_image.setPixmap(QPixmap())
                self._preview_image.setText("Не удалось открыть изображение")
            else:
                self._preview_image.setPixmap(pixmap.scaled(
                    self._preview_image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                ))
        self._preview_counter.setText(f"{self._preview_index + 1} / {len(self._preview_labels)}")
        self._preview_prev.setEnabled(len(self._preview_labels) > 1)
        self._preview_next.setEnabled(len(self._preview_labels) > 1)

    def close_preview(self) -> bool:
        if self._preview_overlay is not None and self._preview_overlay.isVisible():
            self._preview_overlay.hide()
            return True
        return False

    def update_preview_geometry(self):
        if self._preview_overlay is not None and self._preview_overlay.isVisible():
            overlay_parent = self._preview_overlay.parentWidget()
            self._preview_overlay.setGeometry(overlay_parent.rect() if overlay_parent is not None else self.rect())
            self._update_preview_image()

    def dispose_preview(self):
        if self._preview_overlay is not None:
            self._preview_overlay.hide()
            self._preview_overlay.setParent(None)
            self._preview_overlay.deleteLater()
            self._preview_overlay = None
            self._preview_image = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_preview_geometry()

    def selected_version_id(self) -> str:
        return str(self.version_combo.currentData() or "")

    def set_project_icon(self, payload: bytes):
        pixmap = _safe_pixmap(payload, max_pixels=8_000_000, max_edge=512)
        if not pixmap.isNull():
            self.icon.setPixmap(pixmap.scaled(88, 88, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.icon.setText("")

