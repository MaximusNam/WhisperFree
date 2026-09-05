"""Загрузка и сохранение конфигурации WhisperFree.

Конфиг лежит в %APPDATA%\\WhisperFree\\config.toml и создаётся из шаблона при
первом запуске. API-ключ в конфиг не пишется никогда — только в .env или в
переменную окружения, имя которой задаётся в [provider].api_key_env.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from dotenv import load_dotenv

log = logging.getLogger(__name__)

APP_NAME = "WhisperFree"

# Программа называлась VoiceFlow. Имя осталось в путях у тех, кто ставил
# ранние версии, поэтому старый каталог данных мы умеем находить.
LEGACY_APP_NAME = "VoiceFlow"


def program_dir() -> Path:
    """Каталог, где лежит сама программа."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Каталог данных: рядом с программой, если там лежит config.toml.

    Переносимый режим здесь не для красоты. %APPDATA% умеют подменять:
    приложения из Store и MSIX-контейнеры видят вместо
    C:\\Users\\<имя>\\AppData\\Roaming свой LocalCache, при этом печатают
    ровно тот же путь. Из-за этого правка конфига одним процессом может
    просто не дойти до другого, а выглядит это как «программа игнорирует
    настройки». Файл рядом с программой такой подмены не знает.
    """
    portable = program_dir() / "config.toml"
    if portable.is_file():
        return program_dir()

    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_NAME


def config_path() -> Path:
    return app_dir() / "config.toml"


def history_path() -> Path:
    return app_dir() / "history.jsonl"


def lexicon_path() -> Path:
    """Выученные правки. Отдельный файл, а не секция конфига: конфиг человек
    правит руками, и подмешивать туда машинные записи — верный способ
    однажды затереть его комментарии."""
    return app_dir() / "lexicon.json"


def log_path() -> Path:
    return app_dir() / "logs" / "whisperfree.log"


def audio_cache_dir() -> Path:
    return app_dir() / "audio"


def legacy_app_dir() -> Path | None:
    """Каталог данных прежнего имени, если он вообще есть."""
    base = os.environ.get("APPDATA")
    if not base:
        return None
    old = Path(base) / LEGACY_APP_NAME
    return old if old.is_dir() else None


def migrate_legacy_data() -> list[str]:
    """Переносит настройки и историю из каталога прежнего имени.

    Переименование программы не должно выглядеть как потеря настроек.
    Файлы копируются, а не переносятся: старый каталог остаётся нетронутым,
    так что откатиться можно в любой момент. Существующие файлы не трогаем —
    иначе свежий конфиг затёрло бы прошлогодним.
    """
    old = legacy_app_dir()
    if old is None:
        return []

    new = app_dir()
    if old == new:
        return []

    moved: list[str] = []
    for name in ("config.toml", "history.jsonl"):
        source, target = old / name, new / name
        if not source.is_file() or target.exists():
            continue
        try:
            new.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError as exc:
            log.warning("не удалось перенести %s из %s: %s", name, old, exc)
            continue
        moved.append(name)
        log.info("перенесено из старого каталога %s: %s", old, name)
    return moved


DEFAULT_CONFIG_TOML = r"""# Конфигурация WhisperFree. Файл можно править вручную,
# приложение перечитывает его при запуске.

[hotkeys]
# Клавиша удержания для диктовки. Держим — говорим — отпускаем.
# Годятся: ctrl_r, alt_r, scroll_lock, pause, caps_lock, f13..f24.
# Ctrl+Win не берём — это горячая клавиша Wispr Flow.
# НЕ ставьте сюда shift_r: правый Shift нужен для заглавных букв,
# и каждая заглавная запускала бы запись.
dictate = "ctrl_r"

# Вторая клавиша, диктующая на языке language.alt. Пустая строка — выключить.
# Хороший выбор — scroll_lock или f13..f24: их никто не занимает.
dictate_alt = ""

# Вставить последнюю расшифровку, если она не долетела до окна.
# Нажатия подряд идут вглубь истории: последняя, предпоследняя и так далее.
# Не shift+alt+z — это биндинг Wispr Flow, он бы конфликтовал.
paste_last = "ctrl+alt+v"

# Открыть окно истории. Пустая строка — выключить.
open_history = "ctrl+alt+h"

# Запомнить правку. Поправьте вставленный текст руками, выделите его и
# нажмите — программа сравнит со своим вариантом и запомнит разницу.
# Пустая строка — выключить.
learn = "ctrl+alt+u"

