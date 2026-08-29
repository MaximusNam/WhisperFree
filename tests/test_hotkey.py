"""Горячие клавиши: раскладка, подавление и порядок событий."""

from __future__ import annotations

from pynput import keyboard

from whisperfree.hotkey import HotkeyManager, key_names, parse_spec


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
