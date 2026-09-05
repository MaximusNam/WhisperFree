"""Адаптер к любому OpenAI-совместимому endpoint распознавания речи.

Groq, OpenAI, российские прокси и локальные сервера говорят по одному
протоколу: multipart POST на /audio/transcriptions. Поэтому смена провайдера —
это правка base_url и model в конфиге, а не изменение кода.
"""

from __future__ import annotations

import logging
import random
import time

import httpx

from .base import TranscriptionError, TranscriptionRequest

log = logging.getLogger(__name__)

# Коды, при которых имеет смысл повторить запрос.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatProvider:
    """Клиент к /audio/transcriptions."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.name = _host_of(self.base_url)
        # Язык, который провайдер услышал в последней записи. Нужен, чтобы
        # подмена языка перестала быть невидимой: когда Whisper слышит
        # смешанную речь как украинскую, русская половина превращается в
        # мусор, и без этой строки в логе причину не найти.
        self.last_language = ""
        self._client = httpx.Client(timeout=timeout_s)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/audio/transcriptions"

    def transcribe(self, request: TranscriptionRequest) -> str:
        if not self.api_key:
            raise TranscriptionError(
                "нет API-ключа — задайте его в .env рядом с программой"
            )

        self.last_language = ""
        files = {"file": (request.filename, request.audio, "application/octet-stream")}
        # verbose_json вместо json: тот же запрос и та же цена, но в ответе
        # есть поле language — какой язык провайдер определил.
        data: dict[str, str] = {"model": self.model, "response_format": "verbose_json"}
        if request.language:
            data["language"] = request.language
        if request.prompt:
            data["prompt"] = request.prompt

        last_error: TranscriptionError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = min(4.0, 0.4 * (2 ** (attempt - 1))) + random.uniform(0, 0.2)
                log.info("повтор запроса %d через %.1f с", attempt, delay)
                time.sleep(delay)
            try:
                return self._once(files, data)
            except TranscriptionError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
        assert last_error is not None
        raise last_error

    def _once(self, files: dict, data: dict) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = self._client.post(
                self.endpoint, headers=headers, files=files, data=data
            )
        except httpx.TimeoutException as exc:
            raise TranscriptionError(
                f"провайдер не ответил за {self.timeout_s:.0f} с", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError(
                f"сеть недоступна: {type(exc).__name__}", retryable=True
            ) from exc

        if response.status_code >= 400:
            raise _http_error(response)

        try:
            payload = response.json()
            if isinstance(payload, dict):
                self.last_language = str(payload.get("language") or "")
        except Exception:  # pragma: no cover — ответ разберёт _extract_text
            pass
        return _extract_text(response)

    def close(self) -> None:
        self._client.close()


def _host_of(url: str) -> str:
    try:
        return httpx.URL(url).host or url
    except Exception:  # pragma: no cover
        return url


def _http_error(response: httpx.Response) -> TranscriptionError:
    """Превращает ответ с ошибкой в понятное человеку сообщение."""
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
            detail = detail or str(payload.get("message") or "")
    except Exception:
        detail = (response.text or "")[:200]

    code = response.status_code
    if code == 401:
        message = "API-ключ отклонён (401)"
    elif code == 403:
        message = "доступ запрещён (403) — возможно, провайдер недоступен из вашей сети"
    elif code == 413:
        message = "запись слишком длинная для провайдера (413)"
    elif code == 429:
        message = "превышен лимит запросов (429)"
    else:
        message = f"провайдер вернул {code}"

    if detail:
        message = f"{message}: {detail}"
    return TranscriptionError(message, retryable=code in RETRYABLE_STATUS)


def _extract_text(response: httpx.Response) -> str:
    """Достаёт текст из ответа, не полагаясь на строгий формат."""
    try:
        payload = response.json()
    except Exception:
        return (response.text or "").strip()

    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text.strip()
        segments = payload.get("segments")
        if isinstance(segments, list):
            return " ".join(
                str(s.get("text", "")).strip() for s in segments if isinstance(s, dict)
            ).strip()
    elif isinstance(payload, str):
        return payload.strip()

    raise TranscriptionError("непонятный ответ провайдера — нет поля text")
