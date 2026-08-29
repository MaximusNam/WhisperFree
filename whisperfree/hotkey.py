"""Глобальные горячие клавиши поверх низкоуровневого хука Windows.

Две вещи, из-за которых этот модуль сложнее, чем кажется:

1. Раскладка. На русской раскладке физическая клавиша V даёт символ «м», и
   сочетание вида ctrl+alt+v перестало бы работать, если сверять по символу.
   Поэтому клавиши опознаются ещё и по коду vk, который от раскладки не зависит.

2. Сон. Низкоуровневые хуки Windows молча отваливаются после сна или
   гибернации. Приложение при этом выглядит работающим, но не реагирует.
   Сторожевой поток ловит расхождение настенных и монотонных часов и
   пересобирает хук.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from pynput import keyboard

log = logging.getLogger(__name__)

# Виртуальные коды клавиш, которые можно подавлять как клавишу диктовки.
SUPPRESSIBLE_VK = {
    "caps_lock": 0x14,
    "scroll_lock": 0x91,
    "pause": 0x13,
    "insert": 0x2D,
    "apps": 0x5D,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 25)},
}

# Модификаторы подавлять нельзя, и это не придирка.
# Проглотив Ctrl, мы превратили бы Ctrl+C пользователя в обычную букву «c»,
# которая уедет прямо в текст. Не подавлять безопаснее: одиночный Ctrl,
# который видит приложение под курсором, ничего не делает.
MODIFIER_KEY_NAMES = {
    "ctrl", "ctrl_l", "ctrl_r",
    "shift", "shift_l", "shift_r",
    "alt", "alt_l", "alt_r", "alt_gr", "menu",
    "cmd", "cmd_l", "cmd_r", "win",
}

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105


def key_names(key) -> set[str]:
    """Все имена, под которыми можно узнать нажатую клавишу.

    Для Key.ctrl_l это {'ctrl_l', 'ctrl'}, для физической V на любой
    раскладке — {'v', 'vk86'} плюс символ текущей раскладки.
    """
    names: set[str] = set()
    if isinstance(key, keyboard.Key):
        name = key.name
        names.add(name)
        for suffix in ("_l", "_r", "_gr"):
            if name.endswith(suffix):
                names.add(name[: -len(suffix)])
                break
        if "alt" in names:
            names.add("menu")
        if "cmd" in names:
            names.add("win")
    else:
        char = getattr(key, "char", None)
        if char:
            names.add(char.lower())

    vk = getattr(key, "vk", None)
    if vk is not None:
        names.add(f"vk{vk}")
        # Коды латинских букв и цифр совпадают с ASCII и не зависят от раскладки.
        if 0x41 <= vk <= 0x5A:
            names.add(chr(vk).lower())
        elif 0x30 <= vk <= 0x39:
            names.add(chr(vk))
    return names


def parse_spec(spec: str) -> list[str] | None:
    """'ctrl+alt+v' -> ['ctrl', 'alt', 'v']. Пустая строка — выключено."""
    if not spec:
        return None
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    return parts or None


@dataclass
class _Hold:
    """Клавиша удержания: нажал — начали, отпустил — закончили."""

    names: list[str]
    on_press: Callable[[], None]
    on_release: Callable[[], None]
    active: bool = False


@dataclass
class _Combo:
    """Сочетание, срабатывающее один раз при нажатии."""

    names: list[str]
    callback: Callable[[], None]
    armed: bool = True


class HotkeyManager:
    """Слушает клавиатуру и раздаёт события."""

    def __init__(self, suppress: bool = True) -> None:
        self.suppress = suppress
        self._holds: list[_Hold] = []
        self._combos: list[_Combo] = []
        self._pressed: set[str] = set()
        self._lock = threading.RLock()
        self._listener: keyboard.Listener | None = None
        self._suppress_vks: set[int] = set()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    # --- регистрация -----------------------------------------------------------

    def register_hold(
        self, spec: str, on_press: Callable[[], None], on_release: Callable[[], None]
    ) -> bool:
        names = parse_spec(spec)
        if not names:
            return False
        if len(names) > 1:
            log.warning(
                "клавиша удержания %r должна быть одиночной, сочетания тут "
                "работают ненадёжно — пропускаю", spec
            )
            return False
        self._holds.append(_Hold(names=names, on_press=on_press, on_release=on_release))
        suppressed = False
        if self.suppress:
            name = names[0]
            if name in MODIFIER_KEY_NAMES:
                log.info(
                    "клавишу %s не подавляю: проглоченный модификатор превратил бы "
                    "Ctrl+C в букву «c» прямо в тексте", spec
                )
            else:
                vk = SUPPRESSIBLE_VK.get(name)
                if vk is not None:
                    self._suppress_vks.add(vk)
                    suppressed = True
        log.info("клавиша диктовки: %s%s", spec, " (подавляется)" if suppressed else "")
        return True

    def register_combo(self, spec: str, callback: Callable[[], None]) -> bool:
        names = parse_spec(spec)
        if not names:
            return False
        self._combos.append(_Combo(names=names, callback=callback))
        log.info("сочетание: %s", spec)
        return True

    # --- жизненный цикл --------------------------------------------------------

    def start(self) -> None:
        self._build_listener()
        self._stop.clear()
        self._watchdog = threading.Thread(target=self._watch, name="hotkey-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:  # pragma: no cover
                log.debug("ошибка при остановке слушателя: %s", exc)

    def _build_listener(self) -> None:
        kwargs = {"on_press": self._on_press, "on_release": self._on_release}
        if self.suppress and self._suppress_vks:
            kwargs["win32_event_filter"] = self._event_filter
        listener = keyboard.Listener(**kwargs)
        listener.daemon = True
        listener.start()
        self._listener = listener

    def _rebuild(self) -> None:
        log.warning("пересобираю хук клавиатуры")
        with self._lock:
            self._pressed.clear()
            for hold in self._holds:
                if hold.active:
                    hold.active = False
                    try:
                        hold.on_release()
                    except Exception:  # pragma: no cover
                        log.exception("ошибка в обработчике отпускания при пересборке")
        old, self._listener = self._listener, None
        if old is not None:
            try:
                old.stop()
            except Exception:  # pragma: no cover
                pass
        try:
            self._build_listener()
        except Exception:
            log.exception("не удалось пересобрать хук клавиатуры")

    def _watch(self) -> None:
        """Ловит сон системы и смерть слушателя."""
        offset = time.time() - time.monotonic()
        while not self._stop.wait(2.0):
            current = time.time() - time.monotonic()
            slept = abs(current - offset) > 2.0
            offset = current

            listener = self._listener
            dead = listener is None or not listener.running

            if slept:
                log.info("похоже, система просыпалась — обновляю хук")
            elif dead:
                log.warning("слушатель клавиатуры не работает")

            if slept or dead:
                self._rebuild()

    # --- подавление ------------------------------------------------------------

    def _event_filter(self, msg, data):
        """Не даёт клавише диктовки уйти в приложение под курсором."""
        try:
            if msg in (_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP):
                if data.vkCode in self._suppress_vks:
                    listener = self._listener
                    if listener is not None:
                        listener.suppress_event()
        except Exception:
            # Подавление — удобство, а не необходимость. Не работает — и ладно.
            pass
        return True

    # --- обработка -------------------------------------------------------------

    def _on_press(self, key) -> None:
        names = key_names(key)
        with self._lock:
            # Удержание клавиши даёт поток повторных нажатий — реагируем на первое.
            for hold in self._holds:
                if hold.names[0] in names and not hold.active:
                    hold.active = True
                    self._inline(hold.on_press)

            self._pressed |= names
            for combo in self._combos:
                if all(n in self._pressed for n in combo.names):
                    if combo.armed:
                        combo.armed = False
                        self._fire(combo.callback)
                else:
                    combo.armed = True

    def _on_release(self, key) -> None:
        names = key_names(key)
        with self._lock:
            for hold in self._holds:
                if hold.names[0] in names and hold.active:
                    hold.active = False
                    self._inline(hold.on_release)

            self._pressed -= names
            for combo in self._combos:
                if not all(n in self._pressed for n in combo.names):
                    combo.armed = True

    @staticmethod
    def _inline(callback: Callable[[], None]) -> None:
        """Начало и конец записи выполняются прямо в потоке хука.

        Так гарантируется порядок: «начали» не может обогнать «закончили».
        Обработчики обязаны быть мгновенными — вся долгая работа (сеть,
        распознавание) уходит в рабочий поток на стороне приложения. Медленный
        низкоуровневый хук сначала тормозит всю клавиатуру, а потом Windows
        молча его отключает.
        """
        try:
            callback()
        except Exception:
            log.exception("ошибка в обработчике клавиши удержания")

    @staticmethod
    def _fire(callback: Callable[[], None]) -> None:
        """Сочетания могут делать что-то долгое — уводим их в отдельный поток."""
        threading.Thread(target=_guard(callback), daemon=True).start()


def _guard(callback: Callable[[], None]) -> Callable[[], None]:
    def runner() -> None:
        try:
            callback()
        except Exception:
            log.exception("ошибка в обработчике горячей клавиши")

    return runner
