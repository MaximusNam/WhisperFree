"""Автозапуск через папку автозагрузки.

Почему не реестр: HKCU умеют виртуализировать. Процесс в контейнере приложения
пишет в свой куст, читает оттуда же и выглядит довольным, а настоящий сеанс
пользователя записи не видит. Файл в папке автозагрузки пользователь может
открыть глазами и удалить руками — проверяемость важнее традиции.
"""

from __future__ import annotations

import pytest

from whisperfree import autostart


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """Подменяем папку автозагрузки и глушим работу с реестром."""
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_registry_value", lambda: None)
    monkeypatch.setattr(autostart, "_delete_registry_value", lambda: None)
    return tmp_path


class TestEnableDisable:
    def test_enable_creates_the_launcher(self, startup):
        assert autostart.enable()
        assert (startup / "WhisperFree.vbs").is_file()
        assert autostart.is_enabled()

    def test_disable_removes_it(self, startup):
        autostart.enable()
        assert autostart.disable()
        assert not (startup / "WhisperFree.vbs").exists()
        assert not autostart.is_enabled()

    def test_disable_is_idempotent(self, startup):
        assert autostart.disable()
        assert autostart.disable()

    def test_enable_twice_is_harmless(self, startup):
        assert autostart.enable()
        assert autostart.enable()
        assert len(list(startup.iterdir())) == 1

    def test_set_enabled_switches_both_ways(self, startup):
        autostart.set_enabled(True)
        assert autostart.is_enabled()
        autostart.set_enabled(False)
        assert not autostart.is_enabled()

    def test_missing_folder_is_created(self, tmp_path, monkeypatch):
        target = tmp_path / "нет" / "такой" / "папки"
        monkeypatch.setattr(autostart, "startup_dir", lambda: target)
        monkeypatch.setattr(autostart, "_delete_registry_value", lambda: None)

        assert autostart.enable()
        assert (target / "WhisperFree.vbs").is_file()


class TestLauncherFile:
    def test_written_as_utf16_with_bom(self, startup):
        """WSH распознаёт UTF-16 по BOM независимо от кодовой страницы.

        Путь к проекту может содержать кириллицу — как здесь и содержит, —
        так что ASCII не годится, а UTF-8 без BOM WSH прочитает неверно.
        """
        autostart.enable()
        raw = (startup / "WhisperFree.vbs").read_bytes()

        assert raw[:2] in (b"\xff\xfe", b"\xfe\xff"), "нет BOM"
        assert raw.decode("utf-16").startswith("' WhisperFree")

    def test_carriage_returns_are_not_doubled(self, startup):
        # Текстовая запись без newline="" удвоила бы \r\n, и WSH отказался бы
        # выполнять такой файл.
        autostart.enable()
        text = (startup / "WhisperFree.vbs").read_bytes().decode("utf-16")

        assert "\r\r" not in text
        assert "\r\n" in text  # но сами CRLF на месте

    def test_script_launches_hidden_and_does_not_wait(self):
        # 0 — окно скрыто, False — не ждать завершения: иначе wscript висел бы
        # в памяти всё время работы программы.
        assert ", 0, False" in autostart.launcher_script()

    def test_script_sets_the_working_directory(self):
        script = autostart.launcher_script()
        assert "shell.CurrentDirectory" in script
        assert str(autostart.project_dir()) in script

    def test_script_uses_pythonw_not_python(self):
        # python.exe открыл бы чёрное окно консоли при каждом входе в систему.
        assert "pythonw.exe" in autostart.launcher_script()

    def test_script_tells_how_to_turn_it_off(self):
        assert "Delete this file" in autostart.launcher_script()

    def test_frozen_build_launches_the_exe_directly(self, monkeypatch):
        monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
        monkeypatch.setattr(autostart.sys, "executable", r"C:\App\WhisperFree.exe")

        script = autostart.launcher_script()
        assert "WhisperFree.exe" in script
        assert "-m whisperfree" not in script


class TestLegacyRegistry:
    def test_old_registry_entry_still_counts_as_enabled(self, tmp_path, monkeypatch):
        # Конфиг мог остаться от версии, которая писала в реестр.
        monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
        monkeypatch.setattr(autostart, "_registry_value", lambda: "какая-то команда")

        assert autostart.is_enabled()

    def test_enable_clears_the_old_entry(self, tmp_path, monkeypatch):
        # Иначе программа запускалась бы дважды.
        cleared = []
        monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
        monkeypatch.setattr(autostart, "_delete_registry_value", lambda: cleared.append(1))

        autostart.enable()
        assert cleared == [1]

    def test_disable_clears_it_too(self, tmp_path, monkeypatch):
        cleared = []
        monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
        monkeypatch.setattr(autostart, "_delete_registry_value", lambda: cleared.append(1))

        autostart.disable()
        assert cleared == [1]


class TestPreviousName:
    """Автозапуск, оставшийся от имени VoiceFlow.

    Если его не снять, при входе в систему поднимутся две копии: старая по
    старому файлу и новая по новому. Мьютекс не даст второй работать, но
    пользователь увидит в логе непонятную ошибку.
    """

    def test_old_launcher_counts_as_enabled(self, startup):
        (startup / "VoiceFlow.vbs").write_text("' старый", encoding="utf-16")
        assert autostart.is_enabled()

    def test_enable_removes_the_old_launcher(self, startup):
        (startup / "VoiceFlow.vbs").write_text("' старый", encoding="utf-16")

        assert autostart.enable()
        assert (startup / "WhisperFree.vbs").is_file()
        assert not (startup / "VoiceFlow.vbs").exists()

    def test_disable_removes_the_old_launcher_too(self, startup):
        (startup / "VoiceFlow.vbs").write_text("' старый", encoding="utf-16")

        assert autostart.disable()
        assert not (startup / "VoiceFlow.vbs").exists()
        assert not autostart.is_enabled()

    def test_missing_old_launcher_is_not_an_error(self, startup):
        assert autostart.enable()
        assert autostart.disable()
