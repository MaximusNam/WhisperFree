"""Точка входа WhisperFree.

Раскладка по потокам:

* главный поток   — цикл Tk (оверлей и окно истории);
* поток pynput    — хук клавиатуры, обработчики удержания выполняются в нём
                    и обязаны быть мгновенными;
* рабочий поток   — кодирование, запрос к провайдеру, вставка;
* поток pystray   — значок в трее;
* поток PortAudio — колбэк микрофона.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

from . import audio as audio_mod
from . import autostart, config as config_mod, inject
from .history import AudioCache, History, Record
from .history_window import HistoryWindow
from .hotkey import HotkeyManager
from .logutil import Stopwatch, setup_logging
from .overlay import Overlay
from .postprocess import Postprocessor
from .providers import TranscriptionError, TranscriptionRequest, build_provider
from .limits import report as report_limits
from .refine import Refiner
from .singleton import SingleInstance
from .tray import Tray

log = logging.getLogger(__name__)

# Запасное значение порога тишины, если в конфиге его нет.
# Отправлять тишину бессмысленно и вредно: Whisper на ней уверенно выдумывает
# титры из роликов, на которых учился.
DEFAULT_SILENCE_PEAK = 0.02


@dataclass
class Job:
    """Одна диктовка, ждущая обработки.

    Непустой error означает, что обрабатывать нечего: диктовка провалилась
    ещё до отправки, и рабочему потоку остаётся только оставить след в
    истории. Почему через очередь, а не прямо на месте: провал замечают
    потоки хука клавиатуры, а запись в историю — это открыть файл, дописать
    строку и закрыть. Хук обязан вернуться мгновенно, иначе Windows сначала
    тормозит всю клавиатуру, а потом молча снимает хук.

    capture у провала может отсутствовать: нажатие на неготовый микрофон не
    даёт вообще ничего, даже пустой записи.

    pending — запись, за которую уже заплачено, но которая ещё не в журнале.
    Рабочий поток ловит любое исключение; без этой ссылки оплаченная диктовка
    пропала бы бесследно — в лог ушла бы только трассировка.

    session — поколение показа плашки, взятое на нажатии. Рабочий поток
    отчитывается о результате ИМЕННО этим номером, и оверлей молча выбрасывает
    отчёт, если человек уже начал следующую диктовку. Без номера (None)
    сообщение показывается всегда — так ведут себя задания, собранные не
    нажатием клавиши.
    """

    capture: audio_mod.Capture | None
    lang: str
    started_at: float
    error: str = ""
    session: int | None = None
    pending: Record | None = None


class App:
    def __init__(self, cfg: config_mod.Config, root: tk.Tk | None = None) -> None:
        self.cfg = cfg
        self.paused = False
        self._recording = False
        self._state_lock = threading.Lock()
        # Номер последней начатой диктовки — он же поколение показа плашки.
        # Пишется в потоке хука (нажатие) и в потоке Tk (возврат плашки после
        # чужого сообщения), читается ещё и рабочим потоком; чтение и запись
        # целого атомарны, замок ради них не нужен.
        self._session = 0
        # Кому плашка отдана последним: номер, выданный самым свежим
        # _take_plate(). Пока он совпадает с _session, плашка принадлежит
        # диктовке; разошлись — значит её забрала чужая операция (повторная
        # вставка, действие из окна истории, переоткрытие микрофона), и во
        # время записи опрос вернёт «Запись…» на место — см. _poll_once.
        self._plate_session = 0
        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._stopping = threading.Event()

        self.history = History(config_mod.history_path(), cfg.history)
        self.audio_cache = AudioCache(
            config_mod.audio_cache_dir(),
            cfg.history.keep_audio_count if cfg.history.keep_audio else 0,
        )
        self.postprocessor = Postprocessor(cfg.postprocess)
        self.refiner = Refiner(cfg.refine, cfg.provider.base_url, cfg.provider.api_key)
        self.injector = inject.Injector(cfg.inject)
        self.provider = build_provider(cfg.provider)
        self.recorder = audio_mod.Recorder(
            sample_rate=cfg.audio.sample_rate,
            preroll_ms=cfg.audio.preroll_ms,
            device=cfg.audio.device,
            max_seconds=cfg.audio.max_seconds,
            hold_open=cfg.audio.hold_open,
        )
        self.hotkeys = HotkeyManager(suppress=cfg.hotkeys.suppress)

        # Корень можно передать снаружи: Tk плохо переносит несколько корней в
        # одном процессе, и тестам нужен общий.
        self.root = root or tk.Tk()
        self.root.withdraw()
        self.overlay = Overlay(self.root, enabled=cfg.ui.overlay)
        self.history_window = HistoryWindow(
            self.root, self.history, self._paste_record, self._copy_record
        )
        self.tray: Tray | None = None
        self._worker: threading.Thread | None = None

        # Когда поток микрофона открывали в последний раз. Свежий поток
        # честно отвечает is_alive=False, пока не придёт первый блок, и
        # ругаться на это нельзя — устройство просыпается десятки миллисекунд.
        self._mic_opened_at = 0.0
        # Хозяйство повторяющегося опроса записи. Трогается только в потоке Tk,
        # заводится и останавливается тоже там — см. раздел «опрос записи».
        self._poll_job: str | None = None
        self._polled_recording = False
        self._recording_since = 0.0
        self._hint = ""
        self._hint_at = 0.0

    # --- запуск и остановка ----------------------------------------------------

    def run(self) -> int:
        # Первой же строкой пишем, что программа реально собирается делать.
        # Вопрос «почему берётся не тот микрофон» должен решаться чтением лога,
        # а не расследованием.
        log.info("настройки: %s", config_mod.describe(self.cfg))
        try:
            # Микрофон открываем и при hold_open=false тоже: неверное имя
            # устройства должно всплыть сразу, а не при первой диктовке.
            self.recorder.open()
            self._mic_opened_at = time.monotonic()
            if not self.cfg.audio.hold_open:
                self.recorder.close()
                log.info("микрофон освобождён до первой диктовки (hold_open=false)")
        except audio_mod.AudioError as exc:
            log.error("%s", exc)
            self.overlay.error(str(exc))

        self._worker = threading.Thread(target=self._work, name="transcribe", daemon=True)
        self._worker.start()

        self._register_hotkeys()
        self.hotkeys.start()

        if self.cfg.ui.tray:
            self.tray = Tray(
                history=self.history,
                provider_cfg=self.cfg.provider,
                refine_cfg=self.cfg.refine,
                on_toggle_pause=self.toggle_pause,
                is_paused=lambda: self.paused,
                on_paste_record=self._paste_record,
                on_open_history=self.history_window.open,
                on_quit=self.quit,
                config_path=config_mod.config_path(),
                log_path=config_mod.log_path(),
            )
            self.tray.start()

        if self.cfg.ui.autostart and not autostart.is_enabled():
            autostart.enable()

        log.info(
            "WhisperFree готов. Диктовка: %s (%s), альтернативный язык: %s (%s), "
            "повторная вставка: %s",
            self.cfg.hotkeys.dictate,
            self.cfg.language.main,
            self.cfg.hotkeys.dictate_alt or "выключено",
            self.cfg.language.alt,
            self.cfg.hotkeys.paste_last,
        )
        if not self.cfg.provider.api_key:
            message = (
                f"нет ключа {self.cfg.provider.api_key_env} — "
                "положите его в .env рядом с программой"
            )
            log.error("%s", message)
            self.overlay.error(message)

        # Опрос записи заводится ЗДЕСЬ, в потоке Tk, и крутится до конца
        # работы. Заводить его с нажатия клавиши нельзя: см. раздел «опрос
        # записи» — межпоточный root.after блокирует поток хука.
        self._start_polling()

        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    def quit(self) -> None:
        """Можно звать из любого потока."""
        self.root.after(0, self.root.quit)

    def _shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        log.info("завершаю работу")
        self.hotkeys.stop()
        # Прибираем за собой: признак записи снимаем сами, а не надеемся, что
        # его снимет отпускание клавиши, — хук уже остановлен, и отпускания не
        # будет. Оставленный признак заставил бы опрос считать, что запись всё
        # ещё идёт, а через него — трогать оверлей и значок на умирающем Tk.
        with self._state_lock:
            self._recording = False
        self._polled_recording = False
        self._stop_polling()
        self._jobs.put(None)
        if self.tray is not None:
            self.tray.stop()
        self.recorder.close()
        for closeable in (self.provider, self.refiner):
            try:
                closeable.close()
            except Exception:  # pragma: no cover
                pass

    # --- горячие клавиши -------------------------------------------------------

    def _warn_about_risky_keys(self) -> None:
        """Правый Shift как клавиша диктовки делает жизнь невыносимой.

        Он нужен для заглавных букв, поэтому каждая заглавная запускала бы
        запись. Конфиг мог остаться от старой версии, где такой умолчание было,
        поэтому предупреждаем явно, а не молча делаем странное.
        """
        cfg = self.cfg.hotkeys
        for field, value in (("dictate", cfg.dictate), ("dictate_alt", cfg.dictate_alt)):
            if value and "shift" in value.lower():
                message = (
                    f"[hotkeys].{field} = {value!r}: Shift нужен для заглавных букв, "
                    "каждая заглавная будет запускать запись. Возьмите scroll_lock или f13"
                )
                log.warning("%s", message)
                self.overlay.error(f"{field}: Shift — плохая клавиша для диктовки")

    def _register_hotkeys(self) -> None:
        self._warn_about_risky_keys()
        cfg = self.cfg.hotkeys
        self.hotkeys.register_hold(
            cfg.dictate,
            lambda: self._start_dictation(self.cfg.language.main),
            self._stop_dictation,
        )
        if cfg.dictate_alt and cfg.dictate_alt != cfg.dictate:
            self.hotkeys.register_hold(
                cfg.dictate_alt,
                lambda: self._start_dictation(self.cfg.language.alt),
                self._stop_dictation,
            )
        self.hotkeys.register_combo(cfg.paste_last, self._paste_last)
        if cfg.open_history:
            self.hotkeys.register_combo(cfg.open_history, self.history_window.open)

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        log.info("пауза: %s", "включена" if self.paused else "выключена")
        if self.paused:
            with self._state_lock:
                if self._recording:
                    self._recording = False
                    self.recorder.cancel()
            self.overlay.hide()
        if self.tray is not None:
            self.tray.refresh()

    # --- кто сейчас владеет плашкой ---------------------------------------------

    def _take_plate(self) -> int:
        """Взять плашку себе и вернуть номер показа.

        Плашка одна на всё приложение, а говорят в неё многие: диктовка,
        повторная вставка, окно истории, переоткрытие микрофона. Владеет
        плашкой тот, кто взял её последним, — сообщение с номером прошлого
        показа оверлей отбрасывает.

        Номер берётся В НАЧАЛЕ своей операции, а не перед показом: между
        началом и результатом человек успевает нажать клавишу диктовки, и
        отчёт чужой операции лёг бы поверх её «Записи…» — ровно та жалоба
        «нажимаю, и ничего не загорается». Отнятый показ операцию не
        отменяет: вставка и копирование доводятся до конца в любом случае,
        потерять продиктованное хуже, чем не показать плашку.

        Здесь же запоминается, что плашка ушла новому хозяину: по
        расхождению _plate_session и _session опрос в потоке Tk узнаёт, что
        идущую запись затёрли, и возвращает её на место (см. _poll_once).

        Зовётся в том числе из потока хука: внутри только инкремент под
        коротким локом оверлея, ни Tk, ни очереди, ни диска.
        """
        session = self._plate_session = self.overlay.begin_session()
        return session

    # --- диктовка --------------------------------------------------------------

    # Шаг опроса уровня во время записи: 80 мс — это два-три блока PortAudio.
    # Чаще глаз всё равно не различает, реже — полоска начинает дёргаться.
    LEVEL_TICK_MS = 80
    # Шаг между диктовками. Опрос крутится всегда, но вхолостую ему нужно
    # только заметить начало записи, а плашку зажигает само нажатие.
    IDLE_TICK_MS = 200
    # Сколько запись должна не дотягивать до порога, прежде чем плашка скажет
    # «микрофон молчит». Секунда — пауза между фразами ещё нормальна, а вот
    # молчащий микрофон за это время уже стоит показать: человек не должен
    # договаривать абзац в пустоту.
    SILENCE_HINT_S = 1.0
    # Сколько времени с начала записи мёртвому потоку прощается. Микрофон при
    # hold_open=false поднимается уже ПОСЛЕ нажатия, и первые десятки
    # миллисекунд блоков честно нет; ругаться на просыпающееся устройство —
    # ложная тревога. Секунда — это тридцать пропущенных блоков подряд.
    DEAD_HINT_GRACE_S = 1.0
    # Плашка с ошибкой гаснет сама через шесть секунд, а клавишу могут держать
    # дольше. Погасшая плашка посреди записи — ровно та жалоба, с которой всё
    # началось, поэтому пока поток мёртв, сообщение повторяется.
    DEAD_HINT_REPEAT_S = 4.0
    # Две разные поломки — две разные подсказки, и путать их нельзя.
    # «Микрофон молчит» (синяя) — слышу тишину, говорите громче или ближе.
    # Эта (красная) — не слышу вообще ничего: громкость не поможет, поток
    # микрофона мёртв и его надо переоткрыть, а это делается на отпускании.
    DEAD_MIC_HINT = "микрофон не отдаёт звук — отпустите и повторите"

    def _mic_problem(self) -> str | None:
        """Почему начинать запись прямо сейчас бессмысленно. None — всё хорошо.

        Только чтение полей и одно сравнение времени: зовётся из потока хука
        клавиатуры, где нельзя ни ждать, ни ходить на диск.
        """
        if not self.cfg.audio.hold_open:
            # Закрытый поток здесь — норма, его поднимет begin(). Проверять
            # живость нечего: блоков между диктовками не бывает по замыслу.
            return None
        if not self.recorder.is_open:
            # Микрофон могли переподключить, пока приложение работало.
            return "микрофон не готов — поднимаю, повторите"
        if self.recorder.is_alive:
            return None
        if time.monotonic() - self._mic_opened_at < audio_mod.STALL_GRACE:
            # Поток открыт только что и ещё не прислал первый блок. Это не
            # поломка, а просыпающееся устройство — молчим и пишем как обычно.
            return None
        return "микрофон не отдаёт звук — переоткрываю, повторите"

    def _start_dictation(self, lang: str) -> None:
        """Выполняется в потоке хука — только мгновенные операции.

        Ни один выход отсюда не имеет права быть молчаливым, кроме одного:
        повторного срабатывания при уже идущей записи, где плашка и так горит.
        Молчание на нажатие — это и есть та поломка, ради которой всё
        затевалось: человек не видит плашки, договаривает фразу до конца и
        узнаёт, что его не слышали, только на отпускании.
        """
        # Плашка одна на все диктовки подряд, поэтому нажатие открывает новое
        # поколение показа. С этого мгновения всё, что рабочий поток ещё не
        # успел сказать про ПРОШЛУЮ диктовку, до плашки не дойдёт: иначе её
        # «Готово» или ошибка ложится поверх только что зажжённой «Записи…»,
        # и человек видит хвост чужой диктовки вместо начала своей — ровно
        # его жалоба «нажимаю, и ничего не загорается».
        #
        # Номер берётся до всех проверок, потому что плашку зажигает любой
        # исход нажатия, включая паузу и неготовый микрофон: их сообщение
        # тоже нельзя затирать опоздавшим отчётом.
        #
        # _take_plate() — инкремент под коротким локом, ни Tk, ни очереди,
        # так что потоку хука он ничего не стоит.
        session = self._session = self._take_plate()

        if self.paused:
            # Пауза — осознанное решение пользователя, но клавишу он всё-таки
            # нажал. Без ответа это неотличимо от зависшей программы.
            self.overlay.error("пауза: диктовка выключена, включите её в трее")
            return

        # Чем именно упал begin(), если упал: в лог это уедет из фонового
        # потока вместе с переоткрытием, здесь только запоминаем.
        failure: Exception | None = None
        with self._state_lock:
            if self._recording:
                # Единственный молчаливый выход: плашка «Запись…» уже горит,
                # говорить человеку нечего — он и так всё видит.
                return
            problem = self._mic_problem()
            if problem is None:
                pressed_at = time.monotonic()
                try:
                    self.recorder.begin()
                except Exception as exc:
                    # Признак записи выставляется ТОЛЬКО после удачного
                    # begin(). Раньше он выставлялся до вызова, и упавший
                    # begin() оставлял его выставленным навсегда: каждое
                    # следующее нажатие уходило в молчаливый выход «уже
                    # пишем», и программа глохла до перезапуска — та самая
                    # вечная глухота, от которой мы только что ушли.
                    failure = exc
                    problem = "не удалось начать запись — переоткрываю, повторите"
                    # begin() мог успеть включить накопление кадров: без
                    # отмены рекордер копил бы звук в никуда до следующего
                    # нажатия.
                    try:
                        self.recorder.cancel()
                    except Exception:  # pragma: no cover - и отмена может упасть
                        pass
                else:
                    self._recording = True
                    self._lang = lang
                    self._press_at = pressed_at

        if problem is not None:
            # Плашка ПЕРВОЙ: это put в очередь, микросекунды, а ответ на
            # нажатие нужен человеку сразу.
            self.overlay.error(problem)
            self._fail(None, lang, 0.0, problem, session)
            # Жалоба в лог и переоткрытие устройства уходят в фон одним
            # потоком: log.error пишет на диск СИНХРОННО, и в потоке хука ему
            # не место. Замер на живом микрофоне: было медиана 0.97 мс, 95-й
            # перцентиль 2.49 мс, максимум 4.83 мс; стало 0.81 / 0.94 / 0.98
            # при здоровом нажатии в 0.066 мс. Медиана сдвинулась немного —
            # остаток здесь это порождение самого потока, который нужен для
            # переоткрытия, — зато ушёл весь хвост, а хук Windows снимает
            # именно за отдельный долгий вызов, а не за среднее.
            #
            # Сообщение при этом не теряется: тот же текст, тем же уровнем,
            # парой миллисекунд позже.
            #
            # Состояние микрофона считывается ЗДЕСЬ (это чтение двух полей):
            # через несколько миллисекунд поток уже переоткрывают, а в логе
            # нужна картина на момент нажатия.
            threading.Thread(
                target=self._complain_and_reopen,
                args=(
                    "нажата клавиша диктовки, но микрофон не готов: %s "
                    "(поток %s, блоков нет %s)%s",
                    problem,
                    "открыт" if self.recorder.is_open else "закрыт",
                    self._since_block_text(),
                    "" if failure is None else f", begin() упал: {failure!r}",
                ),
                daemon=True,
            ).start()
            return

        # Плашку зажигает само нажатие: overlay.recording() — это put в
        # очередь, микросекунды. Всё остальное (уровень, подсказки, цвет
        # значка) делает опрос в потоке Tk, который и так уже крутится.
        self.overlay.recording()

    # --- опрос записи в потоке Tk ----------------------------------------------
    #
    # Цикл живёт в потоке Tk сам по себе и только СМОТРИТ, идёт ли запись.
    # Заводить и останавливать его из потока хука нельзя, и это не вкусовщина:
    # root.after из чужого потока не кладёт задание в очередь, а вызывает
    # Tcl_ThreadQueueEvent и БЛОКИРУЕТСЯ, пока поток Tk событие не разберёт.
    # Замер: Tk простаивает — 0.07 мс; Tk обновляет окно истории на 2000
    # записей — медиана 0.1 мс, 95-й перцентиль 32.9 мс, максимум 48.5 мс.
    # Хук клавиатуры Windows столько ждать не может: медленный хук сначала
    # тормозит всю клавиатуру, а потом молча снимается системой (порог около
    # 300 мс). Значит, из хука в сторону Tk не должно уходить ничего.

    def _start_polling(self) -> None:
        """Заводит опрос. Только поток Tk (зовётся из run())."""
        if self._poll_job is None:
            self._poll()

    def _stop_polling(self) -> None:
        """Снимает отложенный тик, чтобы он не выстрелил по мёртвым виджетам."""
        job, self._poll_job = self._poll_job, None
        if job is None:
            return
        try:
            self.root.after_cancel(job)
        except (tk.TclError, RuntimeError, ValueError):
            pass

    def _poll(self) -> None:
        """Тик опроса: сделать шаг и назначить следующий. Только поток Tk."""
        self._poll_job = None
        if self._stopping.is_set():
            return
        try:
            self._poll_once()
        except Exception:  # pragma: no cover - показать нечего, но и падать нельзя
            log.exception("ошибка в опросе записи")
        # Между диктовками спешить некуда, во время записи — 80 мс.
        delay = self.LEVEL_TICK_MS if self._recording else self.IDLE_TICK_MS
        try:
            self._poll_job = self.root.after(delay, self._poll)
        except (tk.TclError, RuntimeError):
            # Корень уничтожен: приложение закрывается, показывать больше негде.
            self._poll_job = None

    def _poll_once(self) -> None:
        """Один шаг опроса: значок, полоска уровня, подсказки. Только поток Tk."""
        recording = self._recording
        if recording != self._polled_recording:
            self._polled_recording = recording
            self._recording_since = time.monotonic() if recording else 0.0
            self._hint = ""
            # Значок трея красим ЗДЕСЬ, а не в обработчике нажатия:
            # set_state зовёт Shell_NotifyIcon, и это самое дорогое, что было
            # в потоке хука. Опоздание на один тик значка никого не смущает.
            if self.tray is not None:
                self.tray.set_state(recording)
        if not recording:
            return

        now = time.monotonic()
        alive = self.recorder.is_alive
        # У мёртвого колбэка level не стареет, а ЗАСТЫВАЕТ на последнем блоке:
        # поток умер посреди громкой фразы — полоска навсегда осталась полной
        # и неподвижной. Такая полоска врёт убедительнее любой надписи,
        # поэтому у мёртвого потока уровень ровно нулевой.
        self.overlay.level(self.recorder.level if alive else 0.0)

        hint = self._hint_for(alive, now)
        # Плашку могла забрать чужая операция: повторная вставка, действие из
        # окна истории, провал переоткрытия микрофона. Разошедшиеся поколения
        # показа — единственный признак этого, который у нас есть, и он же
        # самый честный: сообщение с авто-скрытием ГАСНЕТ, и через несколько
        # секунд плашки нет вовсе, а подсказка при здоровом микрофоне не
        # меняется, и звать показ было некому. Человек договаривал абзац,
        # глядя на пустое место, — та же жалоба, только с середины записи.
        stolen = self._plate_session != self._session
        if hint != self._hint:
            if hint == "dead":
                log.error(
                    "микрофон перестал отдавать звук посреди записи: "
                    "блоков нет %s, держат %.1f с",
                    self._since_block_text(),
                    now - self._recording_since,
                )
            self._hint = hint
            self._hint_at = now
            self._show_hint(hint)
        elif stolen or (hint == "dead" and now - self._hint_at >= self.DEAD_HINT_REPEAT_S):
            # Перерисовываем ТОЛЬКО по событию: плашку отняли или пора
            # повторить жалобу на мёртвый поток. Рисовать каждый тик нельзя —
            # 80 мс между кадрами человек видит как дрожь.
            self._hint_at = now
            self._show_hint(hint)
        if stolen:
            # Плашка снова наша, и новое поколение отбросит то сообщение
            # чужой операции, которое ещё не успело долететь. Сама операция
            # при этом уже отработала: показ ей не нужен, чтобы вставить.
            #
            # Под тем же замком, что и отпускание клавиши: иначе новое
            # поколение вклинилось бы МЕЖДУ чтением номера в _stop_dictation
            # и отправкой задания, и человек не увидел бы результата
            # собственной диктовки. Замок держат только на мгновенных
            # операциях, поток Tk на нём не застревает.
            with self._state_lock:
                if self._recording:
                    self._session = self._take_plate()

    def _hint_for(self, alive: bool, now: float) -> str:
        """Что показывать поверх «Запись…»: "" (ничего), "dead" или "silent".

        Живость спрашивается у самого потока, а не выводится из громкости.
        Вывести её из громкости нельзя в принципе: молчание и смерть дают на
        полоске одно и то же (у мёртвого потока — вдобавок неподвижное), но
        лечатся по-разному. Молчит — говорите громче; мёртв — громкость не
        поможет, звука нет вообще.

        «Молчит» сверяется с НАКОПЛЕННЫМ пиком записи, а не с пиком последнего
        блока. Порог silence_peak рассчитан на peak_level() всей записи, и
        сравнивать с ним отдельный 32-мс блок нельзя: величины разные на
        порядок, и на нормальной негромкой речи (пик записи 0.101 при пороге
        0.105) плашка кричала «молчит» 83% времени. С накопленным пиком
        подсказка означает ровно одно и то же с итоговым решением: «отпустишь
        сейчас — запись отвергнут как тишину». Ложных срабатываний у такой
        проверки нет по построению; полоску уровня по-прежнему рисует
        мгновенный level, она для того и нужна.
        """
        if not alive:
            # Свежеоткрытому потоку даём проснуться: при hold_open=false
            # устройство поднимается уже после нажатия.
            return "dead" if now - self._recording_since >= self.DEAD_HINT_GRACE_S else ""
        if self.recorder.peak_since_begin >= self.cfg.audio.silence_peak:
            # Порог взят — до конца этой записи вопрос тишины закрыт:
            # накопленный максимум не убывает.
            return ""
        return "silent" if now - self._recording_since >= self.SILENCE_HINT_S else ""

    def _show_hint(self, hint: str) -> None:
        if hint == "dead":
            self.overlay.error(self.DEAD_MIC_HINT)
        elif hint == "silent":
            self.overlay.silent()
        else:
            self.overlay.recording()

    def _since_block_text(self) -> str:
        gap = self.recorder.seconds_since_block
        return "ни разу" if gap == float("inf") else f"{gap:.1f} с"

    def _stop_dictation(self) -> None:
        """Тоже в потоке хука: забрать буфер и отдать рабочему потоку."""
        with self._state_lock:
            if not self._recording:
                return
            self._recording = False
            capture = self.recorder.end()
            lang = getattr(self, "_lang", self.cfg.language.main)
            started = getattr(self, "_press_at", time.monotonic())
            # Поколение показа этой диктовки: с ним рабочий поток отчитается
            # о результате, и опоздавший отчёт до плашки уже не дойдёт.
            session = self._session

        # Значок в трее гасит опрос в потоке Tk: Shell_NotifyIcon отсюда стоил
        # дороже всей остальной обработки отпускания вместе взятой.
        #
        # stalled проверяется ДО отсечки по длине. Мёртвый поток отдаёт один пре-ролл,
        # то есть заведомо меньше минимума, и отсечка уносила единственное
        # объяснение в отладочную строку: человек видел, что программа молчит,
        # и не мог понять почему.
        if capture.stalled:
            held = time.monotonic() - started
            log.error(
                "микрофон перестал отдавать звук: клавишу держали %.1f с, "
                "записано %.2f с. Переоткрываю поток.",
                held, capture.duration_s,
            )
            self.overlay.error("микрофон замолчал — переоткрываю, повторите")
            self._fail(
                capture,
                lang,
                started,
                f"микрофон замолчал: держали {held:.1f} с, записано {capture.duration_s:.2f} с",
                session,
            )
            threading.Thread(target=self._try_reopen, daemon=True).start()
            return

        if capture.duration_s < self.cfg.audio.min_seconds:
            # В историю НЕ пишем намеренно. Случайных касаний клавиши бывает
            # много — задели, промахнулись мимо соседней, — и они превратили бы
            # историю в ленту мусора, где настоящую неудачу уже не найти.
            # Неудачей это и не является: диктовки не было.
            log.debug("запись %.2f с короче порога — игнорирую", capture.duration_s)
            self.overlay.hide()
            return

        peak = audio_mod.peak_level(capture)
        if peak < self.cfg.audio.silence_peak:
            # Уровень в сообщении обязателен: без него непонятно, микрофон
            # молчит совсем или просто не дотянул до порога.
            log.info(
                "в записи тишина (пик %.3f при пороге %.3f), ничего не отправляю",
                peak, self.cfg.audio.silence_peak,
            )
            reason = (
                f"тишина: уровень {peak:.3f} ниже порога "
                f"{self.cfg.audio.silence_peak:.3f}"
            )
            self.overlay.error(reason)
            self._fail(capture, lang, started, reason, session)
            return
        log.debug("пиковый уровень записи %.3f", peak)

        if capture.truncated:
            log.warning("запись обрезана по лимиту %d с", self.cfg.audio.max_seconds)

        self.overlay.sending()
        self._jobs.put(
            Job(capture=capture, lang=lang, started_at=started, session=session)
        )

    def _fail(
        self,
        capture: audio_mod.Capture | None,
        lang: str,
        started: float,
        reason: str,
        session: int | None = None,
    ) -> None:
        """Отдать провал рабочему потоку, чтобы он оставил след в истории.

        Зовётся из потока хука, поэтому здесь только put в очередь: сама
        запись на диск происходит в рабочем потоке.
        """
        self._jobs.put(
            Job(
                capture=capture,
                lang=lang,
                started_at=started,
                error=reason,
                session=session,
            )
        )

    def _complain_and_reopen(self, message: str, *args) -> None:
        """Пожаловаться в лог и поднять микрофон. Только фоновый поток.

        Оба дела дорогие: log.error синхронно пишет на диск, открытие
        устройства занимает десятки миллисекунд. В потоке хука клавиатуры не
        место ни тому, ни другому.
        """
        log.error(message, *args)
        self._try_reopen()

    def _try_reopen(self) -> None:
        # Поколение берётся в начале операции, как и у всех остальных:
        # переоткрытие устройства занимает десятки миллисекунд, и за это
        # время человек успевает нажать клавишу диктовки. Жалоба на провал
        # не должна лечь поверх его «Записи…».
        session = self._take_plate()
        try:
            self.recorder.reopen()
            self._mic_opened_at = time.monotonic()
            log.info("микрофон переоткрыт")
        except audio_mod.AudioError as exc:
            self.overlay.error(str(exc), session)

    # --- рабочий поток ---------------------------------------------------------

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._run_job(job)
            except Exception:
                log.exception("непредвиденная ошибка при обработке диктовки")
                self._rescue(job)
                self.overlay.error("внутренняя ошибка, подробности в логе", job.session)

    def _rescue(self, job: Job) -> None:
        """Спасает оплаченную диктовку, если обработка сорвалась на полпути.

        Деньги за расшифровку уже списаны, и текст мог быть получен. Потерять
        его молча — худшее, что программа умеет делать: человек не узнает ни
        что сказал, ни что заплатил.
        """
        record = job.pending
        if record is None:
            return
        job.pending = None
        record.error = record.error or "обработка сорвалась, подробности в логе"
        try:
            self.history.add(record)
            self._refresh_tray()
            log.error("диктовка сохранена в истории после сбоя: %s", record.error)
        except Exception:  # pragma: no cover - падать тут уже некуда
            log.exception("не удалось сохранить диктовку после сбоя")

    def _run_job(self, job: Job) -> None:
        if job.error:
            self._record_failure(job)
        else:
            self._process(job)

    def _record_failure(self, job: Job) -> None:
        """Кладёт неудачную диктовку в историю.

        Раньше провал не оставлял следов вообще: плашка гасла через шесть
        секунд, и человек больше не мог ни вспомнить, что случилось, ни
        показать это кому-то. Теперь неудача видна в окне истории наравне
        с удачами — красной строкой и с причиной.

        В счёт расходов такая запись не идёт — но не потому, что у неё
        непустой error: usage() считает и оплаченные неудачи. Не идёт
        потому, что к провайдеру не ходили вовсе, и record.answered остаётся
        False (см. history._was_paid).
        """
        self.history.add(
            Record(
                ts=time.time(),
                text="",
                lang=job.lang,
                provider=self.provider.name,
                model=self.provider.model,
                audio_sec=round(job.capture.duration_s, 2) if job.capture else 0.0,
                error=job.error,
            )
        )
        self._refresh_tray()

    def _outdated(self, job: Job) -> bool:
        """Человек уже начал следующую диктовку, пока мы возились с этой.

        Проверка касается ТОЛЬКО показа плашки, и только тех состояний, у
        которых в оверлее нет номера сессии (overlay.refining()): у ok() и
        error() номер везёт сам оверлей. Вставку, историю и счётчик расходов
        этой проверкой не закрывают никогда — потерять продиктованное хуже,
        чем показать не ту плашку.
        """
        return job.session is not None and job.session < self._session

    def _process(self, job: Job) -> None:
        watch = Stopwatch()
        self._check_for_dropped_audio(job)

        capture = job.capture
        if self.cfg.audio.normalize:
            capture, gain = audio_mod.normalize(capture)
            if gain > 1.05:
                log.debug("громкость поднята в %.1f раза", gain)

        data, filename = audio_mod.encode(capture, self.cfg.audio.encode)
        watch.mark("encode")

        audio_file = self.audio_cache.save(data, filename)

        record = Record(
            ts=time.time(),
            text="",
            lang=job.lang,
            provider=self.provider.name,
            model=self.provider.model,
            audio_sec=round(job.capture.duration_s, 2),
            audio_file=audio_file,
        )

        try:
            raw = self.provider.transcribe(
                TranscriptionRequest(
                    audio=data,
                    filename=filename,
                    language=job.lang,
                    prompt=self.cfg.language.prompt_for(job.lang),
                    duration_s=job.capture.duration_s,
                )
            )
        except TranscriptionError as exc:
            watch.mark("api")
            record.error = str(exc)
            self.history.add(record)
            self._refresh_tray()
            log.error("распознавание не удалось: %s (%s)", exc, watch.summary())
            self.overlay.error(str(exc), job.session)
            return
        watch.mark("api")
        # Ответ получен — значит, за эту диктовку уже заплачено, чем бы ни
        # кончились чистка, правка и вставка. Ставим ДО них: дальше от ответа
        # может не остаться ни текста, ни следов, а деньги списаны. Запись
        # ещё не в журнале (history.add ниже), поэтому присваивание не
        # перезаписывает файл — всё уедет на диск одной записью.
        record.answered = True
        # С этой секунды запись оплачена. Отдаём её рабочему потоку: если
        # дальше что-то бросит, он сохранит её вместо того, чтобы потерять.
        job.pending = record

        cleaned = self.postprocessor.clean(raw)
        watch.mark("post")
        if not cleaned:
            # Раньше здесь молча гасла плашка. Для человека это неотличимо от
            # незамеченного нажатия — при том, что звук записан, запрос ушёл
            # и уже оплачен. Причину надо показать и сохранить.
            preview = " ".join(raw.split())
            if len(preview) > 24:
                preview = preview[:23] + "…"
            reason = (
                f"выдумка на тишине: «{preview}», не вставляю"
                if preview
                else "провайдер вернул пустой ответ"
            )
            self._nothing_to_paste(record, raw, watch, reason, job.session)
            job.pending = None  # уже в журнале, спасать нечего
            return

        # Правка моделью идёт ПЕРЕД словарём замен: иначе она перепишет
        # термины по-своему и последнее слово останется за ней, а не за
        # тем, что пользователь задал в конфиге.
        refined = cleaned
        if self.refiner.enabled:
            # Номер показа передаём в оверлей: проверить и позвать двумя
            # действиями нельзя — между ними человек успевает нажать клавишу,
            # и «Правлю текст…» ложится поверх новой «Записи…».
            self.overlay.refining(job.session)
            refined = self.refiner.refine(cleaned)
            record.refine_in, record.refine_out = getattr(
                self.refiner, "last_usage", (0, 0)
            )
            watch.mark("refine")
            if refined != cleaned:
                log.debug("правка: %r -> %r", cleaned[:60], refined[:60])

        text = self.postprocessor.finish(refined)
        if not text:
            # Сюда попадаем, только если правка моделью вернула пустоту:
            # словарь замен текст не съедает. Диктовка была, деньги за
            # расшифровку заплачены — молчать тем более нельзя.
            self._nothing_to_paste(
                record,
                cleaned,
                watch,
                "правка вернула пустой текст, не вставляю",
                job.session,
            )
            job.pending = None  # уже в журнале, спасать нечего
            return

        record.raw = cleaned if refined != cleaned else ""
        record.text = text
        record.target_exe = inject.foreground_exe()

        # В историю пишем ДО вставки: если приложение упадёт на вставке,
        # продиктованное всё равно не потеряется.
        self.history.add(record)
        job.pending = None

        ok, reason = self.injector.paste(text, target_exe=record.target_exe)
        watch.mark("paste")

        if ok:
            self.overlay.ok(text, job.session)
        else:
            # Причина обязана доехать до диска, а не остаться в памяти.
            # Раньше она приписывалась к уже добавленной записи и в файл не
            # попадала: до перезапуска диктовка из расходов исключалась,
            # после — возвращалась, и счётчик менялся сам по себе.
            # Записать раньше нельзя — причина выясняется только после
            # попытки вставки, а в историю мы намеренно пишем ДО неё, чтобы
            # падение на вставке не съело продиктованное. Поэтому правка:
            # update() перезаписывает файл один раз и заодно чинит объект.
            self.history.update(record, error=reason)
            self.overlay.error(
                f"{reason} · {self.cfg.hotkeys.paste_last} — вставить снова",
                job.session,
            )
            log.warning("вставка не прошла: %s", reason)

        self._refresh_tray()
        log.info(
            "готово: %.1f с звука -> %d симв., %s, окно=%s",
            job.capture.duration_s,
            len(text),
            watch.summary(),
            record.target_exe or "?",
        )

    def _nothing_to_paste(
        self,
        record: Record,
        dropped: str,
        watch: Stopwatch,
        reason: str,
        session: int | None = None,
    ) -> None:
        """Расшифровка пришла, а вставлять нечего.

        Два выхода из _process раньше просто гасили плашку и уходили. Снаружи
        это выглядело как «нажал, поговорил, ничего не произошло» — та же
        поломка, из-за которой всё затевалось, только дороже: звук записан,
        запрос к провайдеру отправлен и ОПЛАЧЕН. Поэтому причина видна на
        плашке и лежит в истории красной строкой, как тишина и провал сети.

        Выброшенное кладём в raw: в окне истории его не видно, но в файле оно
        остаётся, и вопрос «что же там распозналось» решается чтением, а не
        догадками.

        Про расходы: деньги за эту расшифровку списаны, и в счёт она
        попадает. Признак оплаты — record.answered, его ставит _process
        сразу после ответа провайдера, до чистки и правки; usage() смотрит
        на него, а не на пустой error.
        """
        record.error = reason  # до add: журнал записи ещё не знает, файла нет
        record.raw = dropped
        log.info("вставлять нечего: %s (%s)", reason, watch.summary())
        self.history.add(record)
        self._refresh_tray()
        self.overlay.error(reason, session)

    def _check_for_dropped_audio(self, job: Job) -> None:
        """Сравнивает время удержания клавиши с длиной записи.

        Если звука пришло заметно меньше, чем клавишу держали, значит колбэк
        микрофона не успевал и блоки терялись. Снаружи это выглядит как
        «половину фразы не расслышало», и без этой строки в логе причину
        искать негде.
        """
        if job.capture.interrupted:
            # Поток микрофона пересоздали прямо посреди этой записи: stop()
            # с close() и подъём устройства — это около сотни миллисекунд
            # тишины в середине фразы. Без этой строки шов в тексте выглядел
            # бы как «оно опять половину не расслышало».
            log.warning(
                "посреди записи микрофон пересоздавали: в звуке пропуск "
                "около сотни миллисекунд, в тексте возможен шов"
            )
        if not job.started_at:
            return
        held = time.monotonic() - job.started_at
        gap = held - job.capture.duration_s
        if gap > 0.3:
            log.warning(
                "потеряно ~%.1f с звука: клавишу держали %.1f с, записано %.1f с",
                gap, held, job.capture.duration_s,
            )

    def _refresh_tray(self) -> None:
        if self.tray is not None:
            self.tray.refresh()

    # --- повторная вставка -----------------------------------------------------

    def _paste_last(self) -> None:
        """Хоткей повторной вставки. Нажатия подряд идут вглубь истории.

        Поколение показа берётся здесь, в начале операции: поиск записи и
        сама вставка занимают сотни миллисекунд, и человек успевает нажать
        клавишу диктовки. Отчёт о повторной вставке ляжет тогда поверх её
        «Записи…» — см. _take_plate.
        """
        session = self._take_plate()
        record = self.history.next_for_paste()
        if record is None:
            self.overlay.error("история пуста", session)
            return
        self._paste_record(record, reset_cycle=False, session=session)

    def _paste_record(
        self, record: Record, reset_cycle: bool = True, session: int | None = None
    ) -> None:
        """Вставить запись истории. session — поколение показа операции.

        Без него операцию начали не мы, а окно истории или меню в трее:
        тогда поколение берётся здесь, тоже до вставки.

        Показ отнять можно, вставку — нет: текст уходит в окно независимо от
        того, увидит человек отчёт или нет.
        """
        if session is None:
            session = self._take_plate()
        if not record.text:
            self.overlay.error("в этой записи нет текста", session)
            return
        if reset_cycle:
            self.history.reset_cycle()
        ok, reason = self.injector.paste(record.text)
        if ok:
            self.overlay.ok(record.text, session)
        else:
            # В лог — всегда. Отчёт на плашке отбрасывается, если человек уже
            # начал следующую диктовку, и тогда неудачная повторная вставка
            # не оставляла следа нигде.
            log.warning("повторная вставка не удалась: %s", reason)
            self.overlay.error(reason, session)

    def _copy_record(self, record: Record) -> None:
        """Положить запись в буфер. Поколение — как у вставки, до операции."""
        session = self._take_plate()
        if record.text and self.injector.put_in_clipboard(record.text):
            self.overlay.ok("скопировано в буфер", session)
        else:
            self.overlay.error("не удалось положить в буфер", session)


# --- вспомогательные режимы ----------------------------------------------------


def cmd_devices() -> int:
    devices = audio_mod.list_input_devices()
    if not devices:
        print("Микрофоны не найдены.")
        return 1
    print("Доступные микрофоны (индекс — имя):")
    for index, name in devices:
        print(f"  {index:3d}  {name}")
    print('\nВпишите индекс или имя в [audio].device в конфиге.')
    return 0


def cmd_check(cfg: config_mod.Config) -> int:
    """Быстрая проверка: конфиг, микрофон, провайдер."""
    print(f"Конфиг:    {config_mod.config_path()}")
    print(f"История:   {config_mod.history_path()}")
    print(f"Логи:      {config_mod.log_path()}")
    print(f"Провайдер: {cfg.provider.base_url}  модель {cfg.provider.model}")

    key = cfg.provider.api_key
    print(f"Ключ {cfg.provider.api_key_env}: {'найден' if key else 'НЕ НАЙДЕН'}")

    print(f"Микрофон:  {cfg.audio.device or 'системный по умолчанию'}")
    recorder = audio_mod.Recorder(
        sample_rate=cfg.audio.sample_rate,
        preroll_ms=cfg.audio.preroll_ms,
        device=cfg.audio.device,
    )
    try:
        recorder.open()
    except audio_mod.AudioError as exc:
        print(f"Микрофон:  ОШИБКА — {exc}")
        return 1

    print("Микрофон:  открыт. Говорите — слушаю 3 секунды…")
    recorder.begin()
    time.sleep(3.0)
    capture = recorder.end()
    recorder.close()
    peak = audio_mod.peak_level(capture)
    print(
        f"           записано {capture.duration_s:.1f} с, "
        f"пиковый уровень {peak:.3f} (порог тишины {cfg.audio.silence_peak:.3f})"
    )
    if peak < cfg.audio.silence_peak:
        print("           НИЖЕ ПОРОГА — такая запись отправлена не будет.")
        print("           Если вы говорили: уменьшите [audio].silence_peak или")
        print("           прибавьте громкость микрофона в параметрах звука Windows.")
        print("           Если не говорили: так и должно быть.")
    else:
        print("           уровень достаточный, речь будет распознаваться")

    if not key:
        print("\nБез ключа запрос к провайдеру не проверить.")
        return 1

    to_send = capture
    if cfg.audio.normalize:
        to_send, gain = audio_mod.normalize(capture)
        if gain > 1.05:
            print(f"           громкость поднята в {gain:.1f} раза перед отправкой")

    data, filename = audio_mod.encode(to_send, cfg.audio.encode)
    print(f"           {filename}: {len(data) / 1024:.1f} КБ")
    provider = build_provider(cfg.provider)
    watch = Stopwatch()
    try:
        text = provider.transcribe(
            TranscriptionRequest(
                audio=data,
                filename=filename,
                language=cfg.language.main,
                prompt=cfg.language.prompt_for(cfg.language.main),
                duration_s=capture.duration_s,
            )
        )
    except TranscriptionError as exc:
        print(f"Провайдер: ОШИБКА — {exc}")
        return 1
    finally:
        provider.close()

    print(f"Провайдер: ответил за {watch.total * 1000:.0f} мс")
    print(f"Распознано: {text!r}")
    print(f"После обработки: {Postprocessor(cfg.postprocess).process(text)!r}")
    return 0


def _force_utf8_output() -> None:
    """Кириллица в консоли.

    В настоящей консоли Windows Python пишет через юникодный API и всё хорошо,
    но при запуске из Git Bash или с перенаправлением он берёт системную
    кодировку, и русский текст превращается в кашу.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


