"""Окно выученных правок.

Нужно ровно затем, чтобы обучение было видимым и обратимым. Программа,
которая молча подменяет слова в вашем тексте, — это то, чего надо бояться, а
не то, к чему стремиться. Поэтому здесь видно каждую выученную правку, на
каком шаге она возникла и как применяется, и любую можно забыть.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .lexicon import DICTIONARY, REFINE, Lesson, Lexicon

log = logging.getLogger(__name__)

# Пояснение внизу окна. Разница между двумя способами применения — главное,
# что человек должен понимать про обучение, иначе «почему одно слово
# заменяется, а другое нет» выглядит случайностью.
FOOTNOTE = (
    "Подсказка склоняет распознавание к нужному написанию. Замена правит текст "
    "наверняка — её программа создаёт только там, где правило верно в любом "
    "предложении: другой алфавит или регистр."
)


class LexiconWindow:
    """Один экземпляр на приложение, открывается и закрывается многократно."""

    def __init__(
        self,
        root: tk.Tk,
        lexicon: Lexicon,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.lexicon = lexicon
        # Забыв правку, надо сразу перестроить словарь замен и затравки:
        # иначе выброшенное правило доживёт до перезапуска.
        self.on_change = on_change or (lambda: None)
        self._window: tk.Toplevel | None = None
        self._tree: ttk.Treeview | None = None
        self._status: ttk.Label | None = None
        self._rows: list[Lesson] = []

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
        win.title("WhisperFree — выученные правки")
        win.geometry("820x420")
        win.minsize(620, 280)
        win.protocol("WM_DELETE_WINDOW", self._close)

        columns = ("wrong", "right", "blame", "how", "hits")
        tree = ttk.Treeview(win, columns=columns, show="headings", selectmode="browse")
        for name, title, width, stretch in (
            ("wrong", "Слышалось", 170, False),
            ("right", "Пишется", 170, False),
            ("blame", "Где ошибка", 210, False),
            ("how", "Как применяется", 190, True),
            ("hits", "Раз", 50, False),
        ):
            tree.heading(name, text=title)
            tree.column(name, width=width, stretch=stretch, anchor="w")
        tree.column("hits", anchor="center")
        tree.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        tree.bind("<Delete>", lambda _e: self._forget_selected())

        # Виновника видно цветом: правки, испорченные моделью, обычно идут
        # пачкой, и это повод выключить её или сменить.
        tree.tag_configure("refine", foreground="#b9770e")
        tree.tag_configure("dictionary", foreground="#c0392b")

        scroll = ttk.Scrollbar(tree, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        note = ttk.Label(win, text=FOOTNOTE, wraplength=780, foreground="#555555")
        note.pack(fill="x", padx=10, pady=(0, 4))

        bottom = ttk.Frame(win, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Забыть", command=self._forget_selected).pack(side="left")
        ttk.Button(bottom, text="Забыть всё", command=self._forget_all).pack(
            side="left", padx=6
        )
        self._status = ttk.Label(bottom, text="")
        self._status.pack(side="right")

        self._window = win
        self._tree = tree
        self._refresh()

    def _close(self) -> None:
        if self._window is not None:
            self._window.withdraw()

    # --- данные ----------------------------------------------------------------

    def _refresh(self) -> None:
        tree = self._tree
        if tree is None or not tree.winfo_exists():
            return

        # Сверху то, что повторялось чаще: именно эти правки и мешают больше
        # всего, и именно их человек пришёл проверить.
        self._rows = sorted(
            self.lexicon.lessons, key=lambda item: (-item.hits, -item.last_ts)
        )
        tree.delete(*tree.get_children())
        for index, lesson in enumerate(self._rows):
            tags: tuple[str, ...] = ()
            if lesson.kind == REFINE:
                tags = ("refine",)
            elif lesson.kind == DICTIONARY:
                tags = ("dictionary",)
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    lesson.wrong,
                    lesson.right,
                    lesson.blame_ru,
                    self._how(lesson),
                    lesson.hits,
                ),
                tags=tags,
            )
        self._say(f"правок: {len(self._rows)}")

    def _how(self, lesson: Lesson) -> str:
        if not lesson.rule_allowed:
            return "подсказка"
        if self.lexicon.is_rule(lesson):
            return "замена"
        return f"подсказка, замена с {self.lexicon.min_hits}-го раза"

    def _say(self, text: str) -> None:
        if self._status is not None and self._status.winfo_exists():
            self._status.configure(text=text)

    def _selected(self) -> Lesson | None:
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

    # --- действия --------------------------------------------------------------

    def _forget_selected(self) -> None:
        lesson = self._selected()
        if lesson is None:
            self._say("выберите строку")
            return
        if self.lexicon.forget(lesson.wrong, lesson.right):
            log.info("забыл правку: %s → %s", lesson.wrong, lesson.right)
            self.on_change()
            self._refresh()
            self._say(f"забыл: {lesson.wrong} → {lesson.right}")

    def _forget_all(self) -> None:
        count = len(self.lexicon.lessons)
        if not count:
            return
        self.lexicon.clear()
        log.info("забыл все выученные правки: %d", count)
        self.on_change()
        self._refresh()
        self._say(f"забыл всё: {count}")
