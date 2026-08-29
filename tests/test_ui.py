"""Дым-тесты интерфейса: виджеты строятся и переключают состояния без ошибок.

Как это выглядит на экране, проверяется глазами. Здесь ловится другое —
опечатки в Tk API и обращения к виджетам из чужого потока.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from whisperfree.config import HistoryConfig
from whisperfree.history import History, Record
from whisperfree.history_window import HistoryWindow
from whisperfree.overlay import Overlay


def pump(root: tk.Tk, times: int = 6) -> None:
    """Прокрутить цикл Tk, чтобы отложенные задания успели выполниться."""
    for _ in range(times):
        root.update()
        root.after(50, root.quit)
        root.mainloop()


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
