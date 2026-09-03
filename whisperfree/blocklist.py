"""Список записей с переносом текста по строкам.

ttk.Treeview, на котором окна были сделаны раньше, для чтения не годится: он
не умеет ни переносить текст, ни держать строки разной высоты. Длинная
расшифровка уезжала за правый край окна, и прочитать её можно было только
прокруткой вбок. Это и была жалоба: «текст улетает на километр вправо».

Поэтому список рисуется на tk.Text. Там перенос по словам штатный и сам
пересчитывается при изменении ширины окна — замер: одна и та же запись
занимает 4 строки при ширине 700 px и 3 при 1200 px, без единой строчки кода.
Выбор записи держится на тегах: у каждой свой, и по координатам щелчка Tk сам
говорит, на какую попали.

Крупный шрифт добивает Treeview окончательно. Высота строки там не растёт за
буквами сама: её задают числом через ttk.Style, и при шрифте 13 буквы
обрезаются сверху и снизу, пока не поднимешь rowheight хотя бы до 23 (замер
linespace на Segoe UI: 9 -> 15, 13 -> 23, 16 -> 30). У Text высота строки
считается сама.

Цена — выбор записи приходится писать руками, потому что Text про «записи»
ничего не знает. Взамен получаем то, чего у Treeview нет в принципе:
читаемость при любом размере шрифта, ничего не обрезается и не уезжает.
"""

from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable

log = logging.getLogger(__name__)

# Приглушённый цвет строки-заголовка и подсветка выбранной записи.
HEAD_COLOR = "#6b625d"
CHOSEN_BG = "#dce7f7"

# Цвет выделения мышью, когда фокус ушёл из списка. По умолчанию в Tk он
# ПУСТОЙ, и это ловушка: человек выделяет текст, нажимает «Копировать» —
# фокус переходит на кнопку, выделение становится невидимым, и он больше не
# видит, что именно скопируется.
INACTIVE_SELECT_BG = "#c8d4e6"

# Мера строки в знаках. Перенос по ширине окна сам по себе беды не лечит: на
# широком мониторе строка разрастается до двух сотен знаков, и глаз теряет
# начало следующей. Руководства по доступности называют 80 знаков потолком,
# 45–75 — читаемой полосой.
MAX_MEASURE_CHARS = 80

# Строка, по которой считается средняя ширина знака: по одной «х» считать
# нельзя, у русских букв ширина разная.
RULER = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя оеаинтсрв"

# Отступы внутри списка. Воздух между записями нужен, чтобы блоки читались
# как отдельные: без него перенесённый текст следующей записи сливается с
# предыдущей в одну простыню.
PAD_X = 14
PAD_Y = 10
GAP_ABOVE_HEAD = 12
GAP_BELOW_BODY = 4


@dataclass(frozen=True)
class Block:
    """Одна запись списка: приглушённый заголовок и текст под ним."""

    head: str
    body: str
    # Имя тега окраски: пустое — обычная запись. Теги заводит владелец списка
    # через tone(), потому что «что значит красный» знает он, а не список.
    tone: str = ""


