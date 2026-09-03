"""Интерфейс: плашка состояния, её оформление и окно истории.

Плашка рисуется картинкой в слоёном окне Windows, а не виджетами Tk, поэтому
и проверяется здесь картинка — то, что человек в самом деле видит. Проверять
поля объекта было бы удобнее и бесполезнее: они совпадали бы с ожидаемыми и
при сломанной отрисовке.
"""

from __future__ import annotations

import logging
from dataclasses import replace
import threading
import tkinter as tk

import numpy as np
import pytest
from PIL import Image

from whisperfree import plate
from whisperfree.config import HistoryConfig
from whisperfree.history import History, Record
from whisperfree.history_window import HistoryWindow
from whisperfree.overlay import _HIDDEN_Y, Overlay
from whisperfree.theme import Theme, load_themes, parse_color, pick


def pump(root: tk.Tk, times: int = 6) -> None:
    """Прокрутить цикл Tk, чтобы отложенные задания успели выполниться."""
    for _ in range(times):
        root.update()
        root.after(50, root.quit)
        root.mainloop()


def visible_y(overlay: Overlay) -> int:
    """Y из geometry: плашка прячется уводом за край, а не withdraw."""
    return int(overlay._window.geometry().rsplit("+", 1)[1])


def shot(overlay: Overlay) -> np.ndarray:
    """Картинка плашки в её текущем состоянии, RGBA как массив."""
    image = plate.render(
        overlay.theme, overlay._state, overlay._text, overlay._level, overlay._scale
    )
    return np.array(image)


def dot_color(overlay: Overlay) -> tuple[int, int, int]:
    """Цвет точки статуса прямо с картинки.

    Точка — то, чем состояния различаются боковым зрением; подпись в этот
    момент человек не читает, он смотрит туда, куда диктует.
    """
    geo = overlay.theme.geometry
    pixels = shot(overlay)
    pad = plate.shadow_padding(overlay.theme, overlay._scale)
    x = int(pad + (geo.padding_x + geo.dot_diameter / 2) * overlay._scale)
    y = int(pad + geo.height * overlay._scale / 2)
    return tuple(int(v) for v in pixels[y, x, :3])


