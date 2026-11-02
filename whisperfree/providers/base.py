"""Интерфейс провайдера распознавания речи."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class TranscriptionError(RuntimeError):
    """Распознать не удалось. Сообщение показывается пользователю в оверлее.

    retryable — есть смысл повторить запрос тут же, в том же вызове.
    deferrable — ожидание исправит: запрос не дошёл или сервер сейчас не может.
        Такой звук стоит сохранить и отправить позже, а не выбрасывать. Ключ
        отклонён, запись слишком длинная — ожиданием не лечится.
    network — виновата связь, а не сервер: соединение не установилось или
        оборвалось. Повторять такое больше одного раза бессмысленно: сеть
        лежит, и каждый повтор только держит человека перед «Распознаю…».
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        deferrable: bool = False,
        network: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.deferrable = deferrable
        self.network = network


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
