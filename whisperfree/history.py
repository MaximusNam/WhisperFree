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

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.ts)

    def label(self, width: int = 40) -> str:
        """Короткая подпись для меню в трее."""
        text = " ".join(self.text.split())
        if len(text) > width:
            text = text[: width - 1] + "…"
        return f"{self.when:%H:%M}  {text}" if text else f"{self.when:%H:%M}  (пусто)"


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
        self._records = records
        log.info("история загружена: %d записей", len(records))

    def add(self, record: Record) -> None:
        if not self.cfg.enabled:
            return
        with self._lock:
            self._records.append(record)
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
        needle = query.strip().lower()
        with self._lock:
            if not needle:
                return list(reversed(self._records))
            return [
                r
                for r in reversed(self._records)
                if needle in r.text.lower() or needle in r.target_exe.lower()
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
        у Groq короткая диктовка всё равно стоит как десять секунд."""
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
            month = [r for r in self._records if r.ts >= month_start and not r.error]
            today = [r for r in self._records if r.ts >= today_start and not r.error]

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
