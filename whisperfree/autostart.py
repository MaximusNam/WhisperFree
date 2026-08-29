"""Автозапуск при входе в систему.

Используется папка автозагрузки, а не ключ реестра HKCU\\...\\Run, и на то есть
причина. Реестр умеют виртуализировать: процесс в контейнере приложения пишет
в свой куст, читает оттуда же и выглядит вполне довольным, а настоящий сеанс
пользователя этой записи не видит. Ровно так же ведёт себя подменённый
%APPDATA%. Папку автозагрузки пользователь может открыть глазами
(`shell:startup`), увидеть там файл и удалить его руками — проверяемость тут
важнее традиции.

Старая запись в реестре, если она осталась от прежних версий, снимается.
"""

from __future__ import annotations

import logging
import os
import sys
import winreg
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WhisperFree"
LAUNCHER_NAME = "WhisperFree.vbs"

# Прежнее имя программы. Файл и запись под ним нужно снимать, иначе
# при входе в систему запустятся две копии.
LEGACY_VALUE_NAME = "VoiceFlow"
LEGACY_LAUNCHER_NAME = "VoiceFlow.vbs"


def startup_dir() -> Path:
    """Папка автозагрузки текущего пользователя."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData/Roaming"
    return root / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher_path() -> Path:
    return startup_dir() / LAUNCHER_NAME


def legacy_launcher_path() -> Path:
    return startup_dir() / LEGACY_LAUNCHER_NAME


def project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def launcher_script() -> str:
    """Содержимое .vbs для папки автозагрузки.

    Комментарии латиницей, но путь может быть каким угодно: у этого проекта он
    содержит кириллицу. Поэтому файл пишется в UTF-16 с BOM — единственная
    кодировка, которую WSH распознаёт независимо от кодовой страницы системы.
    """
    if getattr(sys, "frozen", False):
        target = str(Path(sys.executable))
        command = f'"""{target}"""'
        workdir = str(Path(sys.executable).parent)
    else:
        executable = Path(sys.executable)
        pythonw = executable.with_name("pythonw.exe")
        runner = pythonw if pythonw.exists() else executable
        workdir = str(project_dir())
        command = f'"""{runner}"" -m whisperfree"'

    lines = [
        "' WhisperFree: starts automatically at sign-in.",
        "' Delete this file to turn autostart off, or use the tray menu.",
        "",
        "Dim shell",
        'Set shell = CreateObject("WScript.Shell")',
        f'shell.CurrentDirectory = "{workdir}"',
        f"shell.Run {command}, 0, False",
        "",
    ]
    return "\r\n".join(lines)


def is_enabled() -> bool:
    return (
        launcher_path().is_file()
        or legacy_launcher_path().is_file()
        or _registry_value() is not None
    )


def enable() -> bool:
    """Кладёт launcher в автозагрузку. Возвращает успех."""
    path = launcher_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # UTF-16 с BOM: WSH распознаёт её независимо от кодовой страницы,
        # а путь к проекту может содержать не только латиницу.
        # newline="" обязателен: без него текстовая запись удвоит возврат
        # каретки в концах строк, и WSH такой файл выполнять откажется.
        path.write_text(launcher_script(), encoding="utf-16", newline="")
    except OSError as exc:
        log.error("не удалось включить автозапуск (%s): %s", path, exc)
        return False

    # Прежние версии писали в реестр и звались иначе — убираем и то и другое,
    # чтобы при входе в систему не поднялись две копии.
    _delete_legacy_launcher()
    _delete_registry_value()
    log.info("автозапуск включён: %s", path)
    return True


def disable() -> bool:
    ok = True
    path = launcher_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.error("не удалось убрать %s: %s", path, exc)
        ok = False
    _delete_legacy_launcher()
    _delete_registry_value()
    log.info("автозапуск выключен")
    return ok


def set_enabled(value: bool) -> bool:
    return enable() if value else disable()


# --- совместимость со старым способом ------------------------------------------


def _registry_value() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in (VALUE_NAME, LEGACY_VALUE_NAME):
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except FileNotFoundError:
                    continue
            return None
    except FileNotFoundError:
        return None
    except OSError as exc:  # pragma: no cover - зависит от прав
        log.debug("не удалось прочитать ключ автозапуска: %s", exc)
        return None


def _delete_registry_value() -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name in (VALUE_NAME, LEGACY_VALUE_NAME):
                try:
                    winreg.DeleteValue(key, name)
                    log.info("снята запись автозапуска из реестра: %s", name)
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover
        log.debug("не удалось снять запись из реестра: %s", exc)


def _delete_legacy_launcher() -> None:
    """Снимает автозапуск, оставшийся от прежнего имени программы."""
    path = legacy_launcher_path()
    try:
        if path.is_file():
            path.unlink()
            log.info("снят старый автозапуск: %s", path)
    except OSError as exc:  # pragma: no cover - зависит от прав
        log.debug("не удалось снять %s: %s", path, exc)
