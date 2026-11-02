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

# Сколько ждать установки соединения. TCP до Groq — около 25 мс, TLS — сотни
# миллисекунд; пять секунд — с запасом даже для раздачи с телефона. Соединение,
# не установленное за пять секунд, не установится и за тридцать.
CONNECT_TIMEOUT_S = 5.0

# Сколько ждать ответа: столько на любую запись плюс столько на секунду звука,
# но не дольше [provider].timeout_s. Замер по журналу пользователя, 3061
# распознавание: медиана 0.3 с, 99,9 % — до 1.7 с, 228 секунд звука — 1.9 с.
# Дольше 2.3 с был один запрос из 3061 — 21 с на пяти секундах, на медленной
# сети; только он и выходит за этот срок, и теперь он ушёл бы в отложенные,
# а не держал плашку.
#
# Раньше ждали 30 с на каждую операцию и трижды подряд. Живой случай 02.11:
# сеть с телефона пропала, и плашка «Распознаю…» висела 127 секунд, а
# следующие диктовки стояли в очереди за ней.
RESPONSE_BASE_S = 10.0
RESPONSE_PER_AUDIO_S = 0.1

# Повторов, когда виновата связь. Один — чтобы пережить случайный сбой
# рукопожатия. Больше бессмысленно: сеть лежит, а звук всё равно уходит в
# отложенные (whisperfree.pending) и отправится сам, когда она вернётся.
NETWORK_RETRIES = 1


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
        self._client = httpx.Client(timeout=timeout_s)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/audio/transcriptions"

    def transcribe(self, request: TranscriptionRequest) -> str:
        if not self.api_key:
            raise TranscriptionError(
                "нет API-ключа — задайте его в .env рядом с программой"
            )

        wait = self.response_timeout(request.duration_s)
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S, read=wait, write=wait, pool=CONNECT_TIMEOUT_S
        )
        files = {"file": (request.filename, request.audio, "application/octet-stream")}
        data: dict[str, str] = {"model": self.model, "response_format": "json"}
        if request.language:
            data["language"] = request.language
        if request.prompt:
            data["prompt"] = request.prompt

        attempt = 0
        while True:
            try:
                return self._once(files, data, timeout, wait)
            except TranscriptionError as exc:
                budget = NETWORK_RETRIES if exc.network else self.max_retries
                if not exc.retryable or attempt >= min(budget, self.max_retries):
                    raise
            attempt += 1
            delay = min(4.0, 0.4 * (2 ** (attempt - 1))) + random.uniform(0, 0.2)
            log.info("повтор запроса %d через %.1f с", attempt, delay)
            time.sleep(delay)

    def response_timeout(self, duration_s: float) -> float:
        """Сколько ждать ответа на запись такой длины — см. RESPONSE_BASE_S."""
        wait = RESPONSE_BASE_S + RESPONSE_PER_AUDIO_S * max(0.0, float(duration_s or 0.0))
        return min(self.timeout_s, wait)

    def _once(self, files: dict, data: dict, timeout: httpx.Timeout, wait: float) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = self._client.post(
                self.endpoint, headers=headers, files=files, data=data, timeout=timeout
            )
        except httpx.ConnectTimeout as exc:
            raise TranscriptionError(
                "сеть недоступна: соединение не установилось за "
                f"{CONNECT_TIMEOUT_S:.0f} с",
                retryable=True, deferrable=True, network=True,
            ) from exc
        except httpx.TimeoutException as exc:
            # Запрос ушёл, а ответа нет. Повторять тут же не будем: ждали уже
            # долго, а звук не потеряется — он уйдёт в отложенные.
            raise TranscriptionError(
                f"провайдер не ответил за {wait:.0f} с", deferrable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError(
                f"сеть недоступна: {type(exc).__name__}",
                retryable=True, deferrable=True, network=True,
            ) from exc

        if response.status_code >= 400:
            raise _http_error(response)

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
    passing = code in RETRYABLE_STATUS
    return TranscriptionError(message, retryable=passing, deferrable=passing)


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