# Показать выученные правки. По умолчанию выключено, и не случайно:
# сочетания не подавляются, то есть доходят и до программы, и до окна под
# курсором, а ctrl+alt+l в средах JetBrains — это «переформатировать код».
# Список и так доступен из трея и через lexicon.bat.
open_lexicon = ""

# Прятать клавишу диктовки от приложения под курсором.
# Работает только для выделенных клавиш (f13..f24, scroll_lock, pause, caps_lock).
# Модификаторы не подавляются никогда: проглоченный Ctrl превратил бы
# Ctrl+C пользователя в обычную букву «c» прямо в тексте.
suppress = true

[provider]
# Любой OpenAI-совместимый endpoint: Groq, OpenAI, прокси, локальный сервер.
base_url = "https://api.groq.com/openai/v1"
model = "whisper-large-v3-turbo"
# Имя переменной окружения с ключом. Сам ключ сюда НЕ пишем.
api_key_env = "GROQ_API_KEY"
timeout_s = 30.0
max_retries = 2
# Для счётчика расходов.
price_per_hour_usd = 0.04
min_billed_seconds = 10

[audio]
# Микрофон. Пустая строка — системный по умолчанию.
# Можно писать часть имени ("Logitech StreamCam") или индекс из --devices.
# Имя надёжнее: индексы съезжают при подключении и отключении устройств.
# Одна железка видна сразу в нескольких звуковых API — по имени берётся та,
# что относится к API по умолчанию.
device = ""
sample_rate = 16000
# Сколько миллисекунд звука ДО нажатия клавиши попадает в запись.
preroll_ms = 250
# Защита от зажатой клавиши.
max_seconds = 300
# Короче этого — считаем случайным нажатием и игнорируем.
min_seconds = 0.35

# Ниже этого пикового уровня (0..1) считаем, что речи не было, и не тратим
# запрос. Порог не нулевой не случайно: у живого микрофона всегда есть фон.
# Значение зависит от микрофона И от его громкости в системе: на одной и той же
# железке порог уезжал в двадцать раз после того, как ползунок громкости подняли.
# Поэтому не угадывайте — измерьте: calibrate.bat три секунды слушает тишину,
# берёт втрое больше измеренного фона и записывает результат сюда сам.
# Если жалуется на тишину, когда вы говорите, — уменьшите; если пропускает
# фон и провайдер выдаёт выдуманные титры — увеличьте.
silence_peak = 0.02
# Подтягивать громкость записи перед отправкой.
# Тихий микрофон — это не только риск не пройти порог тишины: на слабом
# сигнале распознавание ошибается заметно чаще. Усиление ограничено,
# чтобы на пустой записи не раскачать шум до уровня речи.
normalize = true

# flac сжимает без потерь, бит в бит, но на отправку уходит на 78% меньше
# данных: 34.7 КБ против 156.3 КБ на пяти секундах речи. Это прямо
# вычитается из задержки.
encode = "flac"

# Держать микрофон открытым всё время работы программы.
# true  — работает пре-ролл, но Windows постоянно показывает микрофон занятым
#         (значок в трее горит), потому что поток действительно открыт.
# false — микрофон занимается только на время диктовки, значок гаснет.
#         Платой становится пре-ролл: первый слог может срезаться, пока
#         устройство просыпается.
hold_open = true

[language]
# Язык основной клавиши и альтернативной.
main = "ru"
alt = "en"

# Затравка для модели: задаёт пунктуацию и написание терминов латиницей.
# Не больше 224 токенов. Добавляйте сюда свои термины.
prompt_ru = "Привет! Как дела? Он сказал: «Сделаем это сегодня — пока есть время». Работаем с Docker, GitHub, Python, Claude Code, Gemini, ChatGPT, API, Windows, Linux, pull request, commit, deploy."
prompt_en = "Hello! How are you? He said: it is done. We work with Docker, GitHub, Python, Claude Code, Gemini, API, pull request, commit, deploy."

[postprocess]
enabled = true
# Добавлять пробел в конце, чтобы следующая диктовка не слипалась.
trailing_space = true
# Убирать известные галлюцинации Whisper на тишине.
drop_hallucinations = true

# Замены применяются по границам слов, без учёта регистра.
# Слева — как слышится, справа — как надо написать.
# Многословные ключи применяются раньше однословных.
[postprocess.replacements]
# Составные термины заданы регулярками: правка моделью любит ставить дефис
# там, где вы произнесли два слова, и ключ с пробелом мимо такого промахнётся.
# \w* на конце ловит падежи: «пул-реквеста», «коммитом».
're:\bпул[\s-]?реквест\w*' = "pull request"
're:\bгит[\s-]?хаб\w*' = "GitHub"
're:\bчат[\s-]?гпт\w*' = "ChatGPT"
're:\bклод[\s-]?код\w*' = "Claude Code"
're:\bджемини\w*' = "Gemini"
're:\bгемини\w*' = "Gemini"
're:\bдокер\w*' = "Docker"
're:\bпитон\w*' = "Python"
're:\bпайтон\w*' = "Python"
're:\bкоммит\w*' = "commit"
're:\bдеплой\w*' = "deploy"
're:\bклод\b' = "Claude"
"эй пи ай" = "API"

