@echo off
rem WhisperFree: measure the microphone noise floor and set the silence threshold.
rem Run it in a normally quiet room and stay silent for three seconds.
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

".venv\Scripts\python.exe" -m whisperfree --calibrate
echo.
pause
