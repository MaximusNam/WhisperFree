"""Оформление плашки: основной макет и переключаемые запасные.

Темы лежат в JSON рядом с кодом, а не в самом коде: подбирались они глазами
на живых окнах, и менять оттенок не должно означать правку Python.

Что берётся из темы, а что нет. Тема задаёт КОРПУС плашки — фон, кант, цвет
текста — и цвет точки записи. Цвета остальных состояний остаются общими для
всех тем, и это не недоделка: «Ошибка» обязана быть красной, а «Готово»
зелёным в любом оформлении. Тема, перекрасившая ошибку в бирюзовый под цвет
канта, была бы красивой и бесполезной.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PRIMARY_FILE = "theme_primary.json"
# Два файла с запасными: themes.json — варианты со страницы предпросмотра,
# themes_backup.json — альтернативы. Совпадающие id берутся из первого
# прочитанного, поэтому порядок здесь важен.
BACKUP_FILES = ("themes.json", "themes_backup.json")

PRIMARY_ID = "warm_smoky_mocha"

_RGBA = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", re.I
)


def parse_color(value, default=(0, 0, 0, 255)) -> tuple[int, int, int, int]:
    """Цвет из «rgba(...)» или «#RRGGBB» в RGBA. Мусор — default.

    Тему правит человек в текстовом редакторе, поэтому опечатка в цвете не
    должна ронять программу: плашка нужнее точного оттенка.
    """
    if not isinstance(value, str):
        return default

    text = value.strip()
    match = _RGBA.match(text)
    if match:
        r, g, b, a = match.groups()
        alpha = 255 if a is None else round(float(a) * 255)
        return (int(r), int(g), int(b), max(0, min(255, alpha)))

    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) in (6, 8):
            try:
                parts = [int(digits[i : i + 2], 16) for i in range(0, len(digits), 2)]
            except ValueError:
                return default
            if len(parts) == 3:
                return (parts[0], parts[1], parts[2], 255)
            return (parts[0], parts[1], parts[2], parts[3])

    log.debug("не разобрал цвет %r, беру запасной", value)
    return default


@dataclass(frozen=True)
class Geometry:
    """Размеры плашки в пикселях при 100% масштабе экрана."""

    width: int = 185
    height: int = 36
    corner_radius: float = 18.0
    border_width: float = 1.5
    dot_diameter: float = 8.0
    bar_height: float = 2.5
    padding_x: float = 14.0
    # Расстояние от точки статуса до первой буквы.
    text_gap: float = 10.0
    font_size: int = 13

    @classmethod
    def from_json(cls, data: dict) -> "Geometry":
        def number(*names, default):
            for name in names:
                value = data.get(name)
                if isinstance(value, (int, float)):
                    return value
            return default

        return cls(
            width=int(number("width_px", "width", default=cls.width)),
            height=int(number("height_px", "height", default=cls.height)),
            corner_radius=float(
                number("corner_radius_px", "corner_radius", default=cls.corner_radius)
            ),
            border_width=float(
                number("border_width_px", "border_width", default=cls.border_width)
            ),
            dot_diameter=float(
                number(
                    "indicator_dot_diameter_px",
                    "indicator_dot_size",
                    default=cls.dot_diameter,
                )
            ),
            bar_height=float(
                number("level_bar_height_px", "level_bar_height", default=cls.bar_height)
            ),
            padding_x=float(number("padding_left_px", "padding_x", default=cls.padding_x)),
            font_size=int(number("font_size_px", "font_size", default=cls.font_size)),
        )


# Смысловые цвета состояний — запасные, если тема их не задаёт.
# Общие для всех тем не по лени: «Ошибка» обязана быть красной, а «Готово»
# зелёным в любом оформлении. Тема, перекрасившая ошибку под цвет канта,
# была бы красивой и бесполезной.
STATE_COLORS = {
    "recording": "#78716C",
    "silent": "#A8A29E",
    "sending": "#D97706",
    "refining": "#57534E",
    "ok": "#16A34A",
    "error": "#DC2626",
}

STATE_LABELS = {
    "recording": "Запись…",
    "silent": "Микрофон молчит",
    "sending": "Распознаю…",
    "refining": "Правлю текст…",
    "ok": "Готово",
    "error": "Ошибка",
}


@dataclass(frozen=True)
class State:
    """Как выглядит один этап работы."""

    label: str
    accent: tuple[int, int, int, int]
    text: tuple[int, int, int, int]
    # Свечение вокруг точки. Задано в теме отдельным цветом с собственной
    # прозрачностью (dot_glow_rgba), а не выведено из цвета точки: у макета
    # для каждого состояния своя сила свечения.
    glow: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass(frozen=True)
class Theme:
    """Одно оформление плашки."""

    id: str
    name: str
    geometry: Geometry = field(default_factory=Geometry)
    background: tuple[int, int, int, int] = (48, 40, 36, 33)
    border: tuple[int, int, int, int] = (95, 83, 78, 191)
    # Верхняя световая фаска: тонкая светлая дуга, из-за которой капсула
    # читается как стекло, а не как наклейка.
    specular: tuple[int, int, int, int] = (255, 255, 255, 115)
    # Дорожка, по которой ездит полоска уровня.
    bar_track: tuple[int, int, int, int] = (0, 0, 0, 20)
    # Внешняя тень: цвет, смещение вниз и радиус размытия. В CSS макета это
    # box-shadow: 0 6px 16px. Без неё капсула лежит на экране плоской
    # наклейкой, а не парит над документом.
    shadow: tuple[int, int, int, int] = (28, 25, 23, 31)
    shadow_offset: float = 6.0
    shadow_blur: float = 16.0
    text: tuple[int, int, int, int] = (28, 25, 23, 255)
    states: dict[str, State] = field(default_factory=dict)

    def state(self, name: str) -> State:
        return self.states.get(
            name, State(name, (128, 128, 128, 255), self.text)
        )

    @property
    def signature(self) -> tuple:
        """Чем тема отличается на вид. Нужно, чтобы не показывать дубли."""
        return (
            self.background,
            self.border,
            self.text,
            self.states.get("recording", State("", (0, 0, 0, 0), (0, 0, 0, 0))).accent,
        )


def _states_from(data: dict, accent: str | None, text: tuple) -> dict[str, State]:
    """Собирает таблицу состояний темы.

    accent — цвет точки записи, заданный запасной темой. Он подменяет только
    состояние «идёт запись»: у запасных тем своих состояний нет, они меняют
    корпус плашки и огонёк записи.
    """
    declared = data.get("states")
    states: dict[str, State] = {}
    for name, label in STATE_LABELS.items():
        colour = STATE_COLORS[name]
        own_text = text
        entry = declared.get(name) if isinstance(declared, dict) else None
        if isinstance(entry, dict):
            label = entry.get("display_text") or entry.get("label") or label
            colour = (
                entry.get("dot_color_hex")
                or entry.get("bar_color_hex")
                or entry.get("color")
                or colour
            )
            if entry.get("text_color_hex"):
                own_text = parse_color(entry["text_color_hex"], default=text)
        elif name == "recording" and accent:
            colour = accent

        dot = parse_color(colour)
        glow = (0, 0, 0, 0)
        if isinstance(entry, dict) and entry.get("dot_glow_rgba"):
            glow = parse_color(entry["dot_glow_rgba"], default=(0, 0, 0, 0))
        else:
            # У запасных тем свечения в файле нет. Берём цвет точки и ту же
            # прозрачность, что стоит у основного макета, — так они выглядят
            # одинаково живыми, а не плоскими рядом с ним.
            glow = (*dot[:3], 90)
        states[name] = State(label, dot, own_text, glow)
    return states


def _theme_from(theme_id: str, data: dict, geometry: Geometry) -> Theme:
    surface = data.get("surface") or {}
    typography = data.get("typography") or {}
    bar = data.get("audio_level_bar") or {}

    own_geometry = data.get("geometry")
    if isinstance(own_geometry, dict):
        geometry = Geometry.from_json({**own_geometry, **typography})

    text = parse_color(
        typography.get("text_color_hex")
        or surface.get("text_color_hex")
        or surface.get("text_hex"),
        default=(28, 25, 23, 255),
    )
    accent = surface.get("dot_hex") or surface.get("bar_hex")

    return Theme(
        id=theme_id,
        name=(
            data.get("theme_name_ru")
            or data.get("name_ru")
            or data.get("name")
            or theme_id
        ),
        geometry=geometry,
        background=parse_color(
            surface.get("background_color_rgba") or surface.get("background_rgba"),
            default=(48, 40, 36, 33),
        ),
        border=parse_color(
            surface.get("border_color_rgba") or surface.get("border_rgba"),
            default=(95, 83, 78, 191),
        ),
        specular=parse_color(
            surface.get("inner_specular_edge_rgba"), default=(255, 255, 255, 115)
        ),
        bar_track=parse_color(bar.get("track_color_rgba"), default=(0, 0, 0, 20)),
        shadow=parse_color(
            (surface.get("drop_shadow") or {}).get("color_rgba")
            or surface.get("shadow_rgba"),
            default=(28, 25, 23, 31),
        ),
        shadow_offset=float(
            (surface.get("drop_shadow") or {}).get("offset_y_px", 6.0) or 6.0
        ),
        shadow_blur=float(
            (surface.get("drop_shadow") or {}).get("blur_radius_px", 16.0) or 16.0
        ),
        text=text,
        states=_states_from(data, accent, text),
    )


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        # Битый файл темы — не повод остаться без плашки.
        log.warning("тема %s не прочитана (%s), пропускаю", path.name, exc)
        return None


def load_themes(directory: Path | None = None) -> dict[str, Theme]:
    """Все доступные оформления. Первым идёт основное.

    Порядок важен: меню в трее показывает темы в этом же порядке, и человек
    ожидает увидеть утверждённый макет сверху.
    """
    root = directory or Path(__file__).resolve().parent
    themes: dict[str, Theme] = {}

    primary = _read(root / PRIMARY_FILE)
    if primary:
        geometry = Geometry.from_json(primary.get("geometry") or {})
        theme_id = primary.get("theme_id") or PRIMARY_ID
        themes[theme_id] = _theme_from(theme_id, primary, geometry)

    for name in BACKUP_FILES:
        data = _read(root / name)
        if not data:
            continue
        geometry = Geometry.from_json(data.get("geometry") or {})
        bucket = data.get("themes") or data.get("backup_themes") or {}
        if not isinstance(bucket, dict):
            continue
        for theme_id, entry in bucket.items():
            if not isinstance(entry, dict) or theme_id in themes:
                continue
            theme = _theme_from(theme_id, entry, geometry)
            # Один и тот же макет лежит в двух файлах под разными именами:
            # утверждённый «Тёплый Дымчатый Мокко» он же «graphite_warm_taupe»
            # со страницы предпросмотра. Показывать его в меню дважды —
            # значит заставить человека гадать, чем пункты отличаются.
            if any(theme.signature == other.signature for other in themes.values()):
                log.debug("тема %s повторяет уже загруженную, пропускаю", theme_id)
                continue
            themes[theme_id] = theme

    if not themes:
        # Ни одного файла не нашлось — плашка всё равно должна работать.
        log.warning("файлы тем не найдены, беру встроенное оформление")
        themes[PRIMARY_ID] = Theme(
            id=PRIMARY_ID,
            name="Тёплый Дымчатый Мокко",
            states=_states_from({}, None, (28, 25, 23, 255)),
        )
    return themes


def pick(themes: dict[str, Theme], wanted: str | None) -> Theme:
    """Тема по имени, а если такой нет — первая (то есть основная)."""
    if wanted and wanted in themes:
        return themes[wanted]
    if wanted:
        log.warning("темы «%s» нет, беру основную", wanted)
    return next(iter(themes.values()))
