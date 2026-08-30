"""Захват звука с микрофона.

Поток микрофона держится открытым всё время работы приложения и постоянно
пишет в кольцевой буфер. Когда пользователь нажимает клавишу, в запись
попадают последние preroll_ms миллисекунд ДО нажатия — иначе первый слог
систематически срезается, и это главная причина, по которой самодельные
диктовки раздражают.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger(__name__)

BLOCKSIZE = 512  # 32 мс при 16 кГц — достаточно мелко для точного пре-ролла

# Сколько ждать первого блока, прежде чем считать поток мёртвым.
# Блок приходит каждые 32 мс, так что полсекунды — это пятнадцать
# пропущенных подряд: случайностью такое уже не объяснить.
STALL_GRACE = 0.5


class AudioError(RuntimeError):
    pass


@dataclass
class Capture:
    """Результат одной диктовки."""

    samples: np.ndarray  # int16, моно
    sample_rate: int
    truncated: bool = False
    # Колбэк не прислал ни одного блока за всю запись: поток жив по документам,
    # но звука не даёт. Отличать это от короткого нажатия обязательно —
    # выглядят они одинаково, а лечатся по-разному.
    stalled: bool = False

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.sample_rate) if self.sample_rate else 0.0


def parse_device(value: str | int | None):
    """Пустая строка — устройство по умолчанию, цифры — индекс, иначе имя."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    return int(text) if text.lstrip("-").isdigit() else text


def resolve_device(value: str | int | None) -> int | None:
    """Превращает имя устройства в индекс.

    Одна и та же железка видна под несколькими звуковыми API: «Микрофон
    (Logitech StreamCam)» есть отдельно в MME, DirectSound, WASAPI и WDM-KS.
    sounddevice на такое имя отвечает отказом «Multiple input devices found»,
    поэтому выбираем сами — так в конфиге можно писать имя, которое переживёт
    переподключение устройств, а не индекс, который от этого съезжает.
    """
    spec = parse_device(value)
    if spec is None or isinstance(spec, int):
        return spec

    needle = spec.lower()
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_api = sd.default.hostapi
    except Exception as exc:  # pragma: no cover - зависит от звуковой подсистемы
        raise AudioError(f"не удалось получить список устройств: {exc}") from exc

    matches = [
        index
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0 and needle in device["name"].lower()
    ]
    if not matches:
        available = ", ".join(name for _, name in list_input_devices()) or "ничего"
        raise AudioError(
            f"микрофон {spec!r} не найден. Доступны: {available}. "
            "Список с индексами — команда --devices"
        )
    if len(matches) == 1:
        return matches[0]

    def priority(index: int) -> tuple[int, int]:
        api = devices[index]["hostapi"]
        if api == default_api:
            return (0, index)
        if hostapis[api]["name"] == "Windows WASAPI":
            return (1, index)
        return (2, index)

    chosen = min(matches, key=priority)
    log.info(
        "имени %r соответствуют устройства %s, беру %d (%s)",
        spec,
        matches,
        chosen,
        hostapis[devices[chosen]["hostapi"]]["name"],
    )
    return chosen


def list_input_devices() -> list[tuple[int, str]]:
    """Список микрофонов для диагностики и меню в трее."""
    try:
        return [
            (i, d["name"])
            for i, d in enumerate(sd.query_devices())
            if d.get("max_input_channels", 0) > 0
        ]
    except Exception as exc:  # pragma: no cover - зависит от звуковой подсистемы
        log.warning("не удалось получить список устройств: %s", exc)
        return []


