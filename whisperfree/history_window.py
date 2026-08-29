"""Окно истории расшифровок.

Нужно, когда потерялось что-то не из последних десяти: здесь есть поиск,
время, приложение-получатель и кнопки «скопировать» и «вставить».
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .history import History, Record

log = logging.getLogger(__name__)


class HistoryWindow:
    """Один экземпляр на приложение, открывается и закрывается многократно."""

    def __init__(
        self,
        root: tk.Tk,
        history: History,
        on_paste: Callable[[Record], None],
        on_copy: Callable[[Record], None],
    ) -> None:
        self.root = root
        self.history = history
        self.on_paste = on_paste
        self.on_copy = on_copy
        self._window: tk.Toplevel | None = None
        self._tree: ttk.Treeview | None = None
        self._search: tk.StringVar | None = None
        self._rows: list[Record] = []

    def open(self) -> None:
        """Можно звать из любого потока."""
        self.root.after(0, self._open)

    # --- построение ------------------------------------------------------------

    def _open(self) -> None:
        if self._window is not None and self._window.winfo_exists():
            self._window.deiconify()
            self._window.lift()
            self._window.focus_force()
            self._refresh()
            return

        win = tk.Toplevel(self.root)
        win.title("WhisperFree — история")
        win.geometry("820x460")
        win.minsize(560, 300)
        win.protocol("WM_DELETE_WINDOW", self._close)

        top = ttk.Frame(win, padding=(10, 10, 10, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Поиск:").pack(side="left")
        self._search = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self._search)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<KeyRelease>", lambda _e: self._refresh())
        ttk.Button(top, text="Обновить", command=self._refresh).pack(side="left")

        columns = ("time", "app", "text")
        tree = ttk.Treeview(win, columns=columns, show="headings", selectmode="browse")
        tree.heading("time", text="Время")
        tree.heading("app", text="Куда")
        tree.heading("text", text="Текст")
        tree.column("time", width=130, stretch=False, anchor="w")
        tree.column("app", width=150, stretch=False, anchor="w")
        tree.column("text", width=520, anchor="w")
        tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        tree.bind("<Double-1>", lambda _e: self._paste_selected())
        # Строки с ошибкой видно сразу — именно их обычно и ищут.
        tree.tag_configure("failed", foreground="#c0392b")

        scroll = ttk.Scrollbar(tree, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        bottom = ttk.Frame(win, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Вставить", command=self._paste_selected).pack(side="left")
        ttk.Button(bottom, text="Копировать", command=self._copy_selected).pack(
            side="left", padx=6
        )
        self._status = ttk.Label(bottom, text="")
        self._status.pack(side="right")

        self._window = win
        self._tree = tree
        self._refresh()
        entry.focus_set()

    def _close(self) -> None:
        if self._window is not None:
            self._window.withdraw()

    # --- данные ----------------------------------------------------------------

    def _refresh(self) -> None:
        tree = self._tree
        if tree is None or not tree.winfo_exists():
            return
        query = self._search.get() if self._search is not None else ""
        self._rows = self.history.search(query)

        tree.delete(*tree.get_children())
        for index, record in enumerate(self._rows):
            text = " ".join(record.text.split())
            if record.error:
                text = f"[{record.error}] {text}"
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(f"{record.when:%d.%m %H:%M:%S}", record.target_exe or "—", text),
                tags=("failed",) if record.error else (),
            )
        if hasattr(self, "_status"):
            self._status.configure(text=f"записей: {len(self._rows)}")

    def _selected(self) -> Record | None:
        tree = self._tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        try:
            return self._rows[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _paste_selected(self) -> None:
        record = self._selected()
        if record is None:
            return
        # Прячем окно, иначе текст уедет в него, а не в рабочее приложение.
        self._close()
        self.root.after(220, lambda: self.on_paste(record))

    def _copy_selected(self) -> None:
        record = self._selected()
        if record is not None:
            self.on_copy(record)
            if hasattr(self, "_status"):
                self._status.configure(text="скопировано в буфер")
