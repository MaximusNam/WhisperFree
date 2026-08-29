"""Доводка текста — то, что превращает «Джемини» обратно в «Gemini»."""

from __future__ import annotations

import pytest

from whisperfree.config import PostprocessConfig
from whisperfree.postprocess import (
    Postprocessor,
    apply_replacements,
    compile_replacements,
    is_hallucination,
    tidy,
)


def make(**kwargs) -> Postprocessor:
    cfg = PostprocessConfig(
        enabled=kwargs.pop("enabled", True),
        trailing_space=kwargs.pop("trailing_space", False),
        drop_hallucinations=kwargs.pop("drop_hallucinations", True),
        replacements=kwargs.pop("replacements", {}),
    )
    return Postprocessor(cfg)


class TestReplacements:
    def test_cyrillic_term_becomes_latin(self):
        p = make(replacements={"джемини": "Gemini"})
        assert p.process("проверь через джемини") == "проверь через Gemini"

    def test_longer_key_wins_over_shorter(self):
        # «клод код» должен победить «клод», иначе получится «Claude код».
        p = make(replacements={"клод": "Claude", "клод код": "Claude Code"})
        assert p.process("запусти клод код") == "запусти Claude Code"

    def test_lowercase_target_keeps_sentence_capital(self):
        p = make(replacements={"коммит": "commit"})
        assert p.process("Коммит готов") == "Commit готов"

    def test_target_with_own_capitals_is_untouched(self):
        p = make(replacements={"гитхаб": "GitHub"})
        assert p.process("Гитхаб лежит") == "GitHub лежит"

    def test_word_boundaries_prevent_partial_match(self):
        p = make(replacements={"код": "code"})
        assert p.process("кодировка не меняется") == "кодировка не меняется"

    def test_case_insensitive(self):
        p = make(replacements={"докер": "Docker"})
        assert p.process("ДОКЕР и Докер и докер") == "Docker и Docker и Docker"

    def test_regex_escape_hatch_handles_russian_cases(self):
        # Падежи простым совпадением по слову не берутся — для них есть re:.
        compiled = compile_replacements({r"re:\bдокер\w*": "Docker"})
        assert apply_replacements("докера и докером", compiled) == "Docker и Docker"

    def test_broken_regex_is_skipped_not_fatal(self):
        compiled = compile_replacements({"re:[unclosed": "x", "докер": "Docker"})
        assert apply_replacements("докер", compiled) == "Docker"

    def test_disabled_postprocessing_leaves_text_alone(self):
        p = make(enabled=False, replacements={"докер": "Docker"})
        assert p.process("докер") == "докер"


class TestHallucinations:
    @pytest.mark.parametrize(
        "text",
        [
            "Субтитры сделал DimaTorzok",
            "Субтитры создавал DimaTorzok",
            "продолжение следует...",
            "Спасибо за просмотр!",
            "Подписывайтесь на канал",
            "Thank you for watching!",
            "Thanks for watching",
            "[Музыка]",
            "...",
            "   ",
        ],
    )
    def test_known_silence_artifacts_are_dropped(self, text):
        assert is_hallucination(text)
        assert make().process(text) == ""

    @pytest.mark.parametrize(
        "text",
        [
            # Реальный ответ Groq на чистом тоне при проверке ключа.
            "Редактор субтитров А.Семкин Корректор А.Егорова",
            "Редактор субтитров А.Синецкая Корректор А.Кулакова",
            "Редактор субтитров М.Иванова",
            "Корректор А.Егорова",
        ],
    )
    def test_credits_are_matched_by_pattern_not_by_surname(self, text):
        # Фамилии в титрах каждый раз новые — списком их не переловить.
        assert is_hallucination(text)
        assert make().process(text) == ""

    @pytest.mark.parametrize(
        "text",
        [
            "поставь докер и проверь логи",
            "спасибо за просмотр документации, там всё есть",
            "напиши редактору субтитров, что файл готов",
            "подписывайтесь на канал в телеграме, я скину ссылку",
            "продолжение следует из предыдущего пункта, смотри выше",
        ],
    )
    def test_real_speech_survives(self, text):
        # Шаблоны сверяются со всей строкой целиком, поэтому живая диктовка,
        # где эти слова просто встретились, не выбрасывается.
        assert not is_hallucination(text)
        assert make().process(text) == text

    def test_can_be_disabled(self):
        p = make(drop_hallucinations=False)
        assert p.process("Спасибо за просмотр!") == "Спасибо за просмотр!"


class TestTidy:
    def test_collapses_spaces_and_trims(self):
        assert tidy("  привет   мир  ") == "привет мир"

    def test_unwraps_quotes_around_whole_phrase(self):
        assert tidy('"привет мир"') == "привет мир"

    def test_keeps_inner_quotes(self):
        assert tidy('он сказал "да" и ушёл') == 'он сказал "да" и ушёл'

    def test_normalizes_nbsp(self):
        assert tidy("привет мир") == "привет мир"


class TestTrailingSpace:
    def test_added_when_enabled(self):
        assert make(trailing_space=True).process("привет") == "привет "

    def test_not_added_to_empty_result(self):
        assert make(trailing_space=True).process("...") == ""


class TestSoundTags:
    """Пометки о звуке вместо речи.

    Живой замер: три секунды тишины с усиленного микрофона — Groq вернул
    'ДИНАМИЧНАЯ МУЗЫКА'. Вариант со скобками её не поймал, скобок не было.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "ДИНАМИЧНАЯ МУЗЫКА",
            "Динамичная музыка",
            "музыка",
            "МУЗЫКА",
            "спокойная музыка",
            "играет музыка",
            "Аплодисменты",
            "смех",
            "[Музыка]",
            "(музыка)",
            "[Applause]",
            "upbeat music",
            "music playing",
            "Silence",
        ],
    )
    def test_bare_sound_tags_are_dropped(self, text):
        assert is_hallucination(text)
        assert make().process(text) == ""

    @pytest.mark.parametrize(
        "text",
        [
            "включи музыку погромче",
            "музыка в этом фильме отличная",
            "смех в зале был слышен даже на записи",
            "the music library needs updating",
        ],
    )
    def test_real_sentences_about_sound_survive(self, text):
        # Шаблоны сверяются со всей строкой целиком, поэтому осмысленная
        # фраза про музыку не выбрасывается.
        assert not is_hallucination(text)
        assert make().process(text) == text
