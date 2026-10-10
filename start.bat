@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Please run install.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -X utf8 live_qwen3.py

set "TRANSLATOR_EXIT=%ERRORLEVEL%"
if not "%TRANSLATOR_EXIT%"=="0" (
    echo.
    echo Translator stopped with an error. Read the message above.
    pause
)
exit /b %TRANSLATOR_EXIT%
