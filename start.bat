@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Please run install.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -X utf8 live_qwen3.py

pause