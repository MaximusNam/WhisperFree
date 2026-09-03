"""Обучение на правках — и, главное, отказ учиться не тому.

Половина этих тестов проверяет не то, что программа запоминает, а то, что
она НЕ запоминает. Причина в цене ошибки: выученная замена уходит во все
будущие диктовки, и одна неверно понятая правка портит текст молча и без
конца. Числа в порогах взяты из замеров, а тесты стерегут границу.
"""

from __future__ import annotations

import json

import pytest

from whisperfree.config import LexiconConfig, PostprocessConfig
from whisperfree.lexicon import (
    DICTIONARY,
    RECOGNITION,
    REFINE,
    UNKNOWN,
    Lesson,
    Lexicon,
    blame,
    build_editor_prompt,
    build_prompt,
    can_be_rule,
    case_only,
    changes_script,
    diff_pairs,
    estimate_tokens,
    find_source,
    internal_capital,
    learnable,
    same_sound,
    sound,
)
from whisperfree.postprocess import Postprocessor


class FakeRecord:
    """Запись истории в объёме, который нужен обучению."""

    def __init__(self, text: str, raw: str = "", lang: str = "ru") -> None:
        self.text = text
        self.raw = raw
        self.lang = lang


def make(path, **kwargs) -> Lexicon:
    return Lexicon(path / "lexicon.json", LexiconConfig(**kwargs))


class TestSound:
    """Согласный скелет: одно слово в двух алфавитах должно совпасть."""

    @pytest.mark.parametrize(
        "cyrillic, latin",
        [
            ("докер", "Docker"),
            ("клод код", "Claude Code"),
            ("джемини", "Gemini"),
            ("гемини", "Gemini"),
            ("джира", "Jira"),
            ("гитхаб", "GitHub"),
            ("пул реквест", "pull request"),
            ("кубернетес", "Kubernetes"),
            ("эй пи ай", "API"),
            ("селери", "Celery"),
            ("нгинкс", "nginx"),
            ("энжиникс", "nginx"),
            ("визпер", "Whisper"),
            ("тайпскрипт", "TypeScript"),
            ("аутлук", "Outlook"),
            ("яндекс", "Yandex"),
        ],
    )
    def test_transliterated_term_sounds_the_same(self, cyrillic, latin):
        assert same_sound(cyrillic, latin), (
            f"{cyrillic!r} и {latin!r} должны звучать одинаково: "
            f"{sound(cyrillic)!r} против {sound(latin)!r}"
        )

    @pytest.mark.parametrize(
        "russian, english",
        [
            ("встреча", "meeting"),
            ("созвон", "call"),
            ("работа", "job"),
            ("сегодня", "today"),
            ("ошибка", "bug"),
            ("ветка", "branch"),
            ("выпуск", "release"),
            ("письмо", "email"),
        ],
    )
    def test_translation_sounds_different(self, russian, english):
        # Перевод слова — не ошибка распознавания, и звучит он иначе.
        assert not same_sound(russian, english)

    def test_soft_c_and_g_are_not_hard(self):
        # Без этого «селери» не сходится с Celery, а «джемини» — с Gemini.
        assert sound("Celery") == sound("селери")
        assert sound("Gemini") == sound("джемини")

    def test_silent_e_is_dropped(self):
        assert sound("Claude") == sound("клод")

    def test_doubling_is_not_heard(self):
        assert sound("pull") == sound("пул")


