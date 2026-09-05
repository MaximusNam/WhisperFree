"""Горячие клавиши: раскладка, подавление, порядок событий и потерянный KEYUP."""

from __future__ import annotations

import logging

from pynput import keyboard

from whisperfree import hotkey as hotkey_mod
from whisperfree.hotkey import HotkeyManager, key_is_down, key_names, parse_spec, vk_for


class FakeKeyCode:
    """Имитирует pynput.KeyCode: символ зависит от раскладки, vk — нет."""

    def __init__(self, char: str | None, vk: int | None):
        self.char = char
        self.vk = vk


class TestKeyNames:
    def test_modifier_gets_both_sided_and_generic_name(self):
        names = key_names(keyboard.Key.ctrl_r)
        assert "ctrl_r" in names and "ctrl" in names

    def test_alt_is_also_known_as_menu(self):
        assert "menu" in key_names(keyboard.Key.alt_r)

    def test_latin_letter_recognised_on_russian_layout(self):
        # Физическая клавиша V на русской раскладке даёт «м».
        # Без опоры на vk сочетание ctrl+alt+v тут бы не сработало.
        names = key_names(FakeKeyCode(char="м", vk=0x56))
        assert "v" in names
        assert "м" in names

    def test_digit_recognised_by_vk(self):
        assert "5" in key_names(FakeKeyCode(char="%", vk=0x35))

    def test_key_without_vk_still_yields_its_char(self):
        assert key_names(FakeKeyCode(char="q", vk=None)) == {"q"}


class TestParseSpec:
    def test_combo_splits(self):
        assert parse_spec("ctrl+alt+v") == ["ctrl", "alt", "v"]

    def test_case_and_spaces_forgiven(self):
        assert parse_spec("  Ctrl + ALT + V ") == ["ctrl", "alt", "v"]

    def test_empty_means_disabled(self):
        assert parse_spec("") is None
        assert parse_spec("   ") is None


class TestSuppression:
    def test_modifier_is_never_suppressed(self):
        # Проглоченный Ctrl превратил бы Ctrl+C в букву «c» в тексте.
        manager = HotkeyManager(suppress=True)
        manager.register_hold("ctrl_r", lambda: None, lambda: None)
        assert manager._suppress_vks == set()

    def test_dedicated_key_is_suppressed(self):
        manager = HotkeyManager(suppress=True)
        manager.register_hold("f13", lambda: None, lambda: None)
        assert manager._suppress_vks == {0x7C}

    def test_suppression_can_be_turned_off(self):
        manager = HotkeyManager(suppress=False)
        manager.register_hold("scroll_lock", lambda: None, lambda: None)
        assert manager._suppress_vks == set()


class TestComboSuppression:
    """Клавиша сочетания не должна доходить до приложения — это защита текста.

    Настоящий случай от пользователя: он выделял исправленный текст, жал
    Ctrl+Alt+U, и в окно уезжала буква «Г», заменяя собой выделенное. Windows
    считает Ctrl+Alt за AltGr, а на русской раскладке физическая клавиша U —
    это «Г». Человек нажимал «запомнить правку» и терял абзац.
    """

    def make(self, spec="ctrl+alt+u", suppress=True):
        manager = HotkeyManager(suppress=suppress)
        manager.register_combo(spec, lambda: None)
        return manager

    def test_the_combo_key_is_registered_for_hiding(self):
        manager = self.make()
        # 0x55 — код клавиши U, 0x11 — Ctrl, 0x12 — Alt.
        assert manager._suppress_combos == [(0x55, (0x11, 0x12))]

    def test_the_key_is_hidden_while_the_modifiers_are_down(self, monkeypatch):
        manager = self.make()
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: True)
        assert manager._combo_key_held(0x55) is True

    def test_the_bare_letter_still_reaches_the_application(self, monkeypatch):
        """Самое важное здесь. Подавляя клавишу безусловно, как клавишу
        диктовки, мы отняли бы у человека саму букву: он перестал бы набирать
        «у» и «г» вообще."""
        manager = self.make()
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: False)
        assert manager._combo_key_held(0x55) is False

    def test_one_missing_modifier_is_enough_to_let_the_key_through(self, monkeypatch):
        manager = self.make()
        # Ctrl зажат, Alt нет — это не наше сочетание, а Ctrl+U приложения.
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: vk == 0x11)
        assert manager._combo_key_held(0x55) is False

    def test_another_key_is_never_touched(self, monkeypatch):
        manager = self.make()
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: True)
        assert manager._combo_key_held(0x41) is False  # «A»

    def test_every_default_combo_is_hidden(self):
        """Пропускать нельзя ни одно: Ctrl+Alt+V и Ctrl+Alt+H на русской
        раскладке дают «М» и «Р» и портят текст ровно так же."""
        manager = HotkeyManager(suppress=True)
        for spec in ("ctrl+alt+v", "ctrl+alt+h", "ctrl+alt+u", "ctrl+alt+l"):
            manager.register_combo(spec, lambda: None)
        assert len(manager._suppress_combos) == 4

    def test_suppression_can_be_turned_off(self):
        manager = self.make(suppress=False)
        assert manager._suppress_combos == []

    def test_a_single_key_combo_is_not_hidden(self):
        # Без модификаторов проверить нечего, и подавление съело бы букву.
        manager = self.make(spec="u")
        assert manager._suppress_combos == []

    def test_the_filter_is_installed_even_without_a_dictation_key(self):
        # Раньше фильтр ставился, только если пряталась клавиша диктовки.
        # У ctrl_r по умолчанию она не прячется — значит фильтра не было
        # вовсе, и ни одно сочетание не подавлялось.
        manager = HotkeyManager(suppress=True)
        manager.register_hold("ctrl_r", lambda: None, lambda: None)
        manager.register_combo("ctrl+alt+u", lambda: None)
        assert manager._suppress_vks == set(), "клавиша диктовки и не должна прятаться"
        assert manager._suppress_combos, "а сочетание — должно"


