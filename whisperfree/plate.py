"""Отрисовка плашки: капсула со стеклянным фоном поверх любых окон.

Почему не виджеты Tk. В макете фон плашки прозрачен на 87%, а текст поверх
него — непрозрачен. Tk на Windows такого не умеет: у него есть `-alpha` на всё
окно сразу (тогда буквы станут такими же бледными, как фон) и
`-transparentcolor`, который делает прозрачным ровно один цвет целиком.
Прозрачность на каждый пиксель даёт только слоёное окно Windows: мы рисуем
картинку сами и отдаём её через UpdateLayeredWindow. Заодно получаются
сглаженные края капсулы, которых у прямоугольных виджетов не бывает.

Рисуем с увеличением и потом уменьшаем: у PIL нет сглаживания при рисовании
фигур, поэтому кант капсулы в масштабе 1:1 выходит ступенчатым.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .theme import Theme

log = logging.getLogger(__name__)

# Во сколько раз рисуем крупнее перед уменьшением. Четыре — предел, за которым
# разница уже не видна, а время растёт квадратично.
SUPERSAMPLE = 4

# Шрифт макета и запасные. Segoe UI Variable Text есть только в Windows 11,
# поэтому следом идёт Segoe UI Semibold: у макета начертание 600, и обычный
# Segoe UI (400) выглядит на стекле заметно жиже.
FONT_CANDIDATES = (
    "SegUIVar.ttf",
    "seguisb.ttf",
    "segoeui.ttf",
    "arial.ttf",
)


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """Первый доступный шрифт из списка макета."""
    fonts = Path(r"C:\Windows\Fonts")
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(str(fonts / name), size)
        except OSError:
            continue
    log.warning("шрифты Windows не найдены, беру встроенный")
    return ImageFont.load_default()


def _rgba(colour: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple(int(max(0, min(255, c))) for c in colour)


def shadow_padding(theme: Theme, scale: float = 1.0) -> int:
    """Сколько места вокруг капсулы занимает тень.

    Окно приходится делать больше самой плашки, иначе тень обрежется по его
    краю и превратится в тёмную полосу.
    """
    reach = theme.shadow_blur + abs(theme.shadow_offset)
    return int(round(max(2.0, reach) * scale))


def plate_size(theme: Theme, scale: float = 1.0) -> tuple[int, int]:
    """Размер картинки вместе с полем под тень."""
    geo = theme.geometry
    pad = shadow_padding(theme, scale)
    return (
        max(1, int(round(geo.width * scale))) + pad * 2,
        max(1, int(round(geo.height * scale))) + pad * 2,
    )


def _box(theme: Theme, scale: float) -> tuple[float, float, float, float]:
    """Границы капсулы внутри картинки, в увеличенных координатах."""
    geo = theme.geometry
    pad = shadow_padding(theme, scale) * SUPERSAMPLE
    body_w = max(1, int(round(geo.width * scale))) * SUPERSAMPLE
    body_h = max(1, int(round(geo.height * scale))) * SUPERSAMPLE
    return (pad, pad, pad + body_w - 1, pad + body_h - 1)


# Готовые слои, которые не меняются от кадра к кадру. Без них одна отрисовка
# стоила 51 мс — три размытия по всему холсту на каждый кадр, — и пульсация
# точки съедала бы полъядра, подтормаживая поток Tk. Ключ включает масштаб:
# при смене монитора он меняется.
_CHASSIS: dict = {}
_SPRITES: dict = {}
_MASKS: dict = {}


def clear_cache() -> None:
    """Забыть заготовки. Нужно при смене темы или масштаба экрана."""
    _CHASSIS.clear()
    _SPRITES.clear()
    _MASKS.clear()


def _capsule_mask(theme: Theme, scale: float) -> Image.Image:
    """Маска формы капсулы в конечном размере."""
    key = (theme.id, round(scale, 3))
    cached = _MASKS.get(key)
    if cached is not None:
        return cached

    width, height = plate_size(theme, scale)
    k = SUPERSAMPLE
    mask = Image.new("L", (width * k, height * k), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        _box(theme, scale), radius=theme.geometry.corner_radius * scale * k, fill=255
    )
    mask = mask.resize((width, height), Image.LANCZOS)
    _MASKS[key] = mask
    return mask


def _chassis(theme: Theme, scale: float) -> Image.Image:
    """Тень, заливка, кант и внутренняя световая грань — то, что неизменно."""
    key = (theme.id, round(scale, 3))
    cached = _CHASSIS.get(key)
    if cached is not None:
        return cached

    width, height = plate_size(theme, scale)
    k = SUPERSAMPLE
    left, top, right, bottom = _box(theme, scale)
    radius = theme.geometry.corner_radius * scale * k
    border = max(1.0, theme.geometry.border_width * scale * k)

    big = Image.new("RGBA", (width * k, height * k), (0, 0, 0, 0))

    # Внешняя тень: силуэт капсулы, сдвинутый вниз и размытый.
    # В макете это box-shadow: 0 6px 16px. Без неё капсула лежит на экране
    # плоской наклейкой, а не парит над документом.
    if theme.shadow[3] > 0:
        silhouette = Image.new("RGBA", big.size, (0, 0, 0, 0))
        offset = theme.shadow_offset * scale * k
        ImageDraw.Draw(silhouette).rounded_rectangle(
            (left, top + offset, right, bottom + offset),
            radius=radius,
            fill=_rgba(theme.shadow),
        )
        big.alpha_composite(
            silhouette.filter(
                ImageFilter.GaussianBlur(max(1.0, theme.shadow_blur * scale * k / 3.0))
            )
        )

    body = Image.new("RGBA", big.size, (0, 0, 0, 0))
    ImageDraw.Draw(body).rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        fill=_rgba(theme.background),
        outline=_rgba(theme.border),
        width=int(round(border)),
    )

    # Внутренняя световая грань, inset 0 1px 1px в макете: светлая черта идёт
    # ПО ФОРМЕ капсулы. Прямой линией на постоянной высоте её рисовать нельзя —
    # у краёв она ложится поверх канта и проедает в нём две дырки.
    if theme.specular[3] > 0:
        rim = Image.new("RGBA", big.size, (0, 0, 0, 0))
        shift = 1.0 * scale * k
        inner_radius = max(1.0, radius - border)
        ImageDraw.Draw(rim).rounded_rectangle(
            (left + border, top + border + shift, right - border, bottom - border + shift),
            radius=inner_radius,
            outline=_rgba(theme.specular),
            width=max(1, int(round(scale * k))),
        )
        inside = Image.new("L", big.size, 0)
        ImageDraw.Draw(inside).rounded_rectangle(
            (left + border, top + border, right - border, bottom - border),
            radius=inner_radius,
            fill=255,
        )
        rim.putalpha(ImageChops.multiply(rim.getchannel("A"), inside))
        body.alpha_composite(rim)

    big.alpha_composite(body)
    small = big.resize((width, height), Image.LANCZOS)
    _CHASSIS[key] = small
    return small


def _dot_sprite(theme: Theme, state: str, scale: float) -> Image.Image:
    """Точка статуса со свечением, отдельной картинкой.

    Свечение — box-shadow: 0 0 8px цветом состояния из темы (dot_glow_rgba).
    Размытие дорогое, а от кадра к кадру не меняется, поэтому считается раз.
    """
    key = (theme.id, state, round(scale, 3))
    cached = _SPRITES.get(key)
    if cached is not None:
        return cached

    look = theme.state(state)
    k = SUPERSAMPLE
    dot = theme.geometry.dot_diameter * scale
    halo = 8 * scale  # радиус свечения из макета
    side = int(round(dot + halo * 2))
    tile = Image.new("RGBA", (side * k, side * k), (0, 0, 0, 0))
    centre = side * k / 2

    if look.glow[3] > 0:
        glow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        r = (dot / 2 + halo / 2) * k
        ImageDraw.Draw(glow).ellipse(
            (centre - r, centre - r, centre + r, centre + r), fill=_rgba(look.glow)
        )
        tile.alpha_composite(glow.filter(ImageFilter.GaussianBlur(halo * k / 3.0)))

    r = dot / 2 * k
    ImageDraw.Draw(tile).ellipse(
        (centre - r, centre - r, centre + r, centre + r), fill=_rgba(look.accent)
    )
    sprite = tile.resize((side, side), Image.LANCZOS)
    _SPRITES[key] = sprite
    return sprite


def _draw_bar(draw, k, span, height, level, theme, look) -> None:
    """Дорожка и заполненная часть полоски уровня внутри тайла."""
    draw.rounded_rectangle(
        (0, k, span * k, height * k + k), radius=height * k / 2, fill=_rgba(theme.bar_track)
    )
    filled = span * k * max(0.0, min(1.0, level))
    if filled > height * k:
        draw.rounded_rectangle(
            (0, k, filled, height * k + k), radius=height * k / 2, fill=_rgba(look.accent)
        )


def _tile(width: int, height: int, paint, mask: Image.Image, x: int, y: int) -> Image.Image:
    """Маленький кусочек картинки, нарисованный с увеличением и обрезанный.

    Обрезка обязательна: полоска уровня идёт по прямой с отступом 14 px, а у
    пилюли радиус равен половине высоты — у нижнего края форма сужается, и
    концы полоски торчали наружу двумя усами.
    """
    k = SUPERSAMPLE
    width, height = max(1, width), max(1, height)
    tile = Image.new("RGBA", (width * k, height * k), (0, 0, 0, 0))
    paint(ImageDraw.Draw(tile), k)
    tile = tile.resize((width, height), Image.LANCZOS)
    piece = mask.crop((x, y, x + width, y + height))
    tile.putalpha(ImageChops.multiply(tile.getchannel("A"), piece))
    return tile


def render(
    theme: Theme,
    state: str,
    text: str,
    level: float = 0.0,
    scale: float = 1.0,
    phase: float = 0.0,
) -> Image.Image:
    """Готовая картинка плашки с альфой на каждый пиксель.

    Повторяет CSS макета слой за слоем:
        box-shadow: 0 6px 16px <shadow>, inset 0 1px 1px <inner-rim>
        .pill-dot box-shadow: 0 0 8px <dot_glow_rgba>
        .pill-dot::after ripple 1.6s: scale 1 -> 2.2, opacity 0.7 -> 0

    level 0..1 — заполнение полоски звука.
    phase 0..1 — фаза расходящейся волны у точки: растёт со временем, пока
    идёт запись, и равна нулю в остальных состояниях.
    """
    geo = theme.geometry
    look = theme.state(state)
    pad = shadow_padding(theme, scale)
    width, height = plate_size(theme, scale)
    k = SUPERSAMPLE

    image = _chassis(theme, scale).copy()

    # Полоску и волну рисуем маленькими тайлами, а не слоем во весь холст.
    # Увеличенный холст 916x320 с последующим уменьшением стоил 10 мс на кадр,
    # а пульсация обновляется десятки раз в секунду. Тайлы — те же несколько
    # сотен пикселей, и цена падает до единиц миллисекунд.
    mask = _capsule_mask(theme, scale)

    bar_h = geo.bar_height * scale
    border = max(1.0, geo.border_width * scale)
    bar_top = pad + geo.height * scale - border - bar_h - 2 * scale
    bar_left = pad + geo.padding_x * scale
    bar_right = pad + (geo.width - geo.padding_x) * scale
    if bar_right > bar_left:
        image.alpha_composite(
            _tile(
                int(bar_right - bar_left) + 2,
                int(bar_h) + 2,
                lambda d, kk: _draw_bar(d, kk, bar_right - bar_left, bar_h, level, theme, look),
                mask,
                int(bar_left),
                int(bar_top),
            ),
            (int(bar_left), int(bar_top)),
        )

    sprite = _dot_sprite(theme, state, scale)
    dot_cx = pad + (geo.padding_x + geo.dot_diameter / 2) * scale
    dot_cy = pad + geo.height * scale / 2
    image.alpha_composite(
        sprite, (int(round(dot_cx - sprite.width / 2)), int(round(dot_cy - sprite.height / 2)))
    )

    # Расходящаяся волна — ::after у точки в макете: рисуется ПОВЕРХ неё,
    # иначе свечение точки закрывает волну целиком и пульсации не видно.
    if phase > 0.0:
        fade = 0.7 * (1.0 - phase)
        if fade > 0.01:
            grown = geo.dot_diameter * scale * (1.0 + 1.2 * phase)
            side = int(grown) + 2
            cx = pad + (geo.padding_x + geo.dot_diameter / 2) * scale
            cy = pad + geo.height * scale / 2
            colour = (*look.accent[:3], int(255 * fade))
            image.alpha_composite(
                _tile(
                    side,
                    side,
                    lambda d, kk: d.ellipse(
                        (kk, kk, side * kk - kk, side * kk - kk), fill=colour
                    ),
                    mask,
                    int(cx - side / 2),
                    int(cy - side / 2),
                ),
                (int(cx - side / 2), int(cy - side / 2)),
            )

    # Текст рисуем последним и без увеличения: у шрифтов свой хинтинг, и
    # надпись, увеличенная и сжатая обратно, выглядит мутной рядом с системной.
    label = ImageDraw.Draw(image)
    font = find_font(max(7, int(round(geo.font_size * scale))))
    text_left = pad + (geo.padding_x + geo.dot_diameter + geo.text_gap) * scale
    limit = int(round(pad + (geo.width - geo.padding_x) * scale - text_left))
    label.text(
        (text_left, pad + geo.height * scale / 2),
        _fit(text, font, limit),
        font=font,
        fill=_rgba(look.text),
        anchor="lm",
    )
    return image


def _fit(text: str, font, limit: int) -> str:
    """Обрезает подпись по ширине плашки, добавляя многоточие.

    Плашка узкая нарочно — 185 px, чтобы не закрывать текст под собой. Длинные
    сообщения об ошибках в неё не влезают, и лучше честное многоточие, чем
    буквы, уехавшие за кант.
    """
    if limit <= 0 or not text:
        return text
    if font.getlength(text) <= limit:
        return text
    ellipsis = "…"
    cut = text
    while cut and font.getlength(cut + ellipsis) > limit:
        cut = cut[:-1]
    return (cut + ellipsis) if cut else ellipsis


# --- слоёное окно ---------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
# Окно не забирает фокус по щелчку. Без этого перетаскивание плашки уводило бы
# фокус из документа, куда мы собираемся вставлять текст, — и вставка уходила
# бы не туда.
WS_EX_NOACTIVATE = 0x08000000

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _Blend(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


# Прототипы объявлены явно, и это не педантизм. Без них ctypes считает, что
# функция принимает и возвращает C int, то есть 32 бита. Дескрипторы GDI на
# 64-битной Windows приходят со знаковым расширением (CreateCompatibleDC
# отдаёт что-то вроде 0xFFFFFFFF9835...), и следующий же вызов падает с
# «int too long to convert» — причём CreateDIBSection падал молча внутри
# push(), а наружу это выглядело как «плашки просто нет на экране».
# Объявлять приходится ПОСЛЕ структур: их типы нужны в argtypes.
_gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = (
    wintypes.HDC,
    ctypes.POINTER(_BitmapInfo),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
)
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = (wintypes.HDC,)
_gdi32.DeleteDC.restype = wintypes.BOOL
_user32.GetDC.argtypes = (wintypes.HWND,)
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
_user32.ReleaseDC.restype = ctypes.c_int
_user32.UpdateLayeredWindow.argtypes = (
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(_Point),
    ctypes.POINTER(_Size),
    wintypes.HDC,
    ctypes.POINTER(_Point),
    wintypes.DWORD,
    ctypes.POINTER(_Blend),
    wintypes.DWORD,
)
_user32.UpdateLayeredWindow.restype = wintypes.BOOL


def top_level(hwnd: int) -> int:
    """Настоящее окно верхнего уровня для дескриптора от Tk.

    winfo_id() на Windows отдаёт ВНУТРЕННЕЕ окно Tk, а стилями и слоёностью
    управляет его родитель. Ставить WS_EX_LAYERED на внутреннее бесполезно
    и притом молча: все вызовы проходят, ошибок нет, на экране пусто.
    Проверено замером — у внутреннего exstyle 0x4, у родителя 0x88.
    """
    try:
        _user32.GetParent.argtypes = (wintypes.HWND,)
        _user32.GetParent.restype = wintypes.HWND
        parent = _user32.GetParent(hwnd)
    except Exception:  # pragma: no cover - зависит от системы
        return hwnd
    return int(parent) if parent else hwnd


def make_layered(hwnd: int) -> bool:
    """Переводит окно в слоёный режим. False — не вышло, рисовать нечем."""
    try:
        getter = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
        setter = getattr(_user32, "SetWindowLongPtrW", _user32.SetWindowLongW)
        setter.restype = ctypes.c_ssize_t
        setter.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
        getter.restype = ctypes.c_ssize_t
        getter.argtypes = (wintypes.HWND, ctypes.c_int)

        style = getter(hwnd, GWL_EXSTYLE)
        setter(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )
        return True
    except Exception as exc:  # pragma: no cover - зависит от системы
        log.error("не удалось сделать окно слоёным: %s", exc)
        return False


def _premultiplied_bgra(image: Image.Image) -> bytes:
    """BGRA с домноженным на альфу цветом — этого требует UpdateLayeredWindow.

    Без домножения полупрозрачные пиксели светятся: Windows считает, что цвет
    уже умножен, и повторно его не трогает.
    """
    pixels = np.array(image, dtype=np.uint16)
    alpha = pixels[..., 3:4]
    pixels[..., :3] = pixels[..., :3] * alpha // 255
    bgra = pixels[..., [2, 1, 0, 3]].astype(np.uint8)
    return bgra.tobytes()


def push(hwnd: int, image: Image.Image, x: int, y: int) -> bool:
    """Показывает картинку в окне и ставит окно в точку (x, y)."""
    width, height = image.size
    screen_dc = _user32.GetDC(None)
    if not screen_dc:
        return False

    memory_dc = bitmap = old = None
    try:
        memory_dc = _gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            return False

        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        # Отрицательная высота — строки сверху вниз, как в PIL. Иначе картинка
        # окажется перевёрнутой.
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(
            memory_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits:
            return False

        raw = _premultiplied_bgra(image)
        ctypes.memmove(bits, raw, len(raw))
        old = _gdi32.SelectObject(memory_dc, bitmap)

        blend = _Blend(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ok = _user32.UpdateLayeredWindow(
            hwnd,
            screen_dc,
            ctypes.byref(_Point(int(x), int(y))),
            ctypes.byref(_Size(width, height)),
            memory_dc,
            ctypes.byref(_Point(0, 0)),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )
        return bool(ok)
    except Exception as exc:  # pragma: no cover - зависит от системы
        log.debug("не удалось обновить слоёное окно: %s", exc)
        return False
    finally:
        # Уборка не имеет права бросить: исключение отсюда затирает и результат,
        # и настоящую ошибку, если она была.
        try:
            if memory_dc and old:
                _gdi32.SelectObject(memory_dc, old)
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if memory_dc:
                _gdi32.DeleteDC(memory_dc)
            _user32.ReleaseDC(None, screen_dc)
        except Exception:  # pragma: no cover
            log.exception("не удалось освободить объекты GDI")
