"""Диагностика окружения WhisperFree.

Отвечает на один вопрос: КАКОЙ ИМЕННО файл config.toml прочитал ЭТОТ процесс.

Пишется потому, что путь, который печатает программа, и файл, который она
на самом деле открывает, могут не совпадать: у упакованных (MSIX/Store)
приложений Windows подменяет %APPDATA% прозрачно, ниже уровня Win32 API.
Строка пути при этом остаётся прежней, а файл — другой. Поэтому здесь
печатается не путь, а РАЗМЕР, ВРЕМЯ и SHA-256 реально открытого файла:
их можно сравнить между двумя запусками.

Запускать одинаково: и двойным щелчком из Проводника, и из терминала.
Если хеши разошлись — процессы читают разные файлы.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import sys
from pathlib import Path


def line(title: str = "") -> None:
    print("-" * 72 if not title else f"--- {title} " + "-" * max(0, 68 - len(title)))


def fingerprint(path: Path) -> str:
    """Размер + mtime + хеш: подпись файла, не зависящая от его пути."""
    try:
        st = path.stat()
    except OSError as exc:
        return f"НЕТ ({exc.__class__.__name__}: {exc})"
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError as exc:
        return f"есть, но не читается ({exc})"
    import datetime

    when = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return f"{st.st_size} байт  mtime={when}  sha256:{digest}"


def device_in(path: Path) -> str:
    """Значение device из [audio] — сырым текстом, без TOML-парсера."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<не прочитан: {exc}>"
    audio = re.split(r"^\[audio\]\s*$", text, flags=re.M)
    if len(audio) < 2:
        return "<секции [audio] нет>"
    tail = re.split(r"^\[", audio[1], flags=re.M)[0]
    found = re.search(r'^\s*device\s*=\s*(.+?)\s*$', tail, flags=re.M)
    return found.group(1) if found else "<ключа device нет>"


def main() -> int:
    print()
    line("КТО Я")
    print(f"sys.executable   {sys.executable}")
    print(f"sys.version      {sys.version.split()[0]}")
    print(f"sys.prefix       {sys.prefix}")
    print(f"os.getcwd()      {os.getcwd()}")
    print(f"argv[0]          {sys.argv[0]}")
    print(f"PID              {os.getpid()}")

    line("ОКРУЖЕНИЕ")
    for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "USERNAME",
                 "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONUTF8"):
        print(f"{name:<14} {os.environ.get(name, '<не задана>')}")

    line("ПАКЕТ whisperfree")
    try:
        import whisperfree
        from whisperfree import config as vf_config
    except Exception as exc:  # noqa: BLE001
        print(f"НЕ ИМПОРТИРУЕТСЯ: {exc!r}")
        return 2
    print(f"whisperfree        {whisperfree.__file__}")
    print(f"whisperfree.config {vf_config.__file__}")
    print(f"app_dir()        {vf_config.app_dir()}")

    cfg_path = vf_config.config_path()
    log_path = vf_config.log_path()

    line("ЧТО ПРОГРАММА СЧИТАЕТ СВОИМ КОНФИГОМ")
    print(f"путь             {cfg_path}")
    print(f"подпись файла    {fingerprint(cfg_path)}")
    print(f"device в файле   {device_in(cfg_path)}")
    print(f"лог              {log_path}")
    print(f"подпись лога     {fingerprint(log_path)}")

    line("ЧТО ПОЛУЧАЕТСЯ ПОСЛЕ ЗАГРУЗКИ")
    try:
        cfg = vf_config.load_config()
        print(f"cfg.path                 {cfg.path}")
        print(f"cfg.audio.device         {cfg.audio.device!r}")
        print(f"cfg.audio.sample_rate    {cfg.audio.sample_rate}")
        print(f"cfg.hotkeys.dictate      {cfg.hotkeys.dictate!r}")
        print(f"cfg.provider.model       {cfg.provider.model!r}")
        key_env = cfg.provider.api_key_env
        print(f"{key_env:<24} {'найден' if cfg.provider.api_key else 'НЕ НАЙДЕН'}")
        if not cfg.audio.device:
            print()
            print("  ВНИМАНИЕ: device пустой. Либо он пустой в файле выше (сравните")
            print("  строку 'device в файле'), либо конфиг не разобрался и взяты")
            print("  умолчания — тогда выше по выводу есть строка 'не читается'.")
    except Exception as exc:  # noqa: BLE001
        print(f"load_config УПАЛ: {exc!r}")

    line("ПОДМЕНА %APPDATA% (MSIX / Store-контейнер)")
    # Упакованные приложения пишут не в Roaming, а в LocalCache пакета.
    # Процесс внутри контейнера этой подмены не видит: путь тот же, файл другой.
    local = os.environ.get("LOCALAPPDATA", "")
    pattern = os.path.join(local, "Packages", "*", "LocalCache", "Roaming",
                           "WhisperFree", "config.toml")
    shadows = sorted(glob.glob(pattern))
    if shadows:
        for shadow in shadows:
            print(f"копия в контейнере: {shadow}")
            print(f"  подпись          {fingerprint(Path(shadow))}")
            print(f"  device           {device_in(Path(shadow))}")
    else:
        print("копий в контейнерах пакетов не найдено")

    # UNC-путь идёт мимо слоя подмены и показывает настоящий файл на диске.
    drive = os.path.splitdrive(str(cfg_path))[0].rstrip(":")
    bypass = Path(rf"\\localhost\{drive}$" + str(cfg_path)[2:])
    print()
    print(f"тот же путь через UNC (мимо подмены):")
    print(f"  {bypass}")
    print(f"  подпись          {fingerprint(bypass)}")
    print(f"  device           {device_in(bypass)}")
    print()
    print("  Если подписи 'что программа считает своим конфигом' и UNC РАЗНЫЕ —")
    print("  этот процесс работает с подменённым %APPDATA%, и правки, сделанные")
    print("  здесь, до обычного запуска из Проводника не доходят.")

    line("МИКРОФОНЫ")
    try:
        import sounddevice as sd
        default_in = sd.default.device[0]
        for index, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                api = sd.query_hostapis(dev["hostapi"])["name"]
                mark = " <-- по умолчанию" if index == default_in else ""
                print(f"  {index:3d}  {dev['name']}  [{api}]{mark}")
    except Exception as exc:  # noqa: BLE001
        print(f"список устройств недоступен: {exc!r}")

    line()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
