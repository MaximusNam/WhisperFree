"""Окно выученных правок.

Нужно ровно затем, чтобы обучение было видимым и обратимым. Программа,
которая молча подменяет слова в вашем тексте, — это то, чего надо бояться, а
не то, к чему стремиться. Поэтому здесь видно каждую выученную правку, на
каком шаге она возникла и как применяется, и любую можно забыть.

Список блоками, а не таблицей из пяти колонок. Замер: при шрифте 13 колонки
требуют 857 пикселей, при 15 — уже 971, а человек, которому мелко, поднимет
шрифт и выше. Значения начали бы обрезаться — в окне, вся суть которого в
том, чтобы всё было видно.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import uifont
from .blocklist import Block, BlockList
from .lexicon import DICTIONARY, REFINE, Lesson, Lexicon

log = logging.getLogger(__name__)

# Пояснение внизу окна. Разница между двумя способами применения — главное,
# что человек должен понимать про обучение, иначе «почему одно слово
# заменяется, а другое нет» выглядит случайностью.
FOOTNOTE = (
    "Подсказка склоняет распознавание к нужному написанию. Замена правит текст "
    "наверняка — её программа создаёт только там, где правило верно в любом "
    "предложении: другой алфавит или заглавная внутри слова."
)

# Виновника видно и словами, и цветом. Красным — правило из вашего же
# конфига: это единственный случай, который вы можете поправить сами.
REFINE_COLOR = "#b9770e"
DICTIONARY_COLOR = "#c0392b"


class LexiconWindow:
    """Один экземпляр на приложение, открывается и закрывается многократно."""

    def __init__(
        self,
        root: tk.Tk,
        lexicon: Lexicon,
        on_change: Callable[[], None] | None = None,
        on_font: Callable[[int], None] | None = None,
    ) -> None:
        self.root = root
        self.lexicon = lexicon
        # Забыв правку, надо сразу перестроить словарь замен и затравки:
        # иначе выброшенное правило доживёт до перезапуска.
        self.on_change = on_change or (lambda: None)
        self.on_font = on_font
        self._window: tk.Toplevel | None = None
        self._list: BlockList | None = None
        self._status: ttk.Label | None = None
        self._note: ttk.Label | None = None
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
        win.geometry("760x540")
        win.protocol("WM_DELETE_WINDOW", self._close)

        top = ttk.Frame(win, padding=(10, 10, 10, 6))
        top.pack(fill="x")
        # Кнопки размера упаковываем ПЕРВЫМИ: место менеджер отдаёт в порядке
        # вызова, и длинная подпись при крупном шрифте забирала его целиком —
        # «А+» переставала быть видна ровно тогда, когда нужнее всего.
        uifont.zoom_buttons(top, self._zoom)
        ttk.Label(top, text="Выучено из ваших правок").pack(side="left")

        listing = BlockList(win, on_activate=lambda _index: self._forget_selected())
        listing.tone("refine", foreground=REFINE_COLOR)
        listing.tone("dictionary", foreground=DICTIONARY_COLOR)
        listing.frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        listing.text.bind("<Delete>", lambda _e: self._forget_selected())

        note = ttk.Label(win, text=FOOTNOTE, foreground="#555555", justify="left")
        note.pack(fill="x", padx=10, pady=(0, 4))
        # Пояснение обязано переноситься по ширине окна, а не по числу,
        # записанному однажды: окно растягивают, и шрифт в нём меняется.
        note.bind("<Configure>", lambda e: self._wrap_note(e.width))

        bottom = ttk.Frame(win, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Забыть", command=self._forget_selected).pack(side="left")
        ttk.Button(bottom, text="Забыть всё", command=self._forget_all).pack(
            side="left", padx=6
        )
        self._status = ttk.Label(bottom, text="")
        self._status.pack(side="right")

        uifont.bind_zoom(win, self._zoom)

        self._window = win
        self._list = listing
        self._note = note
        uifont.fit_window(win, chars=40, lines=15)
        self._refresh()

    def _close(self) -> None:
        if self._window is not None:
            self._window.withdraw()

    # --- данные ----------------------------------------------------------------

    def _refresh(self) -> None:
        listing = self._list
        if listing is None or not listing.text.winfo_exists():
            return

        # Сверху то, что повторялось чаще: именно эти правки и мешают больше
        # всего, и именно их человек пришёл проверить.
        chosen = self._selected()
        self._rows = sorted(
            self.lexicon.lessons, key=lambda item: (-item.hits, -item.last_ts)
        )
        keep = None
        if chosen is not None:
            for index, lesson in enumerate(self._rows):
                if lesson.key == chosen.key:
                    keep = index
                    break

        listing.set_blocks([self._block(item) for item in self._rows], keep=keep)
        if self._rows:
            self._say(f"правок: {len(self._rows)}")
        else:
            self._say("пока ничего не выучено")

    def _block(self, lesson: Lesson) -> Block:
        """Правка в виде блока: обстоятельства сверху, сама пара под ними."""
        head = f"{lesson.blame_ru} · {self._how(lesson)} · {_times(lesson.hits)}"
        tone = ""
        if lesson.kind == REFINE:
            tone = "refine"
        elif lesson.kind == DICTIONARY:
            tone = "dictionary"
        return Block(head=head, body=f"{lesson.wrong} → {lesson.right}", tone=tone)

    def _how(self, lesson: Lesson) -> str:
        if not lesson.rule_allowed:
            return "подсказка"
        if self.lexicon.is_rule(lesson):
            return "замена"
        return f"подсказка, замена с {self.lexicon.min_hits}-го раза"

    def _wrap_note(self, width: int) -> None:
        """Пояснение переносится по ширине окна, а не по числу из кода.

        Прежнее wraplength=780 было задано в пикселях раз и навсегда: при
        крупном шрифте эти 780 пикселей держали меньше слов, а в окне уже
        780 подпись распирала его обратно.
        """
        if self._note is not None and self._note.winfo_exists():
            self._note.configure(wraplength=max(200, int(width) - 8))

    def _say(self, text: str) -> None:
        if self._status is not None and self._status.winfo_exists():
            self._status.configure(text=text)

    def _selected(self) -> Lesson | None:
        listing = self._list
        if listing is None or listing.chosen is None:
            return None
        try:
            return self._rows[listing.chosen]
        except IndexError:  # pragma: no cover
            return None

    # --- действия --------------------------------------------------------------

    def _forget_selected(self) -> None:
        lesson = self._selected()
        if lesson is None:
            self._say("выберите правку")
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

    # --- размер шрифта ---------------------------------------------------------

    def _zoom(self, delta: int) -> None:
        if self.on_font is not None:
            self.on_font(delta)

    def apply_font(self) -> None:
        """Подхватить изменившийся общий размер шрифта."""
        if self._list is not None and self._list.text.winfo_exists():
            self._list.apply_font()
        if self._window is not None and self._window.winfo_exists():
            uifont.fit_window(self._window, chars=40, lines=15)


def _times(count: int) -> str:
    """«1 раз», «2 раза», «5 раз» — по-русски, а не «x2».

    Окно читают глазами, и «×2» рядом с обычным текстом читается как помеха.
    """
    tail = count % 10
    hundred = count % 100
    if tail == 1 and hundred != 11:
        return f"{count} раз"
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return f"{count} раза"
    return f"{count} раз"