class BlockList:
    """Прокручиваемый список блоков с выбором и двойным щелчком."""

    def __init__(
        self,
        parent: tk.Misc,
        on_activate: Callable[[int], None] | None = None,
        on_select: Callable[[int], None] | None = None,
    ) -> None:
        self.frame = ttk.Frame(parent)

        self._body_font = tkfont.nametofont("TkDefaultFont")
        # Заголовок — свой экземпляр шрифта: его надо сделать полужирным и на
        # единицу мельче текста, а именованный шрифт трогать нельзя, он общий.
        self._head_font = tkfont.Font(
            family=self._body_font.cget("family"),
            size=max(7, self._body_font.cget("size") - 1),
            weight="bold",
        )

        self.text = tk.Text(
            self.frame,
            wrap="word",
            font=self._body_font,
            cursor="arrow",
            padx=PAD_X,
            pady=PAD_Y,
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            spacing1=0,
            takefocus=True,
            inactiveselectbackground=INACTIVE_SELECT_BG,
            # Одна строка запрашиваемой высоты, а растёт список за счёт
            # expand=True. По умолчанию tk.Text просит 24 строки: при высоте
            # строки 23 это 552 пикселя, то есть всё окно целиком, и нижний
            # ряд с кнопками менеджер упаковки оставлял без места. Кнопок
            # «Вставить» и «Копировать» не было видно уже на размере по
            # умолчанию, и выглядело это не как теснота, а как поломка.
            height=1,
            width=1,
        )
        scroll = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        # Полоса — СОСЕДКА списка, а не его ребёнок. В прежнем коде она
        # создавалась дочерней к дереву и упаковывалась внутрь него, то есть
        # ложилась поверх правого края текста и закрывала его.
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure(
            "head",
            font=self._head_font,
            foreground=HEAD_COLOR,
            spacing1=GAP_ABOVE_HEAD,
            spacing3=2,
        )
        # spacing2 — расстояние между строками ВНУТРИ перенесённой записи.
        # У Segoe UI своё отношение высоты строки к кеглю 1.33, а руководства
        # по доступности просят от 1.5; три пикселя добирают разницу.
        self.text.tag_configure("body", spacing3=GAP_BELOW_BODY, spacing2=3)
        self.text.tag_configure("chosen", background=CHOSEN_BG)
        # Выделение мышью рисуется поверх подсветки выбранной записи. Тег sel
        # создан Tk раньше наших и потому ниже по приоритету: без подъёма фон
        # выбранной записи закрашивал бы выделение, и человек тянул бы мышью,
        # не видя результата.
        self.text.tag_raise("sel")

        self._on_activate = on_activate
        self._on_select = on_select
        self._blocks: list[Block] = []
        self._chosen: int | None = None

        self.text.bind("<Configure>", lambda e: self._reflow(e.width))
        self.text.bind("<Button-1>", self._click)
        self.text.bind("<Double-Button-1>", self._double)
        self.text.bind("<Up>", lambda _e: self._step(-1))
        self.text.bind("<Down>", lambda _e: self._step(1))
        self.text.bind("<Return>", lambda _e: self._activate())
        self._lock()

    # --- содержимое ------------------------------------------------------------

    def tone(self, name: str, **options) -> None:
        """Заводит тег окраски: tone('failed', foreground='#c0392b')."""
        self.text.tag_configure(name, **options)

    def set_blocks(self, blocks: list[Block], keep: int | None = None) -> None:
        """Перерисовывает список. keep — какую запись оставить выбранной."""
        # Место прокрутки сохраняем, но только если список той же длины, то
        # есть его просто обновили. При поиске список меняет длину, и
        # возвращать долю прокрутки бессмысленно: она указывала бы в другое
        # место, а часто и за конец нового списка.
        was_count = len(self._blocks)
        where = self.text.yview()[0]

        self._blocks = list(blocks)
        self._unlock()
        try:
            self.text.delete("1.0", "end")
            for index, block in enumerate(self._blocks):
                start = self.text.index("end-1c")
                if block.head:
                    self.text.insert("end", block.head + "\n", ("head",))
                tags = ("body",) + ((block.tone,) if block.tone else ())
                self.text.insert("end", block.body + "\n", tags)
                self.text.tag_add(_tag(index), start, "end-1c")
        finally:
            self._lock()

        if was_count == len(self._blocks) and where:
            self.text.yview_moveto(where)

        self._chosen = None
        if keep is not None and 0 <= keep < len(self._blocks):
            # choose делает see() и может прокрутить список — это правильнее
            # сохранённого места: выбранную запись человек должен видеть.
            self.choose(keep, notify=False)

    def _unlock(self) -> None:
        self.text.configure(state="normal")

    def _lock(self) -> None:
        """Запрещает правку с клавиатуры, оставляя выделение мышью.

        Проверено: при state="disabled" Tk молча игнорирует и insert, и
        delete — ни ошибки, ни изменения. Поэтому перерисовка обязана снимать
        состояние: забыв это сделать, получишь пустой список без единого
        признака поломки.
        """
        self.text.configure(state="disabled")

    # --- выбор -----------------------------------------------------------------

    @property
    def chosen(self) -> int | None:
        return self._chosen

    @property
    def count(self) -> int:
        return len(self._blocks)

    def choose(self, index: int, notify: bool = True) -> None:
        if not (0 <= index < len(self._blocks)):
            return
        self.text.tag_remove("chosen", "1.0", "end")
        tag = _tag(index)
        span = self.text.tag_ranges(tag)
        if span:
            self.text.tag_add("chosen", span[0], span[1])
            # Выделение мышью должно быть видно поверх подсветки выбора,
            # иначе скопированный кусок не отличить от невыделенного.
            self.text.tag_raise("sel")
            self.text.see(span[0])
        self._chosen = index
        if notify and self._on_select is not None:
            self._on_select(index)

    def _at(self, x: int, y: int) -> int | None:
        """Какая запись под этой точкой. None — щёлкнули по пустому месту."""
        try:
            position = self.text.index(f"@{x},{y}")
        except tk.TclError:  # pragma: no cover — окно ещё не отображено
            return None
        for name in self.text.tag_names(position):
            if name.startswith("blk"):
                try:
                    return int(name[3:])
                except ValueError:  # pragma: no cover
                    continue
        return None

    def _click(self, event) -> None:
        self.text.focus_set()
        index = self._at(event.x, event.y)
        # Ниже последней записи тегов нет вовсе — там выбор не меняем, а не
        # сбрасываем: человек мог промахнуться на пиксель, и потерять выбор
        # вместе с кнопками «Вставить» и «Копировать» было бы обидно.
        if index is not None:
            self.choose(index)

    def _double(self, event) -> None:
        index = self._at(event.x, event.y)
        if index is not None:
            self.choose(index)
            self._activate()
        return "break"

    def _step(self, delta: int) -> str:
        if not self._blocks:
            return "break"
        current = self._chosen if self._chosen is not None else -1 if delta > 0 else 0
        self.choose(max(0, min(len(self._blocks) - 1, current + delta)))
        return "break"

    def _activate(self) -> str:
        if self._chosen is not None and self._on_activate is not None:
            self._on_activate(self._chosen)
        return "break"

    # --- размер шрифта и мера строки -------------------------------------------

    def apply_font(self) -> None:
        """Подхватывает изменившийся общий размер шрифта.

        Текст списка привязан к именованному шрифту и меняется сам; заголовку
        нужен отдельный пересчёт, потому что у него свой экземпляр — иначе
        обстоятельства записи остались бы прежнего размера, пока окно не
        пересоздадут.
        """
        self._head_font.configure(
            family=self._body_font.cget("family"),
            size=max(7, self._body_font.cget("size") - 1),
        )
        if self.text.winfo_exists():
            self._reflow(self.text.winfo_width())

    def _reflow(self, width: int) -> None:
        """Держит длину строки в читаемой мере, когда окно шире неё.

        Ограничение задаётся правым полем, а не шириной виджета: полоса
        прокрутки и подсветка выбранной записи должны занимать всю ширину,
        иначе выбранная запись выглядит обрезанной.
        """
        if width <= 1:  # окно ещё не разложено
            return
        per_char = self._body_font.measure(RULER) / len(RULER)
        limit = int(per_char * MAX_MEASURE_CHARS)
        margin = max(0, width - PAD_X * 2 - limit)
        for tag in ("head", "body"):
            self.text.tag_configure(tag, rmargin=margin)


def _tag(index: int) -> str:
    return f"blk{index}"
