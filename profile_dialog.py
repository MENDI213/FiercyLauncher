"""Диалог создания игрового профиля: название, Minecraft и мод-загрузчик."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QFormLayout, QDialogButtonBox,
)


class CreateProfileDialog(QDialog):
    def __init__(self, versions: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Создать игровой профиль")
        self.setModal(True)
        self.setMinimumWidth(470)
        self.setObjectName("CreateProfileDialog")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)
        title = QLabel("Новая сборка")
        title.setStyleSheet("font-size: 18pt; font-weight: 900;")
        subtitle = QLabel("Задай имя профиля и выбери версию Minecraft и загрузчик. После создания лаунчер установит нужные игровые файлы.")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("Muted")
        root.addWidget(title)
        root.addWidget(subtitle)
        form = QFormLayout()
        form.setSpacing(12)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Например: Fiercy Survival")
        self.version_combo = QComboBox()
        for item in versions:
            version_id = str(item.get("id", ""))
            if version_id:
                self.version_combo.addItem(version_id, version_id)
        self.loader_combo = QComboBox()
        for label, value in (("Vanilla", "vanilla"), ("Forge", "forge"),
                             ("NeoForge", "neoforge"), ("Fabric", "fabric"), ("Quilt", "quilt")):
            self.loader_combo.addItem(label, value)
        self.loader_version = QLineEdit()
        self.loader_version.setPlaceholderText("Пусто — последняя совместимая версия")
        form.addRow("Название профиля", self.name_edit)
        form.addRow("Minecraft", self.version_combo)
        form.addRow("Загрузчик", self.loader_combo)
        form.addRow("Версия загрузчика", self.loader_version)
        root.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("Создать и установить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.name_edit.setFocus()

    def values(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "minecraft_version": str(self.version_combo.currentData() or ""),
            "loader": str(self.loader_combo.currentData() or "vanilla"),
            "loader_version": self.loader_version.text().strip(),
        }

    def accept(self):
        if not self.name_edit.text().strip() or not self.version_combo.currentData():
            self.name_edit.setFocus()
            return
        super().accept()
