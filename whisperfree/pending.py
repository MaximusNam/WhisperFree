"""Отложенные диктовки: звук, который не удалось отправить на распознавание.

Живой случай 02.11: сеть с телефона пропадала на шесть минут, и пять диктовок
за это время ушли в никуда — программа писала «сеть недоступна» и выбрасывала
звук. Сегодня это были короткие пробы, но точно так же пропала бы и
полутораминутная диктовка, которая за полчаса до того прошла как раз в эту
сеть.

Теперь звук такой диктовки сохраняется в отдельную папку и отправляется
снова сам — когда сеть вернётся. Хранится он только до удачной отправки или
до суток, смотря что раньше: это не архив, а очередь.

Здесь две части. PendingStore — сама очередь на диске: переживает перезапуск
программы. Resender — сторож, который решает, КОГДА пробовать снова. Саму
отправку он не делает: её делает рабочий поток программы, тот же, что и
обычные диктовки. Иначе к провайдеру шли бы два запроса одновременно из двух
потоков, а клиент у провайдера один.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Дольше суток ждать нет смысла: продиктованное через сутки уже не нужно там,
# куда его диктовали, а звук человека не должен копиться на диске.
MAX_AGE_S = 24 * 3600

# Предел очереди. Пятьдесят диктовок — это часы без сети; дальше выбрасываются
# самые старые, с записью в лог.
MAX_ITEMS = 50

# Первая повторная попытка — через десять секунд после отказа, дальше пауза
# удваивается до минуты. Обрыв 02.11 длился около шести минут. Попытка при
# лежащей сети стоит секунду-десять (соединение не устанавливается), а живая
# диктовка, прошедшая успешно, будит сторожа сразу: сеть, значит, вернулась.
RESEND_FIRST_S = 10.0
RESEND_MAX_S = 60.0

# Сколько ждать, пока рабочий поток доберётся до попытки. Он может быть занят
# живой диктовкой; дольше ждать — значит, он завис, и сторожу пора жить дальше.
SUBMIT_WAIT_S = 600.0


@dataclass
class PendingItem:
    """Одна отложенная диктовка."""

    id: str
    filename: str
    lang: str
    duration_s: float
    # Время записи в истории: по нему удачная отправка находит свою запись и
    # дописывает в неё текст, а не заводит новую.
    record_ts: float
    created: float
    attempts: int = 0
    last_error: str = ""


class PendingStore:
    """Очередь отложенных диктовок на диске — переживает перезапуск."""

    def __init__(
        self, directory: Path, max_items: int = MAX_ITEMS, max_age_s: float = MAX_AGE_S
    ) -> None:
        self.directory = Path(directory)
        self.max_items = max_items
        self.max_age_s = max_age_s
        self._lock = threading.RLock()

    # --- пути ------------------------------------------------------------------

    def _meta(self, item_id: str) -> Path:
        return self.directory / f"{item_id}.json"

    def _audio(self, item: PendingItem) -> Path:
        return self.directory / f"{item.id}{Path(item.filename).suffix or '.flac'}"

    # --- запись ----------------------------------------------------------------

    def add(
        self, data: bytes, filename: str, lang: str, duration_s: float, record_ts: float
    ) -> PendingItem | None:
        """Сохранить звук. None — сохранить не удалось (и в логе сказано почему)."""
        now = time.time()
        item = PendingItem(
            id=f"{time.strftime('%Y%m%d-%H%M%S')}-{int(now * 1000) % 1000:03d}-{os.getpid()}",
            filename=filename,
            lang=lang,
            duration_s=round(float(duration_s), 2),
            record_ts=record_ts,
            created=now,
        )
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                # Сначала звук, потом описание: описание без звука значит
                # «записать не успели», и items() такие выбросит. Обратный
                # порядок оставил бы звук, о котором никто не знает.
                self._audio(item).write_bytes(data)
                self._write_meta(item)
            except OSError as exc:
                log.error("не удалось отложить диктовку: %s", exc)
                return None
            self._trim()
        log.info(
            "диктовка отложена до появления сети: %.1f с звука, в очереди %d",
            item.duration_s, len(self),
        )
        return item

    def _write_meta(self, item: PendingItem) -> None:
        path = self._meta(item.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(item), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def touch(self, item: PendingItem, error: str) -> None:
        """Ещё одна неудачная попытка — для лога и для окна истории."""
        item.attempts += 1
        item.last_error = error
        with self._lock:
            try:
                self._write_meta(item)
            except OSError as exc:  # pragma: no cover
                log.debug("не удалось обновить отложенную диктовку: %s", exc)

    def remove(self, item: PendingItem) -> None:
        with self._lock:
            for path in (self._audio(item), self._meta(item.id)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:  # pragma: no cover
                    log.warning("не удалось удалить %s: %s", path.name, exc)

    # --- чтение ----------------------------------------------------------------

    def read(self, item: PendingItem) -> bytes:
        return self._audio(item).read_bytes()

    def items(self) -> list[PendingItem]:
        """Отложенные диктовки, старые первыми. Просроченные выбрасываются здесь."""
        with self._lock:
            found: list[PendingItem] = []
            try:
                metas = sorted(self.directory.glob("*.json"))
            except OSError:
                return []
            known = set(PendingItem.__dataclass_fields__)
            now = time.time()
            for meta in metas:
                try:
                    payload = json.loads(meta.read_text(encoding="utf-8"))
                    item = PendingItem(**{k: v for k, v in payload.items() if k in known})
                except (OSError, ValueError, TypeError):
                    log.warning("отложенная диктовка %s повреждена — выбрасываю", meta.name)
                    _unlink(meta)
                    continue
                if not self._audio(item).exists():
                    _unlink(meta)
                    continue
                if now - item.created > self.max_age_s:
                    log.warning(
                        "отложенная диктовка от %s так и не ушла за сутки — выбрасываю",
                        time.strftime("%d.%m %H:%M", time.localtime(item.created)),
                    )
                    self.remove(item)
                    continue
                found.append(item)
            found.sort(key=lambda it: it.created)
            return found

    def __len__(self) -> int:
        try:
            return sum(1 for _ in self.directory.glob("*.json"))
        except OSError:  # pragma: no cover
            return 0

    def _trim(self) -> None:
        items = self.items()
        for item in items[: max(0, len(items) - self.max_items)]:
            log.warning(
                "отложенных диктовок больше %d — выбрасываю самую старую (%.1f с звука)",
                self.max_items, item.duration_s,
            )
            self.remove(item)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class Resender:
    """Решает, когда снова пробовать отправить отложенное.

    submit(done) должен поставить попытку в очередь рабочего потока и вызвать
    done.set(), когда она закончится — удачно или нет.
    """

    def __init__(
        self,
        store: PendingStore,
        submit: Callable[[threading.Event], None],
        first_s: float = RESEND_FIRST_S,
        max_s: float = RESEND_MAX_S,
    ) -> None:
        self.store = store
        self.submit = submit
        self.first_s = first_s
        self.max_s = max_s
        self._wake = threading.Event()
        self._now = False
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="pending-resend", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    def added(self) -> None:
        """Отложили новую диктовку: начать отсчёт заново.

        Сразу не пробуем: сеть только что подвела, и попытка через мгновение
        почти наверняка подведёт тоже.
        """
        self._wake.set()

    def online(self) -> None:
        """Сеть заведомо есть — живая диктовка только что прошла. Пробовать сейчас."""
        self._now = True
        self._wake.set()

    def _run(self) -> None:
        delay = self.first_s
        while True:
            if len(self.store) == 0:
                self._wake.wait()
            else:
                self._wake.wait(delay)
            if self._stopped.is_set():
                return
            woke = self._wake.is_set()
            self._wake.clear()
            now, self._now = self._now, False
            if woke and not now:
                delay = self.first_s
                continue
            if len(self.store) == 0:
                delay = self.first_s
                continue
            done = threading.Event()
            try:
                self.submit(done)
            except Exception:  # pragma: no cover — сторож не должен умирать
                log.exception("не удалось поставить отложенные на отправку")
                done.set()
            done.wait(SUBMIT_WAIT_S)
            delay = self.first_s if len(self.store) == 0 else min(delay * 2, self.max_s)
