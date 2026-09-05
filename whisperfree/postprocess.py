"""Доводка распознанного текста.

Whisper через облако неплохо справляется со смесью русского и английского, но
техтермины всё равно норовит записать кириллицей: Gemini превращается в
«Джемини», Docker — в «Докер». Промпт-затравка снимает большую часть, а этот
модуль добивает остальное детерминированным словарём замен.

Второй его job — выкинуть галлюцинации. Whisper на тишине уверенно выдаёт
титры из ютуб-роликов, на которых учился; в поле ввода это выглядит дико.
"""

from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger(__name__)

# Фразы, которые Whisper придумывает, когда в записи нет речи: он учился на
# субтитрах с ютуба и на тишине выдаёт титры оттуда.
HALLUCINATIONS = {
    "все на канале",
    "you",
    "bye.",
    "bye",
    ".",
    "..",
    "...",
}

# Фамилии в титрах каждый раз разные, поэтому ловить надо шаблон, а не список.
# Живой пример от Groq на чистом тоне: «Редактор субтитров А.Семкин Корректор
# А.Егорова» — набора конкретных фамилий тут не хватило бы никогда.
#
# Шаблоны сверяются со ВСЕЙ строкой, а не с её куском: галлюцинация всегда
# приезжает как весь ответ целиком, и полное совпадение не даст выбросить
# настоящую диктовку, где эти слова просто встретились.
HALLUCINATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.UNICODE)
    for pattern in (
        r"субтитры\s+(?:сделал|делал|создавал|подготовил|и перевод).*",
        r"редактор\s+субтитров.*",
        r"корректор\s+[а-яё]\.?\s*[а-яё]*\s*",
        r"продолжение\s+следует[.!…\s]*",
        r"подписывайтесь\s+на\s+канал[.!…\s]*",
        r"спасибо\s+за\s+(?:просмотр|внимание)[.!…\s]*",
        r"(?:ставьте\s+)?лайки?\s+и\s+подпис\w+.*",
        r"(?:thanks?|thank\s+you)\s+for\s+watching[.!…\s]*",
        r"(?:please\s+)?subscribe(?:\s+to\s+.*)?[.!…\s]*",
        # Пометки о звуке — и в скобках, и голыми. Живой замер: на трёх
        # секундах тишины с усиленного микрофона Groq выдал «ДИНАМИЧНАЯ
        # МУЗЫКА» без всяких скобок, и вариант со скобками её не поймал.
        r"[\[(]\s*(?:музыка|аплодисменты|смех|тишина|music|applause|laughter|silence)"
        r"\s*[\])].*",
        r"(?:динамичная|спокойная|тревожная|фоновая|громкая|тихая|играет|звучит)?"
        r"\s*музыка[.!…\s]*",
        r"(?:аплодисменты|смех|шум|тишина|звук\s+\w+)[.!…\s]*",
        r"(?:upbeat\s+|dramatic\s+|soft\s+|background\s+)?music(?:\s+playing)?[.!…\s]*",
        r"(?:applause|laughter|silence|noise)[.!…\s]*",
    )
)


def _normalize_key(text: str) -> str:
    """Приводит строку к виду для сравнения с чёрным списком."""
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.replace("ё", "е")


def is_hallucination(text: str) -> bool:
    """Похоже ли на выдумку модели поверх тишины."""
    key = _normalize_key(text)
    if not key:
        return True
    if key in HALLUCINATIONS:
        return True
    if any(pattern.fullmatch(key) for pattern in HALLUCINATION_PATTERNS):
        return True
    # Голая пунктуация смысла не несёт.
    return not any(ch.isalnum() for ch in key)


