"""Вставка текста в активное окно Windows.

Основной путь: положить текст в буфер обмена и послать один Ctrl+V через
SendInput. Посимвольный ввод через KEYEVENTF_UNICODE медленнее и ломается о
модификаторы, поэтому он остаётся запасным вариантом.

Главная гарантия этого модуля: текст оказывается в буфере обмена ДО попытки
вставки. Определить, вставилось ли на самом деле, в Windows в общем случае
нельзя, поэтому мы не пытаемся ловить провал — мы делаем его безболезненным.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MAPVK_VK_TO_VSC = 0

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_RETURN = 0x0D
VK_TAB = 0x09

MODIFIER_VKS = {
    "ctrl": VK_CONTROL,
    "control": VK_CONTROL,
    "shift": VK_SHIFT,
    "alt": VK_MENU,
    "menu": VK_MENU,
    "win": VK_LWIN,
    "super": VK_LWIN,
    "cmd": VK_LWIN,
}

ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE

kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


# --- окно под курсором --------------------------------------------------------


def foreground_exe() -> str:
    """Имя exe окна, которое сейчас в фокусе. Пустая строка, если не удалось.

    Нужно для двух вещей: выбрать правильную клавишу вставки (в терминалах это
    Ctrl+Shift+V) и записать в историю, куда именно уходил текст.
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return Path(buf.value).name
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:  # pragma: no cover - зависит от прав процесса
        log.debug("не удалось определить активное окно: %s", exc)
        return ""


def has_foreground_window() -> bool:
    try:
        return bool(user32.GetForegroundWindow())
    except Exception:  # pragma: no cover
        return False


# --- буфер обмена -------------------------------------------------------------


def _open_clipboard(retries: int = 12, delay: float = 0.02) -> bool:
    """Буфер часто занят другим процессом — пробуем несколько раз."""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def get_clipboard_text() -> str | None:
    """Текст из буфера или None, если там не текст либо буфер недоступен."""
    if not _open_clipboard():
        log.debug("буфер обмена занят, прочитать не удалось")
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception as exc:  # pragma: no cover
        log.debug("ошибка чтения буфера: %s", exc)
        return None
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """Кладёт текст в буфер. Владение памятью переходит системе.

    Размер берём у самого буфера, а не считаем как len(text) * 2: в UTF-16
    символы вне BMP занимают две единицы, и эмодзи от такого счёта обрезался
    до половины суррогатной пары.
    """
    buffer = ctypes.create_unicode_buffer(text)  # завершающий NUL уже внутри
    size = ctypes.sizeof(buffer)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        log.warning("GlobalAlloc не выделил память под буфер обмена")
        return False

    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(ptr, buffer, size)
    finally:
        kernel32.GlobalUnlock(handle)

    if not _open_clipboard():
        kernel32.GlobalFree(handle)
        log.warning("буфер обмена занят, записать не удалось")
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        # После успешного SetClipboardData память освобождает система.
        return True
    finally:
        user32.CloseClipboard()


# --- клавиатура ---------------------------------------------------------------


def _key_input(vk: int, up: bool = False) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_KEYUP if up else 0
    return INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)),
    )


def _unicode_input(code: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)),
    )


def _send(events: list[INPUT]) -> bool:
    if not events:
        return True
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        log.warning("SendInput отправил %d из %d событий (err=%d)",
                    sent, len(events), ctypes.get_last_error())
        return False
    return True


def parse_combo(spec: str) -> tuple[list[int], int] | None:
    """'ctrl+shift+v' -> ([VK_CONTROL, VK_SHIFT], VK_V).

    Коды виртуальных клавиш не зависят от раскладки: на русской раскладке
    Ctrl+V — это физически Ctrl+М, и VK_V остаётся правильным кодом.
    """
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        return None
    *mods, key = parts
    mod_vks = []
    for m in mods:
        vk = MODIFIER_VKS.get(m)
        if vk is None:
            log.warning("неизвестный модификатор в сочетании %r: %s", spec, m)
            return None
        mod_vks.append(vk)

    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        main_vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        main_vk = 0x70 + int(key[1:]) - 1
    elif key in ("enter", "return"):
        main_vk = VK_RETURN
    elif key == "tab":
        main_vk = VK_TAB
    else:
        log.warning("неизвестная клавиша в сочетании %r: %s", spec, key)
        return None
    return mod_vks, main_vk


def send_combo(spec: str) -> bool:
    """Нажимает и отпускает сочетание клавиш."""
    parsed = parse_combo(spec)
    if parsed is None:
        return False
    mods, main_vk = parsed
    events = [_key_input(vk) for vk in mods]
    events.append(_key_input(main_vk))
    events.append(_key_input(main_vk, up=True))
    events.extend(_key_input(vk, up=True) for vk in reversed(mods))
    return _send(events)


