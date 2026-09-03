"""Выученные правки: программа запоминает, как надо, и перестаёт ошибаться.

Замысел прост. Продиктовали, программа вставила, вы поправили пару слов
руками, выделили исправленное и нажали хоткей. Программа сравнивает то, что
вставила сама, с тем, что осталось после правки, и запоминает разницу.

Дальше выученное работает в трёх местах:
  * затравка распознавания — чтобы Whisper услышал термин правильно СРАЗУ;
  * инструкция модели-редактора — чтобы она не переписывала термин по-своему;
  * словарь замен — детерминированная правка уже в готовом тексте.

Главная опасность здесь не в качестве, а в том, что программа выучит лишнее.
Если принять за ошибку распознавания обычную переформулировку — вы поменяли
«сегодня» на «завтра», — то замена уедет во ВСЕ будущие диктовки, и «сегодня»
больше никогда не напишется. Это хуже, чем не выучить ничего. Поэтому решение
о том, что можно запомнить, здесь принимается не на глазок, а по замерам —
см. same_sound и learnable.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .refine import similarity, translit

log = logging.getLogger(__name__)

# Кто испортил слово. Различить их можно потому, что в истории лежат оба
# текста: raw — что услышало распознавание, text — что осталось после правки
# моделью. См. blame.
RECOGNITION = "recognition"
REFINE = "refine"
DICTIONARY = "dictionary"
UNKNOWN = "unknown"

BLAME_RU = {
    RECOGNITION: "не расслышал микрофон",
    REFINE: "испортила правка моделью",
    DICTIONARY: "написал словарь замен из конфига",
    UNKNOWN: "непонятно, на каком шаге",
}

# Правило замены появляется только со второго раза. Причина не в
# осторожности ради осторожности, а в замерах ниже: разделить «термин
# латиницей» и «перевод слова» по звучанию удаётся не полностью — из 30
# переводов два совпали с термином случайно («удалить»/«delete» и
# «новый»/«new» дают один и тот же согласный скелет). Одиночная правка
# поэтому идёт только в затравку, где она безвредна: затравка подсказывает
# распознаванию слово, но ничего не заменяет. А человек, дважды поправивший
# одно и то же, своё намерение уже подтвердил.
MIN_HITS_FOR_RULE = 2

# Сколько слов с каждой стороны ещё считается правкой слова, а не абзаца.
MAX_WORDS = 3

# Короче этого правку не запоминаем: на двух знаках случайных совпадений
# больше, чем смысла («и» → «или» звучит непохоже, а вот «мой» → «мои»
# неотличимо, и запомнить такое нельзя).
MIN_LENGTH = 3


# --- звучание ------------------------------------------------------------------
#
# Посимвольное сходство для этой задачи не годится, и это показал замер:
# «клод код» → «Claude Code» (настоящая правка) даёт 0.316, а «сделаю» →
# «сделаем» (переформулировка) — 0.769. Порядок ровно обратный нужному, и
# никакой порог их не разделит: множества пересекаются на всём промежутке
# 0.32..0.77.
#
# Различает их звучание. Ошибка распознавания по определению даёт слово,
# которое ЗВУЧИТ как верное, — иначе микрофон бы его не спутал. Поэтому
# сравниваются согласные скелеты: гласные в разных алфавитах записываются как
# попало (Claude — «клод»), а согласные держат слово.
#
# Замер на 56 настоящих терминах и 30 переводах: у 53 терминов скелеты
# совпали ровно, у переводов — только у двух случайных пар.

DIGRAPHS = (
    ("sch", "sh"), ("tch", "ch"), ("dzh", "j"), ("zh", "j"),
    ("ph", "f"), ("ck", "k"), ("th", "t"), ("wh", "v"), ("qu", "kv"),
)

VOWELS = frozenset("aeiou")


def sound(text: str) -> str:
    """Согласный скелет слова, одинаковый для любого алфавита.

    «докер» и «Docker» дают «dkr», «клод код» и «Claude Code» — «kldkd».
    """
    s = translit(text.lower())
    for source, target in DIGRAPHS:
        s = s.replace(source, target)
    # Мягкие c и g: перед e, i, y читаются иначе, чем в остальных случаях.
    # Без этого «селери» не сходится с Celery, а «джемини» — с Gemini.
    s = re.sub(r"c(?=[eiy])", "s", s)
    s = re.sub(r"g(?=[eiy])", "j", s)
    s = s.replace("c", "k").replace("q", "k").replace("x", "ks")
    s = s.replace("w", "v").replace("y", "i")
    # Немая e на конце: Claude, Code, merge.
    s = re.sub(r"e\b", "", s)
    # Звонкость на слух теряется: «зделал»/«сделал», «визпер»/«Whisper».
    s = s.replace("z", "s")
    s = "".join(ch for ch in s if ch.isalnum() and ch not in VOWELS)
    # Удвоение не слышно: pull и «пул».
    return re.sub(r"(.)\1+", r"\1", s)


def same_sound(wrong: str, right: str) -> bool:
    """Звучат ли два написания одинаково."""
    a, b = sound(wrong), sound(right)
    return bool(a) and a == b


CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
LATIN = re.compile(r"[a-zA-Z]")


def changes_script(wrong: str, right: str) -> bool:
    """Сменился ли алфавит: кириллица на латиницу или наоборот.

    Это признак термина, который распознавание записало на слух. Именно на
    таких правках детерминированная замена и осмысленна: слова «докер» в
    русском языке нет, и заменять его на Docker правильно всегда, в любом
    предложении. Для двух слов одного алфавита это не так — и «был», и «были»
    настоящие слова, и глобальная замена одного на другое сломала бы текст.
    """
    cyr_to_lat = (
        CYRILLIC.search(wrong) and LATIN.search(right) and not CYRILLIC.search(right)
    )
    lat_to_cyr = (
        LATIN.search(wrong) and CYRILLIC.search(right) and not LATIN.search(right)
    )
    return bool(cyr_to_lat or lat_to_cyr)


def case_only(wrong: str, right: str) -> bool:
    """Различаются только регистром: «иван» → «Иван», «Github» → «GitHub».

    Буквы ё и е здесь НЕ приравниваются, хотя приравнять их напрашивается.
    Проверка на настоящей истории показала, почему этого делать нельзя:
    самой частой правкой оказалась «все» → «всё» (39 раз), а это разные
    слова — «все пришли» и «всё пришло». То же с «нем» → «нём». Приравняв
    ё к е, программа сочла бы их различием в регистре и завела глобальную
    замену, которая ломала бы текст молча и без конца.
    """
    return wrong != right and wrong.lower() == right.lower()


def internal_capital(text: str) -> bool:
    """Заглавная буква не на первом месте.

    Признак написания самого термина, а не его места в предложении: GitHub,
    TypeScript, JavaScript. Заглавная только в начале («звонок в дверь» →
    «Звонок в дверь») — это большая буква после точки, и правилом замены она
    стать не должна: тогда слово начнёт писаться с большой и в середине фразы.
    """
    return any(ch.isupper() for ch in text[1:])


def learnable(wrong: str, right: str) -> tuple[bool, str]:
    """Можно ли вообще запоминать такую пару. Второе значение — причина отказа."""
    if not wrong or not right:
        return False, "пустая сторона"
    if wrong == right:
        return False, "одинаковые"
    if not any(ch.isalnum() for ch in wrong) or not any(ch.isalnum() for ch in right):
        return False, "одна пунктуация"
    if len(wrong.split()) > MAX_WORDS or len(right.split()) > MAX_WORDS:
        return False, f"больше {MAX_WORDS} слов — это правка фразы, а не слова"
    if len(wrong) < MIN_LENGTH and not case_only(wrong, right):
        return False, f"короче {MIN_LENGTH} знаков"

    if case_only(wrong, right):
        return True, ""
    if changes_script(wrong, right):
        # Смена алфавита без совпадения по звучанию — это перевод, а не
        # ошибка распознавания: «встреча» → «meeting» запоминать нельзя.
        if same_sound(wrong, right):
            return True, ""
        return False, "разное звучание — похоже на перевод, а не на ошибку слуха"

    # Один алфавит. Опечатку («сожелению» → «сожалению») от смены слова
    # («были» → «был») по звучанию не отличить: они звучат одинаково, потому
    # и путаются. Такую правку берём в затравку, но правилом замены не делаем.
    if similarity(wrong, right) >= 0.6 or same_sound(wrong, right):
        return True, ""
    return False, "мало похоже на исправление того же слова"


def can_be_rule(wrong: str, right: str) -> bool:
    """Годится ли пара в детерминированную замену.

    Замена применяется ко всему будущему тексту, поэтому она допустима лишь
    там, где верна в любом контексте: у неверного написания не должно быть
    своей законной жизни в языке.
    """
    if case_only(wrong, right):
        # Только внутренняя заглавная: она принадлежит слову, а не позиции в
        # предложении. Заглавную в начале правит модель-редактор, и правило
        # замены тут только навредило бы.
        return internal_capital(right)
    return changes_script(wrong, right) and same_sound(wrong, right)


# --- разбор правки -------------------------------------------------------------


def diff_pairs(before: str, after: str) -> list[tuple[str, str]]:
    """Пословная разница двух текстов: что на что заменили.

    Вставки и удаления не возвращаются: выброшенное слово-паразит или
    добавленный союз — это не ошибка написания, и запоминать там нечего.
    """
    old, new = before.split(), after.split()
    pairs: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        pairs.append((" ".join(old[i1:i2]), " ".join(new[j1:j2])))
    return pairs


# Знаки, которые прилипают к слову с краёв и в правку не входят.
EDGE = " \t\r\n.,;:!?()[]{}«»\"'…-—–"


def trim(text: str) -> str:
    return text.strip(EDGE)


def blame(wrong: str, right: str, raw: str, dictionary: dict[str, str] | None = None) -> str:
    """На каком шаге слово испортилось.

    raw — то, что услышало распознавание (History.Record.raw). Оно пустое,
    когда правка моделью ничего не изменила: тогда испортить слово могло
    только распознавание.

    dictionary — словарь замен из конфига. Он нужен для случая, когда слово
    написало правило, заданное руками: тогда виноват не микрофон и не модель,
    а само правило, и человеку полезно об этом узнать — иначе он будет
    доучивать программу против её же настройки.
    """
    # Словарь замен — последний шаг конвейера: он работает уже после правки
    # моделью. Поэтому признак прямой: слово в точности совпадает с целью
    # какого-то правила, а в расшифровке этого написания не было — значит,
    # его написало правило. Сверка тут регистрозависимая нарочно: правило
    # «докер» → «Докер» отличается от услышанного «докер» только регистром,
    # и без учёта регистра виновник смазался бы в «не расслышал микрофон».
    if _dictionary_wrote(wrong, raw, dictionary):
        return DICTIONARY
    if not raw:
        return RECOGNITION
    if _has(raw, right):
        # Верное слово в расшифровке было, а в готовом тексте его нет —
        # значит, его переписала модель-редактор.
        return REFINE
    if _has(raw, wrong):
        return RECOGNITION
    return UNKNOWN


def _dictionary_wrote(wrong: str, raw: str, dictionary: dict[str, str] | None) -> bool:
    """Написало ли неверное слово правило замены из конфига."""
    if not dictionary:
        return False
    if any(value.strip() == wrong for value in dictionary.values()):
        return wrong not in raw
    return False


def _has(text: str, word: str) -> bool:
    """Есть ли слово в тексте, по границам слов и без учёта регистра."""
    try:
        return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.IGNORECASE) is not None
    except re.error:  # pragma: no cover — re.escape этого не допускает
        return word.lower() in text.lower()


def find_source(corrected: str, records: list, min_ratio: float = 0.62):
    """Из какой записи истории человек взял этот текст.

    Ищем не по времени, а по похожести: правя текст, человек мог сходить в
    другое окно, продиктовать что-то ещё и вернуться. Заодно так отсекается
    случай, когда выделили вообще не нашу диктовку.

    Выделить могли и кусок — одно предложение из абзаца, — поэтому считается
    не только совпадение целиком, но и доля выделенного, найденная внутри
    записи; см. _match.
    """
    needle = " ".join(corrected.split())
    if not needle:
        return None, 0.0

    # Записи приходят свежими сверху, и строгое сравнение оставляет при
    # равном счёте самую новую: если человек продиктовал одно и то же дважды,
    # поправил он, скорее всего, последнее.
    best, best_ratio = None, 0.0
    for record in records:
        text = " ".join((record.text or "").split())
        if not text:
            continue
        ratio = _match(text, needle)
        if ratio > best_ratio:
            best, best_ratio = record, ratio
    if best_ratio < min_ratio:
        return None, best_ratio
    return best, best_ratio


# Короче этого выделения доля совпадения не считается: в двадцати знаках
# «сколько иглы нашлось в стоге» показывает высокое совпадение со слишком
# многими записями, и выбор становится случайным.
MIN_FRAGMENT = 20

# В долю совпадения идут только связные куски не короче этого. Без такого
# условия доля набирается из случайных букв: постороннее деловое письмо
# совпало с диктовкой на 0.663, и набралось это из 31 обрывка, из которых
# длиннее четырёх знаков были всего три. В русском тексте пробелы и частые
# буквы выстраиваются в цепочку почти с чем угодно.
MIN_BLOCK = 4


def _match(text: str, needle: str) -> float:
    """Насколько выделенный текст похож на эту запись.

    Считается двумя мерами из ОДНОГО разбора: совпадение целиком и доля
    выделенного, найденная внутри записи. Вторая нужна затем, что выделить
    могли одно предложение из абзаца, — тогда совпадение целиком мало́ по
    самой длине, а не по существу.

    Мера считается дважды: как есть и по транслитерации. Второе обязательно:
    правя текст, человек как раз и меняет кириллицу на латиницу («гитхаб» →
    GitHub), и посимвольное сравнение занижает совпадение до отказа.

    Раньше здесь скользило окно по длине выделения, вызывая сравнение до
    десятка раз на запись: поиск по тридцати записям занимал 348 мс в медиане
    и 758 мс в худшем случае. Разбор же даёт оба числа сразу — совпадение
    целиком равно 2·M/T, доля равна M/len(needle), — и это те же самые
    блоки совпадений, за которые уже заплачено.
    """
    return max(
        _both_scores(text, needle),
        _both_scores(translit(text), translit(needle)),
    )


def _both_scores(haystack: str, needle: str) -> float:
    if not haystack or not needle:
        return 0.0
    matcher = difflib.SequenceMatcher(None, haystack, needle, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    whole = 2.0 * matched / (len(haystack) + len(needle))
    if len(needle) < MIN_FRAGMENT:
        return whole
    solid = sum(block.size for block in blocks if block.size >= MIN_BLOCK)
    return max(whole, solid / len(needle))


# --- хранение ------------------------------------------------------------------


@dataclass
class Lesson:
    """Одна выученная правка."""

    wrong: str
    right: str
    kind: str = UNKNOWN
    hits: int = 1
    first_ts: float = 0.0
    last_ts: float = 0.0
    lang: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.wrong.lower(), self.right)

    @property
    def rule_allowed(self) -> bool:
        """Допустима ли для этой пары детерминированная замена вообще."""
        return can_be_rule(self.wrong, self.right)

    @property
    def blame_ru(self) -> str:
        return BLAME_RU.get(self.kind, BLAME_RU[UNKNOWN])


class Lexicon:
    """Журнал выученных правок в %APPDATA%\\WhisperFree\\lexicon.json.

    Отдельный файл, а не секция конфига: конфиг человек пишет руками, и
    подмешивать туда машинные записи — верный способ однажды затереть
    комментарии. Здесь же всё можно очистить, не тронув настройки.
    """

    def __init__(self, path: Path, cfg=None) -> None:
        self.path = path
        self.cfg = cfg
        self._lock = threading.RLock()
        self._lessons: list[Lesson] = []
        self._load()

    def _setting(self, name: str, default):
        """Значение из секции [lexicon] с падением на умолчание."""
        if self.cfg is None:
            return default
        value = getattr(self.cfg, name, None)
        return default if value is None else value

    @property
    def min_hits(self) -> int:
        """Сколько раз надо поправить одно и то же, чтобы появилась замена."""
        return max(1, int(self._setting("min_hits_for_rule", MIN_HITS_FOR_RULE)))

    def is_rule(self, lesson: Lesson) -> bool:
        """Работает ли правка как детерминированная замена."""
        return lesson.rule_allowed and lesson.hits >= self.min_hits

    def describe(self, lesson: Lesson) -> str:
        """Строка для человека: что, кто виноват и как применяется."""
        if not lesson.rule_allowed:
            where = "подсказка"
        elif self.is_rule(lesson):
            where = "замена"
        else:
            where = f"подсказка, замена с {self.min_hits}-го раза"
        return (
            f"{lesson.wrong} → {lesson.right}  "
            f"({lesson.blame_ru}, {where}, ×{lesson.hits})"
        )

    # --- чтение и запись -------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("не удалось прочитать выученные правки: %s", exc)
            return
        raw = payload.get("lessons") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            return
        known = set(Lesson.__dataclass_fields__)
        lessons: list[Lesson] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("wrong") or not item.get("right"):
                continue
            try:
                lessons.append(Lesson(**{k: v for k, v in item.items() if k in known}))
            except TypeError:
                continue
        self._lessons = lessons
        log.info("выученных правок: %d", len(lessons))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "lessons": [asdict(item) for item in self._lessons]}
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            log.error("не удалось сохранить выученные правки: %s", exc)

    # --- доступ ----------------------------------------------------------------

    @property
    def lessons(self) -> list[Lesson]:
        with self._lock:
            return list(self._lessons)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg is None or getattr(self.cfg, "enabled", True))

    def __len__(self) -> int:
        with self._lock:
            return len(self._lessons)

    # --- обучение --------------------------------------------------------------

    def learn(
        self,
        before: str,
        after: str,
        raw: str = "",
        lang: str = "",
        dictionary: dict[str, str] | None = None,
    ) -> tuple[list[Lesson], list[str]]:
        """Запомнить разницу между своим текстом и поправленным.

        Возвращает (что выучили, что отвергли и почему). Вторая половина
        нужна не для порядка: человек нажал клавишу и вправе узнать, почему
        программа ничего не запомнила.
        """
        learned: list[Lesson] = []
        refused: list[str] = []
        if not self.enabled:
            return learned, ["обучение выключено в конфиге"]

        limit = max(1, int(self._setting("max_per_press", 5)))
        pairs = diff_pairs(" ".join(before.split()), " ".join(after.split()))
        if not pairs:
            return learned, ["различий по словам нет"]
        if len(pairs) > limit:
            # Много замен разом — это не правка ошибок, а переписывание
            # текста заново. Запоминать оттуда нечего.
            return learned, [
                f"{len(pairs)} замен подряд — похоже, текст переписан целиком, "
                "а не поправлен"
            ]

        now = time.time()
        with self._lock:
            for wrong_raw, right_raw in pairs:
                wrong, right = trim(wrong_raw), trim(right_raw)
                ok, why = learnable(wrong, right)
                if not ok:
                    refused.append(f"«{wrong_raw}» → «{right_raw}»: {why}")
                    continue
                kind = blame(wrong, right, raw, dictionary)
                lesson = self._remember(wrong, right, kind, lang, now)
                learned.append(lesson)
            if learned:
                self._prune()
                self._save()
        return learned, refused

    def _remember(self, wrong: str, right: str, kind: str, lang: str, now: float) -> Lesson:
        key = (wrong.lower(), right)
        for lesson in self._lessons:
            if lesson.key == key:
                lesson.hits += 1
                lesson.last_ts = now
                # Виновника обновляем: тот же промах мог прийти с другого шага.
                if kind != UNKNOWN:
                    lesson.kind = kind
                return lesson
        lesson = Lesson(
            wrong=wrong, right=right, kind=kind, hits=1,
            first_ts=now, last_ts=now, lang=lang,
        )
        self._lessons.append(lesson)
        return lesson

    def _prune(self) -> None:
        limit = int(self._setting("max_entries", 300))
        if limit <= 0 or len(self._lessons) <= limit:
            return
        # Выбрасываем сначала редкое и давнее: правка, встреченная один раз
        # год назад, полезна меньше, чем вчерашняя, повторённая пять раз.
        self._lessons.sort(key=lambda item: (item.hits, item.last_ts))
        dropped = self._lessons[: len(self._lessons) - limit]
        self._lessons = self._lessons[len(self._lessons) - limit :]
        log.info("выученных правок больше %d, забыл %d редких", limit, len(dropped))

    def forget(self, wrong: str, right: str) -> bool:
        key = (wrong.lower(), right)
        with self._lock:
            before = len(self._lessons)
            self._lessons = [item for item in self._lessons if item.key != key]
            if len(self._lessons) == before:
                return False
            self._save()
            return True

    def clear(self) -> None:
        with self._lock:
            self._lessons = []
            self._save()

    # --- применение ------------------------------------------------------------

    def replacements(self) -> dict[str, str]:
        """Детерминированные замены для словаря постобработки."""
        if not self.enabled or not self._setting("rules", True):
            return {}
        with self._lock:
            return {
                item.wrong: item.right for item in self._lessons if self.is_rule(item)
            }

    def vocabulary(self, lang: str = "", budget_tokens: int | None = None) -> list[str]:
        """Термины для затравки распознавания, самое полезное — первым.

        Затравка Whisper ограничена 224 токенами вместе с тем, что уже задано
        в конфиге, поэтому список приходится обрезать. Порядок: сначала то,
        что повторялось чаще, потом свежее.
        """
        if not self.enabled or not self._setting("teach_recognizer", True):
            return []
        if budget_tokens is None:
            budget_tokens = int(self._setting("prompt_budget_tokens", 90))
        if budget_tokens <= 0:
            return []
        with self._lock:
            lessons = [
                item
                for item in self._lessons
                if not item.lang or not lang or item.lang == lang
            ]
            lessons.sort(key=lambda item: (-item.hits, -item.last_ts))
        out: list[str] = []
        spent = 0
        seen: set[str] = set()
        for lesson in lessons:
            term = lesson.right
            low = term.lower()
            if low in seen:
                continue
            cost = estimate_tokens(term) + 1  # плюс разделитель
            if spent + cost > budget_tokens:
                break
            seen.add(low)
            out.append(term)
            spent += cost
        return out

    def editor_notes(self, limit: int | None = None) -> list[str]:
        """Указания модели-редактору: как писать слова, которые она уже портила.

        Берём только те правки, в которых виновата она сама. Остальное ей
        сообщать незачем: чем короче инструкция, тем надёжнее она работает.
        """
        if not self.enabled or not self._setting("teach_editor", True):
            return []
        if limit is None:
            limit = int(self._setting("editor_notes", 12))
        if limit <= 0:
            return []
        with self._lock:
            lessons = [item for item in self._lessons if item.kind == REFINE]
            lessons.sort(key=lambda item: (-item.hits, -item.last_ts))
        return [f"{item.right} (не «{item.wrong}»)" for item in lessons[:limit]]


def estimate_tokens(text: str) -> int:
    """Прикидка длины в токенах для затравки Whisper.

    Точного токенизатора здесь нет и он не нужен: важно не превысить предел
    в 224 токена, поэтому прикидка сознательно завышена. Кириллица дороже
    латиницы — она приезжает по два-три токена на слово.
    """
    cost = 0.0
    for ch in text:
        if CYRILLIC.match(ch):
            cost += 0.5
        elif ch.isalnum():
            cost += 0.3
        else:
            cost += 0.4
    return max(1, int(cost + 0.999))


def build_prompt(base: str, terms: list[str]) -> str:
    """Затравка распознавания: то, что в конфиге, плюс выученные термины."""
    if not terms:
        return base
    tail = ", ".join(terms)
    if not base:
        return tail
    separator = " " if base.rstrip().endswith((".", "!", "?", ":")) else ". "
    return f"{base.rstrip()}{separator}{tail}."


def build_editor_prompt(base: str, notes: list[str]) -> str:
    """Инструкция редактору с добавленным списком написаний."""
    if not notes:
        return base
    return (
        f"{base.rstrip()} Пиши эти слова именно так, как здесь: "
        f"{'; '.join(notes)}."
    )
