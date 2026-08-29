# PyInstaller: сборка WhisperFree в самостоятельную папку.
#   .venv\Scripts\pyinstaller whisperfree.spec
# Результат: dist\WhisperFree\WhisperFree.exe
#
# Собирается onedir, а не onefile: onefile каждый раз распаковывает всё во
# временный каталог, и запуск заметно медленнее. Папку можно просто заархивировать.

from PyInstaller.utils.hooks import collect_data_files

# sounddevice и soundfile тащат с собой нативные DLL (PortAudio и libsndfile),
# без которых микрофон и кодирование в FLAC не заведутся.
datas = collect_data_files("sounddevice") + collect_data_files("soundfile")

analysis = Analysis(
    # Именно whisperfree_app.py, а не whisperfree/__main__.py: PyInstaller запускает
    # указанный скрипт как __main__ без пакетного контекста, и относительные
    # импорты внутри пакета падают.
    ["whisperfree_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["pystray._win32", "PIL._tkinter_finder"],
    hookspath=[],
    runtime_hooks=[],
    # Исключать подмодули пакета нельзя: "tkinter.test" в этом списке уносил
    # с собой весь tkinter, и собранный exe падал на ModuleNotFoundError.
    excludes=["pytest"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="WhisperFree",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # значок в трее, консоль не нужна
    disable_windowed_traceback=False,
    argv_emulation=False,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="WhisperFree",
)
