"""Диалог создания сборки с официальными списками Minecraft и версий загрузчика."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QFormLayout, QDialogButtonBox
from ui_controls import WheelSafeComboBox


class CreateProfileDialog(QDialog):
    def __init__(self, versions: list[dict], parent=None):
        super().__init__(parent)
        self.owner = parent
        self.setWindowTitle("Создать игровой профиль")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setObjectName("CreateProfileDialog")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)
        title = QLabel("Новая сборка")
        title.setStyleSheet("font-size: 18pt; font-weight: 900;")
        subtitle = QLabel(
            "Выбери версию Minecraft и загрузчик. Версия загрузчика подберётся из официального списка совместимых релизов."
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("Muted")
        root.addWidget(title)
        root.addWidget(subtitle)

        form = QFormLayout()
        form.setSpacing(12)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Например: Fiercy Survival")
        self.version_combo = WheelSafeComboBox()
        self.version_combo.setMinimumHeight(40)
        for item in versions:
            version_id = str(item.get("id", ""))
            if version_id:
                self.version_combo.addItem(version_id, version_id)
        self.loader_combo = WheelSafeComboBox()
        self.loader_combo.setMinimumHeight(40)
        for label, value in (("Vanilla (без модов)", "vanilla"), ("Fabric", "fabric"),
                             ("Forge", "forge"), ("NeoForge", "neoforge"), ("Quilt", "quilt")):
            self.loader_combo.addItem(label, value)
        self.loader_version_combo = WheelSafeComboBox()
        self.loader_version_combo.setMinimumHeight(40)
        self.loader_version_combo.addItem("Последняя стабильная", "")
        self.loader_version_combo.setEnabled(False)
        self.loader_status = QLabel("Для Vanilla отдельная версия загрузчика не требуется.")
        self.loader_status.setObjectName("Muted")
        self.loader_status.setWordWrap(True)
        form.addRow("Название сборки", self.name_edit)
        form.addRow("Minecraft", self.version_combo)
        form.addRow("Загрузчик", self.loader_combo)
        form.addRow("Версия загрузчика", self.loader_version_combo)
        form.addRow("", self.loader_status)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("Создать сборку")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.version_combo.currentIndexChanged.connect(self._load_loader_versions)
        self.loader_combo.currentIndexChanged.connect(self._load_loader_versions)
        self.name_edit.setFocus()
        self._loader_workers = []
        self._load_loader_versions()

    def _load_loader_versions(self, *_args):
        loader = str(self.loader_combo.currentData() or "vanilla")
        minecraft = str(self.version_combo.currentData() or "")
        self.loader_version_combo.blockSignals(True)
        self.loader_version_combo.clear()
        self.loader_version_combo.addItem("Последняя стабильная", "")
        self.loader_version_combo.blockSignals(False)
        if loader == "vanilla":
            self.loader_version_combo.setEnabled(False)
            self.loader_status.setText("Для Vanilla отдельная версия загрузчика не требуется.")
            return
        self.loader_version_combo.setEnabled(False)
        self.loader_status.setText("Загружаем совместимые стабильные версии…")
        worker = None
        if self.owner is not None:
            from minecraft_service import LoaderVersionListWorker
            worker = LoaderVersionListWorker(loader, minecraft, self.owner)
            worker.loaded.connect(self._loader_versions_loaded)
            worker.failed.connect(self._loader_versions_failed)
            worker.finished.connect(lambda current=worker: self._forget_worker(current))
            retain = getattr(self.owner, "_retain_worker", None)
            if callable(retain):
                retain(worker)
            self._loader_workers.append(worker)
            worker.start()

    def _forget_worker(self, worker):
        if worker in self._loader_workers:
            self._loader_workers.remove(worker)

    def _loader_versions_loaded(self, loader: str, minecraft: str, versions: list):
        if (loader != str(self.loader_combo.currentData() or "vanilla")
                or minecraft != str(self.version_combo.currentData() or "")):
            return
        current = self.loader_version_combo.currentData()
        self.loader_version_combo.blockSignals(True)
        self.loader_version_combo.clear()
        self.loader_version_combo.addItem("Последняя стабильная", "")
        for version in versions:
            self.loader_version_combo.addItem(str(version), str(version))
        pos = self.loader_version_combo.findData(current)
        self.loader_version_combo.setCurrentIndex(pos if pos >= 0 else 0)
        self.loader_version_combo.blockSignals(False)
        self.loader_version_combo.setEnabled(bool(versions))
        if versions:
            self.loader_status.setText(f"Найдено стабильных версий загрузчика: {len(versions)}.")
        else:
            self.loader_status.setText("Для этой версии Minecraft стабильные версии не найдены. Попробуй другой Minecraft.")

    def _loader_versions_failed(self, loader: str, minecraft: str, message: str):
        if (loader != str(self.loader_combo.currentData() or "vanilla")
                or minecraft != str(self.version_combo.currentData() or "")):
            return
        self.loader_version_combo.setEnabled(True)
        self.loader_status.setText(f"Не удалось получить список версий. Можно попробовать последнюю: {message}")

    def values(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "minecraft_version": str(self.version_combo.currentData() or ""),
            "loader": str(self.loader_combo.currentData() or "vanilla"),
            "loader_version": str(self.loader_version_combo.currentData() or ""),
        }

    def accept(self):
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            return
        if not self.version_combo.currentData():
            self.loader_status.setText("Выбери версию Minecraft.")
            return
        super().accept()