class TestHoldBehaviour:
    def make(self):
        events: list[str] = []
        manager = HotkeyManager(suppress=False)
        manager.register_hold(
            "ctrl_r", lambda: events.append("start"), lambda: events.append("stop")
        )
        return manager, events

    def test_press_and_release(self):
        manager, events = self.make()
        manager._on_press(keyboard.Key.ctrl_r)
        manager._on_release(keyboard.Key.ctrl_r)
        assert events == ["start", "stop"]

    def test_key_repeat_does_not_restart(self):
        # Удержание клавиши даёт поток повторных нажатий от Windows.
        manager, events = self.make()
        for _ in range(5):
            manager._on_press(keyboard.Key.ctrl_r)
        manager._on_release(keyboard.Key.ctrl_r)
        assert events == ["start", "stop"]

    def test_release_without_press_is_ignored(self):
        manager, events = self.make()
        manager._on_release(keyboard.Key.ctrl_r)
        assert events == []

    def test_other_keys_do_not_trigger(self):
        manager, events = self.make()
        manager._on_press(keyboard.Key.ctrl_l)
        manager._on_release(keyboard.Key.ctrl_l)
        assert events == []

    def test_left_and_right_are_distinct(self):
        manager = HotkeyManager(suppress=False)
        fired: list[str] = []
        manager.register_hold("ctrl_r", lambda: fired.append("r"), lambda: None)
        manager._on_press(keyboard.Key.ctrl_l)
        assert fired == []
        manager._on_press(keyboard.Key.ctrl_r)
        assert fired == ["r"]

    def test_combo_key_is_rejected_as_hold(self):
        manager = HotkeyManager(suppress=False)
        assert manager.register_hold("ctrl+shift", lambda: None, lambda: None) is False

    def test_empty_spec_is_rejected(self):
        manager = HotkeyManager(suppress=False)
        assert manager.register_hold("", lambda: None, lambda: None) is False


class TestKeyCodes:
    """Коды для вопроса Windows «зажата ли клавиша прямо сейчас»."""

    def test_sides_have_their_own_codes(self):
        # Правый и левый Ctrl различаются: иначе диктовка на ctrl_r
        # считалась бы живой, пока человек держит ctrl_l в чужом сочетании.
        assert vk_for("ctrl_r") == 0xA3
        assert vk_for("ctrl_l") == 0xA2

    def test_dedicated_key(self):
        assert vk_for("f13") == 0x7C

    def test_letter_code_does_not_depend_on_layout(self):
        assert vk_for("v") == 0x56

    def test_raw_vk_name(self):
        # key_names пишет код десятичным (vk86 — это физическая V, 0x56),
        # и разбирать его надо так же.
        assert vk_for("vk86") == 86
        assert vk_for("vk86") == vk_for("v")

    def test_unknown_name_gives_nothing(self):
        # «м» — символ русской раскладки, кода клавиши по нему не собрать.
        assert vk_for("м") is None

    def test_windows_answers_about_a_key_nobody_holds(self):
        # Заодно проверка, что ctypes-обвязка вообще жива: F24 во время
        # прогона тестов никто не держит.
        assert key_is_down(0x87) is False


