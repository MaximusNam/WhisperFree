"""Фабрика провайдеров распознавания речи."""

from __future__ import annotations

from .base import TranscriptionError, TranscriptionProvider, TranscriptionRequest
from .openai_compat import OpenAICompatProvider

__all__ = [
    "TranscriptionError",
    "TranscriptionProvider",
    "TranscriptionRequest",
    "OpenAICompatProvider",
    "build_provider",
]


def build_provider(cfg) -> TranscriptionProvider:
    """Собирает провайдер по секции [provider] конфига."""
    return OpenAICompatProvider(
        base_url=cfg.base_url,
        model=cfg.model,
        api_key=cfg.api_key,
        timeout_s=cfg.timeout_s,
        max_retries=cfg.max_retries,
    )
