"""Разбор лимитов провайдера.

Числа берутся из заголовков ответа, а не из документации: тариф у ключа
может отличаться. Здесь проверяется только разбор — сеть не трогаем.
"""

from __future__ import annotations

import pytest

from whisperfree.config import Config
from whisperfree.limits import (
    ceiling,
    format_limits,
    silent_probe,
    tokens_per_dictation,
)

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
    """Потолков два, и назвать только первый — значит обещать лишнее.

    Живой случай: счётчик запросов показывал 461 свободный из 1000, а запросы
    уже получали 429 «tokens per day (TPD): Limit 200000, Used 199672».
    Суточного бюджета токенов в заголовках нет вовсе, поэтому мы его не
    выдумываем, а показываем свой расход на диктовку и говорим, на что делить.
    """

    def test_narrower_limit_wins(self):
        # Каждая диктовка тратит по запросу к обеим моделям, поэтому
        # потолок по запросам задаёт меньший лимит.
        verdict = "\n".join(ceiling(STT, CHAT))
        assert "1000 диктовок" in verdict
        assert "правка" in verdict

    def test_transcription_can_be_the_bottleneck(self):
        verdict = "\n".join(ceiling({"limit-requests": "500"}, {"limit-requests": "9000"}))
        assert "500 диктовок" in verdict
        assert "распознавание" in verdict

    def test_without_refinement_only_transcription_counts(self):
        verdict = "\n".join(ceiling(STT, None))
        assert "2000 диктовок" in verdict
        assert "выключена" in verdict

    def test_missing_data_yields_no_claim(self):
        assert ceiling({}, CHAT) == []
        assert ceiling(STT, {"limit-tokens": "1"}) == []

    def test_the_request_ceiling_says_it_is_about_requests(self):
        # Прежняя формулировка «ПОТОЛОК: 1000 диктовок» читалась как обещание,
        # хотя это лимит только по числу запросов.
        verdict = "\n".join(ceiling(STT, CHAT))
        assert "ПОТОЛОК ПО ЗАПРОСАМ" in verdict

    def test_token_budget_is_named_when_the_spend_is_known(self):
        verdict = "\n".join(ceiling(STT, CHAT, tokens_per_dictation=268))
        assert "268" in verdict
        assert "ТОКЕН" in verdict.upper()
        # 200000 / 268 = 746, и это настоящий потолок вместо обещанной тысячи.
        assert "746" in verdict or "747" in verdict

    def test_without_measured_spend_nothing_is_invented(self):
        # Истории может не быть вовсе — тогда про токены мы молчим,
        # а не подставляем чужое среднее.
        verdict = "\n".join(ceiling(STT, CHAT, tokens_per_dictation=0))
        assert "ПОТОЛОК ПО ЗАПРОСАМ" in verdict
        assert "токен" not in verdict.lower()


class TestTokensPerDictation:
    """Расход считается по своей истории: чужое среднее тут бесполезно."""

    def test_zero_when_refinement_is_off(self):
        cfg = Config()
        cfg.refine.enabled = False
        assert tokens_per_dictation(cfg) == 0.0

    def test_averages_the_recorded_spend(self, tmp_path, monkeypatch):
        from whisperfree import config as config_mod
        from whisperfree.history import History, Record

        path = tmp_path / "history.jsonl"
        monkeypatch.setattr(config_mod, "history_path", lambda: path)

        cfg = Config()
        cfg.refine.enabled = True
        history = History(path, cfg.history)
        for prompt_tokens, completion in ((200, 60), (240, 64)):
            history.add(
                Record(ts=0.0, text="проверка", refine_in=prompt_tokens, refine_out=completion)
            )

        assert tokens_per_dictation(cfg) == pytest.approx(282.0)

    def test_records_without_refinement_are_skipped(self, tmp_path, monkeypatch):
        # Диктовки, сделанные до включения правки, расход не показывают —
        # усреднять их вместе с нулями значило бы занизить цифру вдвое.
        from whisperfree import config as config_mod
        from whisperfree.history import History, Record

        path = tmp_path / "history.jsonl"
        monkeypatch.setattr(config_mod, "history_path", lambda: path)

        cfg = Config()
        cfg.refine.enabled = True
        history = History(path, cfg.history)
        history.add(Record(ts=0.0, text="без правки"))
        history.add(Record(ts=0.0, text="с правкой", refine_in=200, refine_out=60))

        assert tokens_per_dictation(cfg) == pytest.approx(260.0)

    def test_missing_history_is_not_an_error(self, tmp_path, monkeypatch):
        from whisperfree import config as config_mod

        monkeypatch.setattr(config_mod, "history_path", lambda: tmp_path / "нет.jsonl")
        cfg = Config()
        cfg.refine.enabled = True
        assert tokens_per_dictation(cfg) == 0.0


def test_probe_is_a_valid_flac_second():
    import io

    import soundfile as sf

    data, rate = sf.read(io.BytesIO(silent_probe()), dtype="int16")
    assert rate == 16000
    assert len(data) == 16000
