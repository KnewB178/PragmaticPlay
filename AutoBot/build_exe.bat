@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "%~dp0venv\Scripts\python.exe" (
    echo [ERROR] venv not found. Run install_deps.bat first.
    echo.
    pause
    exit /b 1
)

echo ============================================
echo   Build PP_AutoBot.exe  (onefile / console)
echo ============================================
echo.

"%~dp0venv\Scripts\python.exe" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [1/2] Installing PyInstaller ...
    "%~dp0venv\Scripts\python.exe" -m pip install pyinstaller
    if errorlevel 1 goto :fail
) else (
    echo [1/2] PyInstaller already installed.
)

echo [2/2] Building ...
"%~dp0venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --console ^
    --name PP_AutoBot ^
    --distpath "%~dp0dist" ^
    --workpath "%~dp0build" ^
    --specpath "%~dp0build" ^
    --add-data "%~dp0PP EV.py;." ^
    --add-data "%~dp0pdev_local.py;." ^
    --add-data "%~dp0table_id.txt;." ^
    --add-data "%~dp0bot_config.ini;." ^
    "%~dp0main.py"
if errorlevel 1 goto :fail

echo.
echo   Done  -^>  dist\PP_AutoBot.exe
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed. See the messages above.
echo.
pause
exit /b 1