class TestWhatCanBeLearned:
    @pytest.mark.parametrize(
        "wrong, right",
        [
            ("докер", "Docker"),
            ("клод код", "Claude Code"),
            ("сожелению", "сожалению"),
            ("иван", "Иван"),
            ("Github", "GitHub"),
            ("зделал", "сделал"),
            ("все", "всё"),
        ],
    )
    def test_real_mistakes_are_learned(self, wrong, right):
        ok, why = learnable(wrong, right)
        assert ok, f"{wrong!r} → {right!r} должно запоминаться, а отказ: {why}"

    @pytest.mark.parametrize(
        "wrong, right",
        [
            ("сегодня", "завтра"),
            ("утром", "вечером"),
            ("встреча", "meeting"),
            ("созвон", "call"),
            ("и", "или"),
            ("работает", "сломалось"),
            ("быстро", "медленно"),
        ],
    )
    def test_rewording_is_refused(self, wrong, right):
        ok, _ = learnable(wrong, right)
        assert not ok, f"{wrong!r} → {right!r} запоминать нельзя: это другое слово"

    def test_punctuation_alone_is_refused(self):
        assert not learnable("—", "-")[0]

    def test_whole_phrase_is_refused(self):
        ok, why = learnable("надо это сделать быстро", "давайте закончим сегодня")
        assert not ok
        assert "слов" in why


class TestWhatBecomesARule:
    """Замена допустима лишь там, где правило верно в любом предложении."""

    @pytest.mark.parametrize(
        "wrong, right",
        [
            ("докер", "Docker"),
            ("гитхаб", "GitHub"),
            # Заглавная ВНУТРИ слова принадлежит самому термину.
            ("Github", "GitHub"),
            ("javascript", "JavaScript"),
        ],
    )
    def test_script_and_inner_case_changes_may_become_rules(self, wrong, right):
        assert can_be_rule(wrong, right)

    @pytest.mark.parametrize(
        "wrong, right",
        [
            ("были", "был"),
            ("сделаю", "сделаем"),
            ("мой", "мои"),
            ("сожелению", "сожалению"),
        ],
    )
    def test_same_script_never_becomes_a_rule(self, wrong, right):
        # И «был», и «были» — настоящие слова. Глобальная замена одного на
        # другое сломала бы каждый текст, где верным было исходное.
        # «сожелению» правилом тоже не станет: отличить опечатку от смены
        # слова внутри одного алфавита нечем, и цена промаха слишком высока.
        assert not can_be_rule(wrong, right)

    @pytest.mark.parametrize("wrong, right", [("все", "всё"), ("нем", "нём")])
    def test_yo_is_not_a_case_difference(self, wrong, right):
        """Проверка на живой истории: «все» → «всё» — самая частая правка (39
        раз из 641 записи). Стоит приравнять ё к е — и она станет глобальной
        заменой, после которой «все пришли» превратится в «всё пришли».
        Это разные слова, и правилом такая пара быть не может."""
        assert not case_only(wrong, right)
        assert not can_be_rule(wrong, right)

    def test_sentence_capital_does_not_become_a_rule(self):
        # Большая буква после точки принадлежит месту в предложении, а не
        # слову. Правило заставило бы писать его с большой и в середине фразы.
        assert case_only("звонок в дверь", "Звонок в дверь")
        assert not can_be_rule("звонок в дверь", "Звонок в дверь")
        assert not can_be_rule("иван", "Иван")

    def test_inner_capital_is_told_from_the_leading_one(self):
        assert internal_capital("GitHub")
        assert internal_capital("JavaScript")
        assert not internal_capital("Иван")
        assert not internal_capital("Звонок в дверь")

    def test_translation_is_not_a_rule_even_across_scripts(self):
        assert not can_be_rule("встреча", "meeting")

    def test_script_change_is_detected_both_ways(self):
        assert changes_script("докер", "Docker")
        assert changes_script("Docker", "докер")
        assert not changes_script("были", "был")

    def test_case_only_is_about_case_and_nothing_else(self):
        assert case_only("Github", "GitHub")
        assert not case_only("докер", "Docker")
        assert not case_only("мёржить", "мержить")


