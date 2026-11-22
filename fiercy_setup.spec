# -*- mode: python ; coding: utf-8 -*-
a = Analysis(["fiercy_setup.py"], pathex=["."], binaries=[], datas=[], hiddenimports=["PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets", "requests"], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["tkinter", "matplotlib", "numpy"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="FiercySetup", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
          codesign_identity=None, entitlements_file=None)
