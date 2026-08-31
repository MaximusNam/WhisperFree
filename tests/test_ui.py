"""Дым-тесты интерфейса: виджеты строятся и переключают состояния без ошибок.

Как это выглядит на экране, проверяется глазами. Здесь ловится другое —
опечатки в Tk API и обращения к виджетам из чужого потока.
"""

from __future__ import annotations

import itertools
import logging
import math
import tkinter as tk

import pytest

from whisperfree.config import HistoryConfig
from whisperfree.history import History, Record
from whisperfree.history_window import HistoryWindow
from whisperfree.overlay import _BG, _HIDDEN_Y, _STYLES, _WIDTH, Overlay


def pump(root: tk.Tk, times: int = 6) -> None:
    """Прокрутить цикл Tk, чтобы отложенные задания успели выполниться."""
    for _ in range(times):
        root.update()
        root.after(50, root.quit)
        root.mainloop()


def bar_width(overlay: Overlay) -> float:
    """Ширина закрашенной части полоски уровня — то, что видит человек."""
    x0, _y0, x1, _y1 = overlay._bar.coords(overlay._bar_item)
    return x1 - x0


def visible_y(overlay: Overlay) -> int:
    """Y из geometry: плашка прячется уводом за край, а не withdraw."""
    return int(overlay._window.geometry().rsplit("+", 1)[1])


def dot_color(overlay: Overlay) -> str:
    """Цвет кружка — то, чем состояния различаются на глаз, а не по подписи."""
    return overlay._dot.itemcget(overlay._dot.find_all()[0], "fill")


# --- сколько цветов в цвете ---------------------------------------------------
#
# «Цвета достаточно разные» — не вкус, а число. Разность RGB для этого не
# годится: она считает #f00 и #0f0 такими же далёкими, как #700 и #007, хотя
# глаз видит первую пару вдвое яснее. ΔE2000 меряет так, как видит глаз, и
# минимум по всем парам показывает, насколько похожи самые похожие два
# состояния. Формулы живут здесь, а не в overlay.py: на экране они не нужны
# ни разу, а тесту без них остаётся только «цвета не равны».

_D65 = (0.95047, 1.0, 1.08883)


def _to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _to_srgb(value: float) -> float:
    value = min(max(value, 0.0), 1.0)
    return value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def rgb(color: str) -> tuple[float, float, float]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def contrast(one: str, two: str) -> float:
    """Контраст по WCAG: во сколько раз одна яркость больше другой."""
    weights = (0.2126, 0.7152, 0.0722)
    a, b = (
        sum(w * _to_linear(c) for w, c in zip(weights, rgb(color)))
        for color in (one, two)
    )
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def lab(color: str) -> tuple[float, float, float]:
    """CIELAB (D65) — пространство, в котором и считается ΔE2000."""
    r, g, b = (_to_linear(c) for c in rgb(color))
    xyz = (
        0.4124564 * r + 0.3575761 * g + 0.1804375 * b,
        0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
        0.0193339 * r + 0.1191920 * g + 0.9503041 * b,
    )

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = (f(v / w) for v, w in zip(xyz, _D65))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(one: str, two: str) -> float:
    """Насколько два цвета различны для глаза. Ниже единицы — не различить."""
    return _delta_e_lab(lab(one), lab(two))


def _delta_e_lab(first, second) -> float:
    """CIEDE2000. Сверен с эталонными парами Шармы — см. тест ниже."""
    l1, a1, b1 = first
    l2, a2, b2 = second
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cbar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25.0**7))) if cbar else 0.5
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360

    if c1p * c2p == 0:
        dhp, hbar = 0.0, h1p + h2p
    else:
        dhp = (h2p - h1p + 540) % 360 - 180
        if abs(h1p - h2p) <= 180:
            hbar = (h1p + h2p) / 2
        elif h1p + h2p < 360:
            hbar = (h1p + h2p + 360) / 2
        else:
            hbar = (h1p + h2p - 360) / 2

    dl, dc = l2 - l1, c2p - c1p
    dh = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)
    lbar, cbarp = (l1 + l2) / 2, (c1p + c2p) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(hbar - 30))
        + 0.24 * math.cos(math.radians(2 * hbar))
        + 0.32 * math.cos(math.radians(3 * hbar + 6))
        - 0.20 * math.cos(math.radians(4 * hbar - 63))
    )
    sl = 1 + (0.015 * (lbar - 50) ** 2) / math.sqrt(20 + (lbar - 50) ** 2)
    sc = 1 + 0.045 * cbarp
    sh = 1 + 0.015 * cbarp * t
    rt = -2 * math.sqrt(cbarp**7 / (cbarp**7 + 25.0**7)) * math.sin(
        math.radians(60 * math.exp(-(((hbar - 275) / 25) ** 2)))
    )
    return math.sqrt(
        (dl / sl) ** 2 + (dc / sc) ** 2 + (dh / sh) ** 2 + rt * (dc / sc) * (dh / sh)
    )


