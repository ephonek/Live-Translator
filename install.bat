@echo off
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"

if errorlevel 1 (
    echo.
    echo Installation failed. Read the error above.
) else (
    echo.
    echo Installation completed.
)

pause