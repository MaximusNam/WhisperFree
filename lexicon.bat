@echo off
rem WhisperFree: show what the program has learned from your corrections.
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

".venv\Scripts\python.exe" -m whisperfree --lexicon
echo.
pause