class TestDiff:
    def test_replaced_words_are_found(self):
        pairs = diff_pairs(
            "поставил докер и открыл гитхаб",
            "поставил Docker и открыл GitHub",
        )
        assert pairs == [("докер", "Docker"), ("гитхаб", "GitHub")]

    def test_insertions_and_deletions_are_ignored(self):
        # Выброшенное слово-паразит и добавленный союз — не ошибки написания.
        assert diff_pairs("ну вот такой текст", "вот такой текст") == []
        assert diff_pairs("вот такой текст", "вот и такой текст") == []

    def test_identical_texts_give_nothing(self):
        assert diff_pairs("один и тот же", "один и тот же") == []


class TestBlame:
    """Кто испортил слово: микрофон, модель или свой же словарь замен."""

    def test_model_broke_a_correct_word(self):
        # Верное слово в расшифровке было, а в готовом тексте его нет.
        assert blame("докер", "Docker", "Поставил Docker вчера") == REFINE

    def test_microphone_misheard(self):
        assert blame("докер", "Docker", "Поставил докер вчера") == RECOGNITION

    def test_no_refinement_means_microphone(self):
        # raw пустой, когда правка моделью ничего не изменила: испортить
        # слово могло только распознавание.
        assert blame("докер", "Docker", "") == RECOGNITION

    def test_own_dictionary_is_named(self):
        # Правило из конфига написало «Докер», а человек хочет «Docker».
        # Обвинять микрофон тут неправильно: человек пойдёт доучивать
        # программу против её же настройки.
        assert (
            blame("Докер", "Docker", "поставил докер", {"re:докер": "Докер"})
            == DICTIONARY
        )

    def test_unknown_when_neither_form_is_in_the_recognition(self):
        assert blame("докер", "Docker", "совсем другой текст") == UNKNOWN


class TestFindSource:
    def test_matching_record_is_found(self):
        records = [
            FakeRecord("Совсем про другое, про погоду."),
            FakeRecord("Поставил докер и открыл гитхаб."),
        ]
        record, ratio = find_source("Поставил Docker и открыл GitHub.", records)
        assert record is records[1]
        assert ratio > 0.6

    def test_foreign_text_is_not_matched(self):
        records = [FakeRecord("Поставил докер и открыл гитхаб.")]
        record, _ = find_source("Совершенно посторонний текст из письма.", records)
        assert record is None

    def test_selected_fragment_matches_its_record(self):
        # Выделить могли одно предложение из абзаца.
        long_text = (
            "Сначала я поставил докер, потом настроил сеть и проверил порты. "
            "Затем открыл гитхаб и создал пул реквест в основную ветку."
        )
        records = [FakeRecord(long_text)]
        record, ratio = find_source("Затем открыл GitHub и создал pull request", records)
        assert record is records[0], f"кусок не нашёлся, лучшее совпадение {ratio:.2f}"

    def test_unrelated_letter_is_not_taken_for_a_dictation(self):
        """Живой промах: постороннее деловое письмо совпало с диктовкой на
        0.663, и набралось это из 31 обрывка совпадений, из которых длиннее
        четырёх знаков были три. В русском тексте пробелы и частые буквы
        выстраиваются в цепочку почти с чем угодно, поэтому в долю совпадения
        идут только связные куски."""
        records = [
            FakeRecord(
                "Сначала я поставил докер, потом настроил сеть и проверил порты."
            )
        ]
        alien = (
            "Уважаемый коллега, направляю вам во вложении подписанный "
            "акт сверки за третий квартал."
        )
        record, ratio = find_source(alien, records)
        assert record is None, f"чужой текст принят за диктовку, совпадение {ratio:.2f}"

    def test_a_very_short_selection_is_not_matched_by_containment(self):
        # На двадцати знаках «сколько нашлось внутри» совпадает со слишком
        # многими записями, и выбор стал бы случайным.
        records = [FakeRecord("Сначала я поставил докер и проверил порты на сервере.")]
        record, _ = find_source("порты", records)
        assert record is None

    def test_the_newest_record_wins_a_tie(self):
        # Одно и то же продиктовали дважды; поправили, скорее всего, последнее.
        # recent() отдаёт свежие сверху, и при равном счёте берётся первая.
        newest = FakeRecord("поставил редис и проверил порты на сервере")
        older = FakeRecord("поставил редис и проверил порты на сервере")
        record, _ = find_source(
            "поставил Redis и проверил порты на сервере", [newest, older]
        )
        assert record is newest

    def test_empty_selection_finds_nothing(self):
        assert find_source("   ", [FakeRecord("что-нибудь")]) == (None, 0.0)

    def test_records_without_text_are_skipped(self):
        assert find_source("текст", [FakeRecord("")]) == (None, 0.0)


