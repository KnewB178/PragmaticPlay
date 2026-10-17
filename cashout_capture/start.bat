@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" "%~dp0capture_cashout.py"
) else (
  python "%~dp0capture_cashout.py"
)

echo.
pause