def cmd_calibrate(cfg: config_mod.Config, seconds: float = 3.0) -> int:
    """Измеряет фон микрофона и подбирает порог тишины.

    Порог нельзя задать раз и навсегда: он зависит от усиления конкретного
    микрофона. Стоит прибавить громкость в Windows — и фон подскакивает
    вместе с речью. Живой случай: после прибавления громкости фон вырос с
    0.006 до 0.035, прежний порог 0.012 перестал его отсекать, и провайдер
    начал выдумывать текст на пустых записях.
    """
    print(f"Микрофон: {cfg.audio.device or 'системный по умолчанию'}")
    print(f"Текущий порог тишины: {cfg.audio.silence_peak:.3f}")
    print()
    print(f"Сейчас измерю фон. МОЛЧИТЕ {seconds:.0f} секунды…")

    recorder = audio_mod.Recorder(
        sample_rate=cfg.audio.sample_rate,
        preroll_ms=cfg.audio.preroll_ms,
        device=cfg.audio.device,
    )
    try:
        recorder.open()
    except audio_mod.AudioError as exc:
        print(f"ОШИБКА: {exc}")
        return 1

    recorder.begin()
    time.sleep(seconds)
    capture = recorder.end()
    recorder.close()

    noise = audio_mod.peak_level(capture)
    # Втрое выше фона: речь обычно на порядок громче, запас лишним не будет.
    threshold = min(0.30, max(0.010, round(noise * 3, 3)))

    print(f"           фон = {noise:.3f}")
    print(f"           рекомендуемый порог = {threshold:.3f}")

    if noise > 0.15:
        print()
        print("           Фон очень высокий. Возможно, стоит убавить усиление")
        print("           микрофона в параметрах звука Windows (sound.bat).")

    path = config_mod.config_path()
    try:
        import tomlkit

        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        doc.setdefault("audio", tomlkit.table())["silence_peak"] = threshold
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    except Exception as exc:
        print(f"\nНе удалось записать в конфиг ({exc}).")
        print(f"Впишите вручную в {path}:  [audio] silence_peak = {threshold}")
        return 1

    print()
    print(f"Записано в {path}")
    print("Проверьте: check.bat, говорите — уровень должен быть заметно выше порога.")
    print("Если жалуется на тишину, когда вы говорите, — уменьшите silence_peak.")
    return 0