class TestLearning:
    def test_first_correction_is_a_hint_not_a_rule(self, tmp_path):
        lex = make(tmp_path)
        learned, refused = lex.learn("поставил докер", "поставил Docker")
        assert [item.right for item in learned] == ["Docker"]
        assert refused == []
        # Замены пока нет: одиночная правка живёт только в подсказке, где
        # она безвредна.
        assert lex.replacements() == {}
        assert lex.vocabulary() == ["Docker"]

    def test_second_correction_creates_the_rule(self, tmp_path):
        lex = make(tmp_path)
        lex.learn("поставил докер", "поставил Docker")
        lex.learn("снёс докер", "снёс Docker")
        assert lex.replacements() == {"докер": "Docker"}
        assert lex.lessons[0].hits == 2

    def test_min_hits_of_one_learns_immediately(self, tmp_path):
        lex = make(tmp_path, min_hits_for_rule=1)
        lex.learn("поставил докер", "поставил Docker")
        assert lex.replacements() == {"докер": "Docker"}

    def test_same_script_fix_never_becomes_a_rule_however_often(self, tmp_path):
        lex = make(tmp_path, min_hits_for_rule=1)
        for _ in range(5):
            lex.learn("к сожелению", "к сожалению")
        assert lex.replacements() == {}
        assert "сожалению" in lex.vocabulary()

    def test_rewritten_text_teaches_nothing(self, tmp_path):
        lex = make(tmp_path)
        learned, refused = lex.learn(
            "первое слово второе слово третье слово",
            "совсем иная фраза без единого совпадения тут",
        )
        assert learned == []
        assert refused

    def test_too_many_replacements_at_once_are_refused(self, tmp_path):
        # Замены должны быть РАЗДЕЛЕНЫ неизменным текстом: пять слов подряд
        # схлопнулись бы в одну замену, и сработала бы другая защита — от
        # правки фразы целиком.
        lex = make(tmp_path, max_per_press=2)
        learned, refused = lex.learn(
            "поставил докер, открыл гитхаб, запустил питон и поднял редис",
            "поставил Docker, открыл GitHub, запустил Python и поднял Redis",
        )
        assert learned == []
        assert "переписан" in refused[0]

    def test_a_run_of_words_is_refused_as_a_phrase(self, tmp_path):
        # Пять слов подряд приходят одной заменой, и её отклоняет уже предел
        # на длину: запоминать «докер гитхаб питон реакт редис» как одно
        # выражение бессмысленно, оно никогда не встретится снова.
        lex = make(tmp_path)
        learned, refused = lex.learn(
            "докер гитхаб питон реакт редис",
            "Docker GitHub Python React Redis",
        )
        assert learned == []
        assert "слов" in refused[0]

    def test_disabled_learning_does_nothing(self, tmp_path):
        lex = make(tmp_path, enabled=False)
        learned, refused = lex.learn("поставил докер", "поставил Docker")
        assert learned == []
        assert refused

    def test_blame_is_stored_with_the_lesson(self, tmp_path):
        lex = make(tmp_path)
        learned, _ = lex.learn(
            "поставил докер", "поставил Docker", raw="поставил Docker"
        )
        assert learned[0].kind == REFINE
        assert learned[0].blame_ru == "испортила правка моделью"


