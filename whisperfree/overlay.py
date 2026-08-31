"""Индикатор состояния поверх всех окон.

Показывает, что идёт запись, что запрос ушёл к провайдеру и — главное — когда
что-то пошло не так. Молчаливая потеря продиктованного абзаца хуже любой
ошибки на экране, поэтому провал вставки виден всегда.

Окно живёт всё время работы приложения и прячется уводом за край экрана:
withdraw/deiconify на Windows умеет перехватывать фокус, а забирать фокус у
того окна, куда мы собираемся вставлять текст, категорически нельзя.

Во время записи плашка показывает не только «идёт запись», но и живой уровень
звука: статичная надпись врёт одинаково и когда микрофон слышит, и когда он
молчит, а человек узнаёт правду только на отпускании клавиши — когда абзац уже
проговорён впустую. Полоска уровня и отдельное состояние «микрофон молчит»
превращают эту потерю в вопрос «почему не шевелится?» на середине фразы.

Плашка одна на все диктовки подряд, поэтому у показа есть поколение:
begin_session() открывает новое, а сообщение, прилетевшее из рабочего потока с
номером прошлого, отбрасывается. Иначе «Готово» или ошибка от предыдущей
диктовки затирает начало следующей, и снаружи это выглядит ровно как «нажал, и
ничего не загорелось». Номер едет вместе с сообщением по очереди и сверяется
ещё раз в потоке Tk, прямо перед сменой плашки: диктовка начинается асинхронно
и успевает вклиниться между проверкой у отправителя и показом.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk

log = logging.getLogger(__name__)

_WIDTH = 260
_HEIGHT = 44
_HIDDEN_Y = -400

# Фон плашки. Вынесен в константу не для красоты: относительно него считается
# контраст каждого цвета состояния, и тест проверяет ровно этот фон.
_BG = "#1c1c1f"

# Полоска уровня во всю ширину плашки по нижнему краю: заметно краем глаза,
# но не тянет взгляд с текста, куда человек диктует.
_BAR_HEIGHT = 4
_BAR_BG = "#2a2a2f"

# Цвет — единственное, что читается боковым зрением; подпись человек в этот
# момент не читает, он смотрит туда, куда диктует. Поэтому у каждого состояния
# свой цвет, и близких пар быть не должно: «Запись…» и «Ошибка» сначала были
# одинаково красными и различались только буквами, потом розовым против
# красного — а это по ΔE2000 всего 22.9, краем глаза по-прежнему одно и то же.
#
# Палитра подобрана перебором, а не на вкус: максимум минимальной попарной
# ΔE2000 при закреплённых смыслах (ошибка красная, «Готово» зелёное, «микрофон
# молчит» синий — на эти три ссылаются комментарии здесь и в __main__.py) и
# контрасте к фону плашки не ниже 4.5:1. Самая похожая пара разнесена на 39.0
# (было 19.4), «Запись…» и «Ошибка» — на 39.2, а для дихроматов минимум
# поднялся с 7.1 до 12.0. Все эти числа считает и стережёт tests/test_ui.py:
# поправить цвет «чтобы красивее» без прогона тестов теперь не выйдет.
_STYLES = {
    # Розово-пурпурный, а не красный: красный оставлен ошибке (и подсказке про
    # мёртвый микрофон, которая тоже красная), а спутать «идёт запись» с «всё
    # сломалось» нельзя.
    "recording": ("#f500cc", "Запись…"),
    # Синий не совпадает ни с записью, ни с ошибкой: смена цвета сама по себе
    # видна боковым зрением, читать подпись для этого не требуется.
    "silent": ("#1184ff", "Микрофон молчит"),
    "sending": ("#ffc000", "Распознаю…"),
    # Голубой, а не фиолетовый, как было: фиолетовый лежал между «записью» и
    # «микрофон молчит» и тянул минимум пары вниз.
    "refining": ("#45f4ff", "Правлю текст…"),
    "ok": ("#449640", "Готово"),
    "error": ("#ff360f", "Ошибка"),
}

# Состояния, при которых запись идёт и полоска уровня имеет смысл.
_LIVE_STATES = ("recording", "silent")


def _clamp01(value) -> float:
    """0..1 из чего угодно: мусор с чужого потока не должен ломать отрисовку."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    if number < 0.0:
        return 0.0
    if number > 1.0:  # сюда же попадает inf
        return 1.0
    return number


