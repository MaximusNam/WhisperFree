"""Глобальные горячие клавиши поверх низкоуровневого хука Windows.

Три вещи, из-за которых этот модуль сложнее, чем кажется:

1. Раскладка. На русской раскладке физическая клавиша V даёт символ «м», и
   сочетание вида ctrl+alt+v перестало бы работать, если сверять по символу.
   Поэтому клавиши опознаются ещё и по коду vk, который от раскладки не зависит.

2. Сон. Низкоуровневые хуки Windows молча отваливаются после сна или
   гибернации. Приложение при этом выглядит работающим, но не реагирует.
   Сторожевой поток ловит расхождение настенных и монотонных часов и
   пересобирает хук.

3. Потерянное отпускание. KEYUP не доходит, если в момент отпускания сверху
   оказался экран блокировки или окно UAC: событие уходит на защищённый
   рабочий стол мимо нас. Признак «удержание идёт» после этого остаётся
   навсегда, и каждое следующее нажатие проглатывается ещё до обработчика —
   программа выглядит глухой, и в логе об этом ни строки. Поэтому признаку
   не верят на слово: его сверяют с ответом Windows (GetAsyncKeyState) и со
   временем, а каждое снятие застрявшего удержания пишут в лог.
"""

from __future__ import annotations

import ctypes
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

# Коды клавиш для вопроса «зажата ли она прямо сейчас». Список шире
# SUPPRESSIBLE_VK не случайно: подавлять модификаторы нельзя, а спрашивать
# про них у Windows можно и нужно — клавиша диктовки по умолчанию ctrl_r.
HOLD_VK = {
    "ctrl": 0x11, "ctrl_l": 0xA2, "ctrl_r": 0xA3,
    "shift": 0x10, "shift_l": 0xA0, "shift_r": 0xA1,
    "alt": 0x12, "menu": 0x12, "alt_l": 0xA4, "alt_r": 0xA5, "alt_gr": 0xA5,
    "cmd": 0x5B, "win": 0x5B, "cmd_l": 0x5B, "cmd_r": 0x5C,
    **SUPPRESSIBLE_VK,
}

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105

# Дольше этого одно удержание жить не может: [audio].max_seconds всё равно
# режет запись (по умолчанию 300 с), так что признак, переживший предел, —
# заведомо мусор, а не зажатая клавиша.
MAX_HOLD_SECONDS = 300.0

# Пауза между нажатиями, после которой это уже не автоповтор, а новое нажатие.
# Windows повторяет зажатую клавишу не реже двух раз в секунду (500 мс) и ждёт
# до первого повтора не дольше секунды — полторы секунды не задевают ни одну
# штатную настройку клавиатуры.
REPEAT_GAP = 1.5

# Шаг сторожевого потока. Раньше он был 2 с и следил только за сном и смертью
# слушателя. Теперь он же снимает застрявшие удержания, а этого человек ждёт,
# нажимая клавишу и глядя на пустой экран, — полсекунды он не заметит.
WATCH_TICK = 0.5

# Расхождение настенных и монотонных часов, после которого считаем, что
# система спала. От шага не зависит: сон короче двух секунд нам не интересен.
SLEEP_DRIFT = 2.0

# --- физическое состояние клавиши --------------------------------------------

# GetAsyncKeyState отвечает про клавиатуру целиком и одинаково для любого
# потока — в отличие от GetKeyState, который смотрит в очередь вызывающего
# потока и в потоке хука рассказал бы про чужую очередь.
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    _user32.GetAsyncKeyState.restype = ctypes.c_short
except (AttributeError, OSError):  # pragma: no cover — не Windows
    _user32 = None

_KEY_DOWN = 0x8000


def key_is_down(vk: int) -> bool:
    """Зажата ли клавиша прямо сейчас, по мнению Windows.

    Цена вызова замерена здесь же, 200 000 вызовов подряд: 0.003 мс в среднем,
    медиана 0.0026 мс, 95-й перцентиль 0.0067 мс, худший 0.07 мс. Для
    сравнения, root.after из чужого потока стоит 0.07 мс на простаивающем Tk
    и до 48 мс на занятом, а Shell_NotifyIcon в трее — ещё дороже. То есть на
    фоне того, что обработчик нажатия делает и без нас, вызов не виден. В сам
    обработчик он к тому же попадает не на каждое событие: на горячем пути
    автоповторов его нет вовсе, см. _repeat_or_new.

    Замер того же обработчика нажатия целиком, вместе с этой проверкой:
    медиана 0.003 мс, 95-й перцентиль 0.008 мс. Порог, на котором Windows
    молча снимает низкоуровневый хук, — около 300 мс.

    Если спросить не у кого (не Windows), отвечаем «нажата»: выдумать
    отпускание, которого мы не видели, хуже, чем не заметить застрявшее.
    """
    if _user32 is None:  # pragma: no cover — не Windows
        return True
    return bool(_user32.GetAsyncKeyState(vk) & _KEY_DOWN)