class TestPersistence:
    def test_lessons_survive_a_restart(self, tmp_path):
        lex = make(tmp_path)
        lex.learn("поставил докер", "поставил Docker")
        again = make(tmp_path)
        assert [item.right for item in again.lessons] == ["Docker"]

    def test_broken_file_does_not_break_startup(self, tmp_path):
        (tmp_path / "lexicon.json").write_text("{ это не json", encoding="utf-8")
        lex = make(tmp_path)
        assert lex.lessons == []
        # И журнал остаётся работоспособным: новая правка запишется поверх.
        lex.learn("поставил докер", "поставил Docker")
        assert len(lex) == 1

    def test_unknown_fields_from_a_newer_version_are_ignored(self, tmp_path):
        (tmp_path / "lexicon.json").write_text(
            json.dumps(
                {
                    "version": 99,
                    "lessons": [
                        {"wrong": "докер", "right": "Docker", "какое-то-новое-поле": 1}
                    ],
                }
            ),
            encoding="utf-8",
        )
        lex = make(tmp_path)
        assert [item.right for item in lex.lessons] == ["Docker"]

    def test_entries_without_both_sides_are_dropped(self, tmp_path):
        (tmp_path / "lexicon.json").write_text(
            json.dumps({"lessons": [{"wrong": "докер"}, {"right": "Docker"}]}),
            encoding="utf-8",
        )
        assert make(tmp_path).lessons == []

    def test_forget_removes_one_lesson(self, tmp_path):
        lex = make(tmp_path)
        lex.learn("поставил докер", "поставил Docker")
        assert lex.forget("докер", "Docker")
        assert lex.lessons == []
        assert make(tmp_path).lessons == [], "забытая правка вернулась после перезапуска"

    def test_forget_what_is_not_there(self, tmp_path):
        assert make(tmp_path).forget("докер", "Docker") is False

    def test_clear_forgets_everything(self, tmp_path):
        lex = make(tmp_path)
        lex.learn("докер и гитхаб тут", "Docker и GitHub тут")
        lex.clear()
        assert len(lex) == 0

    def test_rare_and_old_lessons_are_dropped_first(self, tmp_path):
        lex = make(tmp_path, max_entries=2, min_hits_for_rule=1)
        lex.learn("поставил докер", "поставил Docker")
        lex.learn("поставил докер", "поставил Docker")  # ×2, самая нужная
        lex.learn("открыл гитхаб", "открыл GitHub")
        lex.learn("поднял редис", "поднял Redis")
        kept = {item.right for item in lex.lessons}
        assert len(kept) == 2
        assert "Docker" in kept, "выбросили самую частую правку"


class TestPrompts:
    def test_terms_are_appended_to_the_config_prompt(self):
        result = build_prompt("Работаем с Docker.", ["Kubernetes", "nginx"])
        assert result == "Работаем с Docker. Kubernetes, nginx."

    def test_empty_term_list_leaves_the_prompt_alone(self):
        assert build_prompt("Работаем с Docker.", []) == "Работаем с Docker."

    def test_empty_base_gives_just_the_terms(self):
        assert build_prompt("", ["Kubernetes"]) == "Kubernetes"

    def test_budget_limits_the_vocabulary(self, tmp_path):
        lex = make(tmp_path, prompt_budget_tokens=4)
        lex.learn("докер и гитхаб", "Docker и GitHub")
        terms = lex.vocabulary(budget_tokens=4)
        assert len(terms) < 2, f"в 4 токена влезло слишком много: {terms}"

    def test_frequent_terms_come_first(self, tmp_path):
        lex = make(tmp_path)
        lex.learn("открыл гитхаб", "открыл GitHub")
        lex.learn("поставил докер", "поставил Docker")
        lex.learn("снёс докер", "снёс Docker")
        assert lex.vocabulary()[0] == "Docker"

    def test_recognizer_hints_can_be_switched_off(self, tmp_path):
        lex = make(tmp_path, teach_recognizer=False)
        lex.learn("поставил докер", "поставил Docker")
        assert lex.vocabulary() == []

    def test_editor_hears_only_about_its_own_mistakes(self, tmp_path):
        lex = make(tmp_path)
        # Эту испортила модель…
        lex.learn("поставил докер", "поставил Docker", raw="поставил Docker")
        # …а эту не расслышал микрофон, и модели про неё говорить незачем.
        lex.learn("открыл гитхаб", "открыл GitHub", raw="открыл гитхаб")
        notes = lex.editor_notes()
        assert notes == ["Docker (не «докер»)"]

    def test_editor_notes_can_be_switched_off(self, tmp_path):
        lex = make(tmp_path, teach_editor=False)
        lex.learn("поставил докер", "поставил Docker", raw="поставил Docker")
        assert lex.editor_notes() == []

    def test_editor_prompt_keeps_the_original_instruction(self):
        base = "Ты редактор расшифровки."
        result = build_editor_prompt(base, ["Docker (не «докер»)"])
        assert result.startswith(base)
        assert "Docker" in result

    def test_editor_prompt_without_notes_is_untouched(self):
        assert build_editor_prompt("Инструкция.", []) == "Инструкция."

    def test_token_estimate_is_not_below_reality(self):
        # Прикидка сознательно завышена: обрезанная провайдером затравка
        # теряет хвост молча.
        assert estimate_tokens("Kubernetes") >= 3
        assert estimate_tokens("кубернетес") > estimate_tokens("Kubernetes")