class Overlay:
    """Плашка состояния. Все публичные методы можно звать из любого потока."""

    def __init__(self, root: tk.Tk, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._window: tk.Toplevel | None = None
        self._dot: tk.Canvas | None = None
        self._label: tk.Label | None = None
        self._bar: tk.Canvas | None = None
        self._bar_item: int | None = None
        self._hide_job: str | None = None
        self._level = 0.0
        self._color = _STYLES["recording"][0]
        # Поколение показа: растёт на begin_session(), сравнивается в _stale().
        # Лок, а не голый +=, потому что номер берут из потока хука, а сверяют
        # из рабочего.
        self._session = 0
        self._session_lock = threading.Lock()
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

        frame = tk.Frame(win, bg=_BG, padx=12, pady=8)

        # Полоску пакуем ПЕРВОЙ: у растянутого frame она иначе отберёт место
        # снизу и уедет за границу окна.
        self._bar = tk.Canvas(
            win, width=_WIDTH, height=_BAR_HEIGHT, bg=_BAR_BG, highlightthickness=0
        )
        # Прямоугольник создаём один раз и потом только двигаем правый край:
        # уровень приходит каждые 80 мс, пересоздавать объекты Tk накладно.
        self._bar_item = self._bar.create_rectangle(
            0, 0, 0, _BAR_HEIGHT, fill=self._color, outline=""
        )
        self._bar.pack(side="bottom", fill="x")

        frame.pack(fill="both", expand=True)

        self._dot = tk.Canvas(
            frame, width=12, height=12, bg=_BG, highlightthickness=0
        )
        self._dot.create_oval(2, 2, 11, 11, fill=self._color, outline="")
        self._dot.pack(side="left", padx=(0, 10))

        self._label = tk.Label(
            frame,
            text="",
            bg=_BG,
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

    def begin_session(self) -> int:
        """Открыть новый показ и вернуть его номер.

        Зовётся на нажатии клавиши, то есть в потоке низкоуровневого хука:
        здесь только инкремент под коротким локом — ни Tk, ни лога, ни очереди.
        Номер отдаётся тому, кто потом отчитается о результате: ok() и error()
        с номером прошлой сессии будут отброшены.
        """
        with self._session_lock:
            self._session += 1
            return self._session

    def recording(self) -> None:
        self._push("recording", None, None)

    def silent(self) -> None:
        """Микрофон молчит, но запись идёт: подпись меняется, плашка остаётся.

        Решение «молчит уже долго» принимает приложение, оверлей только
        показывает. Авто-скрытия здесь быть не должно — клавишу всё ещё держат.
        """
        self._push("silent", None, None)

    def level(self, value: float) -> None:
        """Уровень 0..1: только привести число и положить в очередь.

        Единственный вызывающий — опрос записи в потоке Tk с шагом 80 мс
        (LEVEL_TICK_MS в __main__), он же и читает значение у рекордера;
        сам колбэк PortAudio сюда не ходит. Очередь всё равно нужна: метод
        обещан любому потоку, а виджеты трогает только _pump.
        """
        if not self.enabled:
            return
        self._queue.put(("level", _clamp01(value), None, None, None))

    def sending(self, session: int | None = None) -> None:
        self._push("sending", None, None, session)

    def refining(self, session: int | None = None) -> None:
        # Номер нужен по той же причине, что у ok и error: «Правлю текст…»
        # приходит из рабочего потока через секунду после отпускания и само
        # не гаснет. Без номера оно накрывало бы «Запись…» следующей диктовки
        # до самого её конца.
        self._push("refining", None, None, session)

    def ok(self, text: str = "", session: int | None = None) -> None:
        """Успех. session — номер из begin_session(), если он есть у вызывающего.

        Результат приходит из рабочего потока и легко опаздывает: без номера
        «Готово» от прошлой диктовки затирает уже зажжённую «Запись…».
        """
        if self._stale(session, "готово"):
            return
        preview = " ".join(text.split())
        if len(preview) > 34:
            preview = preview[:33] + "…"
        self._push("ok", preview or None, 1200, session)

    def error(self, message: str, session: int | None = None) -> None:
        """Ошибка. session — как у ok(): опоздавшая ошибка не гасит новую запись."""
        if self._stale(session, message):
            return
        log.warning("оверлей показывает ошибку: %s", message)
        self._push("error", message, 6000, session)

    def hide(self) -> None:
        self._push(None, None, None)

    def _stale(self, session: int | None, what: str) -> bool:
        """True, если сообщение относится к уже закрытой сессии показа.

        Зовётся дважды на одно сообщение, и это не дублирование, а разделение
        ролей. Проверка в ok()/error() — дешёвый отсев в рабочем потоке: она
        оставляет log.warning про ошибку там же, где он был, и не гоняет через
        очередь заведомо мёртвое. Решает же вторая, в _pump: между первой
        проверкой и _push успевает пройти и log.warning (синхронная запись на
        диск), и любой планировщик, так что человек за эту щель успевает нажать
        клавишу заново — а номер, уехавший в очередь вместе с сообщением,
        сверяется уже в потоке Tk ровно перед тем, как поменять плашку. Между
        той сверкой и показом не остаётся ничего.

        Альтернативу — держать _session_lock на всём отрезке «проверил и
        показал» — брать нельзя: показ живёт в потоке Tk, а номер берут в
        потоке хука клавиатуры, которому нельзя ждать вообще (Windows снимает
        медленный хук). Здесь же лок берётся только на чтение одного int.

        Без номера (session=None) сообщение проходит всегда: его шлёт код,
        которому поколение не нужно.
        """
        if session is None:
            return False
        with self._session_lock:
            current = self._session
        if session >= current:
            return False
        # Молча потерянное сообщение отладить нельзя: видно только, что плашка
        # «не то показывает». Поэтому каждый отброс оставляет след.
        log.debug(
            "оверлей отбросил опоздавшее сообщение сессии %d (идёт %d): %s",
            session,
            current,
            what,
        )
        return True

    def _push(self, state, message, auto_hide_ms, session=None) -> None:
        if not self.enabled:
            return
        self._queue.put(("state", state, message, auto_hide_ms, session))

    # --- насос очереди ---------------------------------------------------------

    def _pump(self) -> None:
        """Единственное место, где трогаются виджеты — поток Tk."""
        try:
            while True:
                kind, first, message, auto_hide_ms, session = self._queue.get_nowait()
                if kind == "level":
                    self._apply_level(first)
                # Поколение сверяется и здесь, а не только у отправителя: между
                # его проверкой и этой строкой человек успевает начать новую
                # диктовку. Подробности — в _stale().
                elif not self._stale(session, message or first):
                    self._apply(first, message, auto_hide_ms)
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
            self._level = 0.0
            self._draw_level()
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

        self._color = color
        if state not in _LIVE_STATES:
            # Записи нет — старый уровень на полоске врал бы про живой звук.
            self._level = 0.0
        self._draw_level()

        self._place(visible=True)
        self._window.lift()

        if auto_hide_ms:
            self._hide_job = self.root.after(auto_hide_ms, lambda: self._apply(None, None, None))

    def _apply_level(self, value: float) -> None:
        self._level = _clamp01(value)
        self._draw_level()

    def _draw_level(self) -> None:
        """Перерисовать полоску. Только поток Tk, только coords/itemconfigure."""
        if self._bar is None or self._bar_item is None:
            return
        self._bar.coords(self._bar_item, 0, 0, _WIDTH * self._level, _BAR_HEIGHT)
        self._bar.itemconfigure(self._bar_item, fill=self._color)
