"""История — страховка от потери текста. Ломаться ей нельзя.

Проверяем ровно те свойства, ради которых она существует: запись переживает
перезапуск, битая строка не уносит с собой остальные записи, а повторные
нажатия хоткея идут вглубь истории.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from whisperfree.config import HistoryConfig
from whisperfree.history import History, Record

# Момент, на котором останавливаются часы в тестах расходов. Полдень
# двенадцатого числа выбран нарочно: он далеко и от полуночи, и от границы
# месяца, и от перевода часов, поэтому «сегодня» и «этот месяц» считаются
# одинаково в любом часовом поясе.
FROZEN_NOW = datetime(2024, 6, 12, 12, 0, 0)


@pytest.fixture
def cfg():
    return HistoryConfig(
        enabled=True, max_records=100, retention_days=0, cycle_reset_s=3.0
    )


@pytest.fixture
def history(tmp_path, cfg):
    return History(tmp_path / "history.jsonl", cfg)


@pytest.fixture
def now(monkeypatch) -> float:
    """Останавливает часы, по которым usage() отбивает сутки и месяц.

    usage() берёт границу суток от локальной полуночи в момент вызова, а
    записи создаются раньше. Прогон, пересёкший полночь между этими двумя
    моментами, насчитал бы за сегодня ноль — тест падал бы раз в сутки без
    всякой поломки кода, а на границе месяца ронял бы ещё и месячный счёт.
    Заморозка убирает зависимость от момента запуска и от часового пояса,
    не трогая саму арифметику: границы считаются тем же кодом, что и в бою.

    Возвращает штамп времени, который тесты ставят своим записям.
    """

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW

    monkeypatch.setattr("whisperfree.history.datetime", Frozen)
    return FROZEN_NOW.timestamp()


def rec(text: str, **kwargs) -> Record:
    return Record(ts=kwargs.pop("ts", time.time()), text=text, **kwargs)


class TestPersistence:
    def test_record_survives_restart(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        History(path, cfg).add(rec("поставь докер"))

        reloaded = History(path, cfg)
        assert [r.text for r in reloaded.records] == ["поставь докер"]

    def test_all_fields_round_trip(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        History(path, cfg).add(
            rec("текст", lang="ru", provider="api.groq.com", model="whisper",
                audio_sec=4.2, target_exe="notepad.exe", error="")
        )
        got = History(path, cfg).records[0]
        assert (got.lang, got.provider, got.audio_sec, got.target_exe) == (
            "ru", "api.groq.com", 4.2, "notepad.exe"
        )

    def test_corrupted_line_does_not_lose_the_rest(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        good = json.dumps({"ts": 1.0, "text": "первая"}, ensure_ascii=False)
        later = json.dumps({"ts": 3.0, "text": "третья"}, ensure_ascii=False)
        path.write_text(f"{good}\n{{ обрыв записи\n{later}\n", encoding="utf-8")

        assert [r.text for r in History(path, cfg).records] == ["первая", "третья"]

    def test_unknown_field_from_newer_version_is_ignored(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        path.write_text(
            json.dumps({"ts": 1.0, "text": "привет", "какое_то_новое_поле": 1}) + "\n",
            encoding="utf-8",
        )
        assert History(path, cfg).records[0].text == "привет"

    def test_disabled_history_writes_nothing(self, tmp_path):
        path = tmp_path / "history.jsonl"
        History(path, HistoryConfig(enabled=False)).add(rec("секрет"))
        assert not path.exists()


class TestRotation:
    def test_max_records_trims_oldest(self, tmp_path):
        cfg = HistoryConfig(max_records=5, retention_days=0)
        history = History(tmp_path / "history.jsonl", cfg)
        for i in range(60):
            history.add(rec(f"фраза {i}"))
        history.compact()

        texts = [r.text for r in history.records]
        assert len(texts) == 5
        assert texts[-1] == "фраза 59"

    def test_retention_drops_old_records(self, tmp_path):
        cfg = HistoryConfig(max_records=1000, retention_days=7)
        history = History(tmp_path / "history.jsonl", cfg)
        history.add(rec("старьё", ts=time.time() - 30 * 86400))
        history.add(rec("свежее"))
        history.compact()

        assert [r.text for r in history.records] == ["свежее"]

    def test_compaction_is_persisted(self, tmp_path):
        cfg = HistoryConfig(max_records=3, retention_days=0)
        path = tmp_path / "history.jsonl"
        history = History(path, cfg)
        for i in range(10):
            history.add(rec(f"ф{i}"))
        history.compact()

        assert len(History(path, cfg).records) == 3


class TestPasteCycle:
    def test_first_press_returns_last(self, history):
        history.add(rec("первая"))
        history.add(rec("вторая"))
        assert history.next_for_paste().text == "вторая"

    def test_consecutive_presses_walk_backwards(self, history):
        for text in ("одна", "две", "три", "четыре", "пять"):
            history.add(rec(text))

        walked = [history.next_for_paste().text for _ in range(5)]
        assert walked == ["пять", "четыре", "три", "две", "одна"]

    def test_walk_stops_at_oldest_instead_of_wrapping(self, history):
        history.add(rec("одна"))
        history.add(rec("две"))
        assert [history.next_for_paste().text for _ in range(4)] == [
            "две", "одна", "одна", "одна"
        ]

    def test_pause_resets_to_latest(self, tmp_path):
        cfg = HistoryConfig(cycle_reset_s=0.05)
        history = History(tmp_path / "history.jsonl", cfg)
        history.add(rec("одна"))
        history.add(rec("две"))

        assert history.next_for_paste().text == "две"
        assert history.next_for_paste().text == "одна"
        time.sleep(0.08)
        assert history.next_for_paste().text == "две"

    def test_new_dictation_resets_the_cycle(self, history):
        history.add(rec("одна"))
        history.add(rec("две"))
        history.next_for_paste()
        history.next_for_paste()

        history.add(rec("три"))
        assert history.next_for_paste().text == "три"

    def test_empty_history_returns_none(self, history):
        assert history.next_for_paste() is None


class TestQueries:
    def test_recent_is_newest_first(self, history):
        for text in ("одна", "две", "три"):
            history.add(rec(text))
        assert [r.text for r in history.recent(2)] == ["три", "две"]

    def test_search_matches_text_and_app(self, history):
        history.add(rec("поставь докер", target_exe="notepad.exe"))
        history.add(rec("привет", target_exe="chrome.exe"))

        assert [r.text for r in history.search("докер")] == ["поставь докер"]
        assert [r.text for r in history.search("chrome")] == ["привет"]

    def test_label_is_truncated_for_the_tray(self, history):
        record = rec("а" * 200)
        assert len(record.label(40)) <= 40 + len("00:00  ")


class TestUsage:
    def test_short_dictations_are_billed_at_the_minimum(self, history, now):
        # У Groq минимальный тарифицируемый отрезок 10 с: три диктовки по две
        # секунды стоят как тридцать секунд, а не как шесть.
        for _ in range(3):
            history.add(rec("ага", audio_sec=2.0, ts=now))

        stats = history.usage(price_per_hour=0.04, min_billed_seconds=10)
        assert stats["today_seconds"] == 30.0
        assert stats["today_usd"] == pytest.approx(30 / 3600 * 0.04)

    def test_failed_records_are_not_billed(self, history, now):
        history.add(rec("", audio_sec=20.0, error="сеть недоступна", ts=now))
        assert history.usage(0.04, 10)["today_seconds"] == 0.0

    def test_yesterday_counts_for_the_month_but_not_for_today(self, history, now):
        # Граница суток — локальная полночь, а не «сутки назад».
        history.add(rec("вчера", audio_sec=20.0, ts=now - 86400))
        history.add(rec("сегодня", audio_sec=20.0, ts=now))

        stats = history.usage(0.04, 10)
        assert stats["today_seconds"] == 20.0
        assert stats["month_seconds"] == 40.0


class TestFullCost:
    """Счётчик должен включать обе модели.

    Замер на реальных диктовках: правка стоит примерно столько же, сколько
    распознавание, — показывать только вторую значило бы вдвое занижать сумму.
    """

    PRICE_IN = 0.15   # $/млн токенов входа
    PRICE_OUT = 0.60  # $/млн токенов выхода

    def test_refine_tokens_add_to_the_bill(self, history, now):
        history.add(rec("привет", audio_sec=9.2, refine_in=218, refine_out=96, ts=now))
        stats = history.usage(0.04, 10, self.PRICE_IN, self.PRICE_OUT)

        expected_stt = 10 / 3600 * 0.04           # 9.2 с округляются до минимума
        expected_refine = 218 / 1e6 * 0.15 + 96 / 1e6 * 0.60
        assert stats["today_stt_usd"] == pytest.approx(expected_stt)
        assert stats["today_refine_usd"] == pytest.approx(expected_refine)
        assert stats["today_usd"] == pytest.approx(expected_stt + expected_refine)

    def test_refinement_is_comparable_to_transcription(self, history, now):
        # Не деталь реализации, а факт из замеров: если правка вдруг станет
        # на порядок дороже, это повод заметить.
        history.add(rec("привет", audio_sec=9.2, refine_in=218, refine_out=96, ts=now))
        stats = history.usage(0.04, 10, self.PRICE_IN, self.PRICE_OUT)

        ratio = stats["today_refine_usd"] / stats["today_stt_usd"]
        assert 0.3 < ratio < 3.0

    def test_without_refinement_only_transcription_counts(self, history, now):
        history.add(rec("привет", audio_sec=9.2, ts=now))
        stats = history.usage(0.04, 10, self.PRICE_IN, self.PRICE_OUT)

        assert stats["today_refine_usd"] == 0.0
        assert stats["today_usd"] == stats["today_stt_usd"]

    def test_zero_prices_keep_the_old_behaviour(self, history, now):
        history.add(rec("привет", audio_sec=9.2, refine_in=218, refine_out=96, ts=now))
        stats = history.usage(0.04, 10)
        assert stats["today_usd"] == pytest.approx(10 / 3600 * 0.04)
