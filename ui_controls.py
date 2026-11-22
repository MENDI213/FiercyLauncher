"""Qt controls that never change their value just because the mouse is over them."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox, QSpinBox, QSlider, QDoubleSpinBox, QFontComboBox


class WheelSafeComboBox(QComboBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        # Колесо всегда принадлежит прокрутке страницы. Значение меняется
        # только явным кликом, перетаскиванием или вводом с клавиатуры.
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
