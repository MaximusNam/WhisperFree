"""Файлы запуска для Windows: .bat и .vbs.

Тест существует потому, что я на этом уже обжёгся, а диагностика была
неочевидной: батник с одиночным LF не падает с внятной ошибкой, он теряет по
символу на каждой границе строк, и пользователь видит загадочное
«'cho' is not recognized» и обрубок пути вместо python.exe.

Проверяется три свойства:

1. CRLF. cmd.exe читает батник, рассчитывая на два байта в конце строки.
2. Только ASCII. Батник читается в кодировке консоли (на русской системе
   cp866), а не в UTF-8, поэтому кириллица там превращается в кашу.
   Весь русский текст выводит Python, который с юникодом работает правильно.
3. Имя запускаемого модуля. Оно живёт в файлах, которые не импортирует ни
   один тест, поэтому переименование пакета их не задевает — см.
   TestLaunchersStartThePackage ниже.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import whisperfree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHERS = sorted(
    [*PROJECT_ROOT.glob("*.bat"), *PROJECT_ROOT.glob("*.vbs")], key=lambda p: p.name
)

# Имя пакета берём у самого пакета, а не строкой: тест должен утверждать
# «батники зовут то, что в репозитории есть», а не «батники зовут whisperfree».
PACKAGE = whisperfree.__name__

# Модули, которые запускают не программу, а инструмент: build.bat ставит
# PyInstaller через `python -m pip`.
TOOL_MODULES = {"pip", "venv", "ensurepip"}


def _modules_started_by(path: Path) -> set[str]:
    """Модули, которые файл запускает через `python -m <модуль>`.

    Отрицательный просмотр назад отсекает хвосты длинных ключей вроде
    `--noconfirm`, где сочетание «m + пробел» встречается случайно.
    """
    text = path.read_text(encoding="ascii")
    return set(re.findall(r"(?<![-\w])-m\s+([A-Za-z_][\w.]*)", text))


def _starts_the_program(path: Path) -> bool:
    return bool(_modules_started_by(path) - TOOL_MODULES)


APP_LAUNCHERS = [p for p in LAUNCHERS if _starts_the_program(p)]


def test_launchers_exist():
    names = {p.name for p in LAUNCHERS}
    assert {"check.bat", "devices.bat", "paste-test.bat", "run.bat", "run.vbs"} <= names


@pytest.mark.parametrize("path", LAUNCHERS, ids=lambda p: p.name)
class TestWindowsLaunchers:
    def test_uses_crlf_line_endings(self, path: Path):
        raw = path.read_bytes()
        assert raw.replace(b"\r\n", b"").count(b"\n") == 0, (
            f"{path.name}: одиночный LF. cmd.exe потеряет по символу на каждой "
            "строке — echo станет cho, а путь к python обрубком"
        )

    def test_contains_only_ascii(self, path: Path):
        raw = path.read_bytes()
        offenders = [i for i, byte in enumerate(raw) if byte > 127]
        assert not offenders, (
            f"{path.name}: не-ASCII байт на позиции {offenders[0]}. "
            "cmd.exe читает файл в кодировке консоли, а не в UTF-8"
        )

    def test_returns_to_its_own_folder(self, path: Path):
        # Ярлык и автозапуск могут стартовать батник из любого каталога.
        text = path.read_text(encoding="ascii")
        marker = "%~dp0" if path.suffix == ".bat" else "ScriptFullName"
        assert marker in text


def _runs_python(path: Path) -> bool:
    return "python.exe" in path.read_text(encoding="ascii")


PYTHON_LAUNCHERS = [p for p in LAUNCHERS if p.suffix == ".bat" and _runs_python(p)]


@pytest.mark.parametrize("path", PYTHON_LAUNCHERS, ids=lambda p: p.name)
class TestBatchDetails:
    """Проверки только для тех батников, что запускают программу.

    Вспомогательные ярлыки вроде sound.bat (открывает панель звука Windows)
    ни окружения, ни паузы не требуют.
    """

    def test_checks_for_the_venv_before_using_it(self, path: Path):
        text = path.read_text(encoding="ascii")
        assert "if not exist" in text and ".venv" in text

    def test_waits_before_closing_the_window(self, path: Path):
        # Без pause окно закроется мгновенно и ошибку никто не прочтёт.
        assert "pause" in path.read_text(encoding="ascii")


def test_every_program_launcher_is_seen():
    """Страховка от пустого списка: без неё проверки ниже молча исчезнут.

    Параметризация по APP_LAUNCHERS даёт ноль тестов, если ни один файл не
    признан запускающим программу, — а пустая параметризация не падает, она
    просто ничего не проверяет. Именно так выглядит зелёный прогон при
    сломанных батниках, от которого мы и защищаемся.
    """
    names = {p.name for p in APP_LAUNCHERS}
    assert {"check.bat", "devices.bat", "paste-test.bat", "run.bat", "run.vbs"} <= names


@pytest.mark.parametrize("path", APP_LAUNCHERS, ids=lambda p: p.name)
class TestLaunchersStartThePackage:
    """Батники и run.vbs должны звать существующий модуль программы.

    Зачем это здесь. Пакет уже переименовывали (VoiceFlow -> WhisperFree), и
    остальные тесты такого переименования не замечают: .bat и .vbs никто не
    импортирует, а побайтовые проверки выше смотрят на переводы строк, ASCII
    и pause — на что угодно, кроме имени модуля. Батник с `-m voiceflow`
    прошёл бы их все и упал бы только у пользователя, сообщением
    «No module named voiceflow». Тест ниже ловит это в CI: имя берётся из
    самого пакета, поэтому следующее переименование либо пройдёт по файлам
    запуска, либо покраснеет здесь.
    """

    def test_starts_the_package_and_nothing_else(self, path: Path):
        started = _modules_started_by(path) - TOOL_MODULES
        assert started == {PACKAGE}, (
            f"{path.name} запускает {sorted(started) or 'ничего'}, "
            f"а пакет называется {PACKAGE}"
        )

    def test_the_module_it_names_can_be_run(self, path: Path):
        # `python -m` требует __main__.py: без него имя модуля правильное,
        # а запуск всё равно падает.
        for module in _modules_started_by(path) - TOOL_MODULES:
            entry = PROJECT_ROOT / Path(*module.split(".")) / "__main__.py"
            assert entry.is_file(), f"{path.name}: у модуля {module} нет {entry.name}"
