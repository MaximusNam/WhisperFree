"""Файлы запуска для Windows: .bat и .vbs.

Тест существует потому, что я на этом уже обжёгся, а диагностика была
неочевидной: батник с одиночным LF не падает с внятной ошибкой, он теряет по
символу на каждой границе строк, и пользователь видит загадочное
«'cho' is not recognized» и обрубок пути вместо python.exe.

Проверяется два свойства:

1. CRLF. cmd.exe читает батник, рассчитывая на два байта в конце строки.
2. Только ASCII. Батник читается в кодировке консоли (на русской системе
   cp866), а не в UTF-8, поэтому кириллица там превращается в кашу.
   Весь русский текст выводит Python, который с юникодом работает правильно.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHERS = sorted(
    [*PROJECT_ROOT.glob("*.bat"), *PROJECT_ROOT.glob("*.vbs")], key=lambda p: p.name
)


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
