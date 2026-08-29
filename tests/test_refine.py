"""Правка расшифровки моделью.

Главный риск тут не качество, а самовольство: модель может ответить на текст
вместо того, чтобы его исправить, дописать от себя или выбросить кусок.
Потерять продиктованное хуже, чем оставить его негладким, поэтому при любом
сомнении должен возвращаться исходный текст.
"""

from __future__ import annotations

import httpx
import pytest

from whisperfree.config import RefineConfig
from whisperfree.refine import Refiner, accept, clean

ORIGINAL = "поставь докер и проверь через джемини почему пул реквест не проходит"
FIXED = "Поставь Docker и проверь через Gemini, почему pull request не проходит."


@pytest.fixture
def refiner(monkeypatch):
    monkeypatch.setenv("REFINE_KEY", "test-key")
    cfg = RefineConfig(enabled=True, api_key_env="REFINE_KEY")
    r = Refiner(cfg, "https://api.groq.com/openai/v1", None)
    yield r
    r.close()


def reply(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "https://x/chat/completions"),
    )


class Post:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class TestHappyPath:
    def test_returns_the_corrected_text(self, refiner, monkeypatch):
        monkeypatch.setattr(refiner._client, "post", Post(reply(FIXED)))
        assert refiner.refine(ORIGINAL) == FIXED

    def test_request_shape(self, refiner, monkeypatch):
        post = Post(reply(FIXED))
        monkeypatch.setattr(refiner._client, "post", post)
        refiner.refine(ORIGINAL)

        url, kwargs = post.calls[0]
        body = kwargs["json"]
        assert url.endswith("/chat/completions")
        assert body["model"] == "openai/gpt-oss-120b"
        assert body["temperature"] == 0
        assert body["reasoning_effort"] == "low"
        assert body["messages"][1]["content"] == ORIGINAL
        assert "не отвечай" in body["messages"][0]["content"].lower()

    def test_custom_prompt_replaces_the_builtin(self, monkeypatch):
        monkeypatch.setenv("REFINE_KEY", "k")
        cfg = RefineConfig(enabled=True, api_key_env="REFINE_KEY", prompt="Своя инструкция")
        r = Refiner(cfg, "https://x/v1", None)
        post = Post(reply(FIXED))
        monkeypatch.setattr(r._client, "post", post)
        r.refine(ORIGINAL)

        assert post.calls[0][1]["json"]["messages"][0]["content"] == "Своя инструкция"
        r.close()

    def test_falls_back_to_the_transcription_key(self, monkeypatch):
        monkeypatch.delenv("REFINE_KEY", raising=False)
        r = Refiner(RefineConfig(enabled=True), "https://x/v1", "ключ-распознавания")
        assert r.enabled
        assert r.api_key == "ключ-распознавания"
        r.close()


class TestDisabled:
    def test_off_by_config_returns_input_untouched(self, monkeypatch):
        r = Refiner(RefineConfig(enabled=False), "https://x/v1", "k")
        post = Post(reply(FIXED))
        monkeypatch.setattr(r._client, "post", post)

        assert r.refine(ORIGINAL) == ORIGINAL
        assert post.calls == []  # запрос даже не уходил
        r.close()

    def test_without_a_key_it_stays_off(self, monkeypatch):
        monkeypatch.delenv("REFINE_KEY", raising=False)
        r = Refiner(RefineConfig(enabled=True, api_key_env="REFINE_KEY"), "https://x/v1", None)
        assert not r.enabled
        assert r.refine(ORIGINAL) == ORIGINAL
        r.close()

    def test_empty_text_is_not_sent(self, refiner, monkeypatch):
        post = Post(reply(FIXED))
        monkeypatch.setattr(refiner._client, "post", post)
        assert refiner.refine("   ") == "   "
        assert post.calls == []


