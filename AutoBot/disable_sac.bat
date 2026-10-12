@echo off
chcp 65001 >nul
title Disable Smart App Control
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] Administrator rights required.
    echo     Right-click this file  --^>  "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo ============================================
echo   Disable Smart App Control (SAC)
echo ============================================
echo.
echo [current value]
reg query "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState
echo.
echo [1/2] Setting VerifiedAndReputablePolicyState = 0 (Off) ...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState /t REG_DWORD /d 0 /f
if errorlevel 1 (
    echo [ERROR] "reg add" failed. SAC not changed.
    pause
    exit /b 1
)
echo.
echo [2/2] Applying policy with "CiTool.exe -r" (no reboot required) ...
"%SystemRoot%\System32\CiTool.exe" -r
if errorlevel 1 (
    echo [WARN] "CiTool.exe -r" returned an error. Reboot Windows once, then re-run this file.
) else (
    echo       OK - code integrity policy refreshed.
)
echo.
echo [new value]
reg query "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState
echo.
echo   0 = Off    1 = On (enforcing)    2 = Evaluation
echo   If it still shows 1, reboot Windows once and check again.
echo.
pause
exit /b 0
