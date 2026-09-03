"""Индикатор состояния поверх всех окон.

Показывает, что идёт запись, что запрос ушёл к провайдеру и — главное — когда
что-то пошло не так. Молчаливая потеря продиктованного абзаца хуже любой
ошибки на экране, поэтому провал вставки виден всегда.

Плашка рисуется не виджетами Tk, а картинкой в слоёном окне Windows (см.
plate.py): в макете фон прозрачен на 87%, а текст поверх него непрозрачен,
и Tk такого не умеет — у него прозрачность либо на всё окно сразу, либо
по одному выбранному цвету.

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
import time
import tkinter as tk

from . import plate
from .theme import Theme, load_themes, pick

log = logging.getLogger(__name__)

# Куда уезжает спрятанная плашка. Не withdraw и не прозрачная картинка:
# уводом за край окно точно не мигнёт и точно не отберёт фокус.
_HIDDEN_Y = -400

# Отступ от нижнего края экрана при первом запуске, пока человек не перетащил
# плашку сам.
_BOTTOM_MARGIN = 90

# Состояния, при которых запись идёт и полоска уровня имеет смысл.
_LIVE_STATES = ("recording", "silent")

# Период расходящейся волны у точки, как в макете: ripple 1.6s.
_PULSE_S = 1.6

# Насколько часто разбирается очередь. Совпадает с шагом опроса уровня в
# __main__ (80 мс) с запасом: перерисовка плашки стоит около миллисекунды.
_PUMP_MS = 40


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

    def __init__(
        self,
        root: tk.Tk,
        enabled: bool = True,
        theme: Theme | None = None,
        position: tuple[int, int] | None = None,
        on_move=None,
    ) -> None:
        self.root = root
        self.enabled = enabled
        self.theme = theme or pick(load_themes(), None)
        # Кому сообщать о новом месте плашки. Нужен, чтобы приложение
        # запомнило его в конфиге: перетаскивать заново после каждого
        # запуска — издевательство.
        self.on_move = on_move

        self._queue: queue.Queue[tuple] = queue.Queue()
        self._window: tk.Toplevel | None = None
        self._hwnd: int | None = None
        self._layered = False
        self._hide_job: str | None = None

        self._state: str | None = None
        self._text = ""
        self._level = 0.0
        self._visible = False
        self._scale = 1.0

        self._x, self._y = 0, 0
        self._wanted = position
        self._drag_from: tuple[int, int] | None = None

        self._session = 0
        self._session_lock = threading.Lock()

        if enabled:
            self._build()
        self.root.after(_PUMP_MS, self._pump)

    # --- построение ------------------------------------------------------------

    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)

        # Масштаб экрана: при 125% плашка обязана вырасти вместе со всем
        # остальным, иначе на ноутбуке она превращается в марку.
        try:
            self._scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:  # pragma: no cover - зависит от Tk
            self._scale = 1.0

        width, height = self.size
        self._x, self._y = self._default_position()
        if self._wanted:
            self._x, self._y = self._clamp_to_screen(*self._wanted)
        win.geometry(f"{width}x{height}+{self._x}+{_HIDDEN_Y}")
        win.update_idletasks()

        self._window = win
        try:
            # Не winfo_id() напрямую: Tk отдаёт внутреннее окно, а слоёностью
            # управляет его родитель — см. plate.top_level().
            self._hwnd = plate.top_level(win.winfo_id())
            self._layered = plate.make_layered(self._hwnd)
        except Exception as exc:  # pragma: no cover - зависит от системы
            log.error("плашка не смогла стать слоёной (%s), рисовать нечем", exc)
            self._layered = False

        # Перетаскивание. Окно объявлено не активируемым (WS_EX_NOACTIVATE),
        # поэтому щелчок по нему не уводит фокус из документа, куда мы потом
        # вставляем текст.
        win.bind("<Button-1>", self._grab)
        win.bind("<B1-Motion>", self._drag)
        win.bind("<ButtonRelease-1>", self._drop)

    @property
    def size(self) -> tuple[int, int]:
        """Размер самой капсулы. По нему человек её и таскает."""
        geo = self.theme.geometry
        return (
            max(1, int(round(geo.width * self._scale))),
            max(1, int(round(geo.height * self._scale))),
        )

    @property
    def _pad(self) -> int:
        """Поле вокруг капсулы под тень.

        Окно больше плашки: иначе тень обрезалась бы по его краю и
        превращалась в тёмную полосу.
        """
        return plate.shadow_padding(self.theme, self._scale)

    def _phase(self) -> float:
        """Фаза волны у точки: растёт со временем, только пока идёт запись."""
        if self._state != "recording":
            return 0.0
        return (time.monotonic() % _PULSE_S) / _PULSE_S

    def _default_position(self) -> tuple[int, int]:
        width, height = self.size
        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
        except tk.TclError:  # pragma: no cover
            return (0, 0)
        return ((screen_w - width) // 2, screen_h - height - _BOTTOM_MARGIN)

    def _clamp_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Не даёт плашке уехать за край.

        Экран могли отключить или сменить разрешение, а запомненное место
        осталось от прошлой раскладки: плашка нашлась бы за границей и
        выглядела бы как «перестала показываться».
        """
        width, height = self.size
        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
        except tk.TclError:  # pragma: no cover
            return (x, y)
        x = max(0, min(int(x), max(0, screen_w - width)))
        y = max(0, min(int(y), max(0, screen_h - height)))
        return (x, y)

    # --- перетаскивание --------------------------------------------------------

    def _grab(self, event) -> None:
        self._drag_from = (event.x_root - self._x, event.y_root - self._y)

    def _drag(self, event) -> None:
        if self._drag_from is None:
            return
        dx, dy = self._drag_from
        self._x, self._y = self._clamp_to_screen(event.x_root - dx, event.y_root - dy)
        self._show()

    def _drop(self, event) -> None:
        if self._drag_from is None:
            return
        self._drag_from = None
        log.info("плашка перенесена в (%d, %d)", self._x, self._y)
        if self.on_move is not None:
            try:
                self.on_move(self._x, self._y)
            except Exception:  # pragma: no cover - сохранение не должно ронять UI
                log.exception("не удалось запомнить положение плашки")

    # --- публичный API ---------------------------------------------------------

    def set_theme(self, theme: Theme) -> None:
        """Сменить оформление. Можно из любого потока."""
        self._queue.put(("theme", theme, None, None, None))

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
        обещан любому потоку, а рисует только _pump.
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
        """Единственное место, где рисуется плашка — поток Tk."""
        try:
            while True:
                kind, first, message, auto_hide_ms, session = self._queue.get_nowait()
                if kind == "level":
                    self._apply_level(first)
                elif kind == "theme":
                    self._apply_theme(first)
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
        # Пульсация точки: пока идёт запись, плашку надо перерисовывать, иначе
        # волна замрёт. В остальных состояниях перерисовки нет вовсе — кадр
        # стоит около 4 мс, и жечь их без нужды незачем.
        if self._visible and self._state == "recording":
            try:
                self._show()
            except Exception:  # pragma: no cover
                log.exception("не удалось перерисовать плашку")

        try:
            self.root.after(_PUMP_MS, self._pump)
        except tk.TclError:
            return

    # --- отрисовка -------------------------------------------------------------

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
            self._state = None
            self._level = 0.0
            self._visible = False
            self._park()
            return

        look = self.theme.state(state)
        self._state = state
        self._text = message or look.label
        if state not in _LIVE_STATES:
            # Записи нет — старый уровень на полоске врал бы про живой звук.
            self._level = 0.0
        self._visible = True
        self._show()

        if auto_hide_ms:
            self._hide_job = self.root.after(
                auto_hide_ms, lambda: self._apply(None, None, None)
            )

    def _apply_level(self, value: float) -> None:
        self._level = _clamp01(value)
        if self._visible:
            self._show()

    def _apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        # Заготовки корпуса и точки посчитаны для прежней темы.
        plate.clear_cache()
        log.info("оформление плашки: %s", theme.name)
        # Размер у новой темы может отличаться, а место — остаться прежним.
        self._x, self._y = self._clamp_to_screen(self._x, self._y)
        if self._visible:
            self._show()
        else:
            self._park()

    def _show(self) -> None:
        """Нарисовать плашку и поставить её на место."""
        if self._window is None or self._state is None:
            return
        pad = self._pad
        width, height = plate.plate_size(self.theme, self._scale)
        x, y = self._x - pad, self._y - pad
        try:
            self._window.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            return

        if not self._layered or self._hwnd is None:
            return
        image = plate.render(
            self.theme, self._state, self._text, self._level, self._scale, self._phase()
        )
        plate.push(self._hwnd, image, x, y)

    def _park(self) -> None:
        """Увести плашку за край экрана, запомнив её место."""
        if self._window is None:
            return
        width, height = plate.plate_size(self.theme, self._scale)
        try:
            self._window.geometry(f"{width}x{height}+{self._x}+{_HIDDEN_Y}")
        except tk.TclError:
            return
        if self._layered and self._hwnd is not None and self._state is not None:
            image = plate.render(self.theme, self._state, self._text, 0.0, self._scale)
            plate.push(self._hwnd, image, self._x, _HIDDEN_Y)
