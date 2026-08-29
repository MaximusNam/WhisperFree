@echo off
rem WhisperFree: which config.toml does THIS process actually open?
rem Run it BOTH ways: double-click from Explorer AND from your terminal.
rem Compare the sha256 lines. Different hash = different file.
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo  WhisperFree diagnostics
echo  launched from: %~dp0
echo  parent shell : %ComSpec%
echo ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found next to this .bat
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -X utf8 "%~dp0diag.py"

echo.
pause
