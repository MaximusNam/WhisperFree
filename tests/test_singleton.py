"""Защита от второго экземпляра.

Тест появился по горячим следам: у пользователя разом крутились две копии.
Обе вешают хук на одну клавишу, обе держат микрофон, и на отпускание
срабатывают обе — текст вставился бы дважды.
"""

from __future__ import annotations

from whisperfree.singleton import LEGACY_MUTEX_NAME, MUTEX_NAME, SingleInstance


def guard(name: str) -> SingleInstance:
    """Экземпляр с заведомо несуществующим старым именем.

    Без этого тесты зависели бы от того, крутится ли на машине копия прежней
    версии: проверка старого мьютекса — часть поведения по умолчанию, и она
    честно вернула бы «уже запущен» посреди прогона.
    """
    return SingleInstance(name, legacy_name=name + "-no-legacy")


def test_first_instance_gets_the_lock():
    first = guard("Local\\WhisperFree-test-first")
    try:
        assert first.acquire() is True
    finally:
        first.release()


def test_second_instance_is_refused():
    name = "Local\\WhisperFree-test-second"
    first, second = guard(name), guard(name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        second.release()
        first.release()


def test_lock_is_reusable_after_release():
    name = "Local\\WhisperFree-test-reuse"
    first = guard(name)
    assert first.acquire() is True
    first.release()

    second = guard(name)
    try:
        assert second.acquire() is True
    finally:
        second.release()


def test_different_names_do_not_collide():
    a = guard("Local\\WhisperFree-test-a")
    b = guard("Local\\WhisperFree-test-b")
    try:
        assert a.acquire() is True
        assert b.acquire() is True
    finally:
        a.release()
        b.release()


def test_context_manager_releases():
    name = "Local\\WhisperFree-test-ctx"
    with guard(name) as acquired:
        assert acquired is True
    with guard(name) as acquired_again:
        assert acquired_again is True


def test_release_is_safe_to_call_twice():
    lock = guard("Local\\WhisperFree-test-double-release")
    lock.acquire()
    lock.release()
    lock.release()  # не должно бросать


class TestPreviousName:
    """Копия, запущенная под прежним именем программы.

    Переименование не переименовывает уже работающий процесс. Он держит
    мьютекс со старым именем, нового не знает — и, не проверь мы старое имя,
    обе копии считали бы себя единственными.
    """

    def test_running_old_version_blocks_the_new_one(self):
        legacy = "Local\\WhisperFree-test-legacy-holder"
        old = SingleInstance(legacy, legacy_name=legacy + "-none")
        new = SingleInstance(
            "Local\\WhisperFree-test-legacy-new", legacy_name=legacy
        )
        try:
            assert old.acquire() is True
            assert new.acquire() is False
        finally:
            new.release()
            old.release()

    def test_new_version_starts_once_the_old_one_is_gone(self):
        legacy = "Local\\WhisperFree-test-legacy-freed"
        old = SingleInstance(legacy, legacy_name=legacy + "-none")
        old.acquire()
        old.release()

        new = SingleInstance(
            "Local\\WhisperFree-test-legacy-after", legacy_name=legacy
        )
        try:
            assert new.acquire() is True
        finally:
            new.release()

    def test_checking_does_not_claim_the_old_mutex(self):
        # OpenMutexW, а не CreateMutexW: иначе сама проверка создала бы мьютекс,
        # и следующая копия увидела бы «старая версия работает» на пустом месте.
        legacy = "Local\\WhisperFree-test-legacy-untouched"
        first = SingleInstance("Local\\WhisperFree-test-probe-1", legacy_name=legacy)
        try:
            assert first.acquire() is True
        finally:
            first.release()

        second = SingleInstance("Local\\WhisperFree-test-probe-2", legacy_name=legacy)
        try:
            assert second.acquire() is True
        finally:
            second.release()

    def test_default_names_are_the_real_ones(self):
        lock = SingleInstance()
        assert lock.name == MUTEX_NAME
        assert lock.legacy_name == LEGACY_MUTEX_NAME
        assert "WhisperFree" in MUTEX_NAME
        assert "VoiceFlow" in LEGACY_MUTEX_NAME
