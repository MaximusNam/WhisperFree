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

    def test_search_finds_failures_by_reason(self, history):
        # У неудачи текст пустой, а причина лежит в error. Пока поиск не
        # смотрел в error, любой непустой запрос прятал все неудачи разом —
        # и «когда последний раз молчал микрофон» было не найти, хотя
        # неудачи стали писать в историю именно ради этого.
        history.add(rec("поставь докер", target_exe="notepad.exe"))
        history.add(rec("", audio_sec=4.0, error="микрофон молчал"))

        assert [r.error for r in history.search("микрофон")] == ["микрофон молчал"]

    def test_search_by_reason_does_not_drag_in_the_rest(self, history):
        history.add(rec("", error="сеть недоступна"))
        history.add(rec("поставь докер"))

        assert [r.text for r in history.search("докер")] == ["поставь докер"]

    def test_label_is_truncated_for_the_tray(self):
        record = rec("а" * 200)
        assert len(record.label(40)) <= 40 + len("00:00  ")

    def test_label_of_a_failure_names_the_reason(self):
        # Раньше в меню трея висело «16:40  (пусто)». В трей лезут как раз
        # тогда, когда ничего не вставилось, — и ответа там не находили.
        record = rec("", audio_sec=4.0, error="тишина: уровень 0.002 ниже порога 0.105")

        assert "тишина" in record.label(40)
        assert "(пусто)" not in record.label(40)

    def test_label_of_a_failure_still_fits_a_menu_item(self):
        # Причина бывает длиннее пункта меню — режем её так же, как текст.
        record = rec("", error="провайдер ответил ошибкой: " + "о" * 200)

        assert len(record.label(40)) <= 40 + len("00:00  ")

    def test_label_prefers_the_text_when_there_is_one(self):
        # У провала вставки текст есть; по этому пункту меню кликают, чтобы
        # вставить снова, — значит показывать надо его, а не причину.
        record = rec("поставь докер", error="нет активного окна")

        assert record.label(40).endswith("поставь докер")


