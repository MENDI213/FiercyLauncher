"""Верхние всплывающие уведомления в стиле мобильных toast/banner."""
from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint
from PyQt5.QtWidgets import QFrame, QLabel, QHBoxLayout, QVBoxLayout, QToolButton, QGraphicsOpacityEffect


class ToastNotification(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("ToastCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(520)
        self.setMinimumHeight(82)
        self.hide()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_animated)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._opacity_anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._position_anim = QPropertyAnimation(self, b"pos", self)
        self._hiding = False
        self._opacity_anim.finished.connect(self._finish_hide)

        row = QHBoxLayout(self)
        row.setContentsMargins(17, 12, 13, 12)
        row.setSpacing(12)
        self.icon_label = QLabel("✦")
        self.icon_label.setObjectName("ToastIcon")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setFixedWidth(36)
        row.addWidget(self.icon_label)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(3)
        self.title_label = QLabel("Уведомление")
        self.title_label.setObjectName("ToastTitle")
        self.message_label = QLabel("")
        self.message_label.setObjectName("ToastMessage")
        self.message_label.setWordWrap(True)
        text_box.addWidget(self.title_label)
        text_box.addWidget(self.message_label)
        row.addLayout(text_box, 1)
        self.close_button = QToolButton()
        self.close_button.setObjectName("ToastClose")
        self.close_button.setText("×")
        self.close_button.clicked.connect(self.hide_animated)
        row.addWidget(self.close_button, 0, Qt.AlignTop)

    def show_message(self, title: str, message: str, kind: str = "info", duration: int = 4300):
        icons = {"info": "✦", "success": "✓", "warning": "!", "error": "×"}
        self.icon_label.setText(icons.get(kind, "✦"))
        self.title_label.setText(str(title)[:100])
        self.message_label.setText(str(message)[:500])
        self.adjustSize()
        width = min(560, max(360, self.parentWidget().width() - 50))
        self.setFixedWidth(width)
        self.adjustSize()
        x = (self.parentWidget().width() - self.width()) // 2
        target = QPoint(max(12, x), 76)
        self.move(target.x(), -self.height() - 12)
        self._effect.setOpacity(0.0)
        self.show()
        self.raise_()
        self._position_anim.stop()
        self._position_anim.setDuration(360)
        self._position_anim.setStartValue(self.pos())
        self._position_anim.setEndValue(target)
        self._position_anim.setEasingCurve(QEasingCurve.OutBack)
        self._position_anim.start()
        self._opacity_anim.stop()
        self._opacity_anim.setDuration(230)
        self._opacity_anim.setStartValue(0.0)
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._opacity_anim.start()
        self._hiding = False
        self._hide_timer.start(max(1500, duration))

    def hide_animated(self):
        if not self.isVisible():
            return
        self._hide_timer.stop()
        self._opacity_anim.stop()
        self._opacity_anim.setDuration(190)
        self._opacity_anim.setStartValue(self._effect.opacity())
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.setEasingCurve(QEasingCurve.InCubic)
        self._hiding = True
        self._opacity_anim.start()

    def _finish_hide(self):
        if self._hiding:
            self.hide()
            self._hiding = False

    def reposition(self):
        if self.isVisible():
            x = (self.parentWidget().width() - self.width()) // 2
            self.move(max(12, x), max(76, self.y()))
