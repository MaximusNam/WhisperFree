"""Окно истории расшифровок.

Нужно, когда потерялось что-то не из последних десяти: здесь есть поиск,
время, приложение-получатель и кнопки «скопировать» и «вставить».

Список показан блоками, а не таблицей. Таблица ttk.Treeview, на которой окно
было сделано раньше, не умеет переносить текст: длинная расшифровка уезжала
за правый край, и прочитать её можно было только прокруткой вбок. Почему
пришлось менять сам виджет, а не настройку, — см. blocklist.py.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import uifont
from .blocklist import Block, BlockList
from .history import History, Record

log = logging.getLogger(__name__)

# Красным — записи, где вставить было нечего. Именно их обычно и ищут.
FAILED_COLOR = "#c0392b"


def _in_background(action, record) -> None:
    """Уводит медленную операцию из потока Tk.

    Поток одноразовый и демонский: окно истории закрывается сразу, ждать
    завершения некому, а висящий недемонский поток не дал бы программе выйти.
    """
    threading.Thread(
        target=action, args=(record,), name="history-action", daemon=True
    ).start()


class HistoryWindow:
    """Один экземпляр на приложение, открывается и закрывается многократно."""

    def __init__(
        self,
        root: tk.Tk,
        history: History,
        on_paste: Callable[[Record], None],
        on_copy: Callable[[Record], None],
        on_font: Callable[[int], None] | None = None,
    ) -> None:
        self.root = root
        self.history = history
        self.on_paste = on_paste
        self.on_copy = on_copy
        # Смена размера шрифта уходит в приложение: размер общий для всех окон
        # и запоминается в конфиге, а окно про конфиг знать не обязано.
        self.on_font = on_font
        self._window: tk.Toplevel | None = None
        self._list: BlockList | None = None
        self._search: tk.StringVar | None = None
        self._status: ttk.Label | None = None
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
        win.geometry("900x560")
        win.protocol("WM_DELETE_WINDOW", self._close)

        top = ttk.Frame(win, padding=(10, 10, 10, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Поиск:").pack(side="left")
        # Кнопки справа упаковываем ДО растягивающегося поля: место с краёв
        # менеджер упаковки отдаёт в порядке вызова, и поле, взявшее всё
        # остальное, должно идти последним.
        uifont.zoom_buttons(top, self._zoom)
        ttk.Button(top, text="Обновить", command=self._refresh).pack(side="right")
        self._search = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self._search)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        # Следим за изменением ТЕКСТА, а не за отпусканием клавиш: иначе
        # список перестраивался и на стрелках, и на Ctrl, и на Shift, то есть
        # на нажатиях, от которых поиск не меняется.
        self._search.trace_add("write", lambda *_: self._refresh())

        listing = BlockList(win, on_activate=self._paste_index)
        listing.tone("failed", foreground=FAILED_COLOR)
        listing.frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        bottom = ttk.Frame(win, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Вставить", command=self._paste_selected).pack(side="left")
        ttk.Button(bottom, text="Копировать", command=self._copy_selected).pack(
            side="left", padx=6
        )
        self._status = ttk.Label(bottom, text="")
        self._status.pack(side="right")

        uifont.bind_zoom(win, self._zoom)

        self._window = win
        self._list = listing
        # Наименьший размер считается от шрифта: 46 знаков в строку и 16
        # строк — меньше этого окно перестаёт быть окном истории.
        uifont.fit_window(win, chars=46, lines=16)
        self._refresh()
        entry.focus_set()

    def _close(self) -> None:
        if self._window is not None:
            self._window.withdraw()

    # --- данные ----------------------------------------------------------------

    def _refresh(self) -> None:
        listing = self._list
        if listing is None or not listing.text.winfo_exists():
            return
        query = self._search.get() if self._search is not None else ""
        # Выбранную запись стараемся не терять: человек мог отметить её, а
        # потом дописать букву в поиск.
        chosen = self._selected()
        self._rows = self.history.search(query)

        listing.set_blocks(
            [_block(record) for record in self._rows],
            keep=self._rows.index(chosen) if chosen in self._rows else None,
        )
        self._say(_count_line(len(self._rows), bool(query.strip())))

    def _say(self, text: str) -> None:
        if self._status is not None and self._status.winfo_exists():
            self._status.configure(text=text)

    def _selected(self) -> Record | None:
        listing = self._list
        if listing is None or listing.chosen is None:
            return None
        try:
            return self._rows[listing.chosen]
        except IndexError:  # pragma: no cover
            return None

    # --- действия --------------------------------------------------------------

    def _paste_index(self, index: int) -> None:
        """Двойной щелчок и Enter по записи."""
        self._paste_selected()

    def _paste_selected(self) -> None:
        record = self._selected()
        if record is None:
            self._say("выберите запись")
            return
        # Прячем окно, иначе текст уедет в него, а не в рабочее приложение.
        self._close()
        # Вставка идёт в отдельном потоке, а не прямо здесь. Внутри она ждёт
        # отпускания модификаторов и держит паузу после Ctrl+V — почти полсекунды.
        # В потоке Tk это заморозило бы и насос оверлея, и опрос уровня: плашка
        # замирала бы ровно тогда, когда человек на неё смотрит.
        self.root.after(220, lambda: _in_background(self.on_paste, record))

    def _copy_selected(self) -> None:
        record = self._selected()
        if record is None:
            self._say("выберите запись")
            return
        # Буфер обмена Windows умеет быть занятым чужим процессом, и тогда
        # обращение к нему повторяется с паузами. В потоке Tk это тот же
        # заморозивший плашку случай, что и со вставкой.
        _in_background(self.on_copy, record)
        self._say("скопировано в буфер")

    # --- размер шрифта ---------------------------------------------------------

    def _zoom(self, delta: int) -> None:
        if self.on_font is not None:
            self.on_font(delta)

    def apply_font(self) -> None:
        """Подхватить изменившийся общий размер шрифта."""
        if self._list is not None and self._list.text.winfo_exists():
            self._list.apply_font()
        if self._window is not None and self._window.winfo_exists():
            uifont.fit_window(self._window, chars=46, lines=16)


def _count_line(found: int, searching: bool) -> str:
    """Строка со счётчиком. Пустой результат обязан сказать, что он пустой.

    Иначе окно с нулём записей выглядит одинаково и когда ничего не нашлось,
    и когда программа сломалась.
    """
    if found:
        return f"записей: {found}"
    return "ничего не найдено" if searching else "история пуста"


def _block(record: Record) -> Block:
    """Запись истории в виде блока: время и окно сверху, текст под ними."""
    where = record.target_exe or "—"
    head = f"{record.when:%d.%m %H:%M:%S}   {where}"
    text = " ".join(record.text.split())
    if record.error:
        # Причина в скобках, как и раньше: у неудачной записи текст пустой, и
        # без причины в окне висело бы пустое место.
        body = f"[{record.error}] {text}".strip()
        return Block(head=head, body=body, tone="failed")
    return Block(head=head, body=text or "(пусто)")
