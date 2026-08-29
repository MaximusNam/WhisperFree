"""Защита от второго запущенного экземпляра.

Два работающих WhisperFree — это не просто лишний процесс: оба вешают хук на одну
и ту же клавишу, оба пишут в один лог и оба держат микрофон. На отпускание
клавиши сработают оба, и текст вставится дважды.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183
# Минимальное право доступа: нам нужно только узнать, существует ли мьютекс.
SYNCHRONIZE = 0x00100000

# Local\ вместо Global\: имя действует в пределах сеанса пользователя и не
# требует особых прав.
MUTEX_NAME = "Local\\WhisperFree-singleton-b7f1"

# Программа звалась VoiceFlow, и копия прежней версии держит мьютекс под
# старым именем. Не проверив его, новая версия сочла бы себя единственной,
# и на отпускание клавиши сработали бы обе: текст вставился бы дважды.
LEGACY_MUTEX_NAME = "Local\\VoiceFlow-singleton-b7f1"


class SingleInstance:
    """Именованный мьютекс Windows. Держится, пока жив процесс."""

    def __init__(
        self, name: str = MUTEX_NAME, legacy_name: str = LEGACY_MUTEX_NAME
    ) -> None:
        self.name = name
        self.legacy_name = legacy_name
        self._handle = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.OpenMutexW.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self._kernel32.OpenMutexW.restype = wintypes.HANDLE

    def acquire(self) -> bool:
        """True — мы единственные. False — экземпляр уже запущен."""
        if self._legacy_instance_running():
            log.warning("работает копия под прежним именем VoiceFlow")
            return False

        try:
            handle = self._kernel32.CreateMutexW(None, True, self.name)
            last_error = ctypes.get_last_error()
        except Exception as exc:  # pragma: no cover - системная функция
            log.warning("не удалось проверить единственность экземпляра: %s", exc)
            return True

        if not handle:
            log.warning("CreateMutexW не вернул дескриптор, проверку пропускаю")
            return True

        self._handle = handle
        if last_error == ERROR_ALREADY_EXISTS:
            log.warning("WhisperFree уже запущен")
            return False
        return True

    def _legacy_instance_running(self) -> bool:
        """Проверяет мьютекс прежней версии, ничего им не завладевая.

        OpenMutexW, а не CreateMutexW: создавать чужой мьютекс нельзя — тогда
        мы сами станем тем «уже запущенным экземпляром», который ищем.
        """
        try:
            handle = self._kernel32.OpenMutexW(SYNCHRONIZE, False, self.legacy_name)
        except Exception:  # pragma: no cover - системная функция
            return False
        if not handle:
            return False
        self._kernel32.CloseHandle(handle)
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            try:
                self._kernel32.ReleaseMutex(handle)
                self._kernel32.CloseHandle(handle)
            except Exception:  # pragma: no cover
                pass

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