class TestWiringIntoPostprocess:
    def test_learned_replacement_is_applied(self):
        post = Postprocessor(PostprocessConfig(trailing_space=False))
        post.set_learned({"докер": "Docker"})
        assert post.process("поставил докер") == "поставил Docker"

    def test_config_wins_over_learned(self):
        # Человек написал своё правило руками и вправе рассчитывать, что
        # программа не спорит с ним самообучением.
        post = Postprocessor(
            PostprocessConfig(trailing_space=False, replacements={"докер": "ДОКЕР"})
        )
        post.set_learned({"докер": "Docker"})
        assert post.process("поставил докер") == "поставил ДОКЕР"

    def test_learned_can_be_taken_away(self):
        post = Postprocessor(PostprocessConfig(trailing_space=False))
        post.set_learned({"докер": "Docker"})
        post.set_learned({})
        assert post.process("поставил докер") == "поставил докер"

    def test_disabled_postprocess_ignores_learned(self):
        post = Postprocessor(PostprocessConfig(enabled=False, trailing_space=False))
        post.set_learned({"докер": "Docker"})
        assert post.process("поставил докер") == "поставил докер"

    def test_learned_rule_respects_word_boundaries(self):
        post = Postprocessor(PostprocessConfig(trailing_space=False))
        post.set_learned({"докер": "Docker"})
        # Падежная форма под правило не попадает — и это правильно:
        # «Dockerа» было бы хуже, чем «докера».
        assert post.process("нет докера") == "нет докера"


class TestDescription:
    def test_rule_and_hint_are_named_differently(self, tmp_path):
        lex = make(tmp_path)
        hint = Lesson(wrong="докер", right="Docker", kind=RECOGNITION, hits=1)
        rule = Lesson(wrong="докер", right="Docker", kind=RECOGNITION, hits=2)
        assert "подсказка" in lex.describe(hint)
        assert lex.describe(rule).count("замена") == 1

    def test_hopeless_hint_does_not_promise_a_rule(self, tmp_path):
        lex = make(tmp_path)
        # Правкой внутри одного алфавита замена не станет никогда, и обещать
        # её «со второго раза» было бы обманом.
        lesson = Lesson(wrong="были", right="был", kind=RECOGNITION, hits=9)
        assert lex.describe(lesson).count("замена") == 0

    def test_blame_is_in_the_description(self, tmp_path):
        lex = make(tmp_path)
        lesson = Lesson(wrong="докер", right="Docker", kind=REFINE, hits=1)
        assert "модель" in lex.describe(lesson)
