# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# FiercySetup uses requests before it can download the portable application.
# Its one-file EXE must therefore embed certifi's public CA bundle as well.
certifi_data = collect_data_files("certifi")
certifi_hidden = collect_submodules("certifi")

hidden = [
    "PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets", "requests",
    "update_download", "tls_bootstrap", "certifi", "certifi.core",
] + certifi_hidden

a = Analysis(
    ["fiercy_setup.py"], pathex=["."], binaries=[], datas=certifi_data,
    hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=["rthook_tls.py"],
    excludes=["tkinter", "matplotlib", "numpy"], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name="FiercySetup", debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
)
