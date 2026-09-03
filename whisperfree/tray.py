"""Значок в системном трее.

Отсюда доступны пауза, последние десять расшифровок, окно истории и счётчик
расходов — чтобы было видно, во сколько на самом деле обходится замена
подписки на свой API-ключ.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from . import autostart
from .history import History, Record

log = logging.getLogger(__name__)


def _make_icon(color: str = "#4c7dfd") -> Image.Image:
    """Простой микрофон, нарисованный на месте — чтобы не тащить файл ресурса."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, 62, 62), fill="#1c1c1f")
    draw.rounded_rectangle((25, 14, 39, 38), radius=7, fill=color)
    draw.arc((18, 26, 46, 48), start=0, end=180, fill=color, width=4)
    draw.line((32, 46, 32, 52), fill=color, width=4)
    draw.line((24, 52, 40, 52), fill=color, width=4)
    return image


def _open_in_explorer(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            os.startfile(str(path))  # noqa: S606 - штатный способ на Windows
        else:
            subprocess.Popen(["explorer", "/select,", str(path)])
    except Exception as exc:
        log.warning("не удалось открыть %s: %s", path, exc)


class Tray:
    """Обёртка над pystray с перестроением меню по требованию."""

    def __init__(
        self,
        history: History,
        provider_cfg,
        refine_cfg,
        on_toggle_pause: Callable[[], None],
        is_paused: Callable[[], bool],
        on_paste_record: Callable[[Record], None],
        on_open_history: Callable[[], None],
        on_quit: Callable[[], None],
        config_path: Path,
        log_path: Path,
        themes: dict | None = None,
        current_theme: Callable[[], str] | None = None,
        on_theme: Callable[[str], None] | None = None,
    ) -> None:
        self.history = history
        self.provider_cfg = provider_cfg
        self.refine_cfg = refine_cfg
        self.on_toggle_pause = on_toggle_pause
        self.is_paused = is_paused
        self.on_paste_record = on_paste_record
        self.on_open_history = on_open_history
        self.on_quit = on_quit
        self.config_path = config_path
        self.log_path = log_path
        self.themes = themes or {}
        self.current_theme = current_theme or (lambda: "")
        self.on_theme = on_theme
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    # --- меню ------------------------------------------------------------------

    def _usage(self) -> dict:
        return self.history.usage(
            self.provider_cfg.price_per_hour_usd,
            self.provider_cfg.min_billed_seconds,
            self.refine_cfg.price_in_per_mtok,
            self.refine_cfg.price_out_per_mtok,
        )

    def _usage_text(self) -> str:
        stats = self._usage()
        return (
            f"Сегодня: {int(stats['today_count'])} шт, ${stats['today_usd']:.3f}   "
            f"Месяц: {int(stats['month_count'])} шт, ${stats['month_usd']:.3f}"
        )

    def _breakdown_text(self) -> str:
        """Из чего складывается сумма: правка стоит примерно как распознавание."""
        stats = self._usage()
        return (
            f"   за месяц: распознавание ${stats['month_stt_usd']:.3f}, "
            f"правка ${stats['month_refine_usd']:.3f}"
        )

    def _history_items(self) -> list[pystray.MenuItem]:
        records = self.history.recent(10)
        if not records:
            return [pystray.MenuItem("(пусто)", None, enabled=False)]
        items = []
        for record in records:
            items.append(
                pystray.MenuItem(
                    record.label(),
                    self._paste_action(record),
                )
            )
        return items

    def _paste_action(self, record: Record) -> Callable[[], None]:
        def action(_icon=None, _item=None) -> None:
            self.on_paste_record(record)

        return action

    def _theme_items(self) -> list[pystray.MenuItem]:
        """Пункты выбора оформления плашки.

        Радиокнопками, а не галочками: оформление всегда ровно одно, и
        галочки рядом со всеми пунктами обещали бы возможность включить два.
        """
        if not self.themes or self.on_theme is None:
            return [pystray.MenuItem("(нет тем)", None, enabled=False)]
        items = []
        for theme_id, theme in self.themes.items():
            items.append(
                pystray.MenuItem(
                    theme.name,
                    self._theme_action(theme_id),
                    checked=self._theme_checked(theme_id),
                    radio=True,
                )
            )
        return items

    def _theme_action(self, theme_id: str) -> Callable[..., None]:
        def action(_icon=None, _item=None) -> None:
            if self.on_theme is not None:
                self.on_theme(theme_id)

        return action

    def _theme_checked(self, theme_id: str) -> Callable[..., bool]:
        return lambda _item: self.current_theme() == theme_id

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda _item: "Пауза" if not self.is_paused() else "Возобновить",
                lambda _icon, _item: self.on_toggle_pause(),
                checked=lambda _item: self.is_paused(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Последние расшифровки", pystray.Menu(*self._history_items())),
            pystray.MenuItem("Окно истории…", lambda _icon, _item: self.on_open_history()),
            pystray.MenuItem("Вид плашки", pystray.Menu(*self._theme_items())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _item: self._usage_text(), None, enabled=False),
            pystray.MenuItem(lambda _item: self._breakdown_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Открыть конфиг", lambda _icon, _item: _open_in_explorer(self.config_path)
            ),
            pystray.MenuItem(
                "Открыть логи", lambda _icon, _item: _open_in_explorer(self.log_path)
            ),
            pystray.MenuItem(
                "Запускать при входе",
                lambda _icon, _item: autostart.set_enabled(not autostart.is_enabled()),
                checked=lambda _item: autostart.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda _icon, _item: self.on_quit()),
        )

    # --- жизненный цикл --------------------------------------------------------

    def start(self) -> None:
        self._icon = pystray.Icon(
            "whisperfree", _make_icon(), "WhisperFree", menu=self._build_menu()
        )
        # pystray на Windows поднимает собственный цикл сообщений в том потоке,
        # из которого вызван run(), поэтому фоновый поток его устраивает.
        self._thread = threading.Thread(target=self._icon.run, name="tray", daemon=True)
        self._thread.start()

    def refresh(self) -> None:
        """Перестроить меню — список последних расшифровок изменился."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.menu = self._build_menu()
            icon.update_menu()
        except Exception as exc:  # pragma: no cover
            log.debug("не удалось обновить меню трея: %s", exc)

    def set_state(self, active: bool) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = _make_icon("#e5484d" if active else "#4c7dfd")
        except Exception:  # pragma: no cover
            pass

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # pragma: no cover
                pass
