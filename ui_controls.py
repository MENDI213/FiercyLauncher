"""Управляемые элементы Qt без изменения значений от колёсика мыши."""
from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFrame,
    QListView,
    QProxyStyle,
    QSpinBox,
    QStyle,
    QSlider,
    QTabBar,
)


class CompactPopupProxyStyle(QProxyStyle):
    """Отключает платформенный menu-popup для QComboBox.

    На некоторых сборках Qt/Windows системный popup игнорирует
    maxVisibleItems и рисуется с нативной рамкой. Неиспользование menu-popup
    заставляет QComboBox показывать обычный QListView с ограниченной высотой.
    """

    def __init__(self, base_style="Fusion"):
        super().__init__(base_style)

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


class WheelSafeComboBox(QComboBox):
    """Компактный комбобокс с прокручиваемым popup без системной рамки."""

    MAX_VISIBLE_ROWS = 8
    MAX_POPUP_HEIGHT = 360

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMaxVisibleItems(self.MAX_VISIBLE_ROWS)
        self.setMaxCount(10000)
        self.setAttribute(Qt.WA_StyledBackground, True)

        view = QListView(self)
        view.setObjectName("FiercyComboPopupView")
        view.setFrameShape(QFrame.NoFrame)
        view.setLineWidth(0)
        view.setMidLineWidth(0)
        view.setUniformItemSizes(True)
        view.setMinimumHeight(0)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setView(view)
        self._prepare_popup_frame()

    def _popup_container(self):
        """Получает внутренний popup QComboBox, если Qt уже его создал."""
        view = self.view()
        if view is None:
            return None
        popup = view.window()
        if (popup is self or not popup.isWindow()
                or not (popup.windowFlags() & Qt.Popup)):
            popup = None
            parent = view.parentWidget()
            while parent is not None and parent is not self:
                if parent.isWindow() and (parent.windowFlags() & Qt.Popup):
                    popup = parent
                    break
                parent = parent.parentWidget()
        if popup is None:
            return None
        return popup

    def _prepare_popup_frame(self):
        view = self.view()
        if view is None:
            return
        view.setFrameShape(QFrame.NoFrame)
        view.setLineWidth(0)
        view.setMidLineWidth(0)
        view.setMinimumHeight(0)

        popup = self._popup_container()
        if popup is None:
            return
        popup.setObjectName("FiercyComboPopup")
        popup.setAttribute(Qt.WA_StyledBackground, True)
        popup.setAutoFillBackground(False)
        popup.setMinimumHeight(0)
        popup.setContentsMargins(0, 0, 0, 0)
        popup.setStyleSheet(
            "QFrame#FiercyComboPopup { border: 0px none; padding: 0px; margin: 0px; }"
            "QFrame#FiercyComboPopup QFrame { border: 0px none; padding: 0px; margin: 0px; }"
            "QFrame#FiercyComboPopup QListView { border: 0px none; outline: 0px; }"
        )
        if isinstance(popup, QFrame):
            popup.setFrameShape(QFrame.NoFrame)
            popup.setLineWidth(0)
            popup.setMidLineWidth(0)

        # Внутренний контейнер Qt содержит собственные QFrame/scrollbar.
        # Убираем их рамки, не меняя флаги видимого окна (setWindowFlags()
        # после показа скрывает popup на Windows).
        for frame in popup.findChildren(QFrame):
            frame.setFrameShape(QFrame.NoFrame)
            frame.setLineWidth(0)
            frame.setMidLineWidth(0)
        layout = popup.layout()
        if layout is not None:
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

    def _constrain_popup(self):
        view = self.view()
        popup = self._popup_container()
        if view is None or popup is None:
            return

        screen = QApplication.screenAt(self.mapToGlobal(self.rect().center())) or QApplication.primaryScreen()
        available = screen.availableGeometry().height() if screen else 800
        max_height = min(self.MAX_POPUP_HEIGHT, max(220, int(available * 0.46)))
        row_height = view.sizeHintForRow(0) if view.model() and view.model().rowCount() else 0
        row_height = max(28, row_height or self.sizeHint().height())
        rows_height = row_height * self.MAX_VISIBLE_ROWS + 12
        max_height = min(max_height, rows_height)

        view.setMinimumHeight(0)
        view.setMaximumHeight(max_height)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        popup.setMinimumHeight(0)
        popup.setMaximumHeight(max_height + 4)
        if popup.height() > max_height + 4:
            popup.resize(popup.width(), max_height + 4)
        self._prepare_popup_frame()

    def showPopup(self):
        self.setMaxVisibleItems(self.MAX_VISIBLE_ROWS)
        self._prepare_popup_frame()
        super().showPopup()
        # QComboBox создаёт свой QFrame-контейнер лениво, поэтому ограничиваем
        # высоту после того, как Qt закончит его создание и первоначальную
        # компоновку. Повторный вызов также удаляет рамку нативного контейнера.
        QTimer.singleShot(0, self._constrain_popup)
        QTimer.singleShot(30, self._constrain_popup)

    def wheelEvent(self, event):
        # Колесо всегда принадлежит прокрутке страницы.
        event.ignore()


class WheelSafeFontComboBox(QFontComboBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class WheelSafeSpinBox(QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class WheelSafeDoubleSpinBox(QDoubleSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class WheelSafeSlider(QSlider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class WheelSafeTabBar(QTabBar):
    """Не переключает вкладки при прокрутке колёсиком мыши."""

    def wheelEvent(self, event):
        event.ignore()