class Recorder:
    """Всегда открытый входной поток с пре-роллом."""

    def __init__(
        self,
        sample_rate: int = 16000,
        preroll_ms: int = 250,
        device: str | int | None = None,
        max_seconds: int = 300,
        hold_open: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = resolve_device(device)
        self.max_samples = int(max_seconds * sample_rate)
        # hold_open=False закрывает поток между диктовками: Windows перестаёт
        # показывать микрофон занятым, но пре-ролл пропадает и первый слог
        # может срезаться, пока устройство просыпается.
        self.hold_open = hold_open

        preroll_blocks = max(1, int(round(preroll_ms / 1000 * sample_rate / BLOCKSIZE)))
        self._ring: deque[np.ndarray] = deque(maxlen=preroll_blocks)
        self._frames: list[np.ndarray] = []
        self._captured = 0
        self._capturing = False
        self._truncated = False
        # Блоки, пришедшие ИМЕННО во время записи, — пре-ролл сюда не входит.
        self._fresh_blocks = 0
        self._began_at = 0.0
        self._lock = threading.Lock()
        # Отдельный замок на открытие и закрытие устройства: держать основной
        # замок во время медленных вызовов PortAudio нельзя, иначе колбэк
        # микрофона встанет.
        self._io_lock = threading.Lock()
        self._want_open = False
        self._stream: sd.InputStream | None = None

    # --- жизненный цикл потока -------------------------------------------------

    def open(self) -> None:
        with self._io_lock:
            self._want_open = True
            self._open_locked()

    def _open_locked(self) -> None:
        if self._stream is not None:
            return
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=BLOCKSIZE,
                device=self.device,
                channels=1,
                dtype="int16",
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            raise AudioError(f"не удалось открыть микрофон: {exc}") from exc
        self._stream = stream
        log.info(
            "микрофон открыт: %s, %d Гц, пре-ролл %d блоков",
            self.device if self.device is not None else "устройство по умолчанию",
            self.sample_rate,
            self._ring.maxlen,
        )

    def close(self) -> None:
        with self._io_lock:
            self._want_open = False
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # pragma: no cover
                log.debug("ошибка при закрытии потока: %s", exc)

    def reopen(self) -> None:
        """Пересоздать поток — например, если микрофон переподключили."""
        self.close()
        with self._lock:
            self._ring.clear()
            self._frames.clear()
            self._captured = 0
            self._capturing = False
            self._fresh_blocks = 0
            self._began_at = 0.0
        self.open()

    @property
    def is_open(self) -> bool:
        return self._stream is not None and self._stream.active

    # --- захват ----------------------------------------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("статус аудиопотока: %s", status)
        block = indata[:, 0].copy()
        with self._lock:
            self._ring.append(block)
            if not self._capturing:
                return
            if self._captured >= self.max_samples:
                self._truncated = True
                return
            self._frames.append(block)
            self._captured += len(block)
            self._fresh_blocks += 1

    def begin(self) -> None:
        """Начать запись, забрав в неё пре-ролл.

        Метод обязан возвращаться мгновенно: он выполняется в потоке
        низкоуровневого хука клавиатуры, а медленный хук Windows сначала
        тормозит всю клавиатуру, а потом молча отключает.
        """
        with self._lock:
            self._frames = list(self._ring)
            self._captured = sum(len(b) for b in self._frames)
            self._truncated = False
            self._fresh_blocks = 0
            self._began_at = time.monotonic()
            self._capturing = True

        if not self.hold_open and self._stream is None:
            # Открытие устройства занимает десятки миллисекунд — уводим в поток.
            # Звук пойдёт, как только поток поднимется; начало фразы при этом
            # может срезаться, о чём и предупреждает описание hold_open.
            with self._io_lock:
                self._want_open = True
            threading.Thread(target=self._open_quietly, name="mic-open", daemon=True).start()

    def _open_quietly(self) -> None:
        """Открывает поток, если его всё ещё ждут.

        Проверка внутри замка обязательна: короткое нажатие успевает
        закончиться раньше, чем устройство поднимется, и без неё микрофон
        остался бы открытым навсегда — ровно тот горящий значок в трее,
        от которого мы уходим.
        """
        try:
            with self._io_lock:
                if not self._want_open:
                    return
                self._open_locked()
        except AudioError as exc:
            log.error("%s", exc)

    def end(self) -> Capture:
        """Закончить запись и забрать накопленное."""
        with self._lock:
            self._capturing = False
            frames, self._frames = self._frames, []
            truncated = self._truncated
            fresh = self._fresh_blocks
            held = time.monotonic() - self._began_at if self._began_at else 0.0
            self._captured = 0

        # За STALL_GRACE секунд колбэк обязан сработать хотя бы раз: блок идёт
        # каждые 32 мс. Ни одного за полсекунды — поток мёртв. Короткое нажатие
        # сюда не попадает: оно просто не успевает продлиться так долго.
        stalled = held > STALL_GRACE and fresh == 0

        if not self.hold_open:
            self.close()
            with self._lock:
                self._ring.clear()

        if not frames:
            samples = np.zeros(0, dtype=np.int16)
        else:
            samples = np.concatenate(frames).astype(np.int16, copy=False)
            if len(samples) > self.max_samples:
                samples = samples[: self.max_samples]
                truncated = True
        return Capture(
            samples=samples,
            sample_rate=self.sample_rate,
            truncated=truncated,
            stalled=stalled,
        )

    def cancel(self) -> None:
        """Бросить текущую запись, ничего не возвращая."""
        with self._lock:
            self._capturing = False
            self._frames = []
            self._captured = 0
            self._truncated = False
            self._fresh_blocks = 0
            self._began_at = 0.0


def encode(capture: Capture, fmt: str = "flac") -> tuple[bytes, str]:
    """Кодирует запись в память. FLAC примерно вдвое меньше WAV при той же
    точности, а значит быстрее уходит по сети."""
    fmt = (fmt or "flac").lower()
    if fmt not in {"flac", "wav"}:
        fmt = "flac"

    buf = io.BytesIO()
    sf.write(
        buf,
        capture.samples,
        capture.sample_rate,
        format=fmt.upper(),
        subtype="PCM_16",
    )
    return buf.getvalue(), f"speech.{fmt}"


NORMALIZE_TARGET = 0.7  # до какого пика подтягиваем
# Ограничение усиления. Живой случай: с усиленного микрофона три секунды
# тишины дали пик 0.035, нормировка подняла их в 19.9 раза, и провайдер
# уверенно распознал в этом «ДИНАМИЧНАЯ МУЗЫКА». Порог тишины — первая
# линия защиты, это вторая: настоящей речи столько усиления не нужно.
NORMALIZE_MAX_GAIN = 10.0


def normalize(capture: Capture, target: float = NORMALIZE_TARGET) -> tuple[Capture, float]:
    """Подтягивает уровень записи. Возвращает (запись, применённое усиление).

    Тихий микрофон — это не только риск не пройти порог тишины: модели
    распознавания на слабом сигнале ошибаются заметно чаще. Усиление
    ограничено сверху, иначе на почти пустой записи мы бы раскачали шум
    до уровня речи и получили выдуманный текст вместо пустоты.
    """
    peak = peak_level(capture)
    if peak <= 0.0:
        return capture, 1.0

    gain = min(target / peak, NORMALIZE_MAX_GAIN)
    if gain <= 1.05:  # уже достаточно громко, не трогаем
        return capture, 1.0

    louder = np.clip(
        capture.samples.astype(np.float32) * gain, -32768.0, 32767.0
    ).astype(np.int16)
    return (
        Capture(samples=louder, sample_rate=capture.sample_rate, truncated=capture.truncated),
        gain,
    )


def peak_level(capture: Capture) -> float:
    """Пиковый уровень 0..1 — чтобы отличить тишину от речи."""
    if capture.samples.size == 0:
        return 0.0
    return float(np.max(np.abs(capture.samples.astype(np.int32)))) / 32768.0