def cmd_paste_test(cfg: config_mod.Config, delay: float = 5.0) -> int:
    """Проверка вставки без микрофона и без сети.

    Нужна для обхода по приложениям из плана: блокнот, адресная строка,
    терминал, Electron, Word. Механика вставки везде разная, и единственный
    честный способ проверить — вставить в каждое.
    """
    sample = "Проверка WhisperFree: поставь Docker и проверь через Gemini — раз, два, три."
    print("Переключитесь в окно, куда нужно вставить текст.")
    for remaining in range(int(delay), 0, -1):
        print(f"  {remaining}…", end="\r", flush=True)
        time.sleep(1.0)

    exe = inject.foreground_exe()
    combo = inject.paste_key_for(exe, cfg.inject.default_paste, cfg.inject.paste_overrides)
    watch = Stopwatch()
    ok, reason = inject.Injector(cfg.inject).paste(sample)

    print(f"Окно:      {exe or 'не определено'}")
    print(f"Клавиша:   {combo}")
    print(f"Результат: {'вставлено' if ok else 'НЕ вставлено — ' + reason}")
    print(f"Время:     {watch.total * 1000:.0f} мс")
    if not ok:
        print("\nТекст остался в буфере обмена — нажмите Ctrl+V руками.")
    elif combo == cfg.inject.default_paste and exe:
        print(
            "\nЕсли в этом окне текст не появился, добавьте его в конфиг:\n"
            f'  [inject.paste_overrides]\n  "{exe}" = "ctrl+shift+v"'
        )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="whisperfree", description="Голосовой ввод в любое окно Windows"
    )
    parser.add_argument("--config", type=Path, help="путь к config.toml")
    parser.add_argument("--cwd", type=Path, help="рабочий каталог (нужен для .env при автозапуске)")
    parser.add_argument("--devices", action="store_true", help="показать список микрофонов")
    parser.add_argument("--check", action="store_true", help="проверить микрофон и провайдера")
    parser.add_argument(
        "--calibrate",
        nargs="?",
        const=3.0,
        type=float,
        metavar="СЕК",
        help="измерить фон микрофона и подобрать порог тишины",
    )
    parser.add_argument(
        "--paste-test",
        nargs="?",
        const=5.0,
        type=float,
        metavar="СЕК",
        help="вставить тестовую фразу в активное окно через N секунд",
    )
    parser.add_argument(
        "--limits", action="store_true", help="остаток бесплатных лимитов провайдера"
    )
    parser.add_argument("--debug", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as exc:
            print(f"не удалось перейти в {args.cwd}: {exc}", file=sys.stderr)

    # Перенос до чтения конфига: иначе первый запуск после переименования
    # создал бы пустой конфиг и настройки выглядели бы потерянными.
    moved = config_mod.migrate_legacy_data()

    cfg = config_mod.load_config(args.config)
    setup_logging(
        config_mod.log_path(),
        level=logging.DEBUG if args.debug else logging.INFO,
        console=sys.stderr is not None,
    )
    if moved:
        # Логгер до этой строки ещё не настроен, поэтому сообщаем здесь.
        log.info("перенесено из каталога прежней версии: %s", ", ".join(moved))

    if args.devices:
        return cmd_devices()
    if args.check:
        return cmd_check(cfg)
    if args.calibrate is not None:
        return cmd_calibrate(cfg, args.calibrate)
    if args.limits:
        return report_limits(cfg)
    if args.paste_test is not None:
        return cmd_paste_test(cfg, args.paste_test)

    # Два работающих экземпляра вешают хук на одну клавишу, и на отпускание
    # срабатывают оба: текст вставляется дважды, а микрофон держат обе копии.
    guard = SingleInstance()
    if not guard.acquire():
        message = (
            "WhisperFree уже запущен — значок есть в трее. "
            "Второй экземпляр вставлял бы текст дважды."
        )
        log.error("%s", message)
        print(message, file=sys.stderr)
        return 1
    try:
        return App(cfg).run()
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