class TestNeverLosesTheDictation:
    """При любом сбое должен вернуться исходный текст, а не пустота."""

    def test_timeout(self, refiner, monkeypatch):
        monkeypatch.setattr(refiner._client, "post", Post(httpx.ReadTimeout("slow")))
        assert refiner.refine(ORIGINAL) == ORIGINAL

    def test_network_error(self, refiner, monkeypatch):
        monkeypatch.setattr(refiner._client, "post", Post(httpx.ConnectError("no route")))
        assert refiner.refine(ORIGINAL) == ORIGINAL

    def test_http_error(self, refiner, monkeypatch):
        monkeypatch.setattr(refiner._client, "post", Post(reply("", status=500)))
        assert refiner.refine(ORIGINAL) == ORIGINAL

    def test_malformed_response(self, refiner, monkeypatch):
        broken = httpx.Response(
            200, json={"nope": 1}, request=httpx.Request("POST", "https://x")
        )
        monkeypatch.setattr(refiner._client, "post", Post(broken))
        assert refiner.refine(ORIGINAL) == ORIGINAL

    def test_empty_answer(self, refiner, monkeypatch):
        monkeypatch.setattr(refiner._client, "post", Post(reply("   ")))
        assert refiner.refine(ORIGINAL) == ORIGINAL


class TestGuardsAgainstTheModelGoingRogue:
    def test_answering_instead_of_correcting_is_rejected(self, refiner, monkeypatch):
        # Модель приняла диктовку за вопрос и принялась отвечать.
        rogue = (
            "Конечно! Чтобы установить Docker, скачайте Docker Desktop с "
            "официального сайта, запустите установщик, перезагрузите компьютер, "
            "затем откройте терминал и выполните docker --version для проверки. "
            "После этого можно переходить к настройке контейнеров и docker-compose."
        )
        monkeypatch.setattr(refiner._client, "post", Post(reply(rogue)))
        assert refiner.refine(ORIGINAL) == ORIGINAL

    def test_dropping_half_the_text_is_rejected(self, refiner, monkeypatch):
        monkeypatch.setattr(refiner._client, "post", Post(reply("Поставь Docker.")))
        assert refiner.refine(ORIGINAL) == ORIGINAL

    def test_leaked_reasoning_is_rejected(self, refiner, monkeypatch):
        leaked = "<think>Надо исправить падежи и добавить запятые в этом тексте</think>"
        monkeypatch.setattr(refiner._client, "post", Post(reply(leaked)))
        assert refiner.refine(ORIGINAL) == ORIGINAL

    def test_slight_growth_is_fine(self, refiner, monkeypatch):
        # Пунктуация и заглавные буквы длину почти не меняют.
        monkeypatch.setattr(refiner._client, "post", Post(reply(FIXED)))
        assert refiner.refine(ORIGINAL) == FIXED


class TestAccept:
    def test_normal_correction_passes(self):
        assert accept(ORIGINAL, FIXED, 1.8)[0]

    def test_empty_is_refused(self):
        ok, reason = accept(ORIGINAL, "", 1.8)
        assert not ok and "пустой" in reason

    def test_growth_beyond_the_limit_is_refused(self):
        ok, reason = accept("короткая фраза", "х" * 200, 1.8)
        assert not ok and "вырос" in reason

    def test_shrinking_by_half_is_refused(self):
        ok, reason = accept("а" * 100, "а" * 40, 1.8)
        assert not ok and "усох" in reason

    def test_limit_is_configurable(self):
        grown = "а" * 30
        assert not accept("а" * 10, grown, 1.8)[0]
        assert accept("а" * 10, grown, 5.0)[0]


class TestClean:
    def test_strips_think_block(self):
        assert clean("<think>рассуждение</think>Готовый текст") == "Готовый текст"

    def test_strips_leftover_tags(self):
        assert clean("</think>Текст") == "Текст"

    def test_unwraps_quotes(self):
        assert clean('"Поставь Docker."') == "Поставь Docker."

    def test_keeps_inner_quotes(self):
        assert clean('он сказал "да"') == 'он сказал "да"'

    def test_plain_text_untouched(self):
        assert clean("  Поставь Docker.  ") == "Поставь Docker."
