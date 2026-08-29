"""Остаток бесплатных лимитов провайдера.

Значения берутся из заголовков ответа `x-ratelimit-*`, а не из документации:
тариф у конкретного ключа может отличаться, и гадать тут незачем.

Каждая диктовка расходует один запрос к распознаванию и, если включена
правка, один к чат-модели. Значит потолок задаёт тот лимит, который меньше.
"""

from __future__ import annotations

import io
import logging

import httpx
import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

# Порядок вывода и человеческие названия для заголовков Groq.
FIELDS = (
    ("requests", "запросов"),
    ("tokens", "токенов"),
    ("audio-seconds", "секунд звука"),
)


def fetch(client: httpx.Client, url: str, **kwargs) -> dict[str, str]:
    """Делает минимальный запрос и забирает заголовки с лимитами."""
    response = client.post(url, **kwargs)
    return {
        key.lower().replace("x-ratelimit-", ""): value
        for key, value in response.headers.items()
        if "ratelimit" in key.lower()
    }


def format_limits(limits: dict[str, str]) -> list[str]:
    if not limits:
        return ["   провайдер не сообщает лимиты в заголовках"]

    out = []
    for key, label in FIELDS:
        limit = limits.get(f"limit-{key}")
        if not limit:
            continue
        left = limits.get(f"remaining-{key}", "?")
        reset = limits.get(f"reset-{key}", "")
        spent = ""
        try:
            spent = f", израсходовано {int(limit) - int(left)}"
        except ValueError:
            pass
        out.append(f"   {label:14s} {left} из {limit}{spent}   (восстановление {reset})")
    return out or ["   лимиты в заголовках есть, но в незнакомом виде"]


def ceiling(stt: dict[str, str], chat: dict[str, str] | None) -> str | None:
    """Сколько диктовок в сутки позволяет узкое место."""
    try:
        stt_limit = int(stt["limit-requests"])
    except (KeyError, ValueError):
        return None

    if chat is None:
        return f"ПОТОЛОК: {stt_limit} диктовок в сутки (правка выключена)."

    try:
        chat_limit = int(chat["limit-requests"])
    except (KeyError, ValueError):
        return None

    narrower = "правка" if chat_limit <= stt_limit else "распознавание"
    return (
        f"ПОТОЛОК: {min(stt_limit, chat_limit)} диктовок в сутки — "
        f"узкое место это {narrower}."
    )


def silent_probe() -> bytes:
    """Секунда тишины: минимальная запись, которую примет распознавание."""
    buf = io.BytesIO()
    sf.write(buf, np.zeros(16000, dtype=np.int16), 16000, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


def report(cfg) -> int:
    """Печатает остаток лимитов. Возвращает код выхода."""
    if not cfg.provider.api_key:
        print(f"Нет ключа {cfg.provider.api_key_env}.")
        return 1

    auth = {"Authorization": f"Bearer {cfg.provider.api_key}"}
    stt_url = cfg.provider.base_url.rstrip("/") + "/audio/transcriptions"
    chat_base = (cfg.refine.base_url or cfg.provider.base_url).rstrip("/")

    print(f"Провайдер: {cfg.provider.base_url}")
    print()

    try:
        with httpx.Client(timeout=30.0) as client:
            stt = fetch(
                client,
                stt_url,
                headers=auth,
                files={"file": ("probe.flac", silent_probe(), "application/octet-stream")},
                data={"model": cfg.provider.model, "response_format": "json"},
            )
            print(f"РАСПОЗНАВАНИЕ  {cfg.provider.model}")
            for line in format_limits(stt):
                print(line)

            chat = None
            if cfg.refine.enabled:
                chat = fetch(
                    client,
                    chat_base + "/chat/completions",
                    headers=auth,
                    json={
                        "model": cfg.refine.model,
                        "max_tokens": 5,
                        "messages": [{"role": "user", "content": "ок"}],
                    },
                )
                print()
                print(f"ПРАВКА ТЕКСТА  {cfg.refine.model}")
                for line in format_limits(chat):
                    print(line)
            else:
                print()
                print("Правка выключена — вторая модель не расходуется.")
    except Exception as exc:
        print(f"Не удалось получить лимиты: {type(exc).__name__}: {exc}")
        return 1

    verdict = ceiling(stt, chat)
    if verdict:
        print()
        print(verdict)

    print()
    print("Остаток восстанавливается постепенно, а не разом в полночь.")
    return 0
