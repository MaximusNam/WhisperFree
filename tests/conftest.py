"""Общие фикстуры.

Tk плохо переносит несколько корней в одном процессе: создание второго после
уничтожения первого периодически падает с TclError. Поэтому корень один на весь
прогон, а тесты только чистят за собой дочерние окна.
"""

from __future__ import annotations

import tkinter as tk

import pytest

import whisperfree  # noqa: F401  — настраивает пути к Tcl/Tk при запуске из venv


@pytest.fixture(scope="session")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def root(tk_root):
    """Корень для одного теста: после него дочерние окна убираются."""
    yield tk_root
    for child in list(tk_root.winfo_children()):
        try:
            child.destroy()
        except tk.TclError:
            pass