[refine]
# Причёсывать расшифровку языковой моделью: орфография, пунктуация, падежи,
# согласование слов. Добавляет около секунды к каждой диктовке.
# Если правка не удалась или выглядит подозрительно, вставляется исходный
# текст — потерять продиктованное хуже, чем оставить его негладким.
enabled = false

# Замеры на Groq: gpt-oss-120b с reasoning_effort = "low" даёт лучшее
# качество при 1.1-1.5 с. У 20b дешевле и чуть быстрее, но она правит лишнее.
# Модели qwen вываливают в ответ свои рассуждения и не годятся.
model = "openai/gpt-oss-120b"
reasoning_effort = "low"

# Пустые base_url и api_key_env — берём те же, что и для распознавания.
base_url = ""
api_key_env = ""
timeout_s = 8.0
max_tokens = 800

# Во сколько раз текст может вырасти, прежде чем правку отклонят.
# Резкий рост означает, что модель ответила на текст вместо правки.
max_growth = 1.8

# Для счётчика расходов в трее. Цены Groq за миллион токенов.
price_in_per_mtok = 0.15
price_out_per_mtok = 0.60

# Пустая строка — встроенная инструкция. Своя заменяет её целиком.
prompt = ""

[lexicon]
# Учиться на ваших правках. Поправили вставленный текст, выделили, нажали
# [hotkeys].learn — программа запомнила разницу и больше так не ошибается.
enabled = true

# Выученное подсказывается распознаванию (затравкой) и модели-редактора
# (списком написаний). Это безопасно всегда: подсказка ничего не заменяет,
# она лишь склоняет выбор в нужную сторону.
teach_recognizer = true
teach_editor = true

# Создавать из выученного детерминированные замены, как в
# [postprocess.replacements]. Замена применяется ко ВСЕМУ будущему тексту,
# поэтому программа создаёт её только там, где правило верно в любом
# предложении: смена алфавита («докер» → Docker) и заглавная внутри слова
# («Github» → «GitHub»).
# Правки внутри одного алфавита правилами не становятся никогда — и «был», и
# «были» настоящие слова, и глобальная замена одного на другое сломала бы текст.
# Ё и Е при этом РАЗНЫЕ буквы: «все» → «всё» — самая частая правка в живой
# истории, и заменой она стать не должна, иначе «все пришли» станет
# «всё пришли».
rules = true

# Сколько раз надо поправить одно и то же, чтобы появилась замена.
# Единица означает «с первого раза»; двойка защищает от случая, когда вы
# перевели слово на английский один раз для конкретной фразы.
min_hits_for_rule = 2

# Сколько токенов затравки распознавания отдать выученным терминам.
# Затравка Whisper целиком ограничена 224 токенами вместе с prompt_ru.
prompt_budget_tokens = 90

# Сколько написаний перечислять модели-редактору.
editor_notes = 12

# Предел числа выученных правок. Сверх него забываются редкие и давние.
max_entries = 300

# Больше этого числа замен за одно нажатие — значит, текст переписан заново,
# а не поправлен, и учиться там нечему.
max_per_press = 5

# Насколько выделенный текст должен быть похож на записанную диктовку, чтобы
# программа признала их одним и тем же текстом.
min_match = 0.62

[inject]
# clipboard — быстро и надёжно. unicode — посимвольно, если буфер трогать нельзя.
method = "clipboard"
# Вернуть прежнее содержимое буфера после вставки.
# false — последняя расшифровка всегда остаётся в буфере, как в Wispr Flow.
restore_clipboard = true
restore_delay_ms = 300
default_paste = "ctrl+v"
# Ждать, пока пользователь физически отпустит модификаторы.
wait_modifiers_ms = 400

# В терминалах Ctrl+V работает не везде.
[inject.paste_overrides]
"WindowsTerminal.exe" = "ctrl+shift+v"
"wt.exe" = "ctrl+shift+v"
"conhost.exe" = "ctrl+shift+v"
"mintty.exe" = "ctrl+shift+v"
"putty.exe" = "ctrl+shift+v"

