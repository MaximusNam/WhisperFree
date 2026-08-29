"""Чистые функции вставки: разбор сочетаний и выбор клавиши по приложению.

Сам SendInput здесь не дёргается — это проверяется руками в живых окнах.
"""

from __future__ import annotations

import pytest

from whisperfree.inject import (
    VK_CONTROL,
    VK_RETURN,
    VK_SHIFT,
    _utf16_units,
    parse_combo,
    paste_key_for,
)


class TestParseCombo:
    def test_simple_combo(self):
        assert parse_combo("ctrl+v") == ([VK_CONTROL], 0x56)

    def test_two_modifiers(self):
        assert parse_combo("ctrl+shift+v") == ([VK_CONTROL, VK_SHIFT], 0x56)

    def test_vk_codes_are_layout_independent(self):
        # На русской раскладке физическая V даёт «м», но код клавиши тот же,
        # поэтому Ctrl+V продолжает работать.
        assert parse_combo("ctrl+v")[1] == ord("V")

    def test_function_keys(self):
        assert parse_combo("f13")[1] == 0x7C
        assert parse_combo("ctrl+f1") == ([VK_CONTROL], 0x70)

    def test_named_keys(self):
        assert parse_combo("enter")[1] == VK_RETURN

    def test_alt_and_menu_are_the_same(self):
        assert parse_combo("alt+v") == parse_combo("menu+v")

    def test_case_and_spaces_are_forgiving(self):
        assert parse_combo("  Ctrl + Shift + V ") == ([VK_CONTROL, VK_SHIFT], 0x56)

    @pytest.mark.parametrize("spec", ["", "   ", "ctrl+щщщ", "нечто+v", "ctrl+f99"])
    def test_garbage_returns_none_instead_of_raising(self, spec):
        assert parse_combo(spec) is None


class TestPasteKeyForApp:
    def test_default_for_ordinary_app(self):
        assert paste_key_for("notepad.exe", "ctrl+v", {}) == "ctrl+v"

    def test_terminal_override(self):
        overrides = {"WindowsTerminal.exe": "ctrl+shift+v"}
        assert paste_key_for("WindowsTerminal.exe", "ctrl+v", overrides) == "ctrl+shift+v"

    def test_match_is_case_insensitive(self):
        overrides = {"WindowsTerminal.exe": "ctrl+shift+v"}
        assert paste_key_for("windowsterminal.exe", "ctrl+v", overrides) == "ctrl+shift+v"

    def test_unknown_window_gets_the_default(self):
        assert paste_key_for("", "ctrl+v", {"wt.exe": "ctrl+shift+v"}) == "ctrl+v"


class TestClipboardRoundTrip:
    """Работает с настоящим буфером обмена Windows, поэтому аккуратно
    сохраняет и возвращает то, что там лежало."""

    @pytest.fixture(autouse=True)
    def preserve_clipboard(self):
        from whisperfree.inject import get_clipboard_text, set_clipboard_text

        before = get_clipboard_text()
        yield
        if before is not None:
            set_clipboard_text(before)

    @pytest.mark.parametrize(
        "text",
        [
            "проверь через Gemini и поставь Docker",
            "многострочный\nтекст",
            "кавычки «ёлочки» и тире —",
            pytest.param("эмодзи 😀 вне BMP", id="non-bmp"),
            "",
        ],
    )
    def test_text_survives_the_round_trip(self, text):
        from whisperfree.inject import get_clipboard_text, set_clipboard_text

        assert set_clipboard_text(text)
        assert get_clipboard_text() == text

    def test_surrogate_pair_is_not_truncated(self):
        # Размер буфера считался в кодовых точках, а не в единицах UTF-16,
        # и эмодзи приезжал обрезанным до одной суррогатной половины.
        from whisperfree.inject import get_clipboard_text, set_clipboard_text

        set_clipboard_text("😀")
        assert get_clipboard_text() == "😀"


class TestUtf16:
    def test_bmp_character_is_one_unit(self):
        assert _utf16_units("я") == [0x044F]

    def test_emoji_becomes_a_surrogate_pair(self):
        units = _utf16_units("😀")
        assert len(units) == 2
        assert 0xD800 <= units[0] <= 0xDBFF
        assert 0xDC00 <= units[1] <= 0xDFFF