def vk_for(name: str) -> int | None:
    """Код клавиши для GetAsyncKeyState. None — спросить про неё не выйдет."""
    vk = HOLD_VK.get(name)
    if vk is not None:
        return vk
    if name.startswith("vk") and name[2:].isdigit():
        return int(name[2:])
    if len(name) == 1:
        # Коды латинских букв и цифр совпадают с ASCII и не зависят от
        # раскладки — ровно как в key_names.
        upper = name.upper()
        if "A" <= upper <= "Z" or "0" <= upper <= "9":
            return ord(upper)
    return None


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
    """Клавиша удержания: нажал — начали, отпустил — закончили.

    Отпускание может не дойти, поэтому одного признака active мало: рядом
    лежит время, по которому видно, что удержание застряло, и код клавиши,
    по которому у Windows можно спросить, зажата ли она на самом деле.
    """

    names: list[str]
    on_press: Callable[[], None]
    on_release: Callable[[], None]
    spec: str = ""
    vk: int | None = None
    active: bool = False
    # Монотонные метки: когда удержание началось и когда по нему последний раз
    # приходило нажатие (пока клавишу держат, Windows шлёт автоповторы).
    started_at: float = 0.0
    last_press_at: float = 0.0
    # Имена, под которыми клавиша попала в _pressed. При снятии застрявшего
    # удержания их надо оттуда убрать: иначе имя останется там навсегда и
    # сочетание начнёт срабатывать без своего модификатора.
    pressed_names: set[str] = field(default_factory=set)


@dataclass
class _Combo:
    """Сочетание, срабатывающее один раз при нажатии."""

    names: list[str]
    callback: Callable[[], None]
    armed: bool = True


