"""Адаптер к OpenAI-совместимому endpoint. httpx замокан целиком."""

from __future__ import annotations

import httpx
import pytest

from whisperfree.providers import TranscriptionError, TranscriptionRequest
from whisperfree.providers.openai_compat import OpenAICompatProvider


@pytest.fixture
def provider():
    p = OpenAICompatProvider(
        base_url="https://api.groq.com/openai/v1",
        model="whisper-large-v3-turbo",
        api_key="test-key",
        timeout_s=5.0,
        max_retries=2,
    )
    yield p
    p.close()


def request() -> TranscriptionRequest:
    return TranscriptionRequest(
        audio=b"fake-flac", filename="speech.flac", language="ru", prompt="затравка"
    )


def response(status: int, json_body=None, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json_body,
        text=text if json_body is None else None,
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions"),
    )


class Recorder:
    """Подменяет client.post и запоминает, что уходило на сервер."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class TestSuccess:
    def test_returns_text(self, provider, monkeypatch):
        monkeypatch.setattr(provider._client, "post", Recorder(response(200, {"text": " привет "})))
        assert provider.transcribe(request()) == "привет"

    def test_sends_model_language_and_prompt(self, provider, monkeypatch):
        recorder = Recorder(response(200, {"text": "ок"}))
        monkeypatch.setattr(provider._client, "post", recorder)
        provider.transcribe(request())

        url, kwargs = recorder.calls[0]
        assert url == "https://api.groq.com/openai/v1/audio/transcriptions"
        assert kwargs["data"]["model"] == "whisper-large-v3-turbo"
        assert kwargs["data"]["language"] == "ru"
        # Затравка — главный рычаг качества на русско-английской смеси.
        assert kwargs["data"]["prompt"] == "затравка"
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["files"]["file"][0] == "speech.flac"

    def test_language_omitted_when_not_set(self, provider, monkeypatch):
        recorder = Recorder(response(200, {"text": "ок"}))
        monkeypatch.setattr(provider._client, "post", recorder)
        provider.transcribe(TranscriptionRequest(audio=b"x", filename="a.flac"))
        assert "language" not in recorder.calls[0][1]["data"]

    def test_falls_back_to_segments(self, provider, monkeypatch):
        body = {"segments": [{"text": " раз "}, {"text": "два"}]}
        monkeypatch.setattr(provider._client, "post", Recorder(response(200, body)))
        assert provider.transcribe(request()) == "раз два"

    def test_plain_text_response(self, provider, monkeypatch):
        monkeypatch.setattr(provider._client, "post", Recorder(response(200, text="привет")))
        assert provider.transcribe(request()) == "привет"


class TestFailures:
    def test_missing_key_is_reported_clearly(self):
        p = OpenAICompatProvider("https://x/v1", "m", api_key=None)
        with pytest.raises(TranscriptionError, match="нет API-ключа"):
            p.transcribe(request())
        p.close()

    def test_401_is_not_retried(self, provider, monkeypatch):
        recorder = Recorder(response(401, {"error": {"message": "Invalid API Key"}}))
        monkeypatch.setattr(provider._client, "post", recorder)

        with pytest.raises(TranscriptionError, match="401"):
            provider.transcribe(request())
        assert len(recorder.calls) == 1

    def test_403_hints_at_region_block(self, provider, monkeypatch):
        monkeypatch.setattr(provider._client, "post", Recorder(response(403, {})))
        with pytest.raises(TranscriptionError, match="недоступен из вашей сети"):
            provider.transcribe(request())

    def test_429_is_retried_then_succeeds(self, provider, monkeypatch):
        recorder = Recorder(response(429, {}), response(200, {"text": "получилось"}))
        monkeypatch.setattr(provider._client, "post", recorder)

        assert provider.transcribe(request()) == "получилось"
        assert len(recorder.calls) == 2

    def test_retries_are_bounded(self, provider, monkeypatch):
        recorder = Recorder(response(503, {}))
        monkeypatch.setattr(provider._client, "post", recorder)

        with pytest.raises(TranscriptionError):
            provider.transcribe(request())
        assert len(recorder.calls) == 3  # первый заход плюс два повтора

    def test_timeout_is_retryable_and_readable(self, provider, monkeypatch):
        recorder = Recorder(httpx.ReadTimeout("too slow"))
        monkeypatch.setattr(provider._client, "post", recorder)

        with pytest.raises(TranscriptionError, match="не ответил"):
            provider.transcribe(request())
        assert len(recorder.calls) == 3

    def test_network_error_is_readable(self, provider, monkeypatch):
        monkeypatch.setattr(provider._client, "post", Recorder(httpx.ConnectError("no route")))
        with pytest.raises(TranscriptionError, match="сеть недоступна"):
            provider.transcribe(request())

    def test_server_detail_is_surfaced(self, provider, monkeypatch):
        body = {"error": {"message": "audio file too short"}}
        monkeypatch.setattr(provider._client, "post", Recorder(response(400, body)))

        with pytest.raises(TranscriptionError, match="audio file too short"):
            provider.transcribe(request())

    def test_unparseable_success_body_is_an_error(self, provider, monkeypatch):
        monkeypatch.setattr(provider._client, "post", Recorder(response(200, {"nope": 1})))
        with pytest.raises(TranscriptionError, match="нет поля text"):
            provider.transcribe(request())


class TestEndpoint:
    @pytest.mark.parametrize(
        "base, expected",
        [
            ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1/audio/transcriptions"),
            ("https://api.openai.com/v1/", "https://api.openai.com/v1/audio/transcriptions"),
            ("http://localhost:8000/v1", "http://localhost:8000/v1/audio/transcriptions"),
        ],
    )
    def test_trailing_slash_does_not_matter(self, base, expected):
        p = OpenAICompatProvider(base, "m", "k")
        assert p.endpoint == expected
        p.close()

    def test_name_is_the_host(self):
        p = OpenAICompatProvider("https://api.groq.com/openai/v1", "m", "k")
        assert p.name == "api.groq.com"
        p.close()