def modifiers_down() -> bool:
    """Держит ли пользователь сейчас какой-нибудь модификатор."""
    for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            return True
    return False


def wait_modifiers_released(timeout_ms: int = 400) -> bool:
    """Ждёт, пока пользователь физически отпустит модификаторы.

    Без этого Ctrl+V, посланный сразу после отпускания клавиши диктовки,
    может слиться с ещё зажатым Shift и превратиться в Ctrl+Shift+V.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if not modifiers_down():
            return True
        time.sleep(0.01)
    return not modifiers_down()


def type_unicode(text: str, chunk: int = 40, delay: float = 0.003) -> bool:
    """Посимвольный ввод — запасной путь, когда буфер трогать нельзя."""
    ok = True
    events: list[INPUT] = []

    def flush() -> None:
        nonlocal events, ok
        if events:
            ok = _send(events) and ok
            events = []
            time.sleep(delay)

    for ch in text:
        if ch == "\n":
            flush()
            ok = _send([_key_input(VK_RETURN), _key_input(VK_RETURN, up=True)]) and ok
            continue
        if ch == "\r":
            continue
        for code in _utf16_units(ch):
            events.append(_unicode_input(code))
            events.append(_unicode_input(code, up=True))
        if len(events) >= chunk * 2:
            flush()
    flush()
    return ok


def _utf16_units(ch: str) -> list[int]:
    """Символы вне BMP уходят суррогатной парой."""
    code = ord(ch)
    if code <= 0xFFFF:
        return [code]
    code -= 0x10000
    return [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]


# --- основная операция --------------------------------------------------------


def paste_key_for(exe: str, default: str, overrides: dict[str, str]) -> str:
    """Клавиша вставки для конкретного приложения.

    В терминалах Ctrl+V работает не везде, поэтому для них задан Ctrl+Shift+V.
    """
    if not exe:
        return default
    lowered = {k.lower(): v for k, v in overrides.items()}
    return lowered.get(exe.lower(), default)


class Injector:
    """Вставляет текст в активное окно по правилам из конфига."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._restore_timer: threading.Timer | None = None

    def _schedule_restore(self, previous: str | None) -> None:
        """Возвращает прежний буфер, но с задержкой.

        Мгновенное восстановление убило бы страховку: если вставка не прошла,
        у пользователя должна остаться возможность нажать Ctrl+V руками.
        Роль долговременной страховки играет история, а не буфер.
        """
        if previous is None or not self.cfg.restore_clipboard:
            return
        if self._restore_timer is not None:
            self._restore_timer.cancel()

        def restore() -> None:
            try:
                set_clipboard_text(previous)
            except Exception as exc:  # pragma: no cover
                log.debug("не удалось вернуть буфер обмена: %s", exc)

        self._restore_timer = threading.Timer(self.cfg.restore_delay_ms / 1000.0, restore)
        self._restore_timer.daemon = True
        self._restore_timer.start()

    def put_in_clipboard(self, text: str) -> bool:
        """Кладёт текст в буфер, ничего не вставляя."""
        return set_clipboard_text(text)

    def paste(self, text: str, target_exe: str | None = None) -> tuple[bool, str]:
        """Вставляет текст. Возвращает (успех, описание причины неуспеха).

        Даже при неуспехе текст уже лежит в буфере обмена.
        """
        if not text:
            return True, ""

        exe = target_exe if target_exe is not None else foreground_exe()

        if self.cfg.method == "unicode":
            # Буфер намеренно не трогаем — страховкой остаётся история.
            wait_modifiers_released(self.cfg.wait_modifiers_ms)
            if not has_foreground_window():
                return False, "нет активного окна"
            ok = type_unicode(text)
            return ok, "" if ok else "посимвольный ввод не прошёл"

        previous = get_clipboard_text() if self.cfg.restore_clipboard else None

        if not set_clipboard_text(text):
            return False, "буфер обмена занят другим приложением"

        if not has_foreground_window():
            return False, "нет активного окна — текст в буфере"

        wait_modifiers_released(self.cfg.wait_modifiers_ms)
        # Небольшая пауза: приложения-подписчики буфера должны увидеть новое
        # содержимое до того, как придёт Ctrl+V.
        time.sleep(0.025)

        combo = paste_key_for(exe, self.cfg.default_paste, self.cfg.paste_overrides)
        ok = send_combo(combo)
        self._schedule_restore(previous)

        if not ok:
            return False, "не удалось отправить сочетание клавиш — текст в буфере"
        return True, ""
