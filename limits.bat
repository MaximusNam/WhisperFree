@echo off
rem WhisperFree: how much of the free provider quota is left today.
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ERROR: .venv not found. Run setup first.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m whisperfree --limits
echo.
pause
