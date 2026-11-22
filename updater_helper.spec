# PyInstaller one-file помощник обновления; должен лежать рядом с основным EXE.
a = Analysis(["update_helper.py"], pathex=["."], binaries=[], datas=[], hiddenimports=[],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["tkinter"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="FiercyUpdater",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, argv_emulation=False,
          target_arch=None, codesign_identity=None, entitlements_file=None,
          icon="assets/icon.ico")