class TestAmendingAfterAdd:
    """Часть записи выясняется уже после add.

    В историю пишут ДО вставки, чтобы не потерять текст, если приложение
    упадёт на вставке. Значит провал вставки приходится дописывать в уже
    записанную запись, и дописанное обязано дойти до файла: иначе одна и
    та же диктовка до перезапуска и после считается по-разному.
    """

    def test_add_returns_the_record(self, history):
        record = rec("привет")
        assert history.add(record) is record

    def test_field_set_after_add_reaches_the_file(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        record = History(path, cfg).add(rec("поставь докер", audio_sec=20.0))

        record.error = "окно не приняло вставку"

        assert History(path, cfg).records[0].error == "окно не приняло вставку"

    def test_cost_is_the_same_before_and_after_restart(self, tmp_path, cfg, now):
        record = History(tmp_path / "history.jsonl", cfg).add(
            rec("поставь докер", audio_sec=20.0, ts=now)
        )
        record.error = "окно не приняло вставку"

        live = History(tmp_path / "history.jsonl", cfg)
        # Расшифровку получили и оплатили, не прошла только вставка: такая
        # диктовка в счёт идёт — и до перезапуска, и после него.
        assert live.usage(0.04, 10)["today_seconds"] == 20.0
        assert live.usage(0.04, 10) == History(
            tmp_path / "history.jsonl", cfg
        ).usage(0.04, 10)

    def test_update_writes_several_fields_in_one_go(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        history = History(path, cfg)
        record = history.add(rec("привет"))

        assert history.update(record, error="буфер занят", target_exe="code.exe") is True

        got = History(path, cfg).records[0]
        assert (got.error, got.target_exe) == ("буфер занят", "code.exe")

    def test_update_rejects_a_field_that_does_not_exist(self, history):
        record = history.add(rec("привет"))
        with pytest.raises(ValueError):
            history.update(record, reason="опечатка в имени поля")

    def test_record_loaded_from_disk_is_editable_too(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        History(path, cfg).add(rec("привет"))

        History(path, cfg).records[0].error = "правка после перезапуска"

        assert History(path, cfg).records[0].error == "правка после перезапуска"

    def test_rotated_out_record_is_not_resurrected(self, tmp_path):
        cfg = HistoryConfig(max_records=3, retention_days=0)
        path = tmp_path / "history.jsonl"
        history = History(path, cfg)
        old = history.add(rec("самая старая"))
        for i in range(5):
            history.add(rec(f"ф{i}"))
        history.compact()

        assert history.update(old, error="через update") is False
        old.error = "через присваивание"

        written = path.read_text(encoding="utf-8")
        assert "самая старая" not in written
        assert "через" not in written

    def test_disabled_history_writes_nothing_after_amendment(self, tmp_path):
        path = tmp_path / "history.jsonl"
        record = History(path, HistoryConfig(enabled=False)).add(rec("секрет"))

        record.error = "и причина тоже секрет"

        assert not path.exists()

    def test_link_to_the_journal_does_not_leak_into_the_file(self, tmp_path, cfg):
        path = tmp_path / "history.jsonl"
        History(path, cfg).add(rec("привет"))

        payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert "_journal" not in payload


class TestUsage:
    def test_short_dictations_are_billed_at_the_minimum(self, history, now):
        # У Groq минимальный тарифицируемый отрезок 10 с: три диктовки по две
        # секунды стоят как тридцать секунд, а не как шесть.
        for _ in range(3):
            history.add(rec("ага", audio_sec=2.0, ts=now))

        stats = history.usage(price_per_hour=0.04, min_billed_seconds=10)
        assert stats["today_seconds"] == 30.0
        assert stats["today_usd"] == pytest.approx(30 / 3600 * 0.04)

    def test_a_failure_before_the_answer_is_not_billed(self, history, now):
        # Тишина, мёртвый микрофон, оборванная сеть: ответа провайдера не
        # было, платить не за что. Звук при этом записан — по одной только
        # длительности такую запись от оплаченной не отличить.
        history.add(rec("", audio_sec=20.0, error="сеть недоступна", ts=now))
        history.add(
            rec("", audio_sec=20.0, error="тишина: уровень 0.002 ниже порога 0.105",
                ts=now)
        )
        history.add(rec("", audio_sec=0.0, error="микрофон не открылся", ts=now))

        stats = history.usage(0.04, 10)
        assert stats["today_seconds"] == 0.0
        assert stats["today_count"] == 0.0

    def test_a_paid_failure_is_billed(self, history, now):
        # Ответ пришёл и оплачен, споткнулись уже на вставке. Раньше такая
        # диктовка выпадала из расходов целиком, и счётчик в трее занижал
        # траты тем сильнее, чем чаще не срабатывала вставка.
        history.add(
            rec("поставь докер", audio_sec=20.0, error="нет активного окна", ts=now)
        )

        stats = history.usage(0.04, 10)
        assert stats["today_seconds"] == 20.0
        assert stats["today_count"] == 1.0

    def test_a_thrown_away_transcription_is_billed_too(self, history, now):
        # Чистка убрала выдумку на тишине, вставлять стало нечего — но
        # ответ провайдера уже получен и оплачен, выброшенное лежит в raw.
        history.add(
            rec("", audio_sec=20.0, raw="Субтитры сделал DimaTorzok",
                error="выдумка на тишине: «Субтитры сделал…», не вставляю", ts=now)
        )

        assert history.usage(0.04, 10)["today_seconds"] == 20.0

    def test_a_failure_after_refinement_pays_for_both_models(self, history, now):
        # Правка вернула пустую строку: заплачено и за расшифровку, и за
        # токены правки — обе строки счёта обязаны остаться на месте.
        history.add(
            rec("", audio_sec=9.2, refine_in=218, refine_out=96,
                error="правка вернула пустой текст, не вставляю", ts=now)
        )

        stats = history.usage(0.04, 10, 0.15, 0.60)
        assert stats["today_stt_usd"] == pytest.approx(10 / 3600 * 0.04)
        assert stats["today_refine_usd"] == pytest.approx(
            218 / 1e6 * 0.15 + 96 / 1e6 * 0.60
        )

    def test_three_dictations_with_one_paid_failure(self, history, now):
        # Разбор запуском: три диктовки, у одной не прошла вставка. В счёт
        # идут все три — за неудачную заплачено ровно столько же.
        history.add(rec("первая", audio_sec=20.0, ts=now))
        history.add(rec("вторая", audio_sec=20.0, error="нет активного окна", ts=now))
        history.add(rec("третья", audio_sec=20.0, ts=now))

        stats = history.usage(0.04, 10)
        assert stats["today_count"] == 3.0
        assert stats["today_seconds"] == 60.0

    def test_yesterday_counts_for_the_month_but_not_for_today(self, history, now):
        # Граница суток — локальная полночь, а не «сутки назад».
        history.add(rec("вчера", audio_sec=20.0, ts=now - 86400))
        history.add(rec("сегодня", audio_sec=20.0, ts=now))

        stats = history.usage(0.04, 10)
        assert stats["today_seconds"] == 20.0
        assert stats["month_seconds"] == 40.0


# Все неудачи, какие умеет класть в историю рабочий поток, — по одной на
# строку, ровно в том виде, в каком он их пишет. Провайдера в этот момент
# ещё не спрашивали: звука нет, звук молчит или запрос не доехал.
UNPAID_FAILURES = [
    pytest.param(
        dict(text="", audio_sec=0.0, error="микрофон занят другим приложением"),
        id="микрофон не открылся",
    ),
    pytest.param(
        dict(
            text="",
            audio_sec=0.31,
            error="микрофон замолчал: держали 30.0 с, записано 0.31 с",
        ),
        id="микрофон замолчал посреди фразы",
    ),
    pytest.param(
        dict(
            text="", audio_sec=30.0, error="тишина: уровень 0.002 ниже порога 0.105"
        ),
        id="тишина, отправлять нечего",
    ),
    pytest.param(
        dict(
            text="",
            audio_sec=30.0,
            audio_file="20240612-120000-4231.flac",
            error="сеть недоступна",
        ),
        id="запрос не доехал: ответа нет",
    ),
]

# Здесь ответ провайдера уже получен и оплачен, а споткнулись мы после него.
# Первая строка — та самая, из-за которой признак оплаты и переделан: от
# ответа не осталось ни знака, и по следам такую запись не опознать.
PAID_FAILURES = [
    pytest.param(
        dict(
            text="",
            raw="",
            audio_sec=30.0,
            answered=True,
            error="провайдер вернул пустой ответ",
        ),
        id="ответ пустой: следа не осталось",
    ),
    pytest.param(
        dict(
            text="",
            raw="Субтитры сделал DimaTorzok",
            audio_sec=30.0,
            answered=True,
            error="выдумка на тишине: «Субтитры сделал…», не вставляю",
        ),
        id="выдумку на тишине выбросили",
    ),
    pytest.param(
        dict(
            text="",
            raw="поставь докер",
            refine_in=218,
            refine_out=96,
            audio_sec=30.0,
            answered=True,
            error="правка вернула пустой текст, не вставляю",
        ),
        id="правка вернула пустоту",
    ),
    pytest.param(
        dict(
            text="Поставь Docker. ",
            audio_sec=30.0,
            answered=True,
            target_exe="notepad.exe",
            error="нет активного окна",
        ),
        id="не прошла вставка",
    ),
]


class TestPaidFailures:
    """Счёт идёт по факту «провайдер ответил», а не по остаткам ответа.

    Каждый вид неудачи проверен отдельно: попал он в счёт ровно тогда, когда
    к провайдеру сходили и ответ получили. Раньше оплату опознавали по следам
    ответа в записи — тексту, raw, токенам правки, — и один оплаченный вид
    следа не оставлял вовсе, из-за чего счётчик в трее занижал траты.
    """

    @pytest.mark.parametrize("fields", UNPAID_FAILURES)
    def test_a_failure_before_the_answer_is_not_billed(self, history, now, fields):
        history.add(Record(ts=now, **fields))

        stats = history.usage(0.04, 10)
        assert stats["today_count"] == 0.0
        assert stats["today_seconds"] == 0.0

    @pytest.mark.parametrize("fields", PAID_FAILURES)
    def test_a_failure_after_the_answer_is_billed(self, history, now, fields):
        history.add(Record(ts=now, **fields))

        stats = history.usage(0.04, 10)
        assert stats["today_count"] == 1.0
        assert stats["today_seconds"] == 30.0

    def test_an_empty_answer_is_paid_for_all_the_same(self, history, now):
        # Тот самый вид, что выпадал из счёта. Ответ провайдера пуст, чистке
        # нечего чистить, в raw ложится та же пустота — в записи не остаётся
        # ни одного следа ответа, хотя запрос отработан и оплачен.
        history.add(
            rec("", audio_sec=30.0, raw="", answered=True,
                error="провайдер вернул пустой ответ", ts=now)
        )

        stats = history.usage(0.04, 10)
        assert stats["today_count"] == 1.0
        assert stats["today_seconds"] == 30.0

    def test_six_dictations_with_four_answers(self, history, now):
        # Разбор запуском, с которого началась правка: шесть диктовок по
        # 30 с, провайдер ответил на четыре. Счётчик показывал три.
        history.add(rec("первая", audio_sec=30.0, answered=True, ts=now))
        history.add(
            rec("вторая", audio_sec=30.0, answered=True,
                error="нет активного окна", ts=now)
        )
        history.add(
            rec("", audio_sec=30.0, answered=True, raw="Продолжение следует…",
                error="выдумка на тишине: «Продолжение следу…», не вставляю", ts=now)
        )
        history.add(
            rec("", audio_sec=30.0, answered=True, raw="",
                error="провайдер вернул пустой ответ", ts=now)
        )
        history.add(rec("", audio_sec=30.0, error="сеть недоступна", ts=now))
        history.add(
            rec("", audio_sec=30.0,
                error="тишина: уровень 0.002 ниже порога 0.105", ts=now)
        )

        stats = history.usage(0.04, 10)
        assert stats["today_count"] == 4.0
        assert stats["today_seconds"] == 120.0

    def test_the_duration_of_the_audio_does_not_decide(self, history, now):
        # Длительность меряется ДО отправки: у тишины она такая же, как у
        # диктовки, и признаком оплаты быть не может.
        history.add(
            rec("", audio_sec=3.0,
                error="тишина: уровень 0.002 ниже порога 0.105", ts=now)
        )
        history.add(rec("привет", audio_sec=3.0, answered=True, ts=now))

        stats = history.usage(0.04, 10)
        assert stats["today_count"] == 1.0
        assert stats["today_seconds"] == 10.0

    def test_the_answer_mark_survives_a_restart(self, tmp_path, cfg, now):
        # Оплаченная неудача обязана остаться оплаченной и после перезапуска:
        # счётчик, который меняется сам по себе, уже был отдельной поломкой.
        path = tmp_path / "history.jsonl"
        History(path, cfg).add(
            rec("", audio_sec=30.0, raw="", answered=True,
                error="провайдер вернул пустой ответ", ts=now)
        )

        assert History(path, cfg).usage(0.04, 10)["today_count"] == 1.0

    def test_an_old_journal_keeps_its_paid_failures(self, tmp_path, cfg, now):
        # В журнале, написанном до появления признака, поля нет вовсе. Там
        # оплату по-прежнему выдаёт след ответа — иначе прошлые траты
        # обнулились бы задним числом.
        path = tmp_path / "history.jsonl"
        lines = [
            {"ts": now, "text": "поставь докер", "audio_sec": 30.0,
             "error": "нет активного окна"},
            {"ts": now, "text": "", "audio_sec": 30.0, "raw": "Субтитры сделал…",
             "error": "выдумка на тишине: «Субтитры сделал…», не вставляю"},
            {"ts": now, "text": "", "audio_sec": 30.0,
             "error": "тишина: уровень 0.002 ниже порога 0.105"},
        ]
        path.write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
            encoding="utf-8",
        )

        history = History(path, cfg)
        assert [r.answered for r in history.records] == [False, False, False]
        assert history.usage(0.04, 10)["today_count"] == 2.0


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