class TestLostRelease:
    """Потерянное отпускание не должно делать программу глухой навсегда.

    KEYUP не доходит, если в момент отпускания сверху оказался экран
    блокировки или окно UAC. Раньше признак «удержание идёт» после этого
    оставался навсегда, и каждое следующее нажатие проглатывалось молча:
    ни реакции, ни строки в логе, ни записи в истории.
    """

    def make(self, **kwargs):
        events: list[str] = []
        manager = HotkeyManager(suppress=False, **kwargs)
        manager.register_hold(
            "ctrl_r", lambda: events.append("start"), lambda: events.append("stop")
        )
        return manager, events, manager._holds[0]

    def test_next_press_works_after_lost_release(self, caplog):
        manager, events, hold = self.make()
        manager._on_press(keyboard.Key.ctrl_r)
        # Отпускание не дошло: _on_release никто не вызвал. Пауза, в которую
        # не влезает ни один автоповтор Windows (те идут не реже раза в
        # секунду), — значит, это нажатие уже новое.
        hold.last_press_at -= 5.0
        with caplog.at_level(logging.WARNING, logger="whisperfree.hotkey"):
            manager._on_press(keyboard.Key.ctrl_r)
        assert events == ["start", "stop", "start"]
        assert "потерянное отпускание" in caplog.text

    def test_three_presses_in_a_row_are_not_swallowed(self):
        # Разбор ловил ровно это: нажатие, потерянное отпускание, три нажатия
        # подряд — и обработчик вызван один-единственный раз.
        manager, events, hold = self.make()
        manager._on_press(keyboard.Key.ctrl_r)
        for _ in range(3):
            hold.last_press_at -= 5.0
            manager._on_press(keyboard.Key.ctrl_r)
        assert events.count("start") == 4
        assert manager.lost_releases == 3

    def test_recovered_hold_still_stops_on_release(self):
        manager, events, hold = self.make()
        manager._on_press(keyboard.Key.ctrl_r)
        hold.last_press_at -= 5.0
        manager._on_press(keyboard.Key.ctrl_r)
        manager._on_release(keyboard.Key.ctrl_r)
        assert events == ["start", "stop", "start", "stop"]

    def test_another_key_asks_windows_about_the_stuck_one(self, monkeypatch, caplog):
        manager, events, _ = self.make()
        manager._on_press(keyboard.Key.ctrl_r)
        # Отпускание потерялось, но Windows знает правду: клавиша не нажата.
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: False)
        with caplog.at_level(logging.WARNING, logger="whisperfree.hotkey"):
            manager._on_press(keyboard.Key.f1)
        assert events == ["start", "stop"]
        assert "потерянное отпускание" in caplog.text
        manager._on_press(keyboard.Key.ctrl_r)
        assert events == ["start", "stop", "start"]

    def test_watchdog_frees_stuck_hold_without_any_keypress(self, monkeypatch, caplog):
        # Главный случай: человек больше ничего не нажимает, и снять
        # застрявшее удержание некому, кроме сторожевого потока.
        manager, events, _ = self.make()
        manager._on_press(keyboard.Key.ctrl_r)
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: False)
        with caplog.at_level(logging.WARNING, logger="whisperfree.hotkey"):
            manager._sweep_stale()
        assert events == ["start", "stop"]
        assert manager.lost_releases == 1
        assert "потерянное отпускание" in caplog.text

    def test_watchdog_leaves_a_live_hold_alone(self, monkeypatch):
        # Клавиша действительно зажата — прерывать диктовку нельзя.
        manager, events, _ = self.make()
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: True)
        manager._on_press(keyboard.Key.ctrl_r)
        manager._sweep_stale()
        manager._sweep_stale()
        assert events == ["start"]
        manager._on_release(keyboard.Key.ctrl_r)
        assert events == ["start", "stop"]

    def test_hold_longer_than_the_limit_is_dropped(self, monkeypatch, caplog):
        # Предел взят из [audio].max_seconds: дольше него запись всё равно
        # режется, значит и удержание дольше него — застрявшее. Windows тут
        # отвечает «зажата», и снять удержание может только предел.
        manager, events, hold = self.make(max_hold_seconds=300.0)
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: True)
        manager._on_press(keyboard.Key.ctrl_r)
        hold.started_at -= 301.0
        with caplog.at_level(logging.WARNING, logger="whisperfree.hotkey"):
            manager._sweep_stale()
        assert events == ["start", "stop"]
        assert "потерянное отпускание" in caplog.text

    def test_stuck_key_does_not_stay_among_pressed(self, monkeypatch):
        # Иначе имя застревает в _pressed навсегда и сочетание ctrl+alt+v
        # начинает срабатывать от одного alt+v.
        manager, _, _ = self.make()
        manager._on_press(keyboard.Key.ctrl_r)
        assert "ctrl_r" in manager._pressed
        monkeypatch.setattr(hotkey_mod, "key_is_down", lambda vk: False)
        manager._sweep_stale()
        assert "ctrl_r" not in manager._pressed
        assert "ctrl" not in manager._pressed

    def test_repeat_does_not_ask_windows(self, monkeypatch):
        """Горячий путь автоповтора обходится без системных вызовов.

        Обработчик нажатия выполняется в потоке низкоуровневого хука, и на
        зажатой клавише он вызывается десятки раз в секунду. GetAsyncKeyState
        стоит 0.003 мс, но на этом пути он ещё и бесполезен: внутри KEYDOWN
        Windows всегда отвечает «нажата».
        """
        asked: list[int] = []
        monkeypatch.setattr(
            hotkey_mod, "key_is_down", lambda vk: asked.append(vk) or True
        )
        manager, events, _ = self.make()
        for _ in range(5):
            manager._on_press(keyboard.Key.ctrl_r)
        manager._on_release(keyboard.Key.ctrl_r)
        assert events == ["start", "stop"]
        assert asked == []