def compile_replacements(mapping: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    """Готовит замены. Длинные ключи идут первыми, чтобы «клод код» победил «клод».

    Ключ с префиксом re: трактуется как регулярное выражение — на случай,
    когда простого совпадения по слову не хватает (падежи, приставки).
    """
    compiled: list[tuple[re.Pattern[str], str]] = []
    for source in sorted(mapping, key=lambda s: (-len(s), s)):
        target = mapping[source]
        if not source:
            continue
        try:
            if source.startswith("re:"):
                pattern = re.compile(source[3:], re.IGNORECASE | re.UNICODE)
            else:
                pattern = re.compile(
                    r"\b" + re.escape(source.strip()) + r"\b", re.IGNORECASE | re.UNICODE
                )
        except re.error as exc:
            log.warning("замена %r пропущена, ошибка в шаблоне: %s", source, exc)
            continue
        compiled.append((pattern, target))
    return compiled


def apply_replacements(text: str, compiled: list[tuple[re.Pattern[str], str]]) -> str:
    """Применяет словарь замен, сохраняя заглавную букву в начале предложения."""
    for pattern, target in compiled:

        def substitute(match: re.Match[str], target: str = target) -> str:
            original = match.group(0)
            # «коммит» в начале предложения должен стать «Commit», но «GitHub»
            # свою заглавную букву уже принёс сам.
            if original[:1].isupper() and target[:1].islower():
                return target[:1].upper() + target[1:]
            return target

        text = pattern.sub(substitute, text)
    return text


def tidy(text: str) -> str:
    """Убирает мусор, который приезжает от моделей распознавания."""
    text = unicodedata.normalize("NFKC", text)
    # Типографские дефисы и невидимые пробелы приезжают от языковых моделей
    # и ломают словарь замен: «пул‑реквест» не совпадёт ни с одним ключом.
    # NFKC тут не спасает: он лишь переводит U+2011 в U+2010, тоже не дефис.
    # Тире U+2013 и U+2014 не трогаем, в русском тексте это своя пунктуация.
    for bad, good in (
        (" ", " "),   # неразрывный пробел
        ("‐", "-"),   # типографский дефис
        ("‑", "-"),   # неразрывный дефис
        ("⁠", ""),    # склейка слов
        ("﻿", ""),    # BOM посреди текста
    ):
        text = text.replace(bad, good)
    # Модель иногда оборачивает всю фразу в кавычки.
    stripped = text.strip()
    if len(stripped) > 1 and stripped[0] in '"«' and stripped[-1] in '"»':
        inner = stripped[1:-1]
        if inner.count('"') == 0 and inner.count("«") == 0:
            stripped = inner
    stripped = re.sub(r"[ \t]+", " ", stripped)
    stripped = re.sub(r" *\n *", "\n", stripped)
    return stripped.strip()


class Postprocessor:
    """Собирает всю доводку в один вызов."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._learned: dict[str, str] = {}
        self._compiled: list[tuple[re.Pattern[str], str]] = []
        self._rebuild()

    def set_learned(self, mapping: dict[str, str]) -> None:
        """Задаёт выученные замены поверх заданных в конфиге.

        Вызывается на старте и после каждого нового урока: выученное должно
        работать сразу, а не с перезапуска.
        """
        self._learned = dict(mapping)
        self._rebuild()

    def _rebuild(self) -> None:
        if not self.cfg.enabled:
            self._compiled = []
            return
        # Порядок слияния — не деталь: ключ, заданный человеком руками,
        # выученный перебивать не должен. Программа, спорящая с настройкой
        # своего хозяина, — это не обучение, а самоволие.
        merged = {**self._learned, **self.cfg.replacements}
        self._compiled = compile_replacements(merged)

    def clean(self, text: str) -> str:
        """Первый этап: нормализация и отсев галлюцинаций.

        Отделён от второго намеренно: между ними вклинивается правка текста
        моделью, а словарь замен должен применяться ПОСЛЕ неё — иначе модель
        перепишет термины по-своему и последнее слово останется за ней.
        """
        if not text:
            return ""

        result = tidy(text)
        if not result:
            return ""

        if self.cfg.enabled and self.cfg.drop_hallucinations and is_hallucination(result):
            log.info("отброшена галлюцинация на тишине: %r", result[:60])
            return ""
        return result

    def finish(self, text: str) -> str:
        """Второй этап: словарь замен и пробел в конце."""
        if not text or not self.cfg.enabled:
            return text

        result = apply_replacements(tidy(text), self._compiled)
        if self.cfg.trailing_space:
            result += " "
        return result

    def process(self, text: str) -> str:
        """Оба этапа подряд, без правки моделью между ними."""
        cleaned = self.clean(text)
        return self.finish(cleaned) if cleaned else ""