class HotkeyManager:
    """Слушает клавиатуру и раздаёт события."""

    def __init__(
        self, suppress: bool = True, max_hold_seconds: float = MAX_HOLD_SECONDS
    ) -> None:
        self.suppress = suppress
        # Предел живого удержания. Значение по умолчанию совпадает с
        # [audio].max_seconds: дольше него запись всё равно не живёт.
        self.max_hold_seconds = float(max_hold_seconds)
        # Сколько раз пришлось снимать застрявшее удержание. Число уходит в
        # лог: человек должен знать, что у него теряются отпускания, а не
        # думать, что программа глохнет сама по себе.
        self.lost_releases = 0
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
        vk = vk_for(names[0])
        self._holds.append(
            _Hold(
                names=names,
                on_press=on_press,
                on_release=on_release,
                spec=spec,
                vk=vk,
            )
        )
        if vk is None:
            log.info(
                "физическое состояние клавиши %s у Windows не спросить: "
                "застрявшее удержание снимется только по пределу в %.0f с",
                spec, self.max_hold_seconds,
            )
        suppressed = False
        if self.suppress:
            name = names[0]
            if name in MODIFIER_KEY_NAMES:
                log.info(
                    "клавишу %s не подавляю: проглоченный модификатор превратил бы "
                    "Ctrl+C в букву «c» прямо в тексте", spec
                )
            else:
                suppress_vk = SUPPRESSIBLE_VK.get(name)
                if suppress_vk is not None:
                    self._suppress_vks.add(suppress_vk)
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
                    log.warning(
                        "удержание %s не пережило пересборку хука — закрываю его",
                        hold.spec or hold.names[0],
                    )
                    self._deactivate(hold)
                    self._inline(hold.on_release)
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
        """Ловит сон системы, смерть слушателя и застрявшие удержания."""
        offset = time.time() - time.monotonic()
        while not self._stop.wait(WATCH_TICK):
            current = time.time() - time.monotonic()
            slept = abs(current - offset) > SLEEP_DRIFT
            offset = current

            listener = self._listener
            dead = listener is None or not listener.running

            if slept:
                log.info("похоже, система просыпалась — обновляю хук")
            elif dead:
                log.warning("слушатель клавиатуры не работает")

            if slept or dead:
                # Пересборка сама закрывает все активные удержания.
                self._rebuild()
            else:
                self._sweep_stale()

    def _sweep_stale(self) -> None:
        """Снимает удержания, у которых потерялось отпускание.

        Это главный путь восстановления. Экран блокировки съел KEYUP,
        пользователь больше ничего не нажимает — спросить некому, и без этой
        проверки программа осталась бы глухой навсегда, а запись шла бы до
        предела в 300 с. Проверка живёт в сторожевом потоке, а не в хуке, и
        стоит один GetAsyncKeyState на активное удержание раз в полсекунды.
        """
        if not any(hold.active for hold in self._holds):
            # Обычное состояние: держать нечего, замок и системный вызов ни к
            # чему.
            return
        now = time.monotonic()
        with self._lock:
            for hold in self._holds:
                if hold.active:
                    self._drop_if_abandoned(hold, now)

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
        now = time.monotonic()
        with self._lock:
            for hold in self._holds:
                if hold.names[0] in names:
                    self._press_hold(hold, names, now)
                elif hold.active:
                    # Нажали другую клавишу, а удержание всё ещё «идёт» —
                    # удобный повод спросить Windows, правда ли клавиша
                    # зажата. Один GetAsyncKeyState, и только пока идёт запись.
                    self._drop_if_abandoned(hold, now)

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
                    self._deactivate(hold)
                    self._inline(hold.on_release)

            self._pressed -= names
            for combo in self._combos:
                if not all(n in self._pressed for n in combo.names):
                    combo.armed = True

    # --- застрявшие удержания --------------------------------------------------

    def _press_hold(self, hold: _Hold, names: set[str], now: float) -> None:
        """Нажали клавишу удержания: это начало, автоповтор или новое начало?"""
        if hold.active:
            reason = self._repeat_or_new(hold, now)
            if reason is None:
                # Обычный автоповтор от зажатой клавиши — начинать нечего.
                hold.last_press_at = now
                return
            # Отпускание потерялось. Закрываем зависшее удержание и тут же
            # начинаем новое: для человека это ровно то нажатие, которое
            # раньше проглатывалось молча. Два обработчика подряд в потоке
            # хука — столько же работы, сколько в быстром «отпустил-нажал»,
            # то есть в бюджет хука мы укладываемся.
            self._release_stuck(hold, reason)

        hold.active = True
        hold.started_at = now
        hold.last_press_at = now
        hold.pressed_names = set(names)
        self._inline(hold.on_press)

    def _repeat_or_new(self, hold: _Hold, now: float) -> str | None:
        """Почему нажатие по активному удержанию — не автоповтор. None — повтор.

        Windows тут спрашивать бесполезно: внутри обработчика KEYDOWN она уже
        отвечает «нажата» (проверено на живом хуке), и автоповтор от нового
        нажатия так не отличить. Отличаем по времени — заодно на горячем пути
        автоповторов не остаётся ни одного системного вызова.
        """
        held = now - hold.started_at
        if held > self.max_hold_seconds:
            return (
                f"удержание идёт {held:.0f} с при пределе "
                f"{self.max_hold_seconds:.0f} с"
            )
        gap = now - hold.last_press_at
        if gap > REPEAT_GAP:
            return f"автоповторов не было {gap:.1f} с — клавишу успели отпустить"
        return None

    def _drop_if_abandoned(self, hold: _Hold, now: float) -> bool:
        """Снимает удержание, если признак врёт. Отвечает, снял ли."""
        held = now - hold.started_at
        if held > self.max_hold_seconds:
            reason = (
                f"удержание идёт {held:.0f} с при пределе "
                f"{self.max_hold_seconds:.0f} с"
            )
        elif hold.vk is not None and not key_is_down(hold.vk):
            reason = "Windows говорит, что клавиша не нажата"
        else:
            return False
        self._release_stuck(hold, reason)
        return True

    def _release_stuck(self, hold: _Hold, reason: str) -> None:
        """Снимает застрявшее удержание — обязательно со строкой в логе.

        Молча самоисцеляться нельзя: если отпускания теряются, человек должен
        об этом узнать, а не гадать, почему программа иногда глохнет.
        """
        self.lost_releases += 1
        log.warning(
            "потерянное отпускание %s: %s — снимаю зависшее удержание и "
            "продолжаю слушать (случай №%d за запуск)",
            hold.spec or hold.names[0], reason, self.lost_releases,
        )
        self._deactivate(hold)
        self._inline(hold.on_release)

    def _deactivate(self, hold: _Hold) -> None:
        """Гасит признак удержания и убирает клавишу из нажатых."""
        hold.active = False
        self._pressed -= hold.pressed_names
        hold.pressed_names = set()

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