def bar_width(overlay: Overlay) -> int:
    """Ширина закрашенной части полоски уровня в пикселях."""
    geo = overlay.theme.geometry
    pixels = shot(overlay)
    accent = np.array(overlay.theme.state(overlay._state or "recording").accent[:3])
    pad = plate.shadow_padding(overlay.theme, overlay._scale)
    row = int(pad + (geo.height - geo.bar_height - 2) * overlay._scale)
    row = max(0, min(pixels.shape[0] - 1, row))
    band = pixels[row, :, :3].astype(int)
    close = np.all(np.abs(band - accent) < 40, axis=1)
    return int(close.sum())


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

    def test_disabled_overlay_builds_no_window(self, root):
        overlay = Overlay(root, enabled=False)
        assert overlay._window is None
        overlay.recording()
        overlay.level(0.5)
        overlay.hide()
        pump(root)

    def test_calls_from_other_thread_are_safe(self, root):
        overlay = Overlay(root, enabled=True)

        def worker():
            for _ in range(20):
                overlay.recording()
                overlay.level(0.4)
                overlay.ok("готово")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        pump(root)

    def test_hidden_plate_goes_off_screen(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        pump(root)
        assert visible_y(overlay) != _HIDDEN_Y
        overlay.hide()
        pump(root)
        assert visible_y(overlay) == _HIDDEN_Y


class TestPlateDrawing:
    """Картинка плашки — единственное, что видит человек."""

    def test_size_matches_the_layout(self, root):
        overlay = Overlay(root, enabled=True)
        geo = overlay.theme.geometry
        # Картинка больше капсулы: вокруг неё поле под тень, иначе тень
        # обрезалась бы по краю окна и превращалась в тёмную полосу.
        pad = plate.shadow_padding(overlay.theme)
        image = plate.render(overlay.theme, "recording", "Запись…")
        assert image.size == (geo.width + pad * 2, geo.height + pad * 2)
        assert image.size == plate.plate_size(overlay.theme)
        assert image.mode == "RGBA"

    def test_corners_are_transparent_and_middle_is_not(self, root):
        # Капсула, а не прямоугольник: углы обязаны быть пустыми, иначе поверх
        # документа висит непрозрачная плитка.
        overlay = Overlay(root, enabled=True)
        pixels = np.array(plate.render(overlay.theme, "recording", "Запись…"))
        pad = plate.shadow_padding(overlay.theme)
        assert pixels[pad, pad, 3] < 60, "угол капсулы непрозрачен"
        middle = pixels[pixels.shape[0] // 2, pixels.shape[1] // 2, 3]
        assert middle > 0

    def test_the_glass_lets_the_document_through(self, root):
        # Ради этого макет и рисовался: 13% заливки, текст под плашкой читается.
        overlay = Overlay(root, enabled=True)
        pixels = np.array(plate.render(overlay.theme, "recording", "Запись…"))
        pad = plate.shadow_padding(overlay.theme)
        centre = pixels[pixels.shape[0] // 2, pixels.shape[1] - pad - 20, 3]
        assert 0 < centre < 120, f"заливка непрозрачна: альфа {centre}"

    def test_nothing_escapes_the_capsule(self, root):
        # Полоска уровня идёт по прямой с отступом 14 px, а у пилюли радиус
        # равен половине высоты: у нижнего края форма сужается, и без маски
        # концы полоски торчали наружу двумя усами.
        overlay = Overlay(root, enabled=True)
        pixels = np.array(plate.render(overlay.theme, "recording", "Запись…", level=1.0))
        pad = plate.shadow_padding(overlay.theme)
        alpha = pixels[:, :, 3]
        bottom = alpha[-pad - 2]
        top = alpha[alpha.shape[0] // 2]
        assert int((bottom > 8).sum()) < int((top > 8).sum())

    def test_level_grows_with_the_voice(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        pump(root)
        widths = []
        for value in (0.0, 0.5, 1.0):
            overlay.level(value)
            pump(root)
            widths.append(bar_width(overlay))
        assert widths[0] < widths[1] < widths[2], widths

    def test_long_message_is_cut_to_fit(self, root):
        overlay = Overlay(root, enabled=True)
        font = plate.find_font(overlay.theme.geometry.font_size)
        cut = plate._fit("очень длинная причина " * 10, font, 120)
        assert cut.endswith("…")
        assert font.getlength(cut) <= 120

    def test_short_message_is_left_alone(self, root):
        overlay = Overlay(root, enabled=True)
        font = plate.find_font(overlay.theme.geometry.font_size)
        assert plate._fit("Готово", font, 120) == "Готово"


def over_white(image) -> np.ndarray:
    """Картинка, положенная на белый лист.

    Мерить по сырым каналам нельзя: там, где альфа мала, значения RGB ничего
    не значат — они остались от того, чем рисовали. Человек же видит именно
    результат наложения на документ, обычно светлый.
    """
    sheet = Image.new("RGB", image.size, (255, 255, 255))
    sheet.paste(image, (0, 0), image)
    return np.array(sheet, dtype=int)


def top_edge_colors(overlay: Overlay) -> list[tuple[int, int, int]]:
    """Цвет самого верхнего непрозрачного пикселя вдоль верхней кромки.

    Ровно там пользователь и увидел разрывы: светлая внутренняя грань,
    нарисованная прямой чертой на постоянной высоте, у краёв ложилась поверх
    тёмного канта — а верхний край капсулы к углам загибается вниз.
    """
    geo = overlay.theme.geometry
    pad = plate.shadow_padding(overlay.theme, overlay._scale)
    pixels = shot(overlay)
    width = int(geo.width * overlay._scale)
    out = []
    for x in range(pad + 8, pad + width - 8):
        column = pixels[:, x]
        rows = np.nonzero(column[:, 3] > 140)[0]
        if len(rows):
            out.append(tuple(int(v) for v in column[rows[0], :3]))
    return out


class TestTheLookFromTheSpec:
    """Слои из макета, которые видно глазом: кант, тень, свечение, пульс."""

    def test_the_top_rim_is_not_broken(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        colors = top_edge_colors(overlay)
        assert colors, "верхнюю кромку не нашли"
        brightest = max(sum(c) for c in colors)
        darkest = min(sum(c) for c in colors)
        assert brightest - darkest < 210, f"кант рвётся: от {darkest} до {brightest}"

    def test_the_plate_casts_a_shadow(self, root):
        # box-shadow: 0 6px 16px в макете. Без тени капсула лежит на экране
        # плоской наклейкой, а не парит над документом.
        overlay = Overlay(root, enabled=True)
        pixels = np.array(plate.render(overlay.theme, "recording", "Запись…"))
        pad = plate.shadow_padding(overlay.theme)
        assert pixels[-pad // 2, pixels.shape[1] // 2, 3] > 0, "под плашкой нет тени"

    def test_the_dot_glows(self, root):
        # box-shadow: 0 0 8px цветом состояния. Свечение берётся из темы
        # (dot_glow_rgba), а не выводится из цвета точки.
        #
        # Сравниваем с той же темой без свечения, а не с порогом: внутри
        # капсулы и так есть заливка, и любой порог показал бы «что-то есть»
        # даже при полностью выключенном свечении.
        overlay = Overlay(root, enabled=True)
        theme = overlay.theme
        geo = theme.geometry
        pad = plate.shadow_padding(theme)
        row = int(pad + geo.height / 2)
        # Сразу за краем точки: там свечение сильнее всего, дальше оно тает.
        beside = int(pad + (geo.padding_x + geo.dot_diameter / 2) + geo.dot_diameter * 0.75)

        dark = replace(
            theme,
            id=theme.id + "-без-свечения",
            states={
                name: replace(look, glow=(0, 0, 0, 0))
                for name, look in theme.states.items()
            },
        )
        plate.clear_cache()
        with_glow = over_white(plate.render(theme, "recording", "Запись…"))
        plate.clear_cache()
        without = over_white(plate.render(dark, "recording", "Запись…"))
        plate.clear_cache()

        assert with_glow[row, beside].sum() < without[row, beside].sum(), (
            f"свечения не видно: со свечением {with_glow[row, beside].sum()}, "
            f"без {without[row, beside].sum()}"
        )

    def test_the_ripple_grows_with_the_phase(self, root):
        # ripple 1.6s: круг растёт от 1 до 2.2 и гаснет. Это та самая
        # пульсация, по которой видно, что программа слушает.
        overlay = Overlay(root, enabled=True)
        geo = overlay.theme.geometry
        pad = plate.shadow_padding(overlay.theme)
        row = int(pad + geo.height / 2)
        centre = int(pad + (geo.padding_x + geo.dot_diameter / 2))
        # Смотрим на кольцо, которое лежит ВНЕ маленькой волны и ВНУТРИ
        # большой: радиус волны равен половине точки, умноженной на 1 + 1.2·фаза.
        # Считать пиксели ярче порога бессмысленно — внутри капсулы есть
        # заливка, и счётчик упирается в потолок при любой фазе.
        sample = centre + int(geo.dot_diameter * 0.625)
        shades = []
        for phase in (0.05, 0.45):
            pixels = over_white(
                plate.render(overlay.theme, "recording", "Запись…", phase=phase)
            )
            shades.append(int(pixels[row, sample].sum()))
        assert shades[1] < shades[0], f"волна не растёт: {shades}"

    def test_the_pulse_only_runs_while_recording(self, root):
        # В остальных состояниях перерисовки нет вовсе: кадр стоит около 4 мс,
        # и жечь их без нужды незачем.
        overlay = Overlay(root, enabled=True)
        overlay._state = "sending"
        assert overlay._phase() == 0.0
        overlay._state = "recording"
        assert 0.0 <= overlay._phase() < 1.0


class TestOverlayLevel:
    def test_level_out_of_range_does_not_break_drawing(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        for value in (-1.0, 2.0, float("nan"), float("inf"), None, "нет"):
            overlay.level(value)
            pump(root, 2)
        assert 0.0 <= overlay._level <= 1.0

    def test_level_resets_when_recording_ends(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(0.9)
        pump(root)
        overlay.ok("готово")
        pump(root)
        assert overlay._level == 0.0

    def test_hide_clears_the_level(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        overlay.level(0.8)
        pump(root)
        overlay.hide()
        pump(root)
        assert overlay._level == 0.0


class TestOverlaySilent:
    def test_silent_has_its_own_look(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        pump(root)
        recording = dot_color(overlay)
        overlay.silent()
        pump(root)
        assert overlay._text == overlay.theme.state("silent").label
        assert dot_color(overlay) != recording

    def test_recording_to_silent_and_back(self, root):
        overlay = Overlay(root, enabled=True)
        for _ in range(3):
            overlay.recording()
            pump(root, 2)
            assert overlay._state == "recording"
            overlay.silent()
            pump(root, 2)
            assert overlay._state == "silent"

    def test_silent_does_not_auto_hide(self, root):
        # Клавишу всё ещё держат: плашка обязана остаться на экране.
        overlay = Overlay(root, enabled=True)
        overlay.silent()
        pump(root)
        assert overlay._hide_job is None

    def test_recording_does_not_auto_hide(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.recording()
        pump(root)
        assert overlay._hide_job is None

    def test_error_still_auto_hides(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.error("сеть недоступна")
        pump(root)
        assert overlay._hide_job is not None

    def test_silent_cancels_a_pending_auto_hide(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.ok("готово")
        pump(root)
        overlay.silent()
        pump(root)
        assert overlay._hide_job is None


class TestThemes:
    """Оформление лежит в JSON: его правит человек, значит оно бывает битым."""

    def test_primary_is_first(self):
        themes = load_themes()
        assert next(iter(themes)) == "warm_smoky_mocha"

    def test_every_theme_knows_every_state(self):
        for theme in load_themes().values():
            for state in ("recording", "silent", "sending", "refining", "ok", "error"):
                look = theme.state(state)
                assert look.label
                assert len(look.accent) == 4

    def test_meaning_survives_the_theme(self):
        # Тема меняет корпус плашки и огонёк записи, но не смысл: «Ошибка»
        # обязана быть красной в любом оформлении, иначе тема, подобранная
        # под цвет канта, спрячет поломку.
        for theme in load_themes().values():
            red, green, blue, _ = theme.state("error").accent
            assert red > green and red > blue, theme.id
            red, green, blue, _ = theme.state("ok").accent
            assert green > red, theme.id

    def test_duplicates_are_not_offered_twice(self):
        # Утверждённый макет лежит в двух файлах под разными именами.
        # Два одинаковых пункта в меню заставили бы гадать, чем они отличаются.
        themes = load_themes()
        signatures = [theme.signature for theme in themes.values()]
        assert len(signatures) == len(set(signatures))

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("rgba(48, 40, 36, 0.13)", (48, 40, 36, 33)),
            ("rgb(1, 2, 3)", (1, 2, 3, 255)),
            ("#1C1917", (28, 25, 23, 255)),
            ("#fff", (255, 255, 255, 255)),
        ],
    )
    def test_colours_are_parsed(self, text, expected):
        assert parse_color(text) == expected

    @pytest.mark.parametrize("junk", ["", "синий", None, 42, "rgba()", "#zz"])
    def test_broken_colour_does_not_crash(self, junk):
        assert parse_color(junk, default=(1, 2, 3, 4)) == (1, 2, 3, 4)

    def test_missing_files_still_give_a_theme(self, tmp_path):
        # Без плашки программа становится молчаливой, а это ровно та беда,
        # от которой плашка и заведена.
        themes = load_themes(tmp_path)
        assert themes
        assert pick(themes, None).state("recording").label

    def test_unknown_name_falls_back_to_primary(self, caplog):
        themes = load_themes()
        with caplog.at_level(logging.WARNING):
            chosen = pick(themes, "такой-темы-нет")
        assert chosen.id == next(iter(themes))
        assert "такой-темы-нет" in caplog.text

    def test_switching_changes_what_is_drawn(self, root):
        themes = load_themes()
        overlay = Overlay(root, enabled=True, theme=themes["warm_smoky_mocha"])
        overlay.recording()
        pump(root)
        before = dot_color(overlay)

        overlay.set_theme(themes["graphite_coral_accent"])
        pump(root)
        assert overlay.theme.id == "graphite_coral_accent"
        assert dot_color(overlay) != before


class TestDragging:
    """Плашку можно перетащить: она висит поверх всего и кому-то мешает."""

    class Event:
        def __init__(self, x, y):
            self.x_root, self.y_root = x, y

    def test_drag_moves_the_plate(self, root):
        overlay = Overlay(root, enabled=True, position=(100, 100))
        overlay.recording()
        pump(root)

        overlay._grab(self.Event(150, 120))
        overlay._drag(self.Event(250, 220))
        assert (overlay._x, overlay._y) == (200, 200)

    def test_the_new_place_is_remembered(self, root):
        seen = []
        overlay = Overlay(
            root, enabled=True, position=(100, 100), on_move=lambda x, y: seen.append((x, y))
        )
        overlay._grab(self.Event(100, 100))
        overlay._drag(self.Event(300, 260))
        overlay._drop(self.Event(300, 260))
        assert seen == [(overlay._x, overlay._y)]

    def test_a_click_without_moving_does_not_jump(self, root):
        overlay = Overlay(root, enabled=True, position=(120, 140))
        overlay._grab(self.Event(130, 150))
        overlay._drag(self.Event(130, 150))
        assert (overlay._x, overlay._y) == (120, 140)

    def test_the_plate_cannot_leave_the_screen(self, root):
        # Экран могли отключить или сменить разрешение, а место осталось от
        # прошлой раскладки: плашка нашлась бы за границей и выглядела бы
        # как «перестала показываться».
        overlay = Overlay(root, enabled=True)
        overlay._grab(self.Event(0, 0))
        overlay._drag(self.Event(-5000, -5000))
        assert overlay._x >= 0 and overlay._y >= 0

        overlay._grab(self.Event(overlay._x, overlay._y))
        overlay._drag(self.Event(50000, 50000))
        width, height = overlay.size
        assert overlay._x <= root.winfo_screenwidth() - width
        assert overlay._y <= root.winfo_screenheight() - height

    def test_a_remembered_place_is_used_at_start(self, root):
        overlay = Overlay(root, enabled=True, position=(321, 234))
        assert (overlay._x, overlay._y) == (321, 234)

    def test_saving_that_fails_does_not_break_the_drag(self, root, caplog):
        def explode(x, y):
            raise OSError("конфиг только для чтения")

        overlay = Overlay(root, enabled=True, position=(10, 10), on_move=explode)
        overlay._grab(self.Event(10, 10))
        overlay._drag(self.Event(60, 60))
        with caplog.at_level(logging.ERROR):
            overlay._drop(self.Event(60, 60))
        assert (overlay._x, overlay._y) == (60, 60)
        assert "положение" in caplog.text


class TestOverlaySession:
    def test_begin_session_returns_growing_numbers(self, root):
        overlay = Overlay(root, enabled=True)
        assert [overlay.begin_session() for _ in range(3)] == [1, 2, 3]

    def test_stale_ok_does_not_overwrite_the_running_recording(self, root, caplog):
        overlay = Overlay(root, enabled=True)
        stale = overlay.begin_session()
        overlay.begin_session()
        overlay.recording()
        pump(root)

        with caplog.at_level(logging.DEBUG, logger="whisperfree.overlay"):
            overlay.ok("текст прошлой диктовки", stale)
            pump(root)

        assert overlay._state == "recording"
        assert overlay._hide_job is None
        assert "опоздавшее" in caplog.text

    def test_stale_error_does_not_overwrite_the_running_recording(self, root):
        overlay = Overlay(root, enabled=True)
        stale = overlay.begin_session()
        overlay.begin_session()
        overlay.recording()
        pump(root)

        overlay.error("ошибка прошлой диктовки", stale)
        pump(root)
        assert overlay._state == "recording"

    def test_stale_refining_does_not_overwrite_it_either(self, root):
        overlay = Overlay(root, enabled=True)
        stale = overlay.begin_session()
        overlay.begin_session()
        overlay.recording()
        pump(root)

        overlay.refining(stale)
        pump(root)
        assert overlay._state == "recording"

    def test_ok_from_the_current_session_is_shown(self, root):
        overlay = Overlay(root, enabled=True)
        current = overlay.begin_session()
        overlay.recording()
        pump(root)
        overlay.ok("всё получилось", current)
        pump(root)
        assert overlay._state == "ok"

    def test_call_without_a_session_works_as_before(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.begin_session()
        overlay.recording()
        pump(root)
        overlay.ok("без номера")
        pump(root)
        assert overlay._state == "ok"

    def test_new_state_cancels_the_hanging_auto_hide(self, root):
        overlay = Overlay(root, enabled=True)
        overlay.ok("готово")
        pump(root)
        assert overlay._hide_job is not None
        overlay.recording()
        pump(root)
        assert overlay._hide_job is None

    def test_message_gone_stale_between_check_and_show_is_dropped(self, root):
        # Между проверкой у отправителя и показом в потоке Tk человек успевает
        # нажать клавишу заново: номер едет в очереди и сверяется ещё раз.
        overlay = Overlay(root, enabled=True)
        current = overlay.begin_session()
        overlay.recording()
        pump(root)

        overlay._queue.put(("state", "ok", "поздний ответ", 1200, current))
        overlay.begin_session()  # диктовка началась, пока сообщение лежало в очереди
        pump(root)

        assert overlay._state == "recording"

    def test_begin_session_from_another_thread_is_safe(self, root):
        overlay = Overlay(root, enabled=True)
        seen = []

        def worker():
            for _ in range(50):
                seen.append(overlay.begin_session())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(seen)) == len(seen)


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


class TestLexiconWindow:
    """Окно выученных правок: обучение обязано быть видимым и обратимым."""

    @pytest.fixture
    def lex(self, tmp_path):
        from whisperfree.config import LexiconConfig
        from whisperfree.lexicon import Lexicon

        lexicon = Lexicon(tmp_path / "lexicon.json", LexiconConfig())
        # Испортила модель — такую правку в окне видно цветом.
        lexicon.learn("поднял редис", "поднял Redis", raw="поднял Redis")
        lexicon.learn("снёс редис", "снёс Redis", raw="снёс Redis")
        # Не расслышал микрофон, и правилом замены это не станет никогда.
        lexicon.learn("к сожелению", "к сожалению")
        return lexicon

    def window(self, root, lex, on_change=None):
        from whisperfree.lexicon_window import LexiconWindow

        win = LexiconWindow(root, lex, on_change=on_change)
        win.open()
        pump(root)
        return win

    def test_opens_and_lists_lessons(self, root, lex):
        win = self.window(root, lex)
        assert win._tree is not None
        assert len(win._tree.get_children()) == 2

    def test_frequent_lesson_comes_first(self, root, lex):
        win = self.window(root, lex)
        first = win._tree.get_children()[0]
        assert win._tree.item(first, "values")[1] == "Redis"

    def test_model_mistakes_are_marked(self, root, lex):
        win = self.window(root, lex)
        first = win._tree.get_children()[0]
        assert "refine" in win._tree.item(first, "tags")

    def test_rule_and_hint_are_shown_differently(self, root, lex):
        win = self.window(root, lex)
        rows = {
            win._tree.item(iid, "values")[1]: win._tree.item(iid, "values")[3]
            for iid in win._tree.get_children()
        }
        assert rows["Redis"] == "замена"
        # Правкой внутри одного алфавита замена не станет, и обещать её нельзя.
        assert rows["сожалению"] == "подсказка"

    def test_blame_is_written_in_words(self, root, lex):
        win = self.window(root, lex)
        blames = {
            win._tree.item(iid, "values")[2] for iid in win._tree.get_children()
        }
        assert "испортила правка моделью" in blames

    def test_forgetting_removes_the_row_and_tells_the_app(self, root, lex):
        called = []
        win = self.window(root, lex, on_change=lambda: called.append(True))
        win._tree.selection_set(win._tree.get_children()[0])
        win._forget_selected()
        pump(root)

        assert len(win._tree.get_children()) == 1
        # Приложение обязано узнать сразу: иначе выброшенное правило
        # доживёт до перезапуска.
        assert called == [True]

    def test_forget_all_empties_the_list(self, root, lex):
        called = []
        win = self.window(root, lex, on_change=lambda: called.append(True))
        win._forget_all()
        pump(root)

        assert win._tree.get_children() == ()
        assert lex.lessons == []
        assert called == [True]

    def test_forgetting_nothing_selected_is_harmless(self, root, lex):
        win = self.window(root, lex)
        win._forget_selected()
        pump(root)
        assert len(win._tree.get_children()) == 2

    def test_reopening_shows_fresh_data(self, root, lex):
        win = self.window(root, lex)
        lex.learn("открыл фигму", "открыл Figma")
        win.open()
        pump(root)
        assert len(win._tree.get_children()) == 3

    def test_empty_lexicon_opens_without_rows(self, root, tmp_path):
        from whisperfree.config import LexiconConfig
        from whisperfree.lexicon import Lexicon

        empty = Lexicon(tmp_path / "empty.json", LexiconConfig())
        win = self.window(root, empty)
        assert win._tree.get_children() == ()
