# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = [
    "PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets",
    "minecraft_launcher_lib", "minecraft_launcher_lib.install",
    "minecraft_launcher_lib.command", "minecraft_launcher_lib.runtime",
    "minecraft_launcher_lib.mod_loader", "minecraft_launcher_lib.microsoft_account",
    "keyring", "requests", "packaging.version", "psutil",
    "keyring.backends.Windows", "keyring.backends.SecretService", "pypresence",
    "ui_controls", "profile_dialog", "mod_service", "modpack_service", "instance_import", "storage_worker", "version_metadata",
]
hidden += collect_submodules("minecraft_launcher_lib")
hidden += collect_submodules("keyring")
hidden += collect_submodules("pypresence")
data_files = [("assets", "assets"), *collect_data_files("minecraft_launcher_lib")]

a = Analysis(
    ["main.py"], pathex=["."], binaries=[], datas=data_files,
    hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "cv2"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="FiercyLauncher",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon="assets/icon.ico",
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False,
    upx_exclude=[], name="FiercyLauncher",
)
