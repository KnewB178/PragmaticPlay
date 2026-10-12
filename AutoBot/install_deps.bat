@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   PP AutoBot - setup (Windows)
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 goto :no_python

if exist "%~dp0venv\Scripts\python.exe" goto :venv_ready

echo [1/2] Creating virtual environment
python -m venv "%~dp0venv"
if errorlevel 1 goto :fail_venv

:venv_ready
echo [2/2] Installing Python dependencies
"%~dp0venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :fail_pip

echo.
echo Setup complete. Double-click start.bat to run.
echo.
pause
exit /b 0

:no_python
echo [ERROR] Python not found in PATH.
echo         Install Python 3.10+ from python.org and tick
echo         "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1

:fail_venv
echo [ERROR] Failed to create the virtual environment.
echo.
pause
exit /b 1

:fail_pip
echo [ERROR] pip install failed. Check your internet connection.
echo.
pause
exit /b 1
