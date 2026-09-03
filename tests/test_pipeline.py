"""Сквозной прогон конвейера с подставным провайдером и подставной вставкой.

Проверяется то, ради чего всё писалось: звук доезжает до провайдера с нужными
параметрами, текст проходит доводку, попадает в историю ДО попытки вставки и
вставляется. И то, что при любом сбое продиктованное остаётся доступным.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import tkinter as tk

import numpy as np
import pytest
import tomlkit

from whisperfree import audio as audio_mod
from whisperfree import config as config_mod
from whisperfree.config import DEFAULT_CONFIG_TOML, parse_config
from whisperfree.history import History, Record
from whisperfree.overlay import Overlay
from whisperfree.providers import TranscriptionError


@pytest.fixture
def app(tmp_path, monkeypatch, root):
    """Настоящий App, но без трея, оверлея, микрофона и сети."""
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)

    data = tomlkit.parse(DEFAULT_CONFIG_TOML).unwrap()
    cfg = parse_config(data)
    cfg.ui.tray = False
    cfg.ui.overlay = False
    cfg.history.keep_audio = False

    from whisperfree.__main__ import App

    instance = App(cfg, root=root)
    instance.provider = FakeProvider("привет из докера")
    instance.injector = FakeInjector()
    return instance


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, result):
        self.result = result
        self.requests = []

    def transcribe(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        pass


class FakeInjector:
    def __init__(self, ok=True, reason=""):
        self.ok = ok
        self.reason = reason
        self.pasted = []
        self.clipboard = []

    def paste(self, text, target_exe=None):
        self.pasted.append(text)
        return self.ok, self.reason

    def put_in_clipboard(self, text):
        self.clipboard.append(text)
        return True


def speech(seconds: float = 3.0, sample_rate: int = 16000) -> audio_mod.Capture:
    t = np.linspace(0, seconds, int(sample_rate * seconds), endpoint=False)
    samples = (0.3 * np.sin(2 * np.pi * 200 * t) * 32767).astype(np.int16)
    return audio_mod.Capture(samples=samples, sample_rate=sample_rate)


def job(app, lang="ru", capture=None):
    from whisperfree.__main__ import Job

    return Job(capture=capture or speech(), lang=lang, started_at=0.0)


class TestHappyPath:
    def test_text_reaches_the_active_window(self, app):
        app._process(job(app))
        # «докера» в родительном падеже тоже ловится: ключи словаря заданы
        # регулярками с \w* на конце, иначе половина падежей проходила бы мимо.
        assert app.injector.pasted == ["привет из Docker "]

    def test_request_carries_language_and_prompt(self, app):
        app._process(job(app, lang="ru"))
        request = app.provider.requests[0]

        assert request.language == "ru"
        assert "Docker" in request.prompt  # затравка тянет термины в латиницу
        assert request.filename == "speech.flac"
        assert len(request.audio) > 0

    def test_alternative_language_switches_the_prompt(self, app):
        app._process(job(app, lang="en"))
        assert app.provider.requests[0].prompt == app.cfg.language.prompt_en

    def test_replacements_are_applied_before_pasting(self, app):
        app.provider = FakeProvider("проверь через джемини")
        app._process(job(app))
        assert app.injector.pasted == ["проверь через Gemini "]

    def test_record_lands_in_history(self, app):
        app._process(job(app))
        record = app.history.last()

        assert record.text == "привет из Docker "
        assert record.lang == "ru"
        assert record.provider == "fake"
        assert record.model == "fake-model"
        assert record.audio_sec == pytest.approx(3.0, abs=0.01)
        assert record.error == ""


class TestNothingIsLost:
    def test_history_is_written_even_when_pasting_fails(self, app):
        app.injector = FakeInjector(ok=False, reason="нет активного окна")
        app._process(job(app))

        # Главная гарантия: вставка провалилась, текст никуда не делся.
        assert app.history.last().text == "привет из Docker "

    def test_failed_paste_is_recoverable_by_hotkey(self, app):
        app.injector = FakeInjector(ok=False, reason="нет активного окна")
        app._process(job(app))

        app.injector = FakeInjector(ok=True)
        app._paste_last()
        assert app.injector.pasted == ["привет из Docker "]

    def test_provider_failure_is_recorded_with_its_reason(self, app):
        app.provider = FakeProvider(TranscriptionError("сеть недоступна"))
        app._process(job(app))

        record = app.history.last()
        assert record.error == "сеть недоступна"
        assert record.text == ""
        assert app.injector.pasted == []

    def test_repeated_hotkey_walks_back_through_history(self, app):
        for text in ("одна", "две", "три"):
            app.provider = FakeProvider(text)
            app._process(job(app))
        app.injector = FakeInjector()

        app._paste_last()
        app._paste_last()
        app._paste_last()
        assert app.injector.pasted == ["три ", "две ", "одна "]

    def test_empty_history_does_not_crash(self, app):
        app._paste_last()
        assert app.injector.pasted == []

    def test_a_hole_in_the_sound_is_explained(self, app, caplog):
        # Поток микрофона пересоздали посреди записи: сотня миллисекунд
        # тишины в середине фразы. Без объяснения шов в тексте выглядит как
        # «оно опять половину не расслышало».
        capture = speech()
        capture.interrupted = True
        with caplog.at_level(logging.WARNING):
            app._process(job(app, capture=capture))

        assert "пропуск" in caplog.text
        assert app.injector.pasted == ["привет из Docker "]  # текст всё равно доехал


class TestGarbageIsDropped:
    """Мусор в окно не попадает — но и молча не исчезает.

    Раньше оба этих выхода из _process просто гасили плашку. Снаружи это
    неотличимо от незамеченного нажатия, при том что звук записан, запрос
    к провайдеру отправлен и ОПЛАЧЕН: человек видит, что ничего не
    произошло, и не может понять, на каком шаге его потеряли.
    """

    def test_silence_hallucination_never_reaches_the_window(self, app):
        app.provider = FakeProvider("Субтитры сделал DimaTorzok")
        app._process(job(app))

        assert app.injector.pasted == []

    def test_a_dropped_hallucination_is_shown_and_recorded(self, app):
        app.overlay = FakeOverlay()
        app.provider = FakeProvider("Субтитры сделал DimaTorzok")
        app._process(job(app))

        assert app.overlay.states == ["error"]
        record = app.history.last()
        assert record is not None
        assert record.error
        assert record.text == ""
        # Выброшенное остаётся в файле: «что же там распозналось» должно
        # решаться чтением истории, а не догадками.
        assert "DimaTorzok" in record.raw

    def test_an_empty_answer_is_shown_and_recorded(self, app):
        app.overlay = FakeOverlay()
        app.provider = FakeProvider("   ")
        app._process(job(app))

        assert app.injector.pasted == []
        assert app.overlay.states == ["error"]
        assert "пуст" in app.history.last().error.lower()

    def test_a_refiner_that_eats_the_text_is_not_silent_either(self, app):
        app.overlay = FakeOverlay()
        app.provider = FakeProvider("поставь докер")
        app.refiner = FakeRefiner("")
        app._process(job(app))

        assert app.injector.pasted == []
        assert app.overlay.states == ["refining", "error"]
        assert "правка" in app.history.last().error.lower()


class TestUsageAccounting:
    def test_cost_counter_follows_real_dictations(self, app):
        for _ in range(4):
            app._process(job(app))

        stats = app.history.usage(
            app.cfg.provider.price_per_hour_usd, app.cfg.provider.min_billed_seconds
        )
        # Четыре диктовки по 3 с, но тарифицируются как по 10 с.
        assert stats["today_count"] == 4
        assert stats["today_seconds"] == 40.0
        assert stats["today_usd"] == pytest.approx(40 / 3600 * 0.04)

    def test_an_answered_request_is_marked_as_paid(self, app):
        # Признак ставится сразу после ответа провайдера — до чистки, правки
        # и вставки. Дальше от ответа может не остаться ни текста, ни следов,
        # а деньги за него уже списаны.
        app._process(job(app))

        assert app.history.last().answered is True

    def test_a_request_that_never_got_an_answer_is_not_marked(self, app):
        app.provider = FakeProvider(TranscriptionError("сеть недоступна"))
        app._process(job(app))

        assert app.history.last().answered is False

    def test_a_dictation_that_never_reached_the_provider_is_not_marked(self, app):
        release(app, silence())

        assert app.history.last().answered is False

    def test_a_paid_answer_counts_even_when_there_is_nothing_to_paste(self, app):
        # Выдумка на тишине: вставлять нечего, запись красная — но запрос ушёл
        # и оплачен, и в счёт он обязан попасть.
        app.provider = FakeProvider("Субтитры сделал DimaTorzok")
        app._process(job(app))

        record = app.history.last()
        assert record.error
        assert record.answered is True

        stats = app.history.usage(
            app.cfg.provider.price_per_hour_usd, app.cfg.provider.min_billed_seconds
        )
        assert stats["today_count"] == 1


class FakeRefiner:
    def __init__(self, result=None, enabled=True):
        self.result = result
        self.enabled = enabled
        self.seen = []

    def refine(self, text):
        self.seen.append(text)
        return self.result if self.result is not None else text

    def close(self):
        pass


class TestRefine:
    """Правка моделью встраивается между чисткой и словарём замен."""

    def test_refined_text_is_what_gets_pasted(self, app):
        app.provider = FakeProvider("поставь докер")
        app.refiner = FakeRefiner("Поставь докер.")
        app._process(job(app))

        # Словарь замен отработал ПОСЛЕ правки: «докер» стал Docker.
        assert app.injector.pasted == ["Поставь Docker. "]

    def test_dictionary_has_the_last_word(self, app):
        # Модель написала термин по-своему; конфиг пользователя главнее.
        app.provider = FakeProvider("проверь через джемини")
        app.refiner = FakeRefiner("Проверь через джемини.")
        app._process(job(app))

        assert app.injector.pasted == ["Проверь через Gemini. "]

    def test_refiner_sees_cleaned_text_not_raw(self, app):
        app.provider = FakeProvider('  "поставь докер"  ')
        app.refiner = FakeRefiner()
        app._process(job(app))

        assert app.refiner.seen == ["поставь докер"]

    def test_original_is_kept_in_history(self, app):
        app.provider = FakeProvider("поставь докер")
        app.refiner = FakeRefiner("Поставь докер.")
        app._process(job(app))

        record = app.history.last()
        assert record.text == "Поставь Docker. "
        assert record.raw == "поставь докер"  # до правки, на случай искажения

    def test_raw_is_not_stored_when_nothing_changed(self, app):
        app.provider = FakeProvider("поставь докер")
        app.refiner = FakeRefiner()  # вернёт то же самое
        app._process(job(app))

        assert app.history.last().raw == ""

    def test_disabled_refiner_is_not_called(self, app):
        app.provider = FakeProvider("поставь докер")
        app.refiner = FakeRefiner("НЕ ДОЛЖНО ПОПАСТЬ", enabled=False)
        app._process(job(app))

        assert app.refiner.seen == []
        assert app.injector.pasted == ["поставь Docker "]

    def test_hallucination_is_dropped_before_refining(self, app):
        # Незачем тратить запрос на текст, который всё равно выбросим.
        app.provider = FakeProvider("Субтитры сделал DimaTorzok")
        app.refiner = FakeRefiner()
        app._process(job(app))

        assert app.refiner.seen == []
        assert app.injector.pasted == []


class TestDeadMicrophoneIsNoticed:
    """Мёртвый поток отдаёт один пре-ролл — короче минимума.

    Отсечка по длине срабатывала раньше и уносила единственное объяснение
    в отладочную строку. Человек видел, что программа молчит на каждое
    нажатие, и понять причину не мог: в логе на уровне INFO не было ничего.
    """

    @staticmethod
    def press(app, capture):
        """Проигрывает нажатие и отпускание с заданной записью."""
        app._recording = True
        app._press_at = time.monotonic() - 3.0
        app._lang = app.cfg.language.main
        app.recorder = FakeRecorder(capture)
        app._stop_dictation()

    def test_a_stalled_capture_triggers_a_reopen(self, app, monkeypatch, caplog):
        reopened = []
        monkeypatch.setattr(app, "_try_reopen", lambda: reopened.append(True))

        capture = audio_mod.Capture(
            samples=np.zeros(4160, dtype=np.int16),  # 0.26 с, как в живом случае
            sample_rate=16000,
            stalled=True,
        )
        with caplog.at_level(logging.ERROR):
            self.press(app, capture)

        assert reopened == [True]
        assert app.injector.pasted == []

    def test_the_reason_is_in_the_log_at_error_level(self, app, monkeypatch, caplog):
        # Не DEBUG: пользователь запускает через run.vbs, где лога на экране нет
        # вовсе, а в файл на уровне INFO отладочные строки не попадают.
        monkeypatch.setattr(app, "_try_reopen", lambda: None)
        capture = audio_mod.Capture(
            samples=np.zeros(4160, dtype=np.int16), sample_rate=16000, stalled=True
        )
        with caplog.at_level(logging.ERROR):
            self.press(app, capture)

        assert "микрофон" in caplog.text.lower()
        assert "переоткрываю" in caplog.text.lower()

    def test_an_ordinary_short_press_stays_quiet(self, app, monkeypatch, caplog):
        # Случайное касание клавиши поломкой не считается и в лог не кричит.
        reopened = []
        monkeypatch.setattr(app, "_try_reopen", lambda: reopened.append(True))

        capture = audio_mod.Capture(
            samples=np.zeros(3200, dtype=np.int16), sample_rate=16000, stalled=False
        )
        with caplog.at_level(logging.ERROR):
            self.press(app, capture)

        assert reopened == []
        assert "микрофон" not in caplog.text.lower()


class FakeRecorder:
    """Микрофон, которому можно задать любое состояние.

    is_open, is_alive, level и peak_since_begin — свойства, как и в настоящем
    Recorder: ошибочный вызов `recorder.is_alive()` должен падать здесь ровно
    так же, как он упал бы в бою.
    """

    def __init__(self, capture=None, is_open=True, is_alive=True, level=0.0, peak=None):
        self.capture = capture
        self.reopened = 0
        self.began = 0
        self.closed = 0
        self.cancelled = 0
        self._is_open = is_open
        self._is_alive = is_alive
        self._level = level
        # Накопленный пик не бывает меньше пика текущего блока.
        self._peak = level if peak is None else peak

    def begin(self):
        self.began += 1

    def end(self):
        return self.capture

    def reopen(self):
        self.reopened += 1
        self._is_open = True
        self._is_alive = True

    def close(self):
        self.closed += 1

    def cancel(self):
        self.cancelled += 1

    @property
    def is_open(self):
        return self._is_open

    @property
    def is_alive(self):
        return self._is_alive

    @property
    def level(self):
        return self._level

    @property
    def peak_since_begin(self):
        return self._peak

    @property
    def seconds_since_block(self):
        return 0.0 if self._is_alive else float("inf")


class FakeTray:
    """Значок в трее. set_state зовёт Shell_NotifyIcon и потому дорог."""

    def __init__(self):
        self.states: list[bool] = []
        self.refreshed = 0

    def set_state(self, active):
        self.states.append(active)

    def refresh(self):
        self.refreshed += 1


class SpyRoot:
    """Корень Tk, который запоминает обращения к себе.

    Нужен, чтобы поймать межпоточный root.after из обработчика хука: он не
    кладёт задание в очередь, а блокируется, пока поток Tk занят.
    """

    def __init__(self):
        self.calls: list[str] = []

    def after(self, *_args, **_kwargs):
        self.calls.append("after")
        return "spy-job"

    def after_cancel(self, *_args, **_kwargs):
        self.calls.append("after_cancel")


class FakeOverlay:
    """Запоминает всё, что человек увидел бы на плашке.

    Поколения показа считаются ровно как в настоящем оверлее: begin_session()
    открывает новое, а ok() и error() с номером прошлого до плашки не доходят
    и оседают в dropped. Без этого «увидел бы» было бы неправдой — главная
    поломка как раз в том, что до плашки доходило лишнее.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.levels: list[float] = []
        self.dropped: list[tuple[str, str]] = []
        self.session = 0

    @property
    def states(self) -> list[str]:
        return [state for state, _ in self.calls]

    @property
    def messages(self) -> str:
        return " | ".join(message for _, message in self.calls)

    def begin_session(self) -> int:
        self.session += 1
        return self.session

    def _stale(self, session) -> bool:
        return session is not None and session < self.session

    def recording(self):
        self.calls.append(("recording", ""))

    def silent(self):
        self.calls.append(("silent", ""))

    def sending(self, session=None):
        if self._stale(session):
            self.dropped.append(("sending", ""))
            return
        self.calls.append(("sending", ""))

    def refining(self, session=None):
        # Как у ok и error: «Правлю текст…» приходит из рабочего потока через
        # секунду после отпускания и само не гаснет, поэтому опоздавшее должно
        # отбрасываться так же.
        if self._stale(session):
            self.dropped.append(("refining", ""))
            return
        self.calls.append(("refining", ""))

    def ok(self, text="", session=None):
        if self._stale(session):
            self.dropped.append(("ok", text))
            return
        self.calls.append(("ok", text))

    def error(self, message, session=None):
        if self._stale(session):
            self.dropped.append(("error", message))
            return
        self.calls.append(("error", message))

    def hide(self):
        self.calls.append(("hide", ""))

    def level(self, value):
        self.levels.append(value)