[history]
enabled = true
max_records = 2000
retention_days = 90
# Сколько секунд между нажатиями paste_last считаются одной серией.
cycle_reset_s = 3.0
# Хранить аудио последних диктовок, чтобы перераспознать без переговаривания.
keep_audio = false
keep_audio_count = 20

[ui]
overlay = true
tray = true
autostart = false

# Размер шрифта в окнах истории и выученных правок. Системный по умолчанию —
# 9, и он многим мелок. Менять удобнее не здесь, а в самом окне: кнопки
# «А−» и «А+», Ctrl+колесо, Ctrl+плюс и Ctrl+минус; Ctrl+0 возвращает
# значение по умолчанию. Выбранный размер программа запишет сюда сама.
font_size = 13

# Оформление плашки. Пустая строка — утверждённый макет «Тёплый Дымчатый
# Мокко». Остальные лежат в theme_primary.json, themes.json и
# themes_backup.json рядом с программой; переключить проще из меню в трее,
# оно же и запишет выбор сюда.
theme = ""

# Где стоит плашка. -1 означает «по умолчанию»: по центру внизу экрана.
# Числа сюда пишет сама программа, когда плашку перетаскивают мышью.
overlay_x = -1
overlay_y = -1
"""


@dataclass
class HotkeysConfig:
    dictate: str = "ctrl_r"
    dictate_alt: str = ""
    paste_last: str = "ctrl+alt+v"
    open_history: str = "ctrl+alt+h"
    learn: str = "ctrl+alt+u"
    open_lexicon: str = ""
    suppress: bool = True


@dataclass
class ProviderConfig:
    base_url: str = "https://api.groq.com/openai/v1"
    model: str = "whisper-large-v3-turbo"
    api_key_env: str = "GROQ_API_KEY"
    timeout_s: float = 30.0
    max_retries: int = 2
    price_per_hour_usd: float = 0.04
    min_billed_seconds: int = 10

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


@dataclass
class AudioConfig:
    device: str = ""
    sample_rate: int = 16000
    preroll_ms: int = 250
    max_seconds: int = 300
    min_seconds: float = 0.35
    silence_peak: float = 0.02
    normalize: bool = True
    encode: str = "flac"
    hold_open: bool = True


@dataclass
class LanguageConfig:
    main: str = "ru"
    alt: str = "en"
    prompt_ru: str = ""
    prompt_en: str = ""

    def prompt_for(self, lang: str) -> str:
        return self.prompt_en if lang == "en" else self.prompt_ru


@dataclass
class PostprocessConfig:
    enabled: bool = True
    trailing_space: bool = True
    drop_hallucinations: bool = True
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class RefineConfig:
    enabled: bool = False
    model: str = "openai/gpt-oss-120b"
    reasoning_effort: str = "low"
    base_url: str = ""
    api_key_env: str = ""
    timeout_s: float = 8.0
    max_tokens: int = 800
    max_growth: float = 1.8
    price_in_per_mtok: float = 0.15
    price_out_per_mtok: float = 0.60
    prompt: str = ""

    @property
    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


@dataclass
class LexiconConfig:
    enabled: bool = True
    teach_recognizer: bool = True
    teach_editor: bool = True
    rules: bool = True
    min_hits_for_rule: int = 2
    prompt_budget_tokens: int = 90
    editor_notes: int = 12
    max_entries: int = 300
    max_per_press: int = 5
    min_match: float = 0.62


@dataclass
class InjectConfig:
    method: str = "clipboard"
    restore_clipboard: bool = True
    restore_delay_ms: int = 300
    default_paste: str = "ctrl+v"
    wait_modifiers_ms: int = 400
    paste_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class HistoryConfig:
    enabled: bool = True
    max_records: int = 2000
    retention_days: int = 90
    cycle_reset_s: float = 3.0
    keep_audio: bool = False
    keep_audio_count: int = 20


@dataclass
class UIConfig:
    overlay: bool = True
    tray: bool = True
    autostart: bool = False
    theme: str = ""
    font_size: int = 13
    # -1, а не 0: ноль — это законный левый верхний угол экрана, и отличить
    # «человек поставил плашку в угол» от «место не задано» было бы нечем.
    overlay_x: int = -1
    overlay_y: int = -1


@dataclass
class Config:
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    language: LanguageConfig = field(default_factory=LanguageConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)
    refine: RefineConfig = field(default_factory=RefineConfig)
    lexicon: LexiconConfig = field(default_factory=LexiconConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    path: Path | None = None


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return dict(value) if isinstance(value, dict) else {}


def _build(cls, data: dict[str, Any], **extra):
    """Собирает dataclass, молча игнорируя незнакомые ключи из TOML."""
    known = set(cls.__dataclass_fields__)
    kwargs = {k: v for k, v in data.items() if k in known and not isinstance(v, dict)}
    kwargs.update(extra)
    return cls(**kwargs)


def parse_config(data: dict[str, Any], path: Path | None = None) -> Config:
    """Превращает разобранный TOML в Config.

    Незнакомые ключи игнорируются, отсутствующие берутся из умолчаний —
    старый конфиг не должен ломать запуск после обновления.
    """
    post_raw = _section(data, "postprocess")
    inject_raw = _section(data, "inject")
    return Config(
        hotkeys=_build(HotkeysConfig, _section(data, "hotkeys")),
        provider=_build(ProviderConfig, _section(data, "provider")),
        audio=_build(AudioConfig, _section(data, "audio")),
        language=_build(LanguageConfig, _section(data, "language")),
        postprocess=_build(
            PostprocessConfig,
            post_raw,
            replacements={
                str(k): str(v) for k, v in _section(post_raw, "replacements").items()
            },
        ),
        refine=_build(RefineConfig, _section(data, "refine")),
        lexicon=_build(LexiconConfig, _section(data, "lexicon")),
        inject=_build(
            InjectConfig,
            inject_raw,
            paste_overrides={
                str(k): str(v) for k, v in _section(inject_raw, "paste_overrides").items()
            },
        ),
        history=_build(HistoryConfig, _section(data, "history")),
        ui=_build(UIConfig, _section(data, "ui")),
        path=path,
    )


def ensure_config_file(path: Path | None = None) -> Path:
    """Создаёт config.toml из шаблона, если его ещё нет."""
    target = path or config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return target


def env_locations() -> list[Path]:
    """Где искать .env с ключом, в порядке приоритета.

    Каталога программы в списке не случайно: при автозапуске через реестр
    рабочим каталогом оказывается System32, и .env, лежащий рядом с exe,
    иначе бы не нашёлся.
    """
    places = [Path.cwd()]
    if getattr(sys, "frozen", False):
        places.append(Path(sys.executable).parent)
    else:
        places.append(Path(__file__).resolve().parent.parent)
    places.append(app_dir())

    seen: set[Path] = set()
    unique = []
    for place in places:
        if place not in seen:
            seen.add(place)
            unique.append(place / ".env")
    return unique


def load_config(path: Path | None = None) -> Config:
    """Читает конфиг, подхватывая .env из всех разумных мест."""
    for env_file in env_locations():
        # override=False: побеждает первый найденный, то есть более близкий.
        load_dotenv(env_file, override=False)

    target = ensure_config_file(path)
    try:
        data = tomlkit.parse(target.read_text(encoding="utf-8")).unwrap()
    except Exception as exc:
        # Битый или занятый конфиг не должен мешать запуску, но и молчать про
        # это нельзя: тихий откат на умолчания выглядит как «программа
        # игнорирует настройки», и разбираться в этом потом невозможно.
        #
        # Пишем и в лог, и напрямую в stderr: load_config вызывается раньше
        # setup_logging, поэтому на этот момент обработчиков ещё нет и одна
        # только запись в лог никуда не попадёт.
        message = (
            f"ВНИМАНИЕ: конфиг {target} не прочитан ({type(exc).__name__}: {exc}).\n"
            f"Работаю на настройках по умолчанию — всё, что задано в файле, "
            f"сейчас НЕ действует."
        )
        log.error("%s", message.replace("\n", " "))
        _warn(message)
        data = tomlkit.parse(DEFAULT_CONFIG_TOML).unwrap()
    return parse_config(data, target)


def _warn(message: str) -> None:
    """Сообщение пользователю до того, как настроено логирование."""
    try:
        if sys.stderr is not None:
            print(message, file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - при тихом запуске потоков нет
        pass


def describe(cfg: Config) -> str:
    """Что программа реально будет делать — строкой в лог при старте.

    Без неё вопрос «почему берётся не тот микрофон» приходится расследовать,
    вместо того чтобы просто прочитать.
    """
    return (
        f"конфиг={cfg.path} | микрофон={cfg.audio.device or 'системный по умолчанию'} "
        f"(hold_open={str(cfg.audio.hold_open).lower()}) | "
        f"клавиша={cfg.hotkeys.dictate} | язык={cfg.language.main} | "
        f"модель={cfg.provider.model} @ {cfg.provider.base_url} | "
        f"ключ={'есть' if cfg.provider.api_key else 'НЕТ'} | "
        f"правка={cfg.refine.model if cfg.refine.enabled else 'выключена'}"
    )
