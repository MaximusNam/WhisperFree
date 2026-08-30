"""Сквозной прогон конвейера с подставным провайдером и подставной вставкой.

Проверяется то, ради чего всё писалось: звук доезжает до провайдера с нужными
параметрами, текст проходит доводку, попадает в историю ДО попытки вставки и
вставляется. И то, что при любом сбое продиктованное остаётся доступным.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import pytest
import tomlkit

from whisperfree import audio as audio_mod
from whisperfree import config as config_mod
from whisperfree.config import DEFAULT_CONFIG_TOML, parse_config
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


class TestGarbageIsDropped:
    def test_silence_hallucination_never_reaches_the_window(self, app):
        app.provider = FakeProvider("Субтитры сделал DimaTorzok")
        app._process(job(app))

        assert app.injector.pasted == []
        assert app.history.last() is None

    def test_empty_transcription_is_ignored(self, app):
        app.provider = FakeProvider("   ")
        app._process(job(app))
        assert app.injector.pasted == []


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
    def __init__(self, capture):
        self.capture = capture
        self.reopened = 0

    def end(self):
        return self.capture

    def reopen(self):
        self.reopened += 1

    @property
    def is_open(self):
        return True
