"""Окно выученных правок.

Нужно ровно затем, чтобы обучение было видимым и обратимым. Программа,
которая молча подменяет слова в вашем тексте, — это то, чего надо бояться, а
не то, к чему стремиться. Поэтому здесь видно каждую выученную правку, на
каком шаге она возникла и как применяется, и любую можно забыть.

Здесь же второй путь прислать правку: вставить исправленный текст в поле и
нажать «Отправить правку». Первый путь — хоткей по выделенному тексту —
зависит от того, отдаст ли чужое окно выделение по Ctrl+C, и о неудаче
может сказать только плашкой в 19 знаков. Поле ни от чего такого не зависит,
а итог показывает целиком.

Список блоками, а не таблицей из пяти колонок. Замер: при шрифте 13 колонки
требуют 857 пикселей, при 15 — уже 971, а человек, которому мелко, поднимет
шрифт и выше. Значения начали бы обрезаться — в окне, вся суть которого в
том, чтобы всё было видно.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

from . import uifont
from .blocklist import Block, BlockList
from .lexicon import DICTIONARY, REFINE, LearnOutcome, Lesson, Lexicon

log = logging.getLogger(__name__)

# Пояснение внизу окна. Разница между двумя способами применения — главное,
# что человек должен понимать про обучение, иначе «почему одно слово
# заменяется, а другое нет» выглядит случайностью.
FOOTNOTE = (
    "Подсказка склоняет распознавание к нужному написанию. Замена правит текст "
    "наверняка — её программа создаёт только там, где правило верно в любом "
    "предложении: другой алфавит или заглавная внутри слова."
)

# Подсказка над полем для второго пути.
SEND_HINT = (
    "Вставьте исправленный текст диктовки целиком (Ctrl+V) и нажмите "
    "«Отправить правку» или Ctrl+Enter. Программа сравнит его со своей "
    "диктовкой и запомнит разницу."
)

# Наименьшая высота окна в строках шрифта. Выросла с 15, когда появилось поле
# для исправленного текста: подсказка, три строки поля, кнопка и итог.
WINDOW_LINES = 24

# Сколько строк список сохраняет всегда: столько, чтобы целиком была видна
# хотя бы одна правка — строка о ней и сама пара. Ради них окно убирает
# пояснения, см. _make_room: список — то, ради чего окно открывают. Четыре не
# помещаются при шрифте 28 на экране 1024×768 даже без пояснений. Исключение
# одно: итогу только что отправленной правки окно оставляет RESULT_MIN_LINES
# строк даже за счёт списка.
LIST_MIN_LINES = 3

# Отступы, которые _make_room складывает с высотами частей окна. Те же числа
# стоят в pack() ниже; разойдутся — расчёт места начнёт ошибаться на них.
SEND_PADY = 8
NOTE_PADY = 4
LIST_PADY = 6
NOTE_PADX = 10
RESULT_PADY = 6

# Меньше этого итогу не дают, даже если места нет совсем: две строки с
# прокруткой читаются, одна — уже нет.
RESULT_MIN_LINES = 2

# Цвета итога попытки: удалось, не удалось, идёт сравнение.
RESULT_OK = "#2e7d32"
RESULT_FAIL = "#c0392b"
RESULT_BUSY = "#555555"

# Сочетания в поле по КОДУ клавиши, а не по символу — см. _keys_by_code.
# Коды виртуальных клавиш Windows: V, C, X, Z.
CODE_EVENTS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 90: "<<Undo>>"}
CODE_SELECT_ALL = 65  # A

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
        on_submit: Callable[[str], LearnOutcome] | None = None,
    ) -> None:
        self.root = root
        self.lexicon = lexicon
        # Второй путь обучения: текст из поля уходит сюда, а итог
        # возвращается в окно. Зовётся не из потока Tk — см. _submit.
        self.on_submit = on_submit
        # Забыв правку, надо сразу перестроить словарь замен и затравки:
        # иначе выброшенное правило доживёт до перезапуска.
        self.on_change = on_change or (lambda: None)
        self.on_font = on_font
        self._window: tk.Toplevel | None = None
        self._list: BlockList | None = None
        self._status: ttk.Label | None = None
        self._note: ttk.Label | None = None
        self._box: tk.Text | None = None
        self._send: ttk.Button | None = None
        self._result: tk.Text | None = None
        self._result_row: ttk.Frame | None = None
        self._result_scroll: ttk.Scrollbar | None = None
        self._hint: ttk.Label | None = None
        self._send_frame: ttk.LabelFrame | None = None
        self._top: ttk.Frame | None = None
        self._bottom: ttk.Frame | None = None
        self._room_pending = False
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
        win.geometry("760x660")
        win.protocol("WM_DELETE_WINDOW", self._close)

        top = ttk.Frame(win, padding=(10, 10, 10, 6))
        top.pack(side="top", fill="x")
        # Кнопки размера упаковываем ПЕРВЫМИ: место менеджер отдаёт в порядке
        # вызова, и длинная подпись при крупном шрифте забирала его целиком —
        # «А+» переставала быть видна ровно тогда, когда нужнее всего.
        uifont.zoom_buttons(top, self._zoom)
        ttk.Label(top, text="Выучено из ваших правок").pack(side="left")

        # Низ окна пакуем сразу за верхом и прижимаем к низу. Место менеджер
        # отдаёт в порядке упаковки, и при нехватке без него остаётся то, что
        # упаковано последним: список (он прокручивается), за ним пояснение,
        # за ним раздел с полем — но не кнопки «Забыть». Пока раздел с полем
        # паковался раньше низа, длинный итог попытки при шрифте 28 на
        # экране 1024×768 сдавливал эти кнопки до 16 пикселей из 60.
        bottom = ttk.Frame(win, padding=(10, 0, 10, 10))
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Забыть", command=self._forget_selected).pack(side="left")
        ttk.Button(bottom, text="Забыть всё", command=self._forget_all).pack(
            side="left", padx=6
        )
        self._status = ttk.Label(bottom, text="")
        self._status.pack(side="right")

        self._build_send(win)

        note = ttk.Label(win, text=FOOTNOTE, foreground="#555555", justify="left")
        note.pack(side="bottom", fill="x", padx=NOTE_PADX, pady=(0, NOTE_PADY))
        # Пояснение обязано переноситься по ширине окна, а не по числу,
        # записанному однажды: окно растягивают, и шрифт в нём меняется.
        note.bind("<Configure>", lambda e: self._wrap_note(e.width))

        listing = BlockList(win, on_activate=lambda _index: self._forget_selected())
        listing.tone("refine", foreground=REFINE_COLOR)
        listing.tone("dictionary", foreground=DICTIONARY_COLOR)
        listing.frame.pack(side="top", fill="both", expand=True, padx=10, pady=(0, LIST_PADY))
        listing.text.bind("<Delete>", lambda _e: self._forget_selected())

        uifont.bind_zoom(win, self._zoom)
        win.bind("<Configure>", self._on_resize, add="+")

        self._window = win
        self._list = listing
        self._note = note
        self._top = top
        self._bottom = bottom
        uifont.fit_window(win, chars=40, lines=WINDOW_LINES)
        self._refresh()
        self._make_room()

    def _build_send(self, win: tk.Misc) -> None:
        """Раздел «Исправленный текст»: поле, кнопка и итог попытки."""
        send = ttk.LabelFrame(win, text="Исправленный текст", padding=(10, 4, 10, 8))
        send.pack(side="top", fill="x", padx=10, pady=(0, SEND_PADY))

        hint = ttk.Label(send, text=SEND_HINT, foreground="#555555", justify="left")
        hint.pack(fill="x")
        hint.bind("<Configure>", lambda e: _wrap(e.widget, e.width))

        # Шрифт задаём явно: tk.Text по умолчанию берёт TkFixedFont — Courier
        # New 10, — и поле осталось бы мелким посреди крупного окна.
        box = tk.Text(
            send,
            height=3,
            wrap="word",
            undo=True,
            font=tkfont.nametofont("TkTextFont"),
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
        )
        box.pack(fill="x", pady=(4, 6))
        box.bind("<Control-KeyPress>", self._keys_by_code)
        box.bind("<Control-Return>", lambda _e: self._submit() or "break")

        row = ttk.Frame(send)
        row.pack(fill="x")
        button = ttk.Button(row, text="Отправить правку", command=self._submit)
        button.pack(side="left")

        # Итог — поле только для чтения, а не подпись. Подпись не умеет быть
        # ниже своего текста: длинный итог при шрифте 28 на экране 1024×768
        # не помещался даже без пояснений и обрезался снизу. Поле получает
        # столько строк, сколько есть места, и прокручивается, если нужно
        # больше, — см. _make_room. Заодно причину отказа можно выделить и
        # скопировать. В окно не пакуем, пока сказать нечего: пустое поле всё
        # равно берёт строку высоты. Пакует _show_result.
        result_row = ttk.Frame(send)
        result = tk.Text(
            result_row,
            height=1,
            width=1,
            wrap="word",
            font=tkfont.nametofont("TkDefaultFont"),
            borderwidth=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            relief="flat",
            background=ttk.Style(send).lookup("TLabelframe", "background") or "SystemButtonFace",
            takefocus=0,
            cursor="arrow",
        )
        result.configure(state="disabled")
        result.bind("<Control-KeyPress>", self._keys_by_code)
        scroll = ttk.Scrollbar(result_row, orient="vertical", command=result.yview)
        result.configure(yscrollcommand=scroll.set)
        result.pack(side="left", fill="both", expand=True)

        self._box = box
        self._send = button
        self._result = result
        self._result_row = result_row
        self._result_scroll = scroll
        self._hint = hint
        self._send_frame = send

    # --- второй путь: поле «Исправленный текст» ---------------------------------

    @staticmethod
    def _keys_by_code(event) -> str | None:
        """Ctrl+V, Ctrl+C, Ctrl+X, Ctrl+Z и Ctrl+A — по коду клавиши.

        Tk сопоставляет сочетания по СИМВОЛУ раскладки: на русской физическая
        V даёт «м», и его собственная привязка <Control-v> молчит. Вставить
        текст в поле было бы нельзя ровно тем способом, каким вставляют все.
        Проверено 05.09 на Ctrl+C в окне Tk: на английской раскладке
        копируется, на русской — нет. Код клавиши от раскладки не зависит.

        «break» обязателен: на английской раскладке иначе сработала бы ещё и
        привязка самого Tk, и текст вставился бы дважды.
        """
        action = CODE_EVENTS.get(event.keycode)
        if action is not None:
            event.widget.event_generate(action)
            return "break"
        if event.keycode == CODE_SELECT_ALL:
            event.widget.tag_add("sel", "1.0", "end-1c")
            return "break"
        return None

    def _submit(self) -> None:
        box = self._box
        if box is None or not box.winfo_exists():
            return
        text = box.get("1.0", "end-1c").strip()
        if not text:
            self._show_result("Поле пустое — вставьте исправленный текст.", ok=False)
            return
        if self.on_submit is None:
            self._show_result("Отправлять некуда: окно открыто без программы.", ok=False)
            return

        if self._send is not None and self._send.winfo_exists():
            self._send.state(["disabled"])
        self._show_result("Сравниваю с диктовками…", ok=None)
        submit = self.on_submit

        def work() -> None:
            # Не в потоке Tk: поиск по истории и запись файла правок занимают
            # десятки миллисекунд, а в потоке Tk живёт насос плашки.
            try:
                outcome = submit(text)
            except Exception:
                log.exception("не удалось отправить правку из окна")
                outcome = LearnOutcome(problem="Не получилось: ошибка, подробности в логе.")
            self.root.after(0, lambda: self._finish_submit(outcome))

        threading.Thread(target=work, name="lexicon-submit", daemon=True).start()

    def _finish_submit(self, outcome: LearnOutcome) -> None:
        if self._send is not None and self._send.winfo_exists():
            self._send.state(["!disabled"])
        self._show_result(outcome.full(self.lexicon.describe), ok=outcome.ok)
        if outcome.ok and self._box is not None and self._box.winfo_exists():
            # Выученное поле больше не нужно. Невыученное оставляем: его
            # поправят и отправят снова.
            self._box.delete("1.0", "end")
        self._refresh()

    def _show_result(self, text: str, ok: bool | None) -> None:
        result = self._result
        if result is None or not result.winfo_exists():
            return
        color = RESULT_BUSY if ok is None else (RESULT_OK if ok else RESULT_FAIL)
        result.configure(state="normal", foreground=color)
        result.delete("1.0", "end")
        result.insert("1.0", text)
        result.configure(state="disabled")
        if not self._result_row.winfo_manager():
            self._result_row.pack(fill="x", pady=(RESULT_PADY, 0))
        # С каждым итогом раздел меняет высоту, а окно свой размер нет — и
        # <Configure> окна не придёт. Пересчитывать надо каждый раз, а не
        # только при первом показе: первым идёт однострочное «Сравниваю…»,
        # а настоящий итог бывает в пять строк.
        self._make_room()

    def show_attempt(self, text: str, outcome: LearnOutcome) -> None:
        """Открыть окно с неудавшейся попыткой хоткея. Можно звать из любого потока.

        На плашке причина отказа не помещается — там около 19 знаков. Здесь
        она видна целиком, а выделенный текст уже стоит в поле: его можно
        поправить и отправить снова, не выделяя заново.
        """

        def run() -> None:
            self._open()
            box = self._box
            if box is not None and box.winfo_exists():
                box.delete("1.0", "end")
                box.insert("1.0", text)
            self._show_result(outcome.full(self.lexicon.describe), ok=outcome.ok)

        self.root.after(0, run)

    def notify_changed(self) -> None:
        """Список изменился не отсюда — правку выучили хоткеем. Любой поток."""
        self.root.after(0, self._refresh)

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
            uifont.fit_window(self._window, chars=40, lines=WINDOW_LINES)
            # Размер окна от смены шрифта мог и не поменяться, а с ним и
            # <Configure> не придёт, — тогда как высота частей поменялась.
            self._make_room()

    # --- место в окне ----------------------------------------------------------

    def _on_resize(self, event: tk.Event) -> None:
        # Привязка на окне ловит <Configure> и всех его частей. Нужно только
        # самого окна, и пересчёт один на пачку событий.
        if str(event.widget) != str(self._window) or self._room_pending:
            return
        self._room_pending = True
        self._window.after_idle(self._make_room)

    def _make_room(self) -> None:
        """Делит высоту окна, когда всего в нём не помещается.

        Найдено 02.11: с тех пор как 23.09 в окне появилось поле для
        исправленного текста, при шрифте 16 списку выученных правок оставалась
        одна строка, а при шрифте 28 на экране 1024×768 пропадали кнопки
        «Забыть».

        Кнопки и поле не уступают никогда, список держит LIST_MIN_LINES строк.
        Оставшееся место первым получает итог попытки — это ответ на то, что
        человек только что сделал; не хватает — итог прокручивается, но не
        меньше RESULT_MIN_LINES строк. Дальше подсказка над полем, последним
        пояснение внизу. Пояснения уходят целиком, а не обрезаются: обрезанный
        по высоте текст выглядит поломкой. Растянули окно — вернулись на свои
        места.
        """
        self._room_pending = False
        win, hint, note = self._window, self._hint, self._note
        if win is None or hint is None or note is None or not win.winfo_exists():
            return
        # Сначала раскладка по новому размеру окна: она тоже стоит в очереди
        # отложенных задач, и без этой строки ширина поля бралась прежняя —
        # растянутое одним движением окно не возвращало подсказку.
        win.update_idletasks()
        # Перенос по ширине задаём сами, не дожидаясь <Configure> подписей:
        # у убранной подписи его не будет, а высота нужна именно её. Ширины
        # те же, что подписи получают на своих местах.
        _wrap(hint, self._box.winfo_width())
        self._wrap_note(win.winfo_width() - 2 * NOTE_PADX)
        win.update_idletasks()

        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        hint_h = hint.winfo_reqheight()
        note_h = note.winfo_reqheight() + NOTE_PADY
        row = self._result_row
        has_result = bool(row.winfo_manager())
        send_h = self._send_frame.winfo_reqheight() + SEND_PADY
        if hint.winfo_manager():
            send_h -= hint_h
        if has_result:
            send_h -= row.winfo_reqheight() + RESULT_PADY
        free = win.winfo_height() - (
            self._top.winfo_reqheight()
            + send_h
            + self._bottom.winfo_reqheight()
            + LIST_MIN_LINES * line
            + _inner_padding(self._list.text)
            + LIST_PADY
        )

        if has_result:
            wanted = _display_lines(self._result)
            fits = (free - RESULT_PADY) // line
            lines = max(min(wanted, RESULT_MIN_LINES), min(wanted, fits))
            self._result.configure(height=lines)
            scroll = self._result_scroll
            if lines < wanted and not scroll.winfo_manager():
                scroll.pack(side="right", fill="y", before=self._result)
            elif lines >= wanted and scroll.winfo_manager():
                scroll.pack_forget()
            self._result.yview_moveto(0)
            free -= lines * line + RESULT_PADY

        show_hint = hint_h <= free
        show_note = show_hint and hint_h + note_h <= free

        if show_hint and not hint.winfo_manager():
            hint.pack(fill="x", before=self._box)
        elif not show_hint and hint.winfo_manager():
            hint.pack_forget()
        if show_note and not note.winfo_manager():
            note.pack(
                side="bottom",
                fill="x",
                padx=NOTE_PADX,
                pady=(0, NOTE_PADY),
                after=self._send_frame,
            )
        elif not show_note and note.winfo_manager():
            note.pack_forget()


def _display_lines(text: tk.Text) -> int:
    """Сколько строк занимает текст поля при его нынешней ширине."""
    try:
        counted = text.count("1.0", "end-1c", "update", "displaylines")
    except tk.TclError:  # pragma: no cover
        return 1
    # В разных версиях Python одно и то же приходит числом, кортежем или
    # None (ноль переносов).
    if isinstance(counted, tuple):
        counted = counted[0] if counted else 0
    return int(counted or 0) + 1


def _inner_padding(text: tk.Text) -> int:
    """Рамка и отступы внутри поля списка: строки текста их не включают.

    Без них обещанных строк на деле было на одну меньше: у списка рамка в
    пиксель и отступ в 10 пикселей сверху и снизу.
    """
    total = 0
    for option in ("borderwidth", "highlightthickness", "pady"):
        try:
            total += 2 * int(text.winfo_fpixels(text.cget(option)))
        except (tk.TclError, ValueError):  # pragma: no cover
            pass
    return total


def _wrap(label: ttk.Label, width: int) -> None:
    """Подпись переносится по ширине окна, а не по числу из кода."""
    if label.winfo_exists():
        label.configure(wraplength=max(200, int(width) - 8))


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
