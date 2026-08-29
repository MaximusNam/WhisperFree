"""Индикатор состояния поверх всех окон.

Показывает, что идёт запись, что запрос ушёл к провайдеру и — главное — когда
что-то пошло не так. Молчаливая потеря продиктованного абзаца хуже любой
ошибки на экране, поэтому провал вставки виден всегда.

Окно живёт всё время работы приложения и прячется уводом за край экрана:
withdraw/deiconify на Windows умеет перехватывать фокус, а забирать фокус у
того окна, куда мы собираемся вставлять текст, категорически нельзя.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk

log = logging.getLogger(__name__)

_WIDTH = 260
_HEIGHT = 44
_HIDDEN_Y = -400

_STYLES = {
    "recording": ("#e5484d", "Запись…"),
    "sending": ("#f5a524", "Распознаю…"),
    "refining": ("#8e6fe0", "Правлю текст…"),
    "ok": ("#30a46c", "Готово"),
    "error": ("#e5484d", "Ошибка"),
}


class Overlay:
    """Плашка состояния. Все публичные методы можно звать из любого потока."""

    def __init__(self, root: tk.Tk, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._window: tk.Toplevel | None = None
        self._dot: tk.Canvas | None = None
        self._label: tk.Label | None = None
        self._hide_job: str | None = None
        if enabled:
            self._build()
        self.root.after(40, self._pump)

    # --- построение ------------------------------------------------------------

    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.94)
            win.attributes("-toolwindow", True)
            # Окно не принимает ввод и потому не может украсть фокус.
            win.attributes("-disabled", True)
        except tk.TclError:  # pragma: no cover - зависит от версии Tk
            pass

        frame = tk.Frame(win, bg="#1c1c1f", padx=12, pady=8)
        frame.pack(fill="both", expand=True)

        self._dot = tk.Canvas(
            frame, width=12, height=12, bg="#1c1c1f", highlightthickness=0
        )
        self._dot.create_oval(2, 2, 11, 11, fill="#e5484d", outline="")
        self._dot.pack(side="left", padx=(0, 10))

        self._label = tk.Label(
            frame,
            text="",
            bg="#1c1c1f",
            fg="#f2f2f3",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
        )
        self._label.pack(side="left", fill="x", expand=True)

        self._window = win
        self._place(visible=False)

    def _place(self, visible: bool) -> None:
        if self._window is None:
            return
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - _WIDTH) // 2
        y = screen_h - _HEIGHT - 90 if visible else _HIDDEN_Y
        self._window.geometry(f"{_WIDTH}x{_HEIGHT}+{x}+{y}")

    # --- публичный API ---------------------------------------------------------

    def recording(self) -> None:
        self._push("recording", None, None)

    def sending(self) -> None:
        self._push("sending", None, None)

    def refining(self) -> None:
        self._push("refining", None, None)

    def ok(self, text: str = "") -> None:
        preview = " ".join(text.split())
        if len(preview) > 34:
            preview = preview[:33] + "…"
        self._push("ok", preview or None, 1200)

    def error(self, message: str) -> None:
        log.warning("оверлей показывает ошибку: %s", message)
        self._push("error", message, 6000)

    def hide(self) -> None:
        self._push(None, None, None)

    def _push(self, state, message, auto_hide_ms) -> None:
        if not self.enabled:
            return
        self._queue.put((state, message, auto_hide_ms))

    # --- насос очереди ---------------------------------------------------------

    def _pump(self) -> None:
        """Единственное место, где трогаются виджеты — поток Tk."""
        try:
            while True:
                state, message, auto_hide_ms = self._queue.get_nowait()
                self._apply(state, message, auto_hide_ms)
        except queue.Empty:
            pass
        except tk.TclError:
            # Корень уничтожен — перепланировать себя больше некуда.
            return
        except Exception:  # pragma: no cover
            log.exception("ошибка в обновлении оверлея")
        try:
            self.root.after(40, self._pump)
        except tk.TclError:
            return

    def _apply(self, state, message, auto_hide_ms) -> None:
        if self._window is None:
            return
        if self._hide_job is not None:
            try:
                self.root.after_cancel(self._hide_job)
            except tk.TclError:
                pass
            self._hide_job = None

        if state is None:
            self._place(visible=False)
            return

        color, default_text = _STYLES.get(state, ("#8e8e93", ""))
        text = message or default_text
        if len(text) > 60:
            text = text[:59] + "…"

        if self._dot is not None:
            self._dot.delete("all")
            self._dot.create_oval(2, 2, 11, 11, fill=color, outline="")
        if self._label is not None:
            self._label.configure(text=text)

        self._place(visible=True)
        self._window.lift()

        if auto_hide_ms:
            self._hide_job = self.root.after(auto_hide_ms, lambda: self._apply(None, None, None))
