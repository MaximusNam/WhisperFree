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
    copy_key_for,
    copy_selection,
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


class FakeClipboard:
    """Буфер обмена и Ctrl+C без Windows.

    Ctrl+C изображается так же, как ведёт себя настоящий: он кладёт в буфер
    выделение, если оно есть, и не делает ничего, если его нет.
    """

    def __init__(self, initial=None, selection=None, copy_works=True):
        self.content = initial
        self.selection = selection
        self.copy_works = copy_works
        self.history = []

    def install(self, monkeypatch):
        from whisperfree import inject as inject_mod

        monkeypatch.setattr(inject_mod, "get_clipboard_text", lambda: self.content)
        monkeypatch.setattr(inject_mod, "set_clipboard_text", self._set)
        monkeypatch.setattr(inject_mod, "send_combo", self._copy)
        monkeypatch.setattr(inject_mod, "has_foreground_window", lambda: True)
        monkeypatch.setattr(
            inject_mod, "wait_modifiers_released", lambda timeout_ms=400: True
        )
        return self

    def _set(self, text):
        self.content = text
        self.history.append(text)
        return True

    def _copy(self, spec):
        assert spec == "ctrl+c"
        if not self.copy_works:
            return False
        if self.selection is not None:
            self.content = self.selection
        return True


class TestCopySelection:
    """Прочитать выделение можно только через буфер, и это надо делать честно."""

    def test_selection_is_returned(self, monkeypatch):
        clip = FakeClipboard(initial="было в буфере", selection="выделенное").install(
            monkeypatch
        )
        assert copy_selection() == "выделенное"

    def test_previous_clipboard_is_restored(self, monkeypatch):
        clip = FakeClipboard(initial="было в буфере", selection="выделенное").install(
            monkeypatch
        )
        copy_selection()
        assert clip.content == "было в буфере"

    def test_nothing_selected_is_not_mistaken_for_the_old_clipboard(self, monkeypatch):
        # Самый опасный случай: человек ничего не выделил. Без метки в буфере
        # мы прочитали бы прежнее его содержимое и стали бы учиться на тексте,
        # которого он не выделял.
        clip = FakeClipboard(initial="старый текст", selection=None).install(monkeypatch)
        assert copy_selection() is None
        assert clip.content == "старый текст"

    def test_selection_equal_to_the_clipboard_is_still_seen(self, monkeypatch):
        # Обратный случай: выделено ровно то, что уже лежало в буфере.
        # Метка отличается от обоих, поэтому смена видна.
        clip = FakeClipboard(initial="один и тот же", selection="один и тот же").install(
            monkeypatch
        )
        assert copy_selection() == "один и тот же"

    def test_probe_never_stays_in_the_clipboard(self, monkeypatch):
        clip = FakeClipboard(initial=None, selection=None).install(monkeypatch)
        copy_selection()
        assert clip.content == "", "в буфере осталась служебная метка"

    def test_failed_copy_restores_the_clipboard(self, monkeypatch):
        clip = FakeClipboard(
            initial="важное", selection="выделенное", copy_works=False
        ).install(monkeypatch)
        assert copy_selection() is None
        assert clip.content == "важное"

    def test_no_window_means_no_reading(self, monkeypatch):
        from whisperfree import inject as inject_mod

        clip = FakeClipboard(initial="важное", selection="выделенное").install(monkeypatch)
        monkeypatch.setattr(inject_mod, "has_foreground_window", lambda: False)
        assert copy_selection() is None
        assert clip.content == "важное", "буфер тронули, хотя окна нет"

    def test_the_combo_is_passed_through(self, monkeypatch):
        from whisperfree import inject as inject_mod

        sent = []
        clip = FakeClipboard(initial=None, selection="текст").install(monkeypatch)
        monkeypatch.setattr(
            inject_mod,
            "send_combo",
            lambda spec: sent.append(spec) or clip._set(clip.selection) or True,
        )
        copy_selection(combo="ctrl+shift+c")
        assert sent == ["ctrl+shift+c"]

    def test_modifiers_are_awaited_before_sending_ctrl_c(self, monkeypatch):
        # Хоткей нажимают с зажатыми Ctrl+Alt, и Ctrl+C поверх них стал бы
        # Ctrl+Alt+C — сочетанием, которое чужое окно поймёт как угодно.
        from whisperfree import inject as inject_mod

        order = []
        clip = FakeClipboard(initial=None, selection="текст").install(monkeypatch)
        monkeypatch.setattr(
            inject_mod,
            "wait_modifiers_released",
            lambda timeout_ms=400: order.append("wait") or True,
        )
        real_copy = clip._copy
        monkeypatch.setattr(
            inject_mod, "send_combo", lambda spec: order.append("copy") or real_copy(spec)
        )
        copy_selection()
        assert order == ["wait", "copy"]


class TestCopyKeyForApp:
    """В терминале Ctrl+C прерывает программу, а не копирует."""

    TERMINALS = {
        "WindowsTerminal.exe": "ctrl+shift+v",
        "wt.exe": "ctrl+shift+v",
        "mintty.exe": "ctrl+shift+v",
    }

    def test_ordinary_app_gets_plain_ctrl_c(self):
        assert copy_key_for("notepad.exe", "ctrl+v", self.TERMINALS) == "ctrl+c"

    @pytest.mark.parametrize("exe", ["WindowsTerminal.exe", "wt.exe", "mintty.exe"])
    def test_terminal_gets_ctrl_shift_c(self, exe):
        # Иначе выделенный текст не скопируется, зато у человека умрёт
        # запущенная в терминале программа.
        assert copy_key_for(exe, "ctrl+v", self.TERMINALS) == "ctrl+shift+c"

    def test_match_is_case_insensitive(self):
        assert copy_key_for("windowsterminal.exe", "ctrl+v", self.TERMINALS) == (
            "ctrl+shift+c"
        )

    def test_key_is_derived_from_whatever_the_user_configured(self):
        # Таблица одна: добавив своё приложение в paste_overrides, человек
        # получает верную клавишу копирования без второй настройки.
        overrides = {"myeditor.exe": "ctrl+alt+shift+v"}
        assert copy_key_for("myeditor.exe", "ctrl+v", overrides) == "ctrl+alt+shift+c"

    def test_empty_default_falls_back_to_ctrl_c(self):
        assert copy_key_for("notepad.exe", "", {}) == "ctrl+c"
