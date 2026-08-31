"""История расшифровок — страховка от потери продиктованного текста.

Вставка в чужое окно принципиально ненадёжна: окно могло потерять фокус,
приложение могло съесть Ctrl+V, поле могло оказаться нередактируемым.
Проверить, вставилось ли на самом деле, в Windows в общем случае нельзя.
Поэтому мы не пытаемся ловить провал, а делаем его безболезненным: каждая
расшифровка попадает в append-only журнал, откуда её можно достать хоткеем,
через меню в трее или через окно истории.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _one_line(text: str) -> str:
    """Схлопывает переносы и лишние пробелы: подпись живёт в одну строку."""
    return " ".join(text.split())


def _shorten(text: str, limit: int) -> str:
    """Обрезает по многоточию, не выходя за limit знаков."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


@dataclass
class Record:
    ts: float
    text: str
    lang: str = ""
    provider: str = ""
    model: str = ""
    audio_sec: float = 0.0
    target_exe: str = ""
    error: str = ""
    audio_file: str = ""
    # Текст до правки моделью. Если правка исказит смысл, оригинал
    # останется под рукой в окне истории.
    raw: str = ""
    # Токены, потраченные на правку текста моделью.
    refine_in: int = 0
    refine_out: int = 0
    # Запрос к провайдеру состоялся: ответ получен, значит за эту диктовку
    # заплачено. Ставится сразу после возврата transcribe(), до чистки и
    # правки, — дальше от ответа может не остаться ничего, а деньги уже
    # списаны. Единственный признак оплаты, не зависящий от судьбы текста;
    # зачем он понадобился — см. _was_paid.
    answered: bool = False

    # Журнал, в котором запись уже лежит; проставляют History.add и
    # History._load. Намеренно без аннотации: аннотированное имя стало бы
    # полем dataclass и уехало бы в JSON.
    _journal = None

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.ts)

    def label(self, width: int = 40) -> str:
        """Короткая подпись для меню в трее.

        У неудачи текст пустой, и раньше в меню висело «16:40  (пусто)».
        В трей за историей лезут как раз тогда, когда ничего не вставилось,
        — и не находили там ответа. Поэтому вместо пустоты показываем
        причину: в скобках, как в окне истории, чтобы её не приняли за
        продиктованное. Режем по той же ширине — это пункт меню, а не абзац.
        """
        text = _one_line(self.text)
        if not text and self.error:
            # Скобки съедают два знака из отведённой ширины.
            text = f"[{_shorten(_one_line(self.error), width - 2)}]"
        else:
            text = _shorten(text, width)
        return f"{self.when:%H:%M}  {text}" if text else f"{self.when:%H:%M}  (пусто)"

    def __setattr__(self, name: str, value) -> None:
        """Правка уже добавленной записи доходит до диска.

        Причина провала вставки выясняется позже, чем запись попадает в
        историю: в журнал пишут ДО вставки, чтобы не потерять текст, если
        приложение упадёт на вставке. Раньше дописанная причина оставалась
        только в памяти, и одна и та же диктовка до перезапуска из расходов
        исключалась, а после перезапуска — считалась. Теперь запись, лежащая
        в журнале, правится вместе с файлом; запись, которой в журнале нет
        (ещё не добавлена, вытеснена ротацией, история выключена), остаётся
        обычным объектом и файла не трогает.
        """
        journal = self.__dict__.get("_journal")
        if journal is None or name not in self.__dataclass_fields__:
            object.__setattr__(self, name, value)
        else:
            journal._amend(self, name, value)


def _was_paid(record: Record) -> bool:
    """Платили ли мы за эту диктовку.

    Раньше usage() выбрасывал из счёта любую запись с непустым error, то
    есть считал «получилось или нет» вместо «платили или нет», и счётчик
    в трее занижал траты. Часть неудач оплачена: запрос ушёл, ответ пришёл
    и был оплачен, а споткнулись мы уже после него — не удалась вставка,
    или после чистки от галлюцинаций (а то и после правки) не осталось
    текста.

    Платим мы ровно за ответ провайдера, поэтому и признак прямой:
    record.answered — «ответ получен». Его ставит рабочий поток сразу после
    возврата transcribe(), и он не зависит от того, что стало с текстом
    дальше. Не платили ровно за те неудачи, что случились ДО ответа: тишина,
    мёртвый микрофон, оборванная сеть (короткое касание клавиши в историю не
    попадает вовсе) — у них answered остаётся False.

    Раньше оплату опознавали по косвенным следам ответа в записи — text, raw,
    токены правки. След работает, пока от ответа хоть что-то уцелело, и один
    оплаченный вид неудачи из счёта выпадал: провайдер ответил пустой строкой
    («провайдер вернул пустой ответ»), чистке нечего было чистить, и в raw
    легла та же пустота. Ответ получен и оплачен, а следа нет. Проверка
    запуском это и показала: из шести диктовок провайдер ответил на четыре,
    а счётчик показывал три.

    Следы оставлены запасным вариантом — для записей, сделанных до появления
    поля: в старом журнале answered нет, и без запаса прошлые траты
    обнулились бы задним числом.

    По длительности звука отличить нельзя, хотя она просится первой:
    audio_sec меряется до отправки, и у записи с тишиной он ровно такой же,
    как у оплаченной диктовки.
    """
    if not record.error:
        return True
    if record.answered:
        return True
    return bool(record.text or record.raw or record.refine_in or record.refine_out)