def hue(color: str) -> float:
    """Угол тона в CIELAB: у чистого красного ≈40°, у зелёного ≈136°."""
    _l, a, b = lab(color)
    return math.degrees(math.atan2(b, a)) % 360


# Матрицы Machado, Oliveira, Fernandes (2009), тяжесть 1.0 — то, как цвет
# доходит до глаза без красного (протанопия) или без зелёного (дейтеранопия)
# колбочкового пигмента. Считаются по линейному RGB.
DEUTERANOPIA = (
    (0.367322, 0.860646, -0.227968),
    (0.280085, 0.672501, 0.047413),
    (-0.011820, 0.042940, 0.968881),
)
PROTANOPIA = (
    (0.152286, 1.052583, -0.204868),
    (0.114503, 0.786281, 0.099216),
    (-0.003882, -0.048116, 1.051998),
)


def as_seen_by(color: str, matrix) -> str:
    linear = [_to_linear(c) for c in rgb(color)]
    out = (_to_srgb(sum(m * v for m, v in zip(row, linear))) for row in matrix)
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in out)


def closest_pair(eye=lambda color: color) -> tuple[float, str, str]:
    """Два самых похожих состояния и ΔE между ними — узкое место палитры."""
    return min(
        (delta_e(eye(_STYLES[one][0]), eye(_STYLES[two][0])), one, two)
        for one, two in itertools.combinations(_STYLES, 2)
    )


