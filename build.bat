@echo off
rem WhisperFree: build a standalone folder into dist\WhisperFree
rem
rem TCL_LIBRARY and TK_LIBRARY are set on purpose: inside a venv Tk cannot find
rem its own library, so the PyInstaller hook decides Tcl/Tk is missing and
rem silently drops tkinter. The built exe then dies with ModuleNotFoundError.
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

for /f "delims=" %%i in ('.venv\Scripts\python.exe -c "import sys,pathlib;print(str(pathlib.Path(sys.base_prefix)/'tcl'))"') do set "TCL_ROOT=%%i"
if exist "%TCL_ROOT%\tcl8.6" set "TCL_LIBRARY=%TCL_ROOT%\tcl8.6"
if exist "%TCL_ROOT%\tk8.6" set "TK_LIBRARY=%TCL_ROOT%\tk8.6"
echo TCL_LIBRARY=%TCL_LIBRARY%
echo TK_LIBRARY=%TK_LIBRARY%
echo.

".venv\Scripts\python.exe" -m pip install pyinstaller --quiet
".venv\Scripts\pyinstaller.exe" whisperfree.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Done: dist\WhisperFree\WhisperFree.exe
echo The whole dist\WhisperFree folder can be moved to another machine.
pause
