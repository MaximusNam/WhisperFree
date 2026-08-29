"""Интерфейс провайдера распознавания речи."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class TranscriptionError(RuntimeError):
    """Распознать не удалось. Сообщение показывается пользователю в оверлее."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class TranscriptionRequest:
    audio: bytes
    filename: str
    language: str | None = None
    prompt: str | None = None
    duration_s: float = 0.0


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Любой движок распознавания.

    Отдельный протокол нужен, чтобы можно было дописать не-OpenAI-совместимый
    адаптер (например, Yandex SpeechKit) не трогая остальное приложение.
    """

    name: str
    model: str

    def transcribe(self, request: TranscriptionRequest) -> str: ...

    def close(self) -> None: ...
