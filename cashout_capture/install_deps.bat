@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "%~dp0venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv "%~dp0venv"
)

"%~dp0venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0venv\Scripts\python.exe" -m pip install requests websockets certifi

echo.
echo Done. Use start.bat to run the capture tool.
pause
