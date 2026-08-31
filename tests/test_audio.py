"""Захват звука: выбор устройства, пре-ролл, кодирование.

Настоящий микрофон здесь не открывается — только логика вокруг него.
"""

from __future__ import annotations

import io
import logging

import numpy as np
import pytest
import soundfile as sf

from whisperfree import audio as audio_mod
from whisperfree.audio import AudioError, Capture, encode, parse_device, peak_level, resolve_device


class FakeDefault:
    hostapi = 0
    device = [1, 4]


# Одна и та же железка видна под четырьмя звуковыми API — ровно как на машине,
# где это писалось: «Микрофон (Logitech StreamCam)» с индексами 2, 8, 15, 29.
DEVICES = [
    {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
    {"name": "Line (3- Steinberg UR22C)", "max_input_channels": 2, "hostapi": 0},
    {"name": "Микрофон (Logitech StreamCam)", "max_input_channels": 2, "hostapi": 0},
    {"name": "Динамики (Realtek)", "max_input_channels": 0, "hostapi": 0},
    {"name": "Микрофон (Logitech StreamCam)", "max_input_channels": 2, "hostapi": 1},
    {"name": "Микрофон (Logitech StreamCam)", "max_input_channels": 2, "hostapi": 2},
    {"name": "Микрофон (Logitech StreamCam)", "max_input_channels": 2, "hostapi": 3},
]

HOSTAPIS = [
    {"name": "MME", "default_input_device": 1},
    {"name": "Windows DirectSound", "default_input_device": 4},
    {"name": "Windows WASAPI", "default_input_device": 5},
    {"name": "Windows WDM-KS", "default_input_device": 6},
]


@pytest.fixture
def fake_devices(monkeypatch):
    monkeypatch.setattr(audio_mod.sd, "query_devices", lambda: DEVICES)
    monkeypatch.setattr(audio_mod.sd, "query_hostapis", lambda: HOSTAPIS)
    monkeypatch.setattr(audio_mod.sd, "default", FakeDefault())


class TestParseDevice:
    def test_empty_means_system_default(self):
        assert parse_device("") is None
        assert parse_device(None) is None
        assert parse_device("   ") is None

    def test_digits_become_an_index(self):
        assert parse_device("2") == 2
        assert parse_device(2) == 2

    def test_name_stays_a_string(self):
        assert parse_device("Logitech StreamCam") == "Logitech StreamCam"


class TestResolveDevice:
    def test_index_passes_through(self, fake_devices):
        assert resolve_device(2) == 2
        assert resolve_device("2") == 2

    def test_empty_stays_none(self, fake_devices):
        assert resolve_device("") is None

    def test_ambiguous_name_picks_the_default_host_api(self, fake_devices):
        # sounddevice на такое имя отвечает «Multiple input devices found»,
        # поэтому выбираем сами: MME здесь — API по умолчанию.
        assert resolve_device("Logitech StreamCam") == 2

    def test_partial_name_is_enough(self, fake_devices):
        assert resolve_device("StreamCam") == 2

    def test_match_is_case_insensitive(self, fake_devices):
        assert resolve_device("streamcam") == 2

    def test_unique_name_resolves_directly(self, fake_devices):
        assert resolve_device("Steinberg") == 1

    def test_output_only_devices_are_skipped(self, fake_devices):
        with pytest.raises(AudioError):
            resolve_device("Динамики")

    def test_unknown_name_lists_what_is_available(self, fake_devices):
        with pytest.raises(AudioError, match="не найден"):
            resolve_device("Blue Yeti")

    def test_wasapi_wins_when_default_api_has_no_match(self, monkeypatch, fake_devices):
        class OtherDefault:
            hostapi = 9  # такого API среди совпадений нет
            device = [1, 4]

        monkeypatch.setattr(audio_mod.sd, "default", OtherDefault())
        assert resolve_device("Logitech StreamCam") == 5  # индекс WASAPI


class TestCapture:
    def test_duration_from_sample_count(self):
        capture = Capture(samples=np.zeros(32000, dtype=np.int16), sample_rate=16000)
        assert capture.duration_s == pytest.approx(2.0)

    def test_empty_capture_has_zero_duration(self):
        assert Capture(samples=np.zeros(0, dtype=np.int16), sample_rate=16000).duration_s == 0.0

    def test_peak_level_of_silence_is_below_the_threshold(self):
        capture = Capture(samples=np.zeros(16000, dtype=np.int16), sample_rate=16000)
        assert peak_level(capture) == 0.0

    def test_peak_level_of_loud_signal(self):
        samples = np.full(1000, 16384, dtype=np.int16)
        assert peak_level(Capture(samples=samples, sample_rate=16000)) == pytest.approx(0.5)


class TestEncode:
    @pytest.fixture
    def tone(self):
        t = np.linspace(0, 1.0, 16000, endpoint=False)
        return Capture(
            samples=(0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16),
            sample_rate=16000,
        )

    def test_flac_is_lossless(self, tone):
        data, name = encode(tone, "flac")
        assert name == "speech.flac"

        back, sr = sf.read(io.BytesIO(data), dtype="int16")
        assert sr == 16000
        assert np.array_equal(back, tone.samples)

    def test_flac_is_much_smaller_than_wav(self, tone):
        # Меньше данных на отправку — меньше задержка до вставки.
        flac, _ = encode(tone, "flac")
        wav, _ = encode(tone, "wav")
        assert len(flac) < len(wav) * 0.6

    def test_unknown_format_falls_back_to_flac(self, tone):
        assert encode(tone, "mp3")[1] == "speech.flac"
        assert encode(tone, "")[1] == "speech.flac"


class TestPreroll:
    def test_ring_buffer_holds_the_configured_lead_in(self):
        recorder = audio_mod.Recorder(sample_rate=16000, preroll_ms=250)
        expected = round(0.250 * 16000 / audio_mod.BLOCKSIZE)
        assert recorder._ring.maxlen == expected

    def test_preroll_audio_lands_in_the_capture(self):
        """Звук, сказанный до нажатия клавиши, должен попасть в запись —
        иначе первый слог систематически срезается."""
        recorder = audio_mod.Recorder(sample_rate=16000, preroll_ms=250)
        block = np.full((audio_mod.BLOCKSIZE, 1), 1000, dtype=np.int16)

        for _ in range(10):  # микрофон пишет в кольцевой буфер ещё до нажатия
            recorder._callback(block, audio_mod.BLOCKSIZE, None, None)

        recorder.begin()
        recorder._callback(block, audio_mod.BLOCKSIZE, None, None)
        capture = recorder.end()

        preroll_blocks = recorder._ring.maxlen
        assert len(capture.samples) == (preroll_blocks + 1) * audio_mod.BLOCKSIZE

    def test_capture_is_truncated_at_the_limit(self):
        recorder = audio_mod.Recorder(sample_rate=16000, preroll_ms=0, max_seconds=1)
        block = np.full((audio_mod.BLOCKSIZE, 1), 500, dtype=np.int16)

        recorder.begin()
        for _ in range(100):  # заметно больше секунды
            recorder._callback(block, audio_mod.BLOCKSIZE, None, None)
        capture = recorder.end()

        assert capture.truncated
        assert len(capture.samples) <= 16000

    def test_cancel_throws_the_recording_away(self):
        recorder = audio_mod.Recorder(sample_rate=16000, preroll_ms=0)
        block = np.full((audio_mod.BLOCKSIZE, 1), 500, dtype=np.int16)

        recorder.begin()
        recorder._callback(block, audio_mod.BLOCKSIZE, None, None)
        recorder.cancel()

        assert len(recorder.end().samples) == 0

    def test_blocks_are_ignored_until_recording_starts(self):
        recorder = audio_mod.Recorder(sample_rate=16000, preroll_ms=0)
        block = np.full((audio_mod.BLOCKSIZE, 1), 500, dtype=np.int16)

        recorder._callback(block, audio_mod.BLOCKSIZE, None, None)
        assert len(recorder.end().samples) == 0


class TestHoldOpen:
    """Микрофон, занятый постоянно, Windows показывает горящим значком в трее.

    hold_open=false отпускает устройство между диктовками ценой пре-ролла.
    """

    def test_hold_open_keeps_the_stream(self, monkeypatch):
        opened, closed = [], []
        _patch_stream(monkeypatch, opened, closed)

        recorder = audio_mod.Recorder(preroll_ms=0, hold_open=True)
        recorder.open()
        recorder.begin()
        recorder.end()

        assert len(opened) == 1
        assert not closed  # устройство осталось занятым, как и задумано

    def test_release_mode_frees_the_device_after_each_dictation(self, monkeypatch):
        opened, closed = [], []
        _patch_stream(monkeypatch, opened, closed)

        recorder = audio_mod.Recorder(preroll_ms=0, hold_open=False)
        recorder.begin()
        _join_mic_threads()
        assert recorder.is_open

        recorder.end()
        assert not recorder.is_open
        assert len(closed) == 1

    def test_short_press_does_not_leave_the_microphone_open(self, monkeypatch):
        """Нажатие может кончиться раньше, чем устройство поднимется.

        Без проверки «нас ещё ждут» поток открылся бы уже после end() и
        микрофон остался бы занятым навсегда.
        """
        opened, closed = [], []
        _patch_stream(monkeypatch, opened, closed)

        recorder = audio_mod.Recorder(preroll_ms=0, hold_open=False)
        recorder.begin()
        recorder.end()  # отпустили раньше, чем поток успел открыться
        _join_mic_threads()

        assert not recorder.is_open
        assert recorder._stream is None


class _FakeStream:
    def __init__(self, opened, closed, **kwargs):
        self._opened = opened
        self._closed = closed
        self.active = False

    def start(self):
        self.active = True
        self._opened.append(self)

    def stop(self):
        self.active = False

    def close(self):
        self._closed.append(self)


def _patch_stream(monkeypatch, opened, closed):
    monkeypatch.setattr(
        audio_mod.sd,
        "InputStream",
        lambda **kwargs: _FakeStream(opened, closed, **kwargs),
    )


def _join_mic_threads(timeout: float = 2.0) -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name == "mic-open":
            thread.join(timeout)


class _Clock:
    """Управляемые часы: тест двигает их руками.

    Раньше время подменялось списком значений, но теперь monotonic() спрашивает
    ещё и колбэк микрофона — отмечает, когда пришёл блок. Число вызовов
    перестало быть свойством теста, поэтому считать их больше нельзя.
    """

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fake_clock(monkeypatch, now: float = 100.0) -> _Clock:
    clock = _Clock(now)
    monkeypatch.setattr(audio_mod.time, "monotonic", clock)
    return clock


class TestNormalize:
    """Тихий микрофон — не только риск не пройти порог тишины: на слабом
    сигнале распознавание ошибается заметно чаще."""

    def quiet(self, peak: float, seconds: float = 1.0) -> Capture:
        t = np.linspace(0, seconds, int(16000 * seconds), endpoint=False)
        samples = (peak * np.sin(2 * np.pi * 200 * t) * 32767).astype(np.int16)
        return Capture(samples=samples, sample_rate=16000)

    def test_very_quiet_recording_is_pulled_up_but_capped(self):
        # Замер на StreamCam до прибавления громкости: речь дала 0.027.
        # Усиление упирается в потолок — до цели не дотягиваем намеренно,
        # потому что на таком слабом входе шум усиливается вместе с речью.
        capture, gain = audio_mod.normalize(self.quiet(0.027))

        assert gain == audio_mod.NORMALIZE_MAX_GAIN
        assert peak_level(capture) > 0.2  # всё равно на порядок громче исходного

    def test_moderately_quiet_recording_reaches_the_target(self):
        capture, gain = audio_mod.normalize(self.quiet(0.1))

        assert gain == pytest.approx(7.0, rel=0.05)
        assert peak_level(capture) == pytest.approx(audio_mod.NORMALIZE_TARGET, abs=0.02)

    def test_loud_recording_is_left_alone(self):
        capture, gain = audio_mod.normalize(self.quiet(0.7))
        assert gain == 1.0

    def test_gain_is_capped(self):
        # Иначе на почти пустой записи мы раскачали бы шум до уровня речи
        # и получили бы выдуманный текст вместо пустоты.
        _, gain = audio_mod.normalize(self.quiet(0.0005))
        assert gain == audio_mod.NORMALIZE_MAX_GAIN

    def test_silence_is_not_amplified_into_noise(self):
        silence = Capture(samples=np.zeros(16000, dtype=np.int16), sample_rate=16000)
        capture, gain = audio_mod.normalize(silence)

        assert gain == 1.0
        assert peak_level(capture) == 0.0

    def test_no_clipping(self):
        capture, _ = audio_mod.normalize(self.quiet(0.03))
        assert capture.samples.max() <= 32767
        assert capture.samples.min() >= -32768

    def test_duration_and_rate_are_preserved(self):
        original = self.quiet(0.05, seconds=2.0)
        capture, _ = audio_mod.normalize(original)

        assert capture.sample_rate == original.sample_rate
        assert len(capture.samples) == len(original.samples)


class TestStalledStream:
    """Поток микрофона умирает открытым, и снаружи это неотличимо от тишины.

    Живой случай: с 22:52 каждое нажатие давало ровно 0.26 секунды, сколько
    клавишу ни держи. 0.26 — это пре-ролл, накопленный ДО нажатия: колбэк
    PortAudio перестал срабатывать, а поток остался «открытым», и is_open
    честно отвечал True. Отличить это от короткого нажатия можно только по
    одному признаку: за время записи не пришло НИ ОДНОГО нового блока.
    """

    @staticmethod
    def recorder():
        return audio_mod.Recorder(sample_rate=16000, preroll_ms=250)

    @staticmethod
    def block():
        return np.full((audio_mod.BLOCKSIZE, 1), 1000, dtype=np.int16)

    def test_no_fresh_blocks_over_the_grace_period_is_a_stall(self, monkeypatch):
        recorder = self.recorder()
        clock = _fake_clock(monkeypatch)
        # Пре-ролл набрали ДО нажатия — именно он и возвращался пользователю.
        for _ in range(8):
            recorder._callback(self.block(), audio_mod.BLOCKSIZE, None, None)

        recorder.begin()
        clock.advance(audio_mod.STALL_GRACE + 2.0)
        capture = recorder.end()  # ни одного колбэка между begin и end

        assert capture.stalled
        assert capture.duration_s > 0  # пре-ролл на месте, поэтому не ноль

    def test_a_short_press_is_not_a_stall(self, monkeypatch):
        # Отпустили раньше, чем успел прийти первый блок. Это норма, а не
        # поломка: ругаться тут значило бы кричать на каждое случайное касание.
        recorder = self.recorder()
        clock = _fake_clock(monkeypatch)

        recorder.begin()
        clock.advance(audio_mod.STALL_GRACE / 2)
        assert not recorder.end().stalled

    def test_arriving_audio_is_not_a_stall(self, monkeypatch):
        recorder = self.recorder()
        clock = _fake_clock(monkeypatch)

        recorder.begin()
        recorder._callback(self.block(), audio_mod.BLOCKSIZE, None, None)
        clock.advance(5.0)
        assert not recorder.end().stalled

    def test_one_single_block_is_enough_to_clear_the_alarm(self, monkeypatch):
        # Признак — не «мало звука», а «звука нет вовсе». Потери блоков ловит
        # отдельная проверка, сравнивающая удержание с длиной записи.
        recorder = self.recorder()
        clock = _fake_clock(monkeypatch)

        recorder.begin()
        recorder._callback(self.block(), audio_mod.BLOCKSIZE, None, None)
        clock.advance(30.0)
        assert not recorder.end().stalled

    def test_preroll_alone_does_not_clear_the_alarm(self, monkeypatch):
        # Ровно та ловушка, в которую попала программа: буфер не пустой,
        # длина ненулевая, а звука за время нажатия не пришло.
        recorder = self.recorder()
        clock = _fake_clock(monkeypatch)
        for _ in range(8):
            recorder._callback(self.block(), audio_mod.BLOCKSIZE, None, None)

        recorder.begin()
        clock.advance(3.0)
        capture = recorder.end()

        assert len(capture.samples) > 0
        assert capture.stalled

    def test_cancel_resets_the_counter(self, monkeypatch):
        recorder = self.recorder()
        clock = _fake_clock(monkeypatch)

        recorder.begin()
        recorder._callback(self.block(), audio_mod.BLOCKSIZE, None, None)
        clock.advance(3.0)
        recorder.cancel()

        assert recorder._fresh_blocks == 0
        assert recorder._began_at == 0.0

    def test_a_capture_that_never_began_is_not_a_stall(self):
        # end() без begin(): счётчик времени нулевой, и выдумывать поломку
        # на пустом месте нельзя.
        assert not self.recorder().end().stalled


class TestLiveStream:
    """«Микрофон слышит прямо сейчас?» — вопрос, который задать было некому.

    Уровень считался по готовой записи, то есть узнать о беде можно было
    только после отпускания клавиши: плашка честно горела, человек говорил,
    а звука не было. level, seconds_since_block и is_alive отвечают в любой
    момент, в том числе ДО нажатия.
    """

    @staticmethod
    def recorder():
        return audio_mod.Recorder(sample_rate=16000, preroll_ms=250)

    @staticmethod
    def block(value: int):
        return np.full((audio_mod.BLOCKSIZE, 1), value, dtype=np.int16)

    def feed(self, recorder, value: int) -> None:
        recorder._callback(self.block(value), audio_mod.BLOCKSIZE, None, None)

    # --- уровень ---------------------------------------------------------------

    def test_without_blocks_there_is_no_level_and_no_time(self):
        recorder = self.recorder()

        assert recorder.level == 0.0
        # Не «давно», а «никогда»: с любым порогом это должно давать «не жив».
        assert recorder.seconds_since_block == float("inf")

    def test_level_follows_the_last_block(self):
        recorder = self.recorder()

        self.feed(recorder, 32767)
        assert recorder.level > 0.99

        self.feed(recorder, 400)  # заговорили и замолчали — уровень падает следом
        assert recorder.level == pytest.approx(400 / 32768, abs=0.001)

    def test_quiet_room_stays_below_the_silence_threshold(self):
        # Замер на машине пользователя: фон тихой комнаты около 0.014,
        # порог тишины у него 0.105, обычная речь даёт 0.1..0.7.
        recorder = self.recorder()
        self.feed(recorder, int(0.014 * 32768))

        assert recorder.level == pytest.approx(0.014, abs=0.001)
        assert recorder.level < 0.105

    def test_a_loud_block_is_near_one(self):
        recorder = self.recorder()
        self.feed(recorder, 32767)

        assert recorder.level == pytest.approx(1.0, abs=0.001)

    def test_negative_peak_is_not_lost_to_int16_overflow(self):
        # abs(-32768) в int16 — это снова -32768. Наивный np.abs() дал бы
        # отрицательный «пик», и самый громкий блок выглядел бы тише тишины.
        recorder = self.recorder()
        self.feed(recorder, -32768)

        assert recorder.level == pytest.approx(1.0)

    def test_level_is_measured_in_the_same_units_as_peak_level(self):
        # Единицы обязаны совпадать: порог тишины в конфиге один на оба.
        t = np.linspace(0, 1.0, audio_mod.BLOCKSIZE, endpoint=False)
        samples = (0.37 * np.sin(2 * np.pi * 3 * t) * 32767).astype(np.int16)

        recorder = self.recorder()
        recorder._callback(samples.reshape(-1, 1), audio_mod.BLOCKSIZE, None, None)

        expected = peak_level(Capture(samples=samples, sample_rate=16000))
        assert recorder.level == pytest.approx(expected)

    # --- время последнего блока ------------------------------------------------

    def test_seconds_since_block_counts_from_the_last_callback(self, monkeypatch):
        recorder = self.recorder()
        clock = _fake_clock(monkeypatch)

        self.feed(recorder, 1000)
        assert recorder.seconds_since_block == pytest.approx(0.0)

        clock.advance(1.25)
        assert recorder.seconds_since_block == pytest.approx(1.25)

        self.feed(recorder, 1000)  # новый блок сбрасывает отсчёт
        assert recorder.seconds_since_block == pytest.approx(0.0)

    # --- жив ли поток ----------------------------------------------------------

    def test_a_closed_stream_is_never_alive(self, monkeypatch):
        recorder = self.recorder()
        _fake_clock(monkeypatch)
        self.feed(recorder, 1000)  # блок только что был, но потока нет

        assert not recorder.is_open
        assert not recorder.is_alive

    def test_an_open_stream_with_fresh_blocks_is_alive(self, monkeypatch):
        recorder, clock = self._open(monkeypatch)

        self.feed(recorder, 1000)
        assert recorder.is_alive

        clock.advance(audio_mod.STALL_GRACE / 2)  # блок идёт раз в 32 мс, это норма
        assert recorder.is_alive

    def test_an_open_stream_without_recent_blocks_is_dead(self, monkeypatch):
        # Ровно та поломка: is_open честно True, а колбэк молчит. Раньше это
        # выяснялось только на отпускании, теперь — сразу.
        recorder, clock = self._open(monkeypatch)
        self.feed(recorder, 1000)

        clock.advance(audio_mod.STALL_GRACE + 0.1)

        assert recorder.is_open
        assert not recorder.is_alive

    def test_a_freshly_opened_stream_waits_for_its_first_block(self, monkeypatch):
        # Пока блоков не было, честный ответ — «ещё не знаю», а не «жив».
        # Ложное «жив» и есть та беда, из-за которой человек говорит в пустоту.
        recorder, _ = self._open(monkeypatch)

        assert recorder.is_open
        assert not recorder.is_alive

    def test_silence_is_not_death(self, monkeypatch):
        # Тишина — не мёртвый поток: блоки идут и в тишине, просто нулевые.
        recorder, clock = self._open(monkeypatch)

        for _ in range(10):
            self.feed(recorder, 0)
            clock.advance(0.032)

        assert recorder.level == 0.0
        assert recorder.is_alive

    def test_closing_forgets_what_was_heard(self, monkeypatch):
        # Иначе отметка полусекундной давности от старого потока выдала бы
        # за живой новый, который ещё не прислал ни одного блока.
        recorder, _ = self._open(monkeypatch)
        self.feed(recorder, 32767)

        recorder.close()

        assert recorder.level == 0.0
        assert recorder.seconds_since_block == float("inf")
        assert not recorder.is_alive

    def _open(self, monkeypatch) -> tuple:
        _patch_stream(monkeypatch, [], [])
        clock = _fake_clock(monkeypatch)
        recorder = self.recorder()
        recorder.open()
        return recorder, clock


class TestReopenDuringCapture:
    """Переоткрытие потока посреди уже идущей записи.

    Живой сценарий: микрофон вёл себя странно, приложение завело фоновое
    переоткрытие — и оно опоздало, сработав уже посреди следующей фразы.
    Раньше reopen() чистил кадры, снимал признак записи и обнулял отметку
    начала. Из-за обнулённой отметки end() считал held = 0.0, признак stalled
    давал False, и мёртвый поток выглядел случайным касанием клавиши: полторы
    секунды речи до переоткрытия и полторы после превращались в 0.00 с и
    пропадали молча. Молчаливая потеря — ровно то, на что жалуется человек.
    """

    @staticmethod
    def block(value: int = 1000):
        return np.full((audio_mod.BLOCKSIZE, 1), value, dtype=np.int16)

    def speak(self, recorder, seconds: float, clock=None, value: int = 1000) -> None:
        """Блоки раз в 32 мс — как настоящий PortAudio, с ходом часов."""
        blocks = int(round(seconds * recorder.sample_rate / audio_mod.BLOCKSIZE))
        for _ in range(blocks):
            recorder._callback(self.block(value), audio_mod.BLOCKSIZE, None, None)
            if clock is not None:
                clock.advance(audio_mod.BLOCKSIZE / recorder.sample_rate)

    def recorder(self, monkeypatch) -> tuple:
        _patch_stream(monkeypatch, [], [])
        clock = _fake_clock(monkeypatch)
        recorder = audio_mod.Recorder(sample_rate=16000, preroll_ms=250)
        recorder.open()
        return recorder, clock

    def test_speech_survives_a_reopen_in_the_middle(self, monkeypatch):
        # Дословное воспроизведение жалобы: раньше здесь получалось 0.00 с.
        recorder, clock = self.recorder(monkeypatch)
        self.speak(recorder, 0.25, clock)  # пре-ролл, набранный до нажатия

        recorder.begin()
        self.speak(recorder, 1.5, clock)
        recorder.reopen()  # запоздавшее фоновое переоткрытие
        self.speak(recorder, 1.5, clock)
        capture = recorder.end()

        assert capture.duration_s == pytest.approx(3.26, abs=0.05)
        assert not capture.stalled
        assert capture.interrupted  # шов в звуке вызывающему видно

    def test_a_dead_stream_after_the_reopen_is_not_passed_off_as_a_short_press(
        self, monkeypatch
    ):
        # Новый поток тоже не отдаёт звука. Записанного до переоткрытия меньше
        # минимальной длины, то есть без признака stalled вызывающий выбросил
        # бы это как случайное касание — снова молча, снова без следа.
        recorder, clock = self.recorder(monkeypatch)
        recorder.begin()
        self.speak(recorder, 0.2, clock)

        recorder.reopen()
        clock.advance(3.0)  # держат клавишу, а блоков нет ни одного
        capture = recorder.end()

        assert capture.stalled
        assert capture.interrupted
        assert capture.duration_s > 0  # записанное до переоткрытия на месте

    def test_a_reopen_just_before_release_is_not_a_stall(self, monkeypatch):
        # С нового потока спрашивать пока нечего: между переоткрытием и
        # отпусканием прошло меньше STALL_GRACE, блок просто не успел прийти.
        recorder, clock = self.recorder(monkeypatch)
        recorder.begin()
        self.speak(recorder, 1.5, clock)

        recorder.reopen()
        clock.advance(audio_mod.STALL_GRACE / 2)
        capture = recorder.end()

        assert not capture.stalled
        assert capture.duration_s == pytest.approx(1.5, abs=0.05)

    def test_blocks_from_the_dead_stream_do_not_cover_for_the_new_one(
        self, monkeypatch
    ):
        # Счётчик свежих блоков после переоткрытия начинается заново: иначе
        # звук, отданный покойным потоком, вечно подтверждал бы живость того,
        # который молчит.
        recorder, clock = self.recorder(monkeypatch)
        recorder.begin()
        self.speak(recorder, 1.0, clock)

        recorder.reopen()

        assert recorder._fresh_blocks == 0
        assert recorder._began_at  # отметка начала записи цела
        assert recorder._capturing  # и сама запись продолжается

    def test_an_untouched_recording_is_not_marked_interrupted(self, monkeypatch):
        recorder, clock = self.recorder(monkeypatch)
        recorder.begin()
        self.speak(recorder, 1.0, clock)

        assert not recorder.end().interrupted

    def test_a_reopen_between_dictations_still_clears_everything(self, monkeypatch):
        # Вне записи поведение прежнее: чистим всё, чтобы в следующую диктовку
        # не утёк ни один блок умершего потока.
        recorder, clock = self.recorder(monkeypatch)
        recorder.begin()
        self.speak(recorder, 1.0, clock)
        recorder.end()
        self.speak(recorder, 0.3, clock)  # копится только пре-ролл

        recorder.reopen()

        assert recorder._frames == []
        assert recorder._captured == 0
        assert recorder._began_at == 0.0
        assert len(recorder._ring) == 0
        assert not recorder.end().interrupted

    def test_the_device_is_really_reopened(self, monkeypatch):
        # Сохранение записи не отменяет главного дела reopen(): устройство
        # должно быть закрыто и поднято заново.
        opened, closed = [], []
        _patch_stream(monkeypatch, opened, closed)
        _fake_clock(monkeypatch)
        recorder = audio_mod.Recorder(sample_rate=16000, preroll_ms=250)
        recorder.open()

        recorder.begin()
        recorder.reopen()

        assert len(opened) == 2
        assert len(closed) == 1
        assert recorder.is_open

    def test_the_reopen_leaves_a_trace_in_the_log(self, monkeypatch, caplog):
        # Шов в записи должен быть чем-то объясним, когда человек придёт
        # с вопросом «почему в середине фразы дырка».
        recorder, clock = self.recorder(monkeypatch)
        recorder.begin()
        self.speak(recorder, 1.0, clock)

        with caplog.at_level(logging.WARNING, logger="whisperfree.audio"):
            recorder.reopen()

        assert "посреди записи" in caplog.text


class TestPeakSinceBegin:
    """Накопленный максимум за запись — то, чем плашка обещает то же самое,
    что решится в конце.

    Итог считается по пику ВСЕЙ записи (peak_level готового Capture против
    silence_peak), а level показывает последний блок и в паузе между словами
    падает в ноль. Подсказка «микрофон молчит» по level пугала бы там, где
    запись на самом деле проходит.
    """

    @staticmethod
    def recorder():
        return audio_mod.Recorder(sample_rate=16000, preroll_ms=250)

    def feed(self, recorder, value: int) -> None:
        block = np.full((audio_mod.BLOCKSIZE, 1), value, dtype=np.int16)
        recorder._callback(block, audio_mod.BLOCKSIZE, None, None)

    def test_zero_before_the_recording_starts(self):
        recorder = self.recorder()
        assert recorder.peak_since_begin == 0.0

        # Микрофон пишет в кольцевой буфер и до нажатия, но накапливать
        # максимум ещё не для чего: записи нет.
        self.feed(recorder, 32767)
        assert recorder.peak_since_begin == 0.0
        assert recorder.level > 0.99  # полоска при этом живёт своей жизнью

    def test_it_grows_and_never_falls_back(self):
        recorder = self.recorder()
        recorder.begin()

        self.feed(recorder, int(0.6 * 32768))
        assert recorder.peak_since_begin == pytest.approx(0.6, abs=0.001)

        self.feed(recorder, int(0.014 * 32768))  # пауза между словами
        assert recorder.level == pytest.approx(0.014, abs=0.001)
        assert recorder.peak_since_begin == pytest.approx(0.6, abs=0.001)

        self.feed(recorder, int(0.9 * 32768))
        assert recorder.peak_since_begin == pytest.approx(0.9, abs=0.001)

    def test_a_new_begin_resets_it(self):
        recorder = self.recorder()
        recorder.begin()
        self.feed(recorder, 32767)
        recorder.end()

        recorder.begin()
        assert recorder.peak_since_begin == 0.0

        self.feed(recorder, int(0.2 * 32768))
        assert recorder.peak_since_begin == pytest.approx(0.2, abs=0.001)

    def test_cancel_resets_it(self):
        recorder = self.recorder()
        recorder.begin()
        self.feed(recorder, 32767)

        recorder.cancel()
        assert recorder.peak_since_begin == 0.0

    def test_negative_peak_is_not_lost_to_int16_overflow(self):
        # abs(-32768) в int16 — снова -32768: самый громкий блок не должен
        # оказаться тише тишины.
        recorder = self.recorder()
        recorder.begin()
        self.feed(recorder, -32768)

        assert recorder.peak_since_begin == pytest.approx(1.0)

    def test_it_matches_the_verdict_at_the_end(self):
        # Единицы и значение обязаны совпадать с тем, по чему в конце решается
        # вопрос тишины, — иначе плашка обещает одно, а получается другое.
        t = np.linspace(0, 1.0, audio_mod.BLOCKSIZE, endpoint=False)
        samples = (0.37 * np.sin(2 * np.pi * 3 * t) * 32767).astype(np.int16)

        recorder = self.recorder()
        recorder.begin()
        recorder._callback(samples.reshape(-1, 1), audio_mod.BLOCKSIZE, None, None)
        capture = recorder.end()

        assert recorder.peak_since_begin == pytest.approx(peak_level(capture))

    def test_a_quiet_room_stays_under_the_silence_threshold(self):
        # Фон тихой комнаты у пользователя около 0.014 при пороге 0.105:
        # накопленный максимум не должен доползать до порога сам по себе.
        recorder = self.recorder()
        recorder.begin()
        for _ in range(100):
            self.feed(recorder, int(0.014 * 32768))

        assert recorder.peak_since_begin == pytest.approx(0.014, abs=0.001)
        assert recorder.peak_since_begin < 0.105

    def test_it_survives_a_reopen(self, monkeypatch):
        # Запись переоткрытие переживает — значит и её пик тоже. Обнулять его
        # значило бы показать «микрофон молчит» посреди фразы, которая уже
        # записана и сейчас уедет на распознавание.
        _patch_stream(monkeypatch, [], [])
        _fake_clock(monkeypatch)
        recorder = self.recorder()
        recorder.open()
        recorder.begin()
        self.feed(recorder, int(0.8 * 32768))

        recorder.reopen()

        assert recorder.level == 0.0  # новый поток ещё ничего не прислал
        assert recorder.peak_since_begin == pytest.approx(0.8, abs=0.001)


class _FlickeringRecorder(audio_mod.Recorder):
    """Recorder, у которого close() из фонового потока вклинивается ровно
    между двумя чтениями self._stream.

    Настоящую гонку тестом не поймать: разбор ловил её только при
    искусственно уменьшенном интервале переключения потоков. Поэтому момент
    подмены задан явно — второе чтение поля отдаёт None, как будто поток
    закрыли прямо посреди проверки.
    """

    def __init__(self, **kwargs) -> None:
        self.reads = 0
        self._held_stream = None
        super().__init__(**kwargs)

    @property
    def _stream(self):
        self.reads += 1
        return self._held_stream if self.reads <= 1 else None

    @_stream.setter
    def _stream(self, value) -> None:
        self._held_stream = value


class TestIsOpenUnderClose:
    """is_open зовут из потока хука клавиатуры, а close() — из фонового.

    Редкое падение в хуке хуже частого: повторить его нечем, объяснить
    человеку тоже, а после исключения Windows ещё и снимает хук.
    """

    def test_is_open_survives_the_stream_vanishing_mid_check(self):
        recorder = _FlickeringRecorder(sample_rate=16000, preroll_ms=250)
        stream = _FakeStream([], [])
        stream.start()
        recorder._held_stream = stream
        recorder.reads = 0

        # Раньше здесь летел AttributeError: None не имеет поля active.
        assert recorder.is_open is True
        assert recorder.reads == 1  # поле прочитано ровно один раз

    def test_a_closed_recorder_is_simply_not_open(self):
        recorder = _FlickeringRecorder(sample_rate=16000, preroll_ms=250)
        recorder.reads = 0

        assert recorder.is_open is False
        assert recorder.reads == 1
