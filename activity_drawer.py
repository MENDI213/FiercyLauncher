"""Compact right-side drawer for download tasks and persistent notifications."""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QToolButton,
    QStackedWidget, QScrollArea, QWidget, QProgressBar, QSpinBox,
    QListWidget, QListWidgetItem, QCheckBox,
)


class DownloadRow(QFrame):
    pauseRequested = pyqtSignal(str)
    resumeRequested = pyqtSignal(str)
    cancelRequested = pyqtSignal(str)
    removeRequested = pyqtSignal(str)

    def __init__(self, task_id: str, parent=None):
        super().__init__(parent)
        self.task_id = str(task_id)
        self.setObjectName("DownloadTaskCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)
        top = QHBoxLayout()
        self.title = QLabel("Загрузка")
        self.title.setObjectName("DownloadTaskTitle")
        self.title.setWordWrap(True)
        top.addWidget(self.title, 1)
        self.state = QLabel("В очереди")
        self.state.setObjectName("DownloadTaskState")
        top.addWidget(self.state, 0, Qt.AlignRight | Qt.AlignTop)
        root.addLayout(top)
        self.details = QLabel("")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        root.addWidget(self.details)
        self.progress = QProgressBar()
        self.progress.setObjectName("DownloadTaskProgress")
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(9)
        root.addWidget(self.progress)
        bottom = QHBoxLayout()
        self.speed = QLabel("")
        self.speed.setObjectName("Muted")
        bottom.addWidget(self.speed, 1)
        self.pause_button = QPushButton("Пауза")
        self.pause_button.setObjectName("SecondaryButton")
        self.pause_button.clicked.connect(self._toggle_pause)
        bottom.addWidget(self.pause_button)
        self.remove_button = QPushButton("Отменить")
        self.remove_button.setObjectName("DangerButton")
        self.remove_button.clicked.connect(self._remove_or_cancel)
        bottom.addWidget(self.remove_button)
        root.addLayout(bottom)

    def _toggle_pause(self) -> None:
        if getattr(self, "_state", "") == "paused":
            self.resumeRequested.emit(self.task_id)
        else:
            self.pauseRequested.emit(self.task_id)

    def _remove_or_cancel(self) -> None:
        if getattr(self, "_state", "") in {"queued", "downloading", "paused", "cancelling"}:
            self.cancelRequested.emit(self.task_id)
        else:
            self.removeRequested.emit(self.task_id)

    def update_task(self, task: dict) -> None:
        self._state = str(task.get("state", "queued"))
        self.title.setText(str(task.get("title", "Загрузка"))[:100])
        self.state.setText(str(task.get("state_label", "В очереди")))
        self.details.setText(str(task.get("details", ""))[:260])
        percent = int(task.get("progress", -1))
        if percent < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, percent)))
        self.speed.setText(str(task.get("speed_label", "")))
        state = str(task.get("state", "queued"))
        can_pause = bool(task.get("can_pause", False)) and state in {"downloading", "paused"}
        self.pause_button.setVisible(can_pause)
        self.pause_button.setText("Продолжить" if state == "paused" else "Пауза")
        self.pause_button.setEnabled(True)
        self.remove_button.setText("Отменить" if state in {"queued", "downloading", "paused", "cancelling"} else "Убрать")
        self.remove_button.setObjectName("DangerButton" if state in {"queued", "downloading", "paused", "cancelling"} else "SecondaryButton")
        self.remove_button.style().unpolish(self.remove_button)
        self.remove_button.style().polish(self.remove_button)


