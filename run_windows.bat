@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.14 -m venv .venv
  if errorlevel 1 goto :error
)
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error
.venv\Scripts\python.exe main.py
if errorlevel 1 goto :error
exit /b 0
:error
echo.
echo FiercyLauncher failed. See the error above.
pause
exit /b 1
