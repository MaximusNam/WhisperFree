"""Логирование: ротируемый файл в %APPDATA%\\WhisperFree\\logs плюс консоль."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

# Чужие библиотеки на DEBUG заваливают лог так, что своих строк не найти:
# один только PIL печатает по строке на каждый формат изображения.
NOISY_LOGGERS = ("PIL", "comtypes", "httpx", "httpcore", "urllib3", "asyncio", "matplotlib")


def setup_logging(path: Path, level: int = logging.INFO, console: bool = True) -> None:
    """Настраивает корневой логгер. Повторные вызовы игнорируются."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        # Нет доступа к каталогу логов — не повод не запускаться.
        pass

    # При тихом запуске через run.vbs потоков вывода нет вообще.
    if console and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _CONFIGURED = True


class Stopwatch:
    """Замер стадий одной диктовки для строки таймингов в логе."""

    def __init__(self) -> None:
        import time

        self._time = time.perf_counter
        self._start = self._time()
        self._marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        self._marks.append((name, self._time() - self._start))

    @property
    def total(self) -> float:
        return self._time() - self._start

    def summary(self) -> str:
        parts = [f"{name}={value * 1000:.0f}ms" for name, value in self._marks]
        parts.append(f"total={self.total * 1000:.0f}ms")
        return " ".join(parts)
