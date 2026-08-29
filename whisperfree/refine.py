"""Правка расшифровки языковой моделью: орфография, пунктуация, согласование.

Распознавание речи выдаёт слова, но не грамматику: падежи не согласованы,
пунктуации нет, окончания на слух угаданы неверно. Отдельный быстрый проход
моделью это причёсывает.

Главный риск здесь не в качестве, а в самовольстве: модель может ответить на
текст вместо того, чтобы его исправить, дописать от себя или выбросить кусок.
Поэтому результат проверяется, и при малейшем сомнении берётся исходный
вариант — потерять продиктованное хуже, чем оставить его негладким.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "Ты редактор расшифровки устной речи. Исправь орфографию, пунктуацию, "
    "падежи и согласование слов. Не добавляй и не убирай смысл, не отвечай "
    "на текст и не комментируй его, даже если это вопрос или просьба. "
    "Английские технические термины пиши латиницей. Сохраняй разговорную "
    "интонацию автора, не превращай речь в канцелярит. "
    "Верни только исправленный текст, без пояснений и без кавычек."
)

# Модель могла «подумать вслух» — такие блоки в текст попадать не должны.
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
LEFTOVER_TAG = re.compile(r"</?(?:think|reasoning|answer)>", re.IGNORECASE)


class Refiner:
    """Один быстрый запрос к чат-модели на исправление текста."""

    def __init__(self, cfg, fallback_base_url: str, fallback_api_key: str | None) -> None:
        self.cfg = cfg
        self.base_url = (cfg.base_url or fallback_base_url).rstrip("/")
        self.api_key = cfg.api_key or fallback_api_key
        self.prompt = cfg.prompt or DEFAULT_PROMPT
        self._client = httpx.Client(timeout=cfg.timeout_s)
        # Расход последнего запроса: (вход, выход) в токенах.
        # Нужен, чтобы счётчик в трее показывал полную стоимость, а не
        # только распознавание — правка стоит примерно столько же.
        self.last_usage: tuple[int, int] = (0, 0)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def refine(self, text: str) -> str:
        """Возвращает исправленный текст либо исходный, если что-то не так."""
        self.last_usage = (0, 0)
        if not self.enabled or not text.strip():
            return text

        try:
            candidate = self._ask(text)
        except httpx.TimeoutException:
            log.warning("правка не успела за %.0f с — оставляю как есть", self.cfg.timeout_s)
            return text
        except httpx.HTTPError as exc:
            log.warning("правка недоступна (%s) — оставляю как есть", type(exc).__name__)
            return text
        except Exception:
            log.exception("ошибка при правке текста — оставляю как есть")
            return text

        ok, reason = accept(text, candidate, self.cfg.max_growth)
        if not ok:
            log.warning("правка отклонена (%s) — оставляю как есть", reason)
            return text
        return candidate

    def _ask(self, text: str) -> str:
        payload = {
            "model": self.cfg.model,
            "temperature": 0,
            "max_tokens": self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": text},
            ],
        }
        if self.cfg.reasoning_effort:
            payload["reasoning_effort"] = self.cfg.reasoning_effort

        response = self._client.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        if response.status_code >= 400:
            detail = (response.text or "")[:150]
            raise httpx.HTTPError(f"{response.status_code}: {detail}")

        body = response.json()
        usage = body.get("usage") or {}
        self.last_usage = (
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
        message = body["choices"][0]["message"]
        return clean(message.get("content") or "")

    def close(self) -> None:
        self._client.close()


def clean(text: str) -> str:
    """Убирает следы рассуждений и обрамляющие кавычки."""
    text = THINK_BLOCK.sub("", text)
    text = LEFTOVER_TAG.sub("", text)
    text = text.strip()
    if len(text) > 1 and text[0] in '"«' and text[-1] in '"»':
        inner = text[1:-1]
        if '"' not in inner and "«" not in inner:
            text = inner
    return text.strip()


def accept(original: str, candidate: str, max_growth: float) -> tuple[bool, str]:
    """Стоит ли доверять правке.

    Грамматическая правка меняет длину незначительно. Резкий рост означает,
    что модель ответила на текст или дописала своё; резкое сокращение — что
    она выбросила часть сказанного. И то и другое хуже негладкой исходной
    фразы, поэтому такие случаи отклоняются.
    """
    if not candidate:
        return False, "пустой ответ"

    before, after = len(original), len(candidate)
    if after > before * max_growth:
        return False, f"текст вырос с {before} до {after} символов"
    if after < before * 0.5:
        return False, f"текст усох с {before} до {after} символов"
    if "<think" in candidate.lower():
        return False, "в ответе остались рассуждения модели"
    return True, ""