class TestOverlay:
    def test_builds_without_stealing_focus_attributes(self, root):
        overlay = Overlay(root, enabled=True)
        assert overlay._window is not None
        assert overlay._window.wm_overrideredirect()

    def test_all_states_apply(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.sending()
        overlay.ok("привет из докера")
        overlay.error("сеть недоступна")
        overlay.hide()
        pump(root)  # ошибок в очереди быть не должно

    def test_long_message_is_truncated(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.error("очень длинная причина " * 20)
        pump(root)
        assert len(overlay._label.cget("text")) <= 60

    def test_disabled_overlay_builds_no_window(self, root):
        overlay = Overlay(root, enabled=False)
        assert overlay._window is None
        overlay.recording()
        overlay.error("не должно упасть")
        pump(root)

    def test_calls_from_other_thread_are_safe(self, root):
        import threading

        overlay = Overlay(root, enabled=True)
        threads = [threading.Thread(target=overlay.recording) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        pump(root)


class TestOverlayLevel:
    """Живой уровень: человек должен видеть, что микрофон слышит, ПО ХОДУ речи."""

    def test_level_reaches_the_widget(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(0.5)
        pump(root)

        assert overlay._level == pytest.approx(0.5)
        assert bar_width(overlay) == pytest.approx(_WIDTH * 0.5)

    def test_level_grows_with_the_voice(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(0.1)
        pump(root)
        quiet = bar_width(overlay)

        overlay.level(0.8)
        pump(root)
        assert bar_width(overlay) > quiet

    @pytest.mark.parametrize(
        "value, expected",
        [
            (-1.0, 0.0),
            (-0.0001, 0.0),
            (1.5, 1.0),
            (float("inf"), 1.0),
            (float("-inf"), 0.0),
            (float("nan"), 0.0),
            (None, 0.0),
            ("громко", 0.0),
            (0.0, 0.0),
            (1.0, 1.0),
        ],
    )
    def test_level_out_of_range_does_not_break_drawing(self, root, value, expected):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(value)
        pump(root)

        assert overlay._level == pytest.approx(expected)
        assert bar_width(overlay) == pytest.approx(_WIDTH * expected)

    def test_level_from_another_thread_is_safe(self, root):
        import threading

        overlay = Overlay(root, enabled=True)
        overlay.recording()
        threads = [
            threading.Thread(target=overlay.level, args=(i / 10,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        pump(root)

    def test_level_on_disabled_overlay_does_not_crash(self, root):
        overlay = Overlay(root, enabled=False)
        overlay.level(0.5)
        overlay.silent()
        pump(root)
        assert overlay._window is None

    def test_level_resets_when_recording_ends(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(0.9)
        pump(root)
        assert bar_width(overlay) > 0

        overlay.sending()
        pump(root)
        assert bar_width(overlay) == pytest.approx(0.0)

    def test_hide_clears_the_level(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(0.9)
        pump(root)

        overlay.hide()
        pump(root)
        assert overlay._level == pytest.approx(0.0)
        assert bar_width(overlay) == pytest.approx(0.0)


class TestOverlaySilent:
    """Молчащий микрофон виден на середине фразы, а не после отпускания."""

    def test_silent_has_its_own_look(self, root):
        color, text = _STYLES["silent"]
        assert text
        assert color != _STYLES["recording"][0]
        assert color != _STYLES["error"][0]

        overlay = Overlay(root, enabled=True)
        overlay.silent()
        pump(root)
        assert overlay._label.cget("text") == text

    def test_recording_to_silent_and_back(self, root):
        overlay = Overlay(root, enabled=True)

        overlay.recording()
        pump(root)
        assert overlay._label.cget("text") == _STYLES["recording"][1]

        overlay.silent()
        pump(root)
        assert overlay._label.cget("text") == _STYLES["silent"][1]

        overlay.recording()
        overlay.level(0.4)
        pump(root)
        assert overlay._label.cget("text") == _STYLES["recording"][1]
        assert bar_width(overlay) == pytest.approx(_WIDTH * 0.4)

    def test_silent_keeps_the_running_level(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(0.02)
        overlay.silent()
        pump(root)

        # Запись идёт, полоску не обнуляем — она честно показывает почти ноль.
        assert overlay._level == pytest.approx(0.02)

    def test_silent_does_not_auto_hide(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.silent()
        pump(root)

        assert overlay._hide_job is None
        assert visible_y(overlay) != _HIDDEN_Y

    def test_recording_does_not_auto_hide(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        pump(root)

        assert overlay._hide_job is None
        assert visible_y(overlay) != _HIDDEN_Y

    def test_error_still_auto_hides(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.error("сеть недоступна")
        pump(root)

        assert overlay._hide_job is not None

    def test_silent_cancels_a_pending_auto_hide(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.error("сеть недоступна")
        pump(root)
        assert overlay._hide_job is not None

        overlay.silent()
        pump(root)
        assert overlay._hide_job is None
        assert visible_y(overlay) != _HIDDEN_Y


class TestOverlayColors:
    """Цвет виден боковым зрением, подпись — нет: у состояний он должен различаться.

    Пороги ниже — закреплённые числа, а не пожелания. Палитра подбиралась
    перебором под максимум минимальной попарной ΔE2000, и если следующая
    правка цветов уронит этот минимум, тест обязан упасть, а не смолчать.
    """

    # Достигнутый минимум по всем 15 парам — 39.0 (было 19.4). Порог чуть ниже:
    # место для округлений, но не для «подкрутил цвет, стало 25, никто не
    # заметил».
    MIN_DELTA_E = 38.0
    # То же для дихроматов: достигнуто 12.0 (было 7.1). Узкое место там —
    # «Готово» против «Ошибки»: зелёное и красное заданы смыслом и по тону для
    # такого глаза сходятся, разводит их только светлота.
    MIN_DELTA_E_CVD = 11.0

    def test_delta_e_matches_the_reference_pairs(self):
        """Сначала поверяем линейку: пары из набора Шармы для CIEDE2000.

        Без этого «минимум 38» — число из ниоткуда: ошибись в формуле, и тест
        будет уверенно охранять неправильную величину.
        """
        reference = [
            ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
            ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
            ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
            ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
            ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
            ((50.0, 2.4900, -0.0010), (50.0, -2.4900, 0.0009), 7.1792),
            ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
            ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ]
        for first, second, expected in reference:
            assert _delta_e_lab(first, second) == pytest.approx(expected, abs=1e-4)

    def test_no_two_states_look_alike(self):
        """Узкое место палитры — самая похожая пара; она и должна быть далеко."""
        distance, one, two = closest_pair()
        assert distance >= self.MIN_DELTA_E, f"ближе всех {one} и {two}: ΔE {distance:.1f}"

    def test_recording_and_error_cannot_be_confused(self):
        # Та самая пара, ради которой всё затевалось: боковым зрением человек
        # должен мгновенно понимать, идёт запись или что-то сломалось.
        # Было 22.9 (розовый против красного) — на глаз почти одно и то же.
        assert delta_e(_STYLES["recording"][0], _STYLES["error"][0]) >= 35.0

    def test_every_color_is_readable_on_the_plate(self):
        """Контраст к фону плашки не ниже 4.5:1 — иначе цвет не разглядеть."""
        for state, (color, _text) in _STYLES.items():
            assert contrast(color, _BG) >= 4.5, f"{state} {color} тонет в фоне"

    def test_meanings_survive_the_repaint(self):
        """Ошибка тревожная, «Готово» зелёное, «микрофон молчит» синий.

        На все три ссылаются комментарии в __main__.py и в самом overlay.py;
        подбор палитры не имеет права молча их перекрасить.
        """
        assert 15.0 <= hue(_STYLES["error"][0]) <= 46.0  # у чистого красного 40°
        assert 125.0 <= hue(_STYLES["ok"][0]) <= 175.0
        assert 245.0 <= hue(_STYLES["silent"][0]) <= 305.0

    @pytest.mark.parametrize(
        "eye", [DEUTERANOPIA, PROTANOPIA], ids=["дейтеранопия", "протанопия"]
    )
    def test_colour_blind_eyes_still_tell_the_states_apart(self, eye):
        """Красное и зелёное для дихромата почти одно и то же — но не здесь.

        Красный у ошибки и зелёный у «Готово» заданы смыслом, поэтому по тону
        они для такого глаза сходятся, и развести их может только светлота.
        """
        distance, one, two = closest_pair(lambda color: as_seen_by(color, eye))
        assert distance >= self.MIN_DELTA_E_CVD, (
            f"ближе всех {one} и {two}: ΔE {distance:.1f}"
        )

    def test_recording_and_error_do_not_share_a_color(self):
        # Красная «Запись…» и красная «Ошибка» отличались только буквами —
        # ровно то, чего полоска уровня и цвета заводились не допускать.
        assert _STYLES["recording"][0] != _STYLES["error"][0]

    def test_every_state_has_its_own_color(self):
        colors = [color for color, _text in _STYLES.values()]
        assert len(set(colors)) == len(_STYLES)

    def test_dot_changes_color_from_recording_to_error(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        pump(root)
        recording_color = dot_color(overlay)

        overlay.error("сеть недоступна")
        pump(root)
        assert dot_color(overlay) != recording_color


class TestOverlaySession:
    """Плашка одна на все диктовки: хвост прошлой не должен гасить следующую."""

    def test_begin_session_returns_growing_numbers(self, root):
        overlay = Overlay(root, enabled=True)
        assert overlay.begin_session() < overlay.begin_session()

    def test_stale_ok_does_not_overwrite_the_running_recording(self, root, caplog):
        overlay = Overlay(root, enabled=True)
        stale = overlay.begin_session()
        overlay.begin_session()  # человек нажал клавишу заново
        overlay.recording()
        pump(root, 3)

        with caplog.at_level(logging.DEBUG, logger="whisperfree.overlay"):
            overlay.ok("хвост прошлой диктовки", session=stale)
            pump(root, 3)

        assert overlay._label.cget("text") == _STYLES["recording"][1]
        assert overlay._hide_job is None  # и авто-скрытия от старого «Готово» нет
        assert visible_y(overlay) != _HIDDEN_Y
        # Отброс без следа в логе отладить было бы нечем.
        assert any(
            record.levelno == logging.DEBUG and "отбросил" in record.getMessage()
            for record in caplog.records
        )

    def test_stale_error_does_not_overwrite_the_running_recording(self, root):
        overlay = Overlay(root, enabled=True)
        stale = overlay.begin_session()
        overlay.begin_session()
        overlay.recording()
        pump(root, 3)

        overlay.error("сеть недоступна", session=stale)
        pump(root, 3)

        assert overlay._label.cget("text") == _STYLES["recording"][1]
        assert overlay._hide_job is None

    def test_ok_from_the_current_session_is_shown(self, root):
        overlay = Overlay(root, enabled=True)
        current = overlay.begin_session()
        overlay.recording()
        pump(root, 3)

        overlay.ok("готовый текст", session=current)
        pump(root, 3)

        assert overlay._label.cget("text") == "готовый текст"
        assert overlay._hide_job is not None

    def test_call_without_a_session_works_as_before(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.begin_session()
        overlay.begin_session()
        overlay.recording()
        pump(root, 3)

        # Поколение есть, но вызывающий его не знает — сообщение проходит.
        overlay.ok("без поколения")
        pump(root, 3)
        assert overlay._label.cget("text") == "без поколения"

        overlay.error("тоже без поколения")
        pump(root, 3)
        assert overlay._label.cget("text") == "тоже без поколения"

    def test_new_state_cancels_the_hanging_auto_hide(self, root):
        """Отложенное скрытие прошлой диктовки не гасит только что зажжённую плашку."""
        overlay = Overlay(root, enabled=True)
        overlay.ok("прошлая диктовка")
        pump(root, 3)
        assert overlay._hide_job is not None

        overlay.recording()
        pump(root, 3)

        assert overlay._hide_job is None
        assert overlay._label.cget("text") == _STYLES["recording"][1]
        assert visible_y(overlay) != _HIDDEN_Y

    @pytest.mark.parametrize("say", ["ok", "error"])
    def test_message_gone_stale_between_check_and_show_is_dropped(
        self, root, caplog, monkeypatch, say
    ):
        """Щель между проверкой поколения и показом.

        В error() между _stale() и _push() стоит log.warning — синхронная
        запись на диск, и это не единственная задержка: поток может уступить
        процессор в любой точке. Клавишу успевают нажать ровно там, и тогда
        сообщение, признанное свежим, доходит до плашки уже устаревшим —
        и гасит только что зажжённую «Запись…».
        """
        overlay = Overlay(root, enabled=True)
        stale = overlay.begin_session()
        overlay.recording()
        pump(root, 3)

        queue_message = overlay._push

        def new_dictation_starts_first(*args, **kwargs):
            # Ровно та щель: поколение уже проверено, сообщение ещё не в
            # очереди, и здесь человек нажимает клавишу заново.
            overlay.begin_session()
            queue_message(*args, **kwargs)

        monkeypatch.setattr(overlay, "_push", new_dictation_starts_first)

        with caplog.at_level(logging.DEBUG, logger="whisperfree.overlay"):
            if say == "ok":
                overlay.ok("хвост прошлой диктовки", session=stale)
            else:
                overlay.error("хвост прошлой ошибки", session=stale)
            pump(root, 3)

        assert overlay._label.cget("text") == _STYLES["recording"][1]
        assert overlay._hide_job is None
        assert visible_y(overlay) != _HIDDEN_Y
        assert any(
            record.levelno == logging.DEBUG and "отбросил" in record.getMessage()
            for record in caplog.records
        )

    def test_fresh_message_still_reaches_the_plate(self, root):
        """Обратная сторона: проверка в момент показа не должна глотать своё."""
        overlay = Overlay(root, enabled=True)
        current = overlay.begin_session()
        overlay.recording()
        pump(root, 3)

        overlay.error("сеть недоступна", session=current)
        pump(root, 3)
        assert overlay._label.cget("text") == "сеть недоступна"

    def test_begin_session_from_another_thread_is_safe(self, root):
        import threading

        overlay = Overlay(root, enabled=True)
        seen: list[int] = []
        lock = threading.Lock()

        def take() -> None:
            number = overlay.begin_session()
            with lock:
                seen.append(number)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Номера не повторяются — иначе два показа считались бы одним.
        assert sorted(seen) == list(range(1, 9))


class TestHistoryWindow:
    @pytest.fixture
    def history(self, tmp_path):
        h = History(tmp_path / "history.jsonl", HistoryConfig())
        h.add(Record(ts=1_700_000_000.0, text="поставь докер", target_exe="notepad.exe"))
        h.add(
            Record(
                ts=1_700_000_100.0,
                text="проверь через Gemini",
                target_exe="chrome.exe",
                error="нет активного окна",
            )
        )
        return h

    def test_opens_and_lists_records(self, root, history):
        window = HistoryWindow(root, history, lambda r: None, lambda r: None)
        window.open()
        pump(root)

        assert window._tree is not None
        assert len(window._tree.get_children()) == 2

    def test_failed_record_is_marked(self, root, history):
        window = HistoryWindow(root, history, lambda r: None, lambda r: None)
        window.open()
        pump(root)

        first = window._tree.get_children()[0]
        assert "failed" in window._tree.item(first, "tags")

    def test_search_filters(self, root, history):
        window = HistoryWindow(root, history, lambda r: None, lambda r: None)
        window.open()
        pump(root)

        window._search.set("докер")
        window._refresh()
        assert len(window._tree.get_children()) == 1

    def test_copy_calls_back_with_the_record(self, root, history):
        copied = []
        window = HistoryWindow(root, history, lambda r: None, copied.append)
        window.open()
        pump(root)

        window._tree.selection_set(window._tree.get_children()[0])
        window._copy_selected()
        assert copied[0].text == "проверь через Gemini"

    def test_reopening_reuses_the_window(self, root, history):
        window = HistoryWindow(root, history, lambda r: None, lambda r: None)
        window.open()
        pump(root)
        first = window._window

        window._close()
        window.open()
        pump(root)
        assert window._window is first
