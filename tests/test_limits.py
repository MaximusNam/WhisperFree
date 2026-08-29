"""Разбор лимитов провайдера.

Числа берутся из заголовков ответа, а не из документации: тариф у ключа
может отличаться. Здесь проверяется только разбор — сеть не трогаем.
"""

from __future__ import annotations

import pytest

from whisperfree.limits import ceiling, format_limits, silent_probe

# Реальные заголовки Groq по бесплатному ключу.
STT = {
    "limit-requests": "2000",
    "remaining-requests": "1999",
    "reset-requests": "43.2s",
}
CHAT = {
    "limit-requests": "1000",
    "remaining-requests": "999",
    "reset-requests": "1m26.4s",
    "limit-tokens": "8000",
    "remaining-tokens": "7923",
    "reset-tokens": "577ms",
}


class TestFormat:
    def test_shows_left_of_total_and_spent(self):
        lines = format_limits(STT)
        assert len(lines) == 1
        assert "1999 из 2000" in lines[0]
        assert "израсходовано 1" in lines[0]
        assert "43.2s" in lines[0]

    def test_tokens_and_requests_both_shown(self):
        lines = format_limits(CHAT)
        assert len(lines) == 2
        assert any("запросов" in l for l in lines)
        assert any("токенов" in l for l in lines)

    def test_missing_headers_are_reported_not_faked(self):
        assert "не сообщает" in format_limits({})[0]

    def test_unparseable_numbers_do_not_crash(self):
        lines = format_limits({"limit-requests": "много", "remaining-requests": "?"})
        assert "много" in lines[0]


class TestCeiling:
    def test_narrower_limit_wins(self):
        # Каждая диктовка тратит по запросу к обеим моделям, поэтому
        # потолок задаёт меньший лимит.
        verdict = ceiling(STT, CHAT)
        assert "1000 диктовок" in verdict
        assert "правка" in verdict

    def test_transcription_can_be_the_bottleneck(self):
        verdict = ceiling({"limit-requests": "500"}, {"limit-requests": "9000"})
        assert "500 диктовок" in verdict
        assert "распознавание" in verdict

    def test_without_refinement_only_transcription_counts(self):
        verdict = ceiling(STT, None)
        assert "2000 диктовок" in verdict
        assert "выключена" in verdict

    def test_missing_data_yields_no_claim(self):
        assert ceiling({}, CHAT) is None
        assert ceiling(STT, {"limit-tokens": "1"}) is None


def test_probe_is_a_valid_flac_second():
    import io

    import soundfile as sf

    data, rate = sf.read(io.BytesIO(silent_probe()), dtype="int16")
    assert rate == 16000
    assert len(data) == 16000
