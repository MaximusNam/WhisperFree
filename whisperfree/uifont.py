"""Размер шрифта в окнах программы.

Раньше он нигде не задавался: окна брали системный по умолчанию, а это на
Windows Segoe UI 9. Человеку, которому такой кегль мелок, читать историю было
тяжело — с этого и началась правка.

Размер задаётся один на все окна и меняется через именованные шрифты Tk.
Так получается короче и полнее, чем расставлять шрифт каждому виджету:
именованный шрифт наследуют все виджеты tk и ttk, у которых он не задан
прямо, — значит одна правка достаёт и подписи, и кнопки, и поле поиска, и
текст списков.

Плашку-оверлей это не задевает, хотя она и живёт в том же процессе Tk: она
рисуется картинкой PIL со своим шрифтом из C:\\Windows\\Fonts и про
именованные шрифты Tk ничего не знает.
"""

from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

log = logging.getLogger(__name__)

# Системный по умолчанию — 9, и именно он оказался мелок. 13 крупнее ровно
# настолько, чтобы разница была видна сразу: высота строки растёт с 15 до 23
# пикселей (замер на Segoe UI). Дальше человек подстраивает под себя сам —
# Ctrl+колесо и кнопки в окне, — и выбранный размер запоминается в конфиге.
DEFAULT_SIZE = 13

# Ниже 8 текст снова становится нечитаемым, выше 28 в окно перестают влезать
# кнопки. Пределы нужны не из вкуса, а чтобы промах колесом мыши не оставил
# человека с окном, в котором ничего не разобрать и нечем это исправить.
MIN_SIZE = 8
MAX_SIZE = 28

# Именованные шрифты, которые вообще участвуют в наших окнах. TkDefaultFont —
# подписи, кнопки, текст списков; TkTextFont — поля ввода; TkHeadingFont —
# заголовки; TkMenuFont — меню.
NAMED_FONTS = ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont")


def clamp(size) -> int:
    """Размер в допустимых пределах. Мусор превращается в размер по умолчанию."""
    try:
        value = int(size)
    except (TypeError, ValueError):
        return DEFAULT_SIZE
    return max(MIN_SIZE, min(MAX_SIZE, value))


def apply_size(root: tk.Misc, size) -> int:
    """Задаёт размер шрифта во всех окнах. Возвращает применённый размер."""
    value = clamp(size)
    for name in NAMED_FONTS:
        try:
            tkfont.nametofont(name, root).configure(size=value)
        except tk.TclError:  # pragma: no cover — шрифта нет в этой сборке Tk
            log.debug("именованного шрифта %s в этой сборке Tk нет", name)
    return value


def current_size(root: tk.Misc) -> int:
    """Размер, который стоит сейчас."""
    try:
        return int(tkfont.nametofont("TkDefaultFont", root).cget("size"))
    except (tk.TclError, TypeError, ValueError):  # pragma: no cover
        return DEFAULT_SIZE


def min_window(root: tk.Misc, chars: int, lines: int) -> tuple[int, int]:
    """Наименьший размер окна, при котором его части не сдавливаются.

    Считать надо от шрифта, а не оставлять числом: при крупном кегле кнопки
    и подписи в прежнем окне сдавливаются менеджером упаковки, и выглядит это
    ровно как обрезка шрифтом — человек решит, что сломалось увеличение, а
    сломалась геометрия. Пределы экрана учитываем, иначе окно нельзя будет
    ни закрыть, ни передвинуть.
    """
    font = tkfont.nametofont("TkDefaultFont", root)
    width = int(font.measure("0") * chars)
    height = int(font.metrics("linespace") * lines)
    try:
        width = min(width, root.winfo_screenwidth() - 80)
        height = min(height, root.winfo_screenheight() - 120)
    except tk.TclError:  # pragma: no cover
        pass
    return max(320, width), max(200, height)


def fit_window(window: tk.Misc, chars: int, lines: int) -> None:
    """Задаёт окну наименьший размер и растит его, если он стал меньше него."""
    width, height = min_window(window, chars, lines)
    window.minsize(width, height)
    try:
        if window.winfo_width() < width or window.winfo_height() < height:
            window.geometry(f"{max(width, window.winfo_width())}x{max(height, window.winfo_height())}")
    except tk.TclError:  # pragma: no cover
        pass


# --- как человек меняет размер ------------------------------------------------
#
# Оба окна дают одно и то же: две кнопки и привычные сочетания. Кнопки видимые,
# а не только горячие клавиши, — человек, которому текст мелок, должен уметь
# это исправить, не читая документацию.


def zoom_buttons(parent: tk.Misc, zoom) -> ttk.Frame:
    """Кнопки «мельче» и «крупнее», прижатые к правому краю.

    Вправо, а не влево: при крупном шрифте подпись слева разрастается и
    менеджер упаковки начинает отбирать место у того, что упаковано позже.
    Кнопки размера — последнее, что можно отнять у человека, которому мелко:
    без них он не сможет вернуть себе читаемый текст.
    """
    box = ttk.Frame(parent)
    ttk.Button(box, text="А−", width=4, command=lambda: zoom(-1)).pack(side="left")
    ttk.Button(box, text="А+", width=4, command=lambda: zoom(1)).pack(side="left", padx=(2, 0))
    box.pack(side="right", padx=(8, 0))
    return box


def bind_zoom(window: tk.Misc, zoom) -> None:
    """Ctrl+плюс, Ctrl+минус, Ctrl+колесо и Ctrl+0 — как принято везде.

    Раскладок у плюса несколько: на основном ряду это Ctrl+= без Shift, на
    цифровом блоке — своя клавиша. Привязываем все, иначе сочетание работает
    у одного человека и не работает у другого.
    """
    for sequence in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
        window.bind(sequence, lambda _e: zoom(1))
    for sequence in ("<Control-minus>", "<Control-underscore>", "<Control-KP_Subtract>"):
        window.bind(sequence, lambda _e: zoom(-1))
    window.bind("<Control-MouseWheel>", lambda e: zoom(1 if e.delta > 0 else -1))
    # Ctrl+0 — вернуть размер по умолчанию; ноль как «сбросить», а не «шаг».
    window.bind("<Control-Key-0>", lambda _e: zoom(0))
