@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "%~dp0venv\Scripts\python.exe" goto :no_venv

"%~dp0venv\Scripts\python.exe" "%~dp0main.py"

echo.
pause
exit /b 0

:no_venv
echo [ERROR] Virtual environment not found in this folder.
echo         Run install_deps.bat first.
echo.
pause
exit /b 1
