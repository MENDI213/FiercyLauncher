@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)"
  if errorlevel 1 (
    echo Existing .venv uses an unsupported Python version. Recreating it with Python 3.12...
    rmdir /s /q .venv
  )
)
if not exist ".venv\Scripts\python.exe" py -3.12 -m venv .venv
if errorlevel 1 goto :python_missing
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo Using build environment:
.venv\Scripts\python.exe --version
echo [1/4] Building main onedir application...
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean launcher.spec
if errorlevel 1 goto :error

echo [2/4] Building standalone updater...
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean updater_helper.spec --distpath dist\updater --workpath build\updater
if errorlevel 1 goto :error
copy /Y "dist\updater\FiercyUpdater.exe" "dist\FiercyLauncher\FiercyUpdater.exe" >nul
if errorlevel 1 goto :error

echo [3/4] Building standalone GitHub setup installer...
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean fiercy_setup.spec --distpath dist/setup --workpath build/setup
if errorlevel 1 goto :error

if not exist "release_assets" mkdir "release_assets"
copy /Y "dist\setup\FiercySetup.exe" "release_assets\FiercySetup.exe" >nul
if errorlevel 1 goto :error

echo [4/4] Creating release archives...
.venv\Scripts\python.exe package_release.py
if errorlevel 1 goto :error

echo.
echo Build complete. EXE: dist\FiercyLauncher\FiercyLauncher.exe
echo Release ZIP files: release_assets\
pause
exit /b 0
:python_missing
echo.
echo Python 3.12 was not found. Install Python 3.12 x64 with the py launcher enabled.
pause
exit /b 1
:error
echo.
echo Build failed. Read the error above.
pause
exit /b 1