class ActivityDrawer(QFrame):
    pauseRequested = pyqtSignal(str)
    resumeRequested = pyqtSignal(str)
    cancelRequested = pyqtSignal(str)
    removeRequested = pyqtSignal(str)
    speedLimitChanged = pyqtSignal(int)
    notificationRead = pyqtSignal(str)
    filtersChanged = pyqtSignal(dict)
    clearReadRequested = pyqtSignal()
    closeRequested = pyqtSignal()

    def __init__(self, parent=None, speed_limit_mbps: int = 0, filters: dict | None = None):
        super().__init__(parent)
        self.setObjectName("ActivityDrawer")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(408)
        self._rows: dict[str, DownloadRow] = {}
        self._filters = {k: bool((filters or {}).get(k, True)) for k in ("info", "success", "warning", "error")}
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title = QLabel("Центр активности")
        title.setObjectName("ActivityTitle")
        title_col.addWidget(title)
        self.counter_label = QLabel("Все операции и сообщения в одном месте")
        self.counter_label.setObjectName("Muted")
        title_col.addWidget(self.counter_label)
        header.addLayout(title_col, 1)
        close = QToolButton()
        close.setObjectName("ToastClose")
        close.setText("×")
        close.clicked.connect(self.closeRequested)
        header.addWidget(close, 0, Qt.AlignTop)
        root.addLayout(header)

        tabs = QHBoxLayout()
        self.downloads_tab = QPushButton("↓  Загрузки")
        self.notifications_tab = QPushButton("♧  Уведомления")
        for button in (self.downloads_tab, self.notifications_tab):
            button.setObjectName("ActivityTab")
            button.setCheckable(True)
            tabs.addWidget(button, 1)
        self.downloads_tab.setChecked(True)
        root.addLayout(tabs)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        downloads_page = QWidget()
        downloads_layout = QVBoxLayout(downloads_page)
        downloads_layout.setContentsMargins(0, 0, 0, 0)
        downloads_layout.setSpacing(8)
        self.download_scroll = QScrollArea()
        self.download_scroll.setWidgetResizable(True)
        self.download_scroll.setFrameShape(QFrame.NoFrame)
        self.download_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.download_list_host = QWidget()
        self.download_list_layout = QVBoxLayout(self.download_list_host)
        self.download_list_layout.setContentsMargins(0, 0, 0, 0)
        self.download_list_layout.setSpacing(8)
        self.empty_downloads = QLabel("Пока нет загрузок\nНовые задачи появятся здесь автоматически.")
        self.empty_downloads.setObjectName("Muted")
        self.empty_downloads.setAlignment(Qt.AlignCenter)
        self.download_list_layout.addWidget(self.empty_downloads)
        self.download_list_layout.addStretch(1)
        self.download_scroll.setWidget(self.download_list_host)
        downloads_layout.addWidget(self.download_scroll, 1)
        speed_card = QFrame()
        speed_card.setObjectName("DownloadSpeedCard")
        speed_layout = QVBoxLayout(speed_card)
        speed_layout.setContentsMargins(12, 10, 12, 10)
        speed_layout.setSpacing(5)
        speed_top = QHBoxLayout()
        speed_top.addWidget(QLabel("Ограничение скорости"), 1)
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(0, 1000)
        self.speed_spin.setSingleStep(1)
        self.speed_spin.setValue(max(0, min(1000, int(speed_limit_mbps or 0))))
        self.speed_spin.setSuffix(" МБ/с")
        self.speed_spin.setToolTip("0 — без ограничения. Лимит общий для поддерживаемых загрузок лаунчера.")
        self.speed_spin.valueChanged.connect(self.speedLimitChanged)
        speed_top.addWidget(self.speed_spin)
        speed_layout.addLayout(speed_top)
        speed_hint = QLabel("0 = без лимита · значение применяется ко всем активным загрузкам")
        speed_hint.setObjectName("Muted")
        speed_hint.setWordWrap(True)
        speed_layout.addWidget(speed_hint)
        downloads_layout.addWidget(speed_card)
        self.pages.addWidget(downloads_page)

        notifications_page = QWidget()
        notifications_layout = QVBoxLayout(notifications_page)
        notifications_layout.setContentsMargins(0, 0, 0, 0)
        notifications_layout.setSpacing(8)
        filters_card = QFrame()
        filters_card.setObjectName("NotificationFilters")
        filters_layout = QVBoxLayout(filters_card)
        filters_layout.setContentsMargins(10, 8, 10, 8)
        filters_layout.setSpacing(6)
        filters_heading = QLabel("Показывать типы")
        filters_heading.setObjectName("Muted")
        filters_layout.addWidget(filters_heading)
        filters_row = QHBoxLayout()
        self.filter_checks = {}
        filter_names = (("info", "Инфо"), ("success", "Успех"), ("warning", "Важно"), ("error", "Ошибки"))
        for key, label in filter_names:
            check = QCheckBox(label)
            check.setChecked(self._filters[key])
            check.toggled.connect(lambda checked, token=key: self._filter_changed(token, checked))
            self.filter_checks[key] = check
            filters_row.addWidget(check)
        filters_layout.addLayout(filters_row)
        notifications_layout.addWidget(filters_card)
        self.notification_list = QListWidget()
        self.notification_list.setObjectName("NotificationHistoryList")
        self.notification_list.itemClicked.connect(self._notification_clicked)
        notifications_layout.addWidget(self.notification_list, 1)
        clear_read = QPushButton("Очистить прочитанные")
        clear_read.setObjectName("SecondaryButton")
        clear_read.clicked.connect(self.clearReadRequested)
        notifications_layout.addWidget(clear_read, 0, Qt.AlignRight)
        self.pages.addWidget(notifications_page)

        self.downloads_tab.clicked.connect(lambda: self._select_page(0))
        self.notifications_tab.clicked.connect(lambda: self._select_page(1))
        self.hide()

    def _select_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.downloads_tab.setChecked(index == 0)
        self.notifications_tab.setChecked(index == 1)

    def _filter_changed(self, key: str, enabled: bool) -> None:
        self._filters[key] = bool(enabled)
        self.filtersChanged.emit(dict(self._filters))

    def _notification_clicked(self, item: QListWidgetItem) -> None:
        notification_id = str(item.data(Qt.UserRole) or "")
        if notification_id:
            self.notificationRead.emit(notification_id)

    def show_downloads(self) -> None:
        self._select_page(0)
        self.show()
        self.raise_()

    def show_notifications(self) -> None:
        self._select_page(1)
        self.show()
        self.raise_()

    def update_downloads(self, tasks: list[dict]) -> None:
        incoming = {str(task["id"]): task for task in tasks if task.get("id")}
        for task_id in list(self._rows):
            if task_id not in incoming:
                row = self._rows.pop(task_id)
                self.download_list_layout.removeWidget(row)
                row.deleteLater()
        self.empty_downloads.setVisible(not bool(incoming))
        for task_id, task in incoming.items():
            row = self._rows.get(task_id)
            if row is None:
                row = DownloadRow(task_id, self.download_list_host)
                row.pauseRequested.connect(self.pauseRequested)
                row.resumeRequested.connect(self.resumeRequested)
                row.cancelRequested.connect(self.cancelRequested)
                row.removeRequested.connect(self.removeRequested)
                self._rows[task_id] = row
                self.download_list_layout.insertWidget(max(0, self.download_list_layout.count() - 1), row)
            row.update_task(task)
        active = sum(1 for task in incoming.values() if task.get("state") in {"queued", "downloading", "paused", "cancelling"})
        if self.pages.currentIndex() == 0:
            self.counter_label.setText(f"Активных загрузок: {active} · Всего в списке: {len(incoming)}")

    def update_notifications(self, notifications: list[dict]) -> None:
        self.notification_list.clear()
        visible = 0
        for item in reversed(notifications):
            kind = str(item.get("kind", "info"))
            if not self._filters.get(kind, True):
                continue
            visible += 1
            stamp = str(item.get("time_label", ""))
            title = str(item.get("title", "Уведомление"))
            message = str(item.get("message", ""))
            prefix = "● " if not item.get("read", False) else ""
            text = f"{prefix}{stamp}  ·  {title}\n{message}"
            row = QListWidgetItem(text)
            row.setData(Qt.UserRole, str(item.get("id", "")))
            row.setToolTip(text)
            self.notification_list.addItem(row)
        if not visible:
            self.notification_list.addItem(QListWidgetItem("Нет уведомлений с выбранными фильтрами."))
        if self.pages.currentIndex() == 1:
            self.counter_label.setText(f"Показано уведомлений: {visible}")

    def filter_state(self) -> dict:
        return dict(self._filters)
