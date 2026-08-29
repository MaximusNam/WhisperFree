@echo off
rem WhisperFree: paste a test phrase into the window you switch to.
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ERROR: .venv not found. Run setup first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e .
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m whisperfree --paste-test
echo.
pause