class DeadRoot:
    """Корень Tk, который уже уничтожен: любой after падает."""

    def after(self, *_args, **_kwargs):
        raise tk.TclError('application has been destroyed')


def pump(root: tk.Tk, times: int = 4) -> None:
    """Прокрутить цикл Tk: очередь оверлея разбирается только в его потоке."""
    for _ in range(times):
        root.update()
        root.after(50, root.quit)
        root.mainloop()


def plate(overlay: Overlay) -> str:
    """Что написано на плашке прямо сейчас — то, что видит человек.

    Плашка рисуется картинкой, а не виджетом, поэтому подпись берётся из
    того же поля, из которого её берёт и отрисовка.
    """
    return overlay._text


def wait_for(predicate, timeout: float = 2.0) -> bool:
    """Ждёт результата работы фонового потока, не полагаясь на планировщик."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def drain(app) -> None:
    """Проигрывает то, что сделал бы рабочий поток: очередь -> история."""
    while True:
        try:
            item = app._jobs.get_nowait()
        except queue.Empty:
            return
        if item is not None:
            app._run_job(item)


def silence(seconds: float = 3.0, sample_rate: int = 16000) -> audio_mod.Capture:
    return audio_mod.Capture(
        samples=np.zeros(int(sample_rate * seconds), dtype=np.int16),
        sample_rate=sample_rate,
    )


def quiet_speech(seconds: float = 3.0, sample_rate: int = 16000) -> audio_mod.Capture:
    """Речь, не дотянувшая до порога: пик 0.05 при пороге 0.105."""
    t = np.linspace(0, seconds, int(sample_rate * seconds), endpoint=False)
    samples = (0.05 * np.sin(2 * np.pi * 200 * t) * 32767).astype(np.int16)
    return audio_mod.Capture(samples=samples, sample_rate=sample_rate)


def release(app, capture, held: float = 3.0) -> None:
    """Отпускание клавиши с заданной записью, включая работу рабочего потока."""
    app._recording = True
    app._press_at = time.monotonic() - held
    app._lang = app.cfg.language.main
    app.recorder = FakeRecorder(capture=capture)
    app._stop_dictation()
    drain(app)


class TestPressIsNeverIgnored:
    """Жалоба дословно: «нажимаю Ctrl, и вообще ничего не загорается».

    Человек не видит плашки, продолжает говорить и узнаёт правду только на
    отпускании — когда абзац уже потерян. Поэтому у нажатия ровно один
    молчаливый исход: повторное срабатывание при уже идущей записи.
    """

    @staticmethod
    def press(app, **recorder_kwargs) -> FakeOverlay:
        app.overlay = FakeOverlay()
        app.recorder = FakeRecorder(**recorder_kwargs)
        app._start_dictation("ru")
        return app.overlay

    def test_closed_stream_lights_the_plate_instead_of_nothing(self, app):
        overlay = self.press(app, is_open=False, is_alive=False)

        assert overlay.states == ["error"]
        assert "микрофон" in overlay.messages.lower()
        assert "повторите" in overlay.messages.lower()

    def test_closed_stream_does_not_pretend_to_record(self, app):
        self.press(app, is_open=False, is_alive=False)

        assert app._recording is False
        assert app.recorder.began == 0  # говорить в пустоту не предлагаем

    def test_closed_stream_raises_the_microphone(self, app):
        self.press(app, is_open=False, is_alive=False)
        assert wait_for(lambda: app.recorder.reopened == 1)

    def test_closed_stream_leaves_a_trace_in_history(self, app):
        self.press(app, is_open=False, is_alive=False)
        drain(app)

        record = app.history.last()
        assert record is not None
        assert "микрофон" in record.error.lower()
        assert record.text == ""

    def test_dead_stream_is_caught_before_the_phrase_not_after(self, app):
        # Поток открыт по документам, но блоков нет: раньше плашка честно
        # горела «Запись…», а правда всплывала только на отпускании.
        overlay = self.press(app, is_open=True, is_alive=False)

        assert overlay.states == ["error"]
        assert app.recorder.began == 0
        assert wait_for(lambda: app.recorder.reopened == 1)

    def test_the_reason_for_a_dead_stream_is_in_the_log(self, app, caplog):
        # Жалоба уходит на диск из фонового потока — того же, что поднимает
        # микрофон. Сообщение при этом не теряется, только приходит на
        # несколько миллисекунд позже, поэтому его тут дожидаются.
        with caplog.at_level(logging.ERROR):
            self.press(app, is_open=True, is_alive=False)

            assert wait_for(lambda: "микрофон" in caplog.text.lower())

    def test_a_just_opened_stream_is_given_its_grace(self, app):
        # Свежий поток отвечает is_alive=False, пока не придёт первый блок.
        # Ругаться на просыпающееся устройство нельзя — это ложная тревога.
        app._mic_opened_at = time.monotonic()
        overlay = self.press(app, is_open=True, is_alive=False)

        assert overlay.states == ["recording"]
        assert app.recorder.began == 1

    def test_an_ordinary_press_still_shows_recording(self, app):
        overlay = self.press(app, is_open=True, is_alive=True)

        assert overlay.states == ["recording"]
        assert app.recorder.began == 1
        assert app._recording is True

    def test_a_closed_stream_is_normal_without_hold_open(self, app):
        # При hold_open=false поток закрыт между диктовками нарочно,
        # его поднимает begin(). Ругаться тут не на что.
        app.cfg.audio.hold_open = False
        overlay = self.press(app, is_open=False, is_alive=False)

        assert overlay.states == ["recording"]
        assert app.recorder.began == 1

    def test_pause_answers_too(self, app):
        app.paused = True
        overlay = self.press(app)

        assert overlay.states == ["error"]
        assert "пауза" in overlay.messages.lower()

    def test_the_only_silent_case_is_a_repeat_press(self, app):
        app._recording = True
        overlay = self.press(app)

        # Плашка «Запись…» уже горит — говорить нечего.
        assert overlay.calls == []
        assert app.recorder.began == 0


class ExplodingRecorder(FakeRecorder):
    """Микрофон, у которого не выходит начать запись."""

    def begin(self):
        self.began += 1
        raise audio_mod.AudioError("устройство занято другим приложением")


class TestABrokenStartDoesNotDeafenTheProgram:
    """begin() упал — программа обязана остаться слышащей.

    Признак идущей записи выставлялся ДО begin(). Упавший begin() оставлял
    его выставленным навсегда, и каждое следующее нажатие уходило в
    молчаливый выход «уже пишем»: вечная глухота до перезапуска — ровно то
    состояние, от которого мы только что ушли.
    """

    @staticmethod
    def press(app, recorder) -> None:
        app.recorder = recorder
        app._start_dictation("ru")

    def test_a_failed_begin_does_not_leave_the_program_deaf(self, app):
        app.overlay = FakeOverlay()
        self.press(app, ExplodingRecorder())
        assert app._recording is False

        healthy = FakeRecorder()
        self.press(app, healthy)

        # Следующее нажатие начинает запись, а не проглатывается.
        assert app._recording is True
        assert healthy.began == 1
        assert app.overlay.states[-1] == "recording"

    def test_a_failed_begin_answers_the_press(self, app):
        app.overlay = FakeOverlay()
        self.press(app, ExplodingRecorder())

        assert app.overlay.states == ["error"]
        assert "повторите" in app.overlay.messages.lower()

    def test_a_failed_begin_leaves_a_trace_in_history(self, app):
        app.overlay = FakeOverlay()
        self.press(app, ExplodingRecorder())
        drain(app)

        record = app.history.last()
        assert record is not None
        assert record.error
        # К провайдеру не ходили — за это нажатие никто не платил.
        assert record.answered is False

    def test_a_failed_begin_raises_the_microphone(self, app):
        app.overlay = FakeOverlay()
        recorder = ExplodingRecorder()
        self.press(app, recorder)

        assert wait_for(lambda: recorder.reopened == 1)

    def test_the_recorder_is_not_left_capturing(self, app):
        # begin() успевает включить накопление кадров до падения: без отмены
        # рекордер копил бы звук в никуда до следующего нажатия.
        app.overlay = FakeOverlay()
        recorder = ExplodingRecorder()
        self.press(app, recorder)

        assert recorder.cancelled == 1

    def test_the_reason_reaches_the_log_from_a_background_thread(self, app, caplog):
        with caplog.at_level(logging.ERROR):
            app.overlay = FakeOverlay()
            self.press(app, ExplodingRecorder())

            assert wait_for(lambda: "begin()" in caplog.text)


class TestThePlateBelongsToTheCurrentDictation:
    """Плашка одна на все диктовки подряд, и хвост прошлой гасит новую.

    Результат приходит из рабочего потока и легко опаздывает: человек уже
    нажал клавишу снова, а на экране «Готово» или ошибка от предыдущей
    диктовки — с авто-скрытием на шесть секунд. Снаружи это неотличимо от
    «нажал, и вообще ничего не загорелось», то есть от той самой жалобы.

    Опоздавшим считается ТОЛЬКО показ плашки. Вставка, история и расходы
    обязаны отработать полностью: потерять продиктованное хуже, чем показать
    не ту плашку.
    """

    @staticmethod
    def press(app) -> None:
        """Нажатие клавиши диктовки на здоровом микрофоне."""
        app.recorder = FakeRecorder(capture=speech())
        app._start_dictation(app.cfg.language.main)

    @classmethod
    def dictate(cls, app):
        """Нажали и отпустили. Задание возвращается НЕ обработанным.

        Так воспроизводится главный случай: рабочий поток ещё возится с
        прошлой диктовкой, а человек уже начал следующую.
        """
        cls.press(app)
        app._stop_dictation()
        return app._jobs.get_nowait()

    # --- то, ради чего всё затевалось ------------------------------------------

    def test_the_previous_result_does_not_cover_the_new_recording(self, app):
        # Настоящий оверлей: проверяем не вызовы, а надпись на плашке.
        app.overlay = Overlay(app.root, enabled=True)

        job = self.dictate(app)  # первая диктовка ушла в работу
        self.press(app)  # человек нажал снова, не дождавшись
        pump(app.root)
        assert plate(app.overlay) == app.overlay.theme.state("recording").label

        app._run_job(job)  # и только теперь прошлая доложила «Готово»
        pump(app.root)

        assert plate(app.overlay) == app.overlay.theme.state("recording").label
        # Авто-скрытие от чужого «Готово» погасило бы плашку через 1.2 с
        # посреди новой записи — этого тоже быть не должно.
        assert app.overlay._hide_job is None

    def test_the_previous_text_is_pasted_and_recorded_anyway(self, app):
        # Оборотная сторона: плашку у прошлой диктовки отняли, но саму её
        # довели до конца. Потерять продиктованное было бы куда хуже.
        job = self.dictate(app)
        self.press(app)
        app._run_job(job)

        assert app.injector.pasted == ["привет из Docker "]
        assert app.history.last().text == "привет из Docker "
        assert app.history.last().error == ""

    def test_a_late_failure_does_not_cover_the_new_recording_either(self, app):
        app.provider = FakeProvider(TranscriptionError("сеть недоступна"))
        app.overlay = FakeOverlay()

        job = self.dictate(app)
        self.press(app)
        app._run_job(job)

        assert app.overlay.states == ["recording", "sending", "recording"]
        assert app.overlay.dropped == [("error", "сеть недоступна")]
        # Причина всё равно доехала до истории, иначе её было бы не найти.
        assert app.history.last().error == "сеть недоступна"

    def test_a_late_refinement_does_not_cover_the_new_recording(self, app):
        # «Правлю текст…» само не гаснет: чужая правка накрыла бы «Запись…»
        # до самого конца новой диктовки.
        app.refiner = FakeRefiner("правленый текст")
        app.overlay = FakeOverlay()

        job = self.dictate(app)
        self.press(app)
        app._run_job(job)

        assert "refining" not in app.overlay.states
        assert app.injector.pasted == ["правленый текст "]  # но правка отработала

    def test_a_press_that_could_not_start_owns_the_plate_too(self, app):
        # Нажатие на неготовый микрофон плашку зажигает — значит, и её хвост
        # прошлой диктовки затирать не должен.
        app.overlay = FakeOverlay()
        job = self.dictate(app)

        app.recorder = FakeRecorder(is_open=False, is_alive=False)
        app._start_dictation("ru")
        app._run_job(job)

        assert app.overlay.states[-1] == "error"
        assert "микрофон" in app.overlay.messages.lower()
        assert app.overlay.dropped == [("ok", "привет из Docker ")]

    # --- поколение берёт не только диктовка ------------------------------------

    def test_a_repeat_paste_does_not_cover_a_new_recording(self, app):
        """ctrl+alt+v, через полсекунды — клавиша диктовки.

        Отчёт повторной вставки прилетал поверх «Записи…»: поколение показа
        выдавалось только диктовкам, а все остальные говорили в плашку без
        номера и потому затирали что угодно.
        """
        app.overlay = FakeOverlay()
        self.press(app)
        app._stop_dictation()
        drain(app)  # в истории появилось, что вставлять
        app.overlay.calls.clear()

        pasted: list[str] = []

        def paste_while_a_new_dictation_starts(text, target_exe=None):
            self.press(app)  # человек нажал клавишу, не дождавшись отчёта
            pasted.append(text)
            return True, ""

        app.injector.paste = paste_while_a_new_dictation_starts
        app._paste_last()

        assert app.overlay.states == ["recording"]
        assert app.overlay.dropped == [("ok", "привет из Docker ")]
        # И главное: показ отняли, а вставку — нет.
        assert pasted == ["привет из Docker "]

    def test_a_repeat_paste_still_reports_when_nobody_interrupts(self, app):
        app.overlay = FakeOverlay()
        self.press(app)
        app._stop_dictation()
        drain(app)
        app.overlay.calls.clear()
        app.injector = FakeInjector()

        app._paste_last()

        assert app.overlay.states == ["ok"]
        assert app.injector.pasted == ["привет из Docker "]

    def test_pasting_from_the_history_window_does_not_cover_it_either(self, app):
        app.overlay = FakeOverlay()
        record = Record(ts=time.time(), text="старая запись")
        pasted: list[str] = []

        def paste_while_a_new_dictation_starts(text, target_exe=None):
            self.press(app)
            pasted.append(text)
            return False, "нет активного окна"

        app.injector.paste = paste_while_a_new_dictation_starts
        app._paste_record(record)

        assert app.overlay.states == ["recording"]
        assert app.overlay.dropped == [("error", "нет активного окна")]
        assert pasted == ["старая запись"]

    def test_copying_from_the_history_window_does_not_cover_it_either(self, app):
        app.overlay = FakeOverlay()
        record = Record(ts=time.time(), text="старая запись")
        copied: list[str] = []

        def copy_while_a_new_dictation_starts(text):
            self.press(app)
            copied.append(text)
            return True

        app.injector.put_in_clipboard = copy_while_a_new_dictation_starts
        app._copy_record(record)

        assert app.overlay.states == ["recording"]
        assert app.overlay.dropped == [("ok", "скопировано в буфер")]
        assert copied == ["старая запись"]

    def test_a_failed_reopen_does_not_cover_a_new_recording(self, app):
        # Переоткрытие идёт в фоне и занимает десятки миллисекунд: за это
        # время человек успевает нажать клавишу диктовки.
        app.overlay = FakeOverlay()

        class DeadRecorder(FakeRecorder):
            def reopen(self):
                TestThePlateBelongsToTheCurrentDictation.press(app)
                raise audio_mod.AudioError("микрофон не найден")

        app.recorder = DeadRecorder()
        app._try_reopen()

        assert app.overlay.states == ["recording"]
        assert app.overlay.dropped == [("error", "микрофон не найден")]

    # --- и то, что было раньше, никуда не делось -------------------------------

    def test_an_ordinary_dictation_shows_every_state_in_order(self, app):
        app.overlay = FakeOverlay()

        self.press(app)
        app._stop_dictation()
        drain(app)

        assert app.overlay.states == ["recording", "sending", "ok"]
        assert app.overlay.calls[-1] == ("ok", "привет из Docker ")

    def test_an_ordinary_dictation_on_the_real_plate(self, app):
        app.overlay = Overlay(app.root, enabled=True)

        self.press(app)
        app._stop_dictation()
        drain(app)
        pump(app.root)

        assert plate(app.overlay) == "привет из Docker"  # хвостовой пробел съеден
        assert app.overlay._hide_job is not None  # «Готово» гаснет само

    def test_two_dictations_in_a_row_each_get_their_own_result(self, app):
        app.overlay = FakeOverlay()

        first = self.dictate(app)
        app._run_job(first)
        second = self.dictate(app)
        app._run_job(second)

        assert app.overlay.states == [
            "recording", "sending", "ok",
            "recording", "sending", "ok",
        ]
        assert app.overlay.dropped == []


class TestThePlateComesBackDuringRecording:
    """Чужое сообщение накрыло идущую запись — плашка обязана вернуться.

    Сообщение с авто-скрытием гаснет само, а опрос в потоке Tk звал показ
    только при СМЕНЕ подсказки: при здоровом микрофоне она не меняется, и
    возвращать «Запись…» было некому. Человек договаривал абзац, глядя на
    пустое место, — та же жалоба, только с середины записи.
    """

    @staticmethod
    def recording(app, level=0.3, peak=None, held=0.0) -> FakeOverlay:
        """Идущая запись, начатая настоящим нажатием: поколение взято им."""
        app.overlay = FakeOverlay()
        app.recorder = FakeRecorder(capture=speech(), level=level, peak=peak)
        app._start_dictation(app.cfg.language.main)
        app._polled_recording = True
        app._recording_since = time.monotonic() - held
        app._hint = ""
        return app.overlay

    @staticmethod
    def steal(app) -> None:
        """Чужая операция говорит в плашку посреди записи."""
        app._copy_record(Record(ts=time.time(), text="старая запись"))

    def test_a_covered_recording_is_put_back(self, app):
        overlay = self.recording(app)
        self.steal(app)
        assert overlay.states == ["recording", "ok"]  # запись накрыли

        app._poll_once()
        assert overlay.states == ["recording", "ok", "recording"]

    def test_the_plate_is_not_redrawn_on_every_tick(self, app):
        # Возврат по событию, а не по таймеру: перерисовка каждые 80 мс
        # видна человеку как дрожь.
        overlay = self.recording(app)
        for _ in range(5):
            app._poll_once()

        assert overlay.states == ["recording"]  # только то, что зажгло нажатие

    def test_the_return_happens_once(self, app):
        overlay = self.recording(app)
        self.steal(app)
        app._poll_once()
        settled = list(overlay.states)

        for _ in range(5):
            app._poll_once()

        assert overlay.states == settled

    def test_the_returned_plate_tells_the_truth_of_the_moment(self, app):
        # Возвращается не «Запись…» вслепую, а то, что программа считает
        # правдой прямо сейчас: молчащий микрофон остаётся молчащим.
        app.cfg.audio.silence_peak = 0.105
        overlay = self.recording(app, level=0.004, peak=0.02, held=2.0)
        app._poll_once()
        assert overlay.states == ["recording", "silent"]

        self.steal(app)
        app._poll_once()

        assert overlay.states == ["recording", "silent", "ok", "silent"]

    def test_the_message_of_the_thief_stops_arriving(self, app):
        # Плашку забирают назад вместе с поколением: отчёт чужой операции,
        # который ещё не долетел, до плашки уже не дойдёт.
        overlay = self.recording(app)
        session = app._take_plate()  # чужая операция началась
        app._poll_once()  # запись вернула плашку себе
        overlay.ok("вставлено", session)  # и только теперь та отчиталась

        assert overlay.states == ["recording", "recording"]
        assert overlay.dropped == [("ok", "вставлено")]

    def test_nothing_is_returned_between_dictations(self, app):
        # Между диктовками плашка чужая по праву: возвращать нечего.
        overlay = self.recording(app)
        app._recording = False
        app._plate_session = app._session + 1
        app._poll_once()

        assert overlay.states == ["recording"]


class TestThePlateDoesNotLie:
    """Что видно на плашке, пока клавишу держат.

    Опрос живёт в потоке Tk сам по себе и только смотрит, идёт ли запись,
    поэтому тесты зовут его шаг напрямую — ровно так же, как его позовёт
    root.after().
    """

    @staticmethod
    def recording(app, level=0.0, peak=None, is_alive=True, held=0.0) -> FakeOverlay:
        """Идущая запись, начавшаяся `held` секунд назад, и чистая плашка."""
        app.overlay = FakeOverlay()
        app.recorder = FakeRecorder(level=level, peak=peak, is_alive=is_alive)
        app._recording = True
        app._polled_recording = True
        app._recording_since = time.monotonic() - held
        app._hint = ""
        return app.overlay

    # --- полоска уровня --------------------------------------------------------

    def test_the_level_reaches_the_plate(self, app):
        overlay = self.recording(app, level=0.42)
        app._poll_once()

        assert overlay.levels == [0.42]

    def test_nothing_is_drawn_between_dictations(self, app):
        overlay = self.recording(app, level=0.42)
        app._recording = False
        app._poll_once()

        assert overlay.levels == []
        assert overlay.calls == []

    # --- мёртвый поток ---------------------------------------------------------

    def test_a_dead_stream_is_seen_at_once_not_after_the_phrase(self, app):
        # Воспроизведение из жалобы: поток убит через 1.1 с после нажатия,
        # человек «говорит» ещё четыре секунды. Уровень застыл на 0.350 —
        # ветка тишины по громкости не сработает уже никогда, и плашка всё
        # это время горела «Запись…» с полной неподвижной полоской.
        overlay = self.recording(app, level=0.350, is_alive=False, held=4.0)
        app._poll_once()

        assert overlay.states == ["error"]
        assert "не отдаёт звук" in overlay.messages

    def test_a_dead_stream_is_not_the_same_as_a_quiet_one(self, app):
        dead = self.recording(app, level=0.350, is_alive=False, held=4.0)
        app._poll_once()

        quiet = self.recording(app, level=0.001, held=4.0)
        app._poll_once()

        # Молчит — «слышу тишину, говорите громче». Мёртв — «не слышу вообще
        # ничего», и громкость тут не поможет. Для человека разница
        # существенная, поэтому и сообщения обязаны быть разными.
        assert dead.states == ["error"]
        assert quiet.states == ["silent"]
        assert dead.messages != quiet.messages

    def test_a_frozen_level_is_not_drawn_as_a_full_bar(self, app):
        # Неподвижная полная полоска врёт убедительнее любой надписи.
        overlay = self.recording(app, level=0.350, is_alive=False, held=4.0)
        app._poll_once()

        assert overlay.levels == [0.0]

    def test_a_waking_device_is_not_called_dead(self, app):
        # При hold_open=false микрофон поднимается уже ПОСЛЕ нажатия и первые
        # десятки миллисекунд честно не отдаёт блоков.
        overlay = self.recording(app, is_alive=False, held=0.1)
        app._poll_once()

        assert overlay.calls == []

    def test_the_dead_hint_survives_a_long_hold(self, app):
        # Плашка с ошибкой гаснет сама через шесть секунд, а держать могут
        # дольше: погасшая плашка — ровно та жалоба, с которой всё началось.
        overlay = self.recording(app, level=0.35, is_alive=False, held=4.0)
        app._poll_once()
        app._poll_once()  # подряд — не мигаем
        assert overlay.states == ["error"]

        app._hint_at = time.monotonic() - app.DEAD_HINT_REPEAT_S
        app._poll_once()
        assert overlay.states == ["error", "error"]

    def test_a_revived_stream_returns_the_plate_to_recording(self, app):
        overlay = self.recording(app, level=0.35, is_alive=False, held=4.0)
        app._poll_once()

        app.recorder = FakeRecorder(level=0.35)
        app._poll_once()

        assert overlay.states == ["error", "recording"]

    # --- «микрофон молчит» -----------------------------------------------------

    def test_pauses_between_words_are_not_silence(self, app):
        # Порог рассчитан на пик ВСЕЙ записи. Сравнение с пиком одного 32-мс
        # блока кричало «молчит» в каждой паузе между словами: на живой
        # записи с пиком 0.101 — 83% времени.
        app.cfg.audio.silence_peak = 0.105
        overlay = self.recording(app, level=0.30, peak=0.30, held=5.0)
        for level in (0.30, 0.02, 0.0, 0.004, 0.30, 0.01):
            app.recorder = FakeRecorder(level=level, peak=0.30)
            app._poll_once()

        assert "silent" not in overlay.states
        # Полоску по-прежнему рисует мгновенный уровень — она для того и есть.
        assert overlay.levels == [0.30, 0.02, 0.0, 0.004, 0.30, 0.01]

    def test_real_silence_is_still_reported(self, app):
        app.cfg.audio.silence_peak = 0.105
        overlay = self.recording(
            app, level=0.004, peak=0.02, held=2 * app.SILENCE_HINT_S
        )
        app._poll_once()

        assert overlay.states == ["silent"]

    def test_a_short_pause_at_the_start_is_not_silence_yet(self, app):
        overlay = self.recording(app, level=0.0, held=0.2)
        app._poll_once()

        assert overlay.calls == []

    def test_the_hint_promises_exactly_what_the_release_will_decide(self, app):
        """«Молчит» означает ровно одно: отпустишь сейчас — запись отвергнут."""
        app.cfg.audio.silence_peak = 0.105
        quiet = quiet_speech()
        overlay = self.recording(
            app, level=0.02, peak=audio_mod.peak_level(quiet), held=2.0
        )
        app._poll_once()
        assert overlay.states == ["silent"]

        release(app, quiet)
        assert "тишина" in app.history.last().error.lower()

    def test_speech_above_the_threshold_is_never_called_silence(self, app):
        """И обратное: раз «молчит» не показали — запись пройдёт."""
        app.cfg.audio.silence_peak = 0.105
        loud = speech()
        overlay = self.recording(
            app, level=0.004, peak=audio_mod.peak_level(loud), held=9.0
        )
        for _ in range(20):
            app._poll_once()
        assert "silent" not in overlay.states

        release(app, loud)
        assert app.injector.pasted == ["привет из Docker "]

    # --- сам цикл --------------------------------------------------------------

    def test_a_tick_schedules_the_next_one(self, app):
        # Цикл держит себя сам: включать и выключать его из чужого потока
        # нельзя, а значит некому и перезапустить.
        app._poll()
        assert app._poll_job is not None

        app._stop_polling()
        assert app._poll_job is None

    def test_the_loop_is_started_once(self, app):
        app._start_polling()
        first = app._poll_job
        app._start_polling()
        assert app._poll_job is first  # второй копии цикла не завелось

        app._stop_polling()

    def test_a_destroyed_root_does_not_break_the_tick(self, app):
        overlay = self.recording(app, level=0.3)
        app.root = DeadRoot()

        app._poll()

        assert app._poll_job is None
        assert overlay.levels == [0.3]


class TestTheHookTouchesNobodyElse:
    """Нажатие и отпускание выполняются в потоке pynput и обязаны быть мгновенными.

    Медленный низкоуровневый хук Windows сначала тормозит всю клавиатуру, а
    потом молча снимается системой. Поэтому из хука нельзя ни ходить в поток
    Tk (root.after оттуда не кладёт задание в очередь, а БЛОКИРУЕТСЯ, пока Tk
    занят: замер — до 48 мс на обновлении окна истории), ни трогать значок в
    трее (set_state зовёт Shell_NotifyIcon).
    """

    @staticmethod
    def press(app) -> None:
        app.overlay = FakeOverlay()
        app.recorder = FakeRecorder()
        app._start_dictation("ru")

    def test_a_press_does_not_reach_into_tk(self, app):
        app.root = SpyRoot()
        self.press(app)

        assert app.root.calls == []
        assert app.overlay.states == ["recording"]  # плашка всё равно горит

    def test_a_release_does_not_reach_into_tk(self, app):
        app.root = SpyRoot()
        release(app, speech())

        assert app.root.calls == []

    def test_a_press_does_not_call_shell_notify_icon(self, app):
        app.tray = FakeTray()
        self.press(app)
        assert app.tray.states == []

        app._poll_once()  # красит значок опрос в потоке Tk — и красит
        assert app.tray.states == [True]

    def test_a_release_does_not_call_shell_notify_icon(self, app):
        app.tray = FakeTray()
        self.press(app)
        app._poll_once()

        release(app, speech())
        assert app.tray.states == [True]

        app._poll_once()
        assert app.tray.states == [True, False]

    def test_a_press_does_not_write_to_the_log_from_the_hook(self, app, monkeypatch):
        """log.error пишет на диск синхронно — в потоке хука ему не место.

        Замер на живом микрофоне: неудачное нажатие стоило медиану 0.97 мс
        при максимуме 4.83 мс, а здоровое — 0.066 мс. Само сообщение никуда
        не девается: оно уходит тем же текстом и тем же уровнем, но из
        фонового потока, вместе с переоткрытием микрофона.
        """
        from whisperfree import __main__ as main_mod

        writers: list[threading.Thread] = []
        monkeypatch.setattr(
            main_mod.log,
            "error",
            lambda *args, **kwargs: writers.append(threading.current_thread()),
        )
        app.overlay = FakeOverlay()
        app.recorder = FakeRecorder(is_open=False, is_alive=False)

        app._start_dictation("ru")

        # Плашка зажглась сразу, а на диск сходил кто-то другой.
        assert app.overlay.states == ["error"]
        assert wait_for(lambda: writers)
        assert writers[0] is not threading.current_thread()


class TestShutdownTidiesUp:
    def test_shutdown_clears_the_recording_flag_and_the_pending_tick(self, app):
        app.recorder = FakeRecorder()
        app._recording = True
        app._start_polling()
        assert app._poll_job is not None

        app._shutdown()

        assert app._recording is False
        assert app._poll_job is None

    def test_a_tick_after_shutdown_does_not_come_back(self, app):
        app.recorder = FakeRecorder()
        app._shutdown()
        app._poll()

        assert app._poll_job is None


class TestFailuresLeaveATrace:
    """«И в истории не остаётся записи» — вторая половина жалобы."""

    def test_silence_is_recorded_with_its_reason(self, app):
        release(app, silence())

        record = app.history.last()
        assert record is not None
        assert "тишина" in record.error.lower()
        assert record.text == ""
        assert record.audio_sec == pytest.approx(3.0, abs=0.01)

    def test_a_dead_microphone_is_recorded(self, app, monkeypatch):
        monkeypatch.setattr(app, "_try_reopen", lambda: None)
        capture = audio_mod.Capture(
            samples=np.zeros(4160, dtype=np.int16), sample_rate=16000, stalled=True
        )
        release(app, capture)

        record = app.history.last()
        assert record is not None
        assert "микрофон" in record.error.lower()

    def test_a_short_tap_leaves_no_trace(self, app):
        # Случайных касаний бывает много; в истории они были бы мусором,
        # в котором не найти настоящую неудачу.
        release(app, silence(seconds=0.1))

        assert app.history.last() is None
        assert app._jobs.empty()

    def test_a_provider_failure_is_recorded(self, app):
        app.provider = FakeProvider(TranscriptionError("сеть недоступна"))
        app._process(job(app))

        assert app.history.last().error == "сеть недоступна"

    def test_a_failed_paste_reaches_the_file_with_its_reason(self, app):
        # Причина провала выясняется только после попытки вставки, а в историю
        # мы пишем ДО неё — иначе падение на вставке съело бы продиктованное.
        # Раньше дописанная причина оставалась в памяти, и на диск уходила
        # пустая: до перезапуска запись считалась неудачей, после —
        # полноценной диктовкой, и счётчик расходов менялся сам по себе.
        app.injector = FakeInjector(ok=False, reason="нет активного окна")
        app._process(job(app))

        saved = json.loads(
            app.history.path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert saved["error"] == "нет активного окна"
        assert saved["text"] == "привет из Docker "  # текст на месте

    def test_the_cost_counter_survives_a_restart(self, app):
        app.injector = FakeInjector(ok=False, reason="нет активного окна")
        app._process(job(app))

        prices = (app.cfg.provider.price_per_hour_usd, app.cfg.provider.min_billed_seconds)
        after_restart = History(app.history.path, app.cfg.history)
        assert after_restart.usage(*prices) == app.history.usage(*prices)

    def test_failures_do_not_inflate_the_cost_counter(self, app):
        release(app, silence())
        app._process(job(app))

        stats = app.history.usage(
            app.cfg.provider.price_per_hour_usd, app.cfg.provider.min_billed_seconds
        )
        # Две записи в истории, но платим только за одну удачную.
        assert len(app.history.records) == 2
        assert stats["today_count"] == 1


class TestPaidDictationIsNeverLost:
    """Оплаченная диктовка не должна исчезать при сбое посреди обработки.

    Признак оплаты ставится сразу после ответа провайдера, а в журнал запись
    попадает много позже — после чистки, правки и определения окна. Исключение
    между этими точками уносило в лог трассировку, а саму диктовку — никуда:
    деньги списаны, текста нет, следа нет. Поэтому запись отдаётся рабочему
    потоку через job.pending, и он спасает её в своём обработчике исключений.
    """

    def test_a_crash_after_the_answer_leaves_the_record_to_be_rescued(self, app):
        def explode(text):
            raise RuntimeError("что-то пошло не так внутри")

        app.postprocessor.clean = explode
        task = job(app)

        with pytest.raises(RuntimeError):
            app._process(task)

        assert task.pending is not None, "оплаченная диктовка потеряна"
        assert task.pending.answered, "потерян признак оплаты"

    def test_the_rescue_writes_it_to_the_journal(self, app, caplog):
        task = job(app)
        task.pending = Record(ts=0.0, text="", answered=True, audio_sec=3.0)

        with caplog.at_level(logging.ERROR):
            app._rescue(task)

        saved = app.history.recent(limit=5)
        assert saved, "спасённая диктовка не попала в историю"
        assert saved[0].answered
        assert saved[0].error, "не записана причина"
        assert task.pending is None, "спасать повторно нечего"

    def test_the_rescue_keeps_a_reason_that_is_already_known(self, app):
        task = job(app)
        task.pending = Record(ts=0.0, text="", answered=True, error="окно закрылось")
        app._rescue(task)
        assert app.history.recent(limit=1)[0].error == "окно закрылось"

    def test_nothing_to_rescue_is_not_an_error(self, app):
        task = job(app)
        app._rescue(task)
        assert app.history.recent(limit=5) == []

    def test_a_normal_dictation_leaves_nothing_pending(self, app):
        task = job(app)
        app._process(task)
        assert task.pending is None

    def test_a_dropped_hallucination_leaves_nothing_pending(self, app):
        # Запись уже в журнале, спасать нечего — иначе она попала бы туда дважды.
        app.provider = FakeProvider("Субтитры сделал DimaTorzok")
        task = job(app)
        app._process(task)
        assert task.pending is None


class TestRepeatPasteLeavesATrace:
    def test_a_failed_repeat_paste_is_logged_even_if_the_plate_is_taken(
        self, app, caplog
    ):
        # Отчёт на плашке отбрасывается, если человек уже начал новую диктовку.
        # Тогда лог остаётся единственным местом, где видна причина.
        app.injector = FakeInjector(ok=False, reason="окно не приняло вставку")
        app.history.add(Record(ts=0.0, text="что-то было"))

        with caplog.at_level(logging.WARNING):
            app._paste_last()

        assert "повторная вставка не удалась" in caplog.text
        assert "окно не приняло вставку" in caplog.text


class TestRefinementDoesNotCoverTheNextRecording:
    def test_a_late_refining_is_dropped(self, app):
        app.overlay = FakeOverlay()
        # «Правлю текст…» приходит из рабочего потока через секунду после
        # отпускания и само не гаснет: без номера показа оно накрыло бы
        # «Запись…» следующей диктовки до самого её конца.
        stale = app.overlay.begin_session()
        app.overlay.begin_session()

        app.overlay.refining(stale)

        assert "refining" not in app.overlay.states
        assert ("refining", "") in app.overlay.dropped

    def test_the_current_refining_is_shown(self, app):
        app.overlay = FakeOverlay()
        current = app.overlay.begin_session()
        app.overlay.refining(current)
        assert "refining" in app.overlay.states


class TestLearningFromCorrections:
    """Обучение на правках: от нажатия хоткея до изменившегося текста."""

    def correct(self, app, monkeypatch, selection):
        """Изображает «выделил исправленный текст и нажал хоткей»."""
        from whisperfree import inject as inject_mod

        monkeypatch.setattr(
            inject_mod, "copy_selection", lambda **kwargs: selection
        )
        app._learn_now(0)

    def test_correction_is_matched_against_history_and_learned(
        self, app, monkeypatch
    ):
        # «редис», а не «докер»: докер в словаре замен из конфига уже есть,
        # и правильный текст править было бы нечем.
        app.provider = FakeProvider("поднял редис вчера")
        app._process(job(app))
        pasted = app.injector.pasted[-1]
        assert "редис" in pasted, "конфиг уже исправил слово, учиться нечему"

        self.correct(app, monkeypatch, pasted.replace("редис", "Redis"))
        assert [item.right for item in app.lexicon.lessons] == ["Redis"]
        assert app.lexicon.lessons[0].wrong == "редис"

    def test_foreign_selection_teaches_nothing(self, app, monkeypatch):
        app.provider = FakeProvider("поставил докер вчера")
        app._process(job(app))

        self.correct(app, monkeypatch, "Совершенно посторонний текст из письма.")
        assert app.lexicon.lessons == []

    def test_empty_selection_teaches_nothing(self, app, monkeypatch):
        app._process(job(app))
        self.correct(app, monkeypatch, "")
        assert app.lexicon.lessons == []

    def test_learned_term_reaches_the_recognition_prompt(self, app):
        app.lexicon.learn("поднял редис", "поднял Redis")
        app._process(job(app))
        assert "Redis" in app.provider.requests[-1].prompt

    def test_confirmed_correction_changes_the_pasted_text(self, app, monkeypatch):
        # Первая правка — только подсказка, вторая делает замену, и текст
        # начинает выходить правильным сам.
        app.provider = FakeProvider("поднял редис вчера")
        for _ in range(2):
            app._process(job(app))
            pasted = app.injector.pasted[-1]
            self.correct(app, monkeypatch, pasted.replace("редис", "Redis"))

        app._process(job(app))
        assert "Redis" in app.injector.pasted[-1]
        assert "редис" not in app.injector.pasted[-1]

    def test_model_mistakes_are_reported_to_the_editor(self, app):
        app.lexicon.learn("поднял редис", "поднял Redis", raw="поднял Redis")
        app._apply_lexicon()
        assert "Redis" in app.refiner.prompt
        # Исходная инструкция при этом никуда не делась.
        assert "редактор расшифровки" in app.refiner.prompt

    def test_editor_prompt_does_not_grow_with_every_lesson(self, app):
        app.lexicon.learn("поднял редис", "поднял Redis", raw="поднял Redis")
        app._apply_lexicon()
        once = len(app.refiner.prompt)
        app._apply_lexicon()
        app._apply_lexicon()
        assert len(app.refiner.prompt) == once, "список написаний приписался дважды"

    def test_forgetting_removes_the_rule_at_once(self, app, monkeypatch):
        app.lexicon.learn("поднял редис", "поднял Redis")
        app.lexicon.learn("снёс редис", "снёс Redis")
        app._apply_lexicon()
        app.provider = FakeProvider("поднял редис вчера")
        app._process(job(app))
        assert "Redis" in app.injector.pasted[-1]

        app.lexicon.forget("редис", "Redis")
        app._apply_lexicon()
        app._process(job(app))
        assert "редис" in app.injector.pasted[-1], "забытое правило продолжает работать"

    def test_prompt_stays_within_the_provider_limit(self, app):
        from whisperfree import lexicon as lexicon_mod
        from whisperfree.__main__ import WHISPER_PROMPT_TOKENS

        # Затравка из конфига уже длинная, а выученного пусть будет много.
        for index in range(60):
            app.lexicon.learn(f"термин{index} тут", f"Term{index} тут")
        prompt = app._stt_prompt("ru")
        assert lexicon_mod.estimate_tokens(prompt) <= WHISPER_PROMPT_TOKENS

    def test_learning_can_be_switched_off_entirely(self, app, monkeypatch):
        app.cfg.lexicon.enabled = False
        app.provider = FakeProvider("поставил докер вчера")
        app._process(job(app))
        self.correct(
            app, monkeypatch, app.injector.pasted[-1].replace("докер", "Docker")
        )
        assert app.lexicon.lessons == []
