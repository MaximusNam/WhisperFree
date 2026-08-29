"""WhisperFree — голосовой ввод для Windows."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__version__ = "0.1.0"


def _fix_tcl_paths() -> None:
    """Указывает Tk, где лежит его библиотека, если запуск идёт из venv.

    На Windows _tkinter ищет init.tcl относительно sys.executable. В venv это
    .venv\\Scripts\\python.exe, рядом с которым каталога tcl нет, и Tk падает с
    «Can't find a usable init.tcl». Сам интерпретатор при этом полностью
    исправен — не хватает только пути.
    """
    if getattr(sys, "frozen", False):
        return  # в собранном exe пути раскладывает PyInstaller
    if sys.prefix == sys.base_prefix:
        return  # обычный запуск, Tk найдёт всё сам

    tcl_root = Path(sys.base_prefix) / "tcl"
    if not tcl_root.is_dir():
        return

    for variable, prefix, marker in (
        ("TCL_LIBRARY", "tcl", "init.tcl"),
        ("TK_LIBRARY", "tk", "tk.tcl"),
    ):
        if os.environ.get(variable):
            continue
        for candidate in sorted(tcl_root.glob(f"{prefix}8.*"), reverse=True):
            if (candidate / marker).is_file():
                os.environ[variable] = str(candidate)
                break


if sys.platform == "win32":
    _fix_tcl_paths()