class History:
    """Журнал расшифровок в %APPDATA%\\WhisperFree\\history.jsonl."""

    def __init__(self, path: Path, cfg) -> None:
        self.path = path
        self.cfg = cfg
        self._lock = threading.RLock()
        self._records: list[Record] = []
        self._cycle_index = 0
        self._cycle_at = 0.0
        self._since_compact = 0
        if cfg.enabled:
            self._load()

    # --- чтение и запись -------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        records: list[Record] = []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        # Битую строку (например, после падения при записи)
                        # пропускаем, но остальную историю не теряем.
                        continue
                    if isinstance(payload, dict) and "text" in payload:
                        records.append(_record_from(payload))
        except OSError as exc:
            log.warning("не удалось прочитать историю: %s", exc)
            return
        for record in records:
            object.__setattr__(record, "_journal", self)
        self._records = records
        log.info("история загружена: %d записей", len(records))

    def add(self, record: Record) -> Record:
        """Кладёт запись в журнал и возвращает её же.

        После add запись остаётся живой: правка её полей — хоть
        `record.error = ...`, хоть update() — перезапишет файл, так что
        память и диск не расходятся.
        """
        if not self.cfg.enabled:
            return record
        with self._lock:
            self._records.append(record)
            object.__setattr__(record, "_journal", self)
            self._cycle_index = 0
            self._cycle_at = 0.0
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            except OSError as exc:
                log.error("не удалось записать в историю: %s", exc)

            self._since_compact += 1
            if self._since_compact >= 50 or len(self._records) > self.cfg.max_records * 1.25:
                self.compact()
        return record

    def update(self, record: Record, **fields) -> bool:
        """Меняет поля уже добавленной записи — и в памяти, и в файле.

        Единственный способ дописать то, что выяснилось после add: причину
        провала вставки, окно-получатель. Одна перезапись файла на вызов,
        поэтому несколько полей разом дешевле, чем по одному.

        Возвращает False, если журнал этой записи не хранит: её вытеснила
        ротация, её не добавляли или история выключена. Тогда правка
        остаётся только в переданном объекте — и это честно, потому что
        править на диске нечего.
        """
        unknown = set(fields) - set(Record.__dataclass_fields__)
        if unknown:
            raise ValueError("нет таких полей в записи: " + ", ".join(sorted(unknown)))
        with self._lock:
            changed = False
            for name, value in fields.items():
                changed = changed or getattr(record, name) != value
                object.__setattr__(record, name, value)
            stored = any(r is record for r in self._records)
            if changed and stored:
                self._rewrite(self._records)
            return stored

    def _amend(self, record: Record, name: str, value) -> None:
        """Точка входа для `record.поле = значение` из Record.__setattr__."""
        with self._lock:
            changed = getattr(record, name) != value
            object.__setattr__(record, name, value)
            if changed and any(r is record for r in self._records):
                self._rewrite(self._records)

    def compact(self) -> None:
        """Применяет ограничения по количеству и сроку хранения."""
        with self._lock:
            self._since_compact = 0
            kept = self._records

            if self.cfg.retention_days > 0:
                cutoff = time.time() - self.cfg.retention_days * 86400
                kept = [r for r in kept if r.ts >= cutoff]
            if self.cfg.max_records > 0 and len(kept) > self.cfg.max_records:
                kept = kept[-self.cfg.max_records :]

            if len(kept) == len(self._records):
                return
            self._records = kept
            self._rewrite(kept)

    def _rewrite(self, records: list[Record]) -> None:
        """Перезапись через временный файл, чтобы не потерять историю при сбое."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("не удалось перезаписать историю: %s", exc)

    def clear(self) -> None:
        with self._lock:
            self._records = []
            self._cycle_index = 0
            self._rewrite([])

    # --- доступ ----------------------------------------------------------------

    @property
    def records(self) -> list[Record]:
        with self._lock:
            return list(self._records)

    def recent(self, limit: int = 10) -> list[Record]:
        """Последние записи, свежие сверху."""
        with self._lock:
            return list(reversed(self._records[-limit:]))

    def last(self) -> Record | None:
        with self._lock:
            return self._records[-1] if self._records else None

    def search(self, query: str) -> list[Record]:
        """Ищет по тексту, окну-получателю и причине неудачи.

        Причина здесь обязательна: у неудачной записи текст пустой, поэтому
        без неё любой непустой запрос прятал бы все неудачи разом — а
        «когда у меня последний раз молчал микрофон» ищут именно среди них.
        """
        needle = query.strip().lower()
        with self._lock:
            if not needle:
                return list(reversed(self._records))
            return [
                r
                for r in reversed(self._records)
                if needle in r.text.lower()
                or needle in r.target_exe.lower()
                or needle in r.error.lower()
            ]

    def next_for_paste(self) -> Record | None:
        """Запись для хоткея повторной вставки.

        Нажатия подряд идут вглубь истории: первое отдаёт последнюю
        расшифровку, второе — предпоследнюю. Пауза сбрасывает счётчик.
        """
        now = time.monotonic()
        with self._lock:
            if not self._records:
                return None
            if now - self._cycle_at > self.cfg.cycle_reset_s:
                self._cycle_index = 0
            else:
                self._cycle_index += 1
            self._cycle_at = now

            if self._cycle_index >= len(self._records):
                # Дошли до начала истории — остаёмся на самой старой записи.
                self._cycle_index = len(self._records) - 1
            return self._records[-1 - self._cycle_index]

    def reset_cycle(self) -> None:
        with self._lock:
            self._cycle_index = 0
            self._cycle_at = 0.0

    # --- статистика ------------------------------------------------------------

    def usage(
        self,
        price_per_hour: float,
        min_billed_seconds: int,
        refine_in_price: float = 0.0,
        refine_out_price: float = 0.0,
    ) -> dict[str, float]:
        """Оценка расходов. Считает минимальный тарифицируемый отрезок —
        у Groq короткая диктовка всё равно стоит как десять секунд.

        В счёт идут все оплаченные диктовки, в том числе неудачные:
        почему именно такое условие — см. _was_paid.
        """
        month_start = datetime.now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        def billed(records: list[Record]) -> float:
            return sum(max(r.audio_sec, float(min_billed_seconds)) for r in records)

        def refine_cost(records: list[Record]) -> float:
            return sum(
                r.refine_in / 1e6 * refine_in_price + r.refine_out / 1e6 * refine_out_price
                for r in records
            )

        with self._lock:
            month = [r for r in self._records if r.ts >= month_start and _was_paid(r)]
            today = [r for r in self._records if r.ts >= today_start and _was_paid(r)]

        month_seconds, today_seconds = billed(month), billed(today)
        month_refine, today_refine = refine_cost(month), refine_cost(today)
        return {
            "today_count": float(len(today)),
            "today_seconds": today_seconds,
            "today_stt_usd": today_seconds / 3600.0 * price_per_hour,
            "today_refine_usd": today_refine,
            "today_usd": today_seconds / 3600.0 * price_per_hour + today_refine,
            "month_count": float(len(month)),
            "month_seconds": month_seconds,
            "month_stt_usd": month_seconds / 3600.0 * price_per_hour,
            "month_refine_usd": month_refine,
            "month_usd": month_seconds / 3600.0 * price_per_hour + month_refine,
        }


def _record_from(payload: dict) -> Record:
    """Читает запись, переживая появление и исчезновение полей между версиями."""
    known = set(Record.__dataclass_fields__)
    kwargs = {k: v for k, v in payload.items() if k in known}
    kwargs.setdefault("ts", 0.0)
    kwargs.setdefault("text", "")
    try:
        return Record(**kwargs)
    except TypeError:
        return Record(ts=float(payload.get("ts") or 0.0), text=str(payload.get("text") or ""))


class AudioCache:
    """Хранит аудио последних диктовок, чтобы можно было перераспознать
    запись другой моделью, не наговаривая её заново."""

    def __init__(self, directory: Path, keep: int) -> None:
        self.directory = directory
        self.keep = max(0, int(keep))

    def save(self, data: bytes, filename: str) -> str:
        if self.keep <= 0:
            return ""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            suffix = Path(filename).suffix or ".flac"
            target = self.directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}{suffix}"
            target.write_bytes(data)
            self._prune()
            return str(target)
        except OSError as exc:
            log.warning("не удалось сохранить аудио: %s", exc)
            return ""

    def _prune(self) -> None:
        try:
            files = sorted(
                (p for p in self.directory.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return
        for path in files[: max(0, len(files) - self.keep)]:
            try:
                path.unlink()
            except OSError:
                pass
