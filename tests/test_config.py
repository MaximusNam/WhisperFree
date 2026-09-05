"""Конфиг. Главное требование: битый или устаревший файл не мешает запуску."""

from __future__ import annotations

import pytest
import tomlkit

from whisperfree import config as config_mod
from whisperfree.config import DEFAULT_CONFIG_TOML, Config, load_config, parse_config


def parse(text: str) -> Config:
    return parse_config(tomlkit.parse(text).unwrap())


class TestDefaults:
    def test_shipped_template_parses(self):
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert cfg.hotkeys.dictate == "ctrl_r"
        assert cfg.provider.model == "whisper-large-v3-turbo"
        assert cfg.audio.sample_rate == 16000

    def test_template_has_the_terminal_overrides(self):
        # В терминалах Ctrl+V работает не везде — ради Claude Code это важно.
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert cfg.inject.paste_overrides["WindowsTerminal.exe"] == "ctrl+shift+v"

    def test_template_prompt_seeds_latin_terms(self):
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert "Docker" in cfg.language.prompt_ru
        assert "GitHub" in cfg.language.prompt_ru

    def test_empty_config_falls_back_to_defaults(self):
        cfg = parse("")
        assert cfg.hotkeys.dictate == "ctrl_r"
        assert cfg.history.enabled is True

    def test_dictate_key_avoids_wispr_flow_binding(self):
        # Wispr Flow по умолчанию занимает Ctrl+Win, конфликтовать нельзя.
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert "win" not in cfg.hotkeys.dictate
        assert cfg.hotkeys.paste_last != "shift+alt+z"


class TestResilience:
    def test_unknown_keys_are_ignored(self):
        # Ключ из будущей версии не должен ронять запуск на старой.
        cfg = parse('[hotkeys]\ndictate = "f13"\nsome_future_key = 1\n')
        assert cfg.hotkeys.dictate == "f13"

    def test_unknown_section_is_ignored(self):
        cfg = parse('[hotkeys]\ndictate = "f13"\n\n[future_feature]\nenabled = true\n')
        assert cfg.hotkeys.dictate == "f13"

    def test_missing_sections_use_defaults(self):
        cfg = parse('[provider]\nmodel = "whisper-large-v3"\n')
        assert cfg.provider.model == "whisper-large-v3"
        assert cfg.audio.preroll_ms == 250

    def test_broken_file_still_starts(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text("[hotkeys\nэто не toml", encoding="utf-8")
        monkeypatch.setattr("whisperfree.config.app_dir", lambda: tmp_path)

        cfg = load_config(path)
        assert cfg.hotkeys.dictate == "ctrl_r"

    def test_file_is_created_on_first_run(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        monkeypatch.setattr("whisperfree.config.app_dir", lambda: tmp_path)

        load_config(path)
        assert path.exists()
        assert "prompt_ru" in path.read_text(encoding="utf-8")


class TestSecrets:
    def test_key_comes_from_environment_not_the_file(self, monkeypatch):
        cfg = parse(DEFAULT_CONFIG_TOML)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert cfg.provider.api_key is None

        monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
        assert cfg.provider.api_key == "gsk_secret"

    def test_empty_env_var_counts_as_absent(self, monkeypatch):
        cfg = parse(DEFAULT_CONFIG_TOML)
        monkeypatch.setenv("GROQ_API_KEY", "")
        assert cfg.provider.api_key is None

    def test_template_contains_no_key(self):
        assert "gsk_" not in DEFAULT_CONFIG_TOML
        assert "sk-" not in DEFAULT_CONFIG_TOML

    def test_env_is_searched_next_to_the_program(self):
        # При автозапуске через реестр рабочим каталогом становится System32,
        # поэтому одного Path.cwd() мало.
        from pathlib import Path

        from whisperfree.config import env_locations

        places = env_locations()
        assert all(p.name == ".env" for p in places)
        assert len(places) == len(set(places))  # без дублей

        project_root = Path(__file__).resolve().parent.parent
        assert project_root / ".env" in places

    def test_app_dir_is_the_last_resort(self):
        from whisperfree.config import app_dir, env_locations

        assert env_locations()[-1] == app_dir() / ".env"


class TestLanguagePrompts:
    def test_prompt_switches_with_language(self):
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert cfg.language.prompt_for("ru") == cfg.language.prompt_ru
        assert cfg.language.prompt_for("en") == cfg.language.prompt_en

    def test_unknown_language_falls_back_to_russian(self):
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert cfg.language.prompt_for("de") == cfg.language.prompt_ru


class TestDiagnostics:
    def test_describe_names_the_microphone_and_key_state(self, monkeypatch):
        from whisperfree.config import describe

        cfg = parse(DEFAULT_CONFIG_TOML)
        cfg.audio.device = "Logitech StreamCam"
        monkeypatch.setenv("GROQ_API_KEY", "gsk_x")

        line = describe(cfg)
        assert "Logitech StreamCam" in line
        assert "ctrl_r" in line
        assert "whisper-large-v3-turbo" in line
        assert "ключ=есть" in line

    def test_describe_says_default_microphone_explicitly(self):
        from whisperfree.config import describe

        cfg = parse(DEFAULT_CONFIG_TOML)
        assert "системный по умолчанию" in describe(cfg)

    def test_describe_flags_a_missing_key(self, monkeypatch):
        from whisperfree.config import describe

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert "ключ=НЕТ" in describe(parse(DEFAULT_CONFIG_TOML))

    def test_broken_config_is_reported_not_swallowed(self, tmp_path, monkeypatch, capsys):
        """Тихий откат на умолчания выглядит как «настройки игнорируются»
        и не оставляет следов — именно на этом легко потерять час.

        Проверяем stderr, а не лог: load_config вызывается раньше
        setup_logging, обработчиков на этот момент ещё нет, и запись
        только в лог никуда бы не попала.
        """
        path = tmp_path / "config.toml"
        path.write_text("[audio\nэто не toml", encoding="utf-8")
        monkeypatch.setattr("whisperfree.config.app_dir", lambda: tmp_path)

        cfg = load_config(path)

        assert cfg.audio.device == ""
        assert "не прочитан" in capsys.readouterr().err

    def test_bom_in_config_is_reported(self, tmp_path, monkeypatch, capsys):
        """Файл, сохранённый Блокнотом, получает BOM, разбор TOML на нём
        падает, и настройки молча перестают действовать.

        Снаружи это выглядит так, будто программа игнорирует конфиг:
        путь печатается правильный, ошибок не видно, а device пустой.
        """
        path = tmp_path / "config.toml"
        path.write_bytes('﻿[audio]\ndevice = "StreamCam"\n'.encode("utf-8"))
        monkeypatch.setattr("whisperfree.config.app_dir", lambda: tmp_path)

        cfg = load_config(path)

        assert cfg.audio.device == ""  # настройка из файла не применилась
        assert "не прочитан" in capsys.readouterr().err


class TestSilenceThreshold:
    def test_threshold_is_above_a_real_microphone_noise_floor(self):
        # У живого микрофона фон никогда не нулевой: StreamCam в тихой комнате
        # даёт около 0.010. Нулевой порог пропускал бы тишину к провайдеру,
        # а тот на ней выдумывает титры из роликов.
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert cfg.audio.silence_peak > 0.010

    def test_threshold_is_below_normal_speech(self):
        # Обычная речь даёт 0.1–0.7, порог не должен её отсекать.
        cfg = parse(DEFAULT_CONFIG_TOML)
        assert cfg.audio.silence_peak < 0.1

    def test_threshold_is_configurable(self):
        cfg = parse("[audio]\nsilence_peak = 0.005\n")
        assert cfg.audio.silence_peak == 0.005


class TestPortableConfig:
    """config.toml рядом с программой побеждает %APPDATA%.

    Это не удобство, а защита: приложения из Store и MSIX-контейнеры видят
    подменённый %APPDATA%, печатая при этом тот же самый путь. Правка конфига
    одним процессом тогда не доходит до другого, а выглядит это как
    «программа игнорирует настройки». На поиск такой причины уходит час.
    """

    def test_file_next_to_the_program_wins(self, tmp_path, monkeypatch):
        from whisperfree.config import app_dir, config_path

        monkeypatch.setattr("whisperfree.config.program_dir", lambda: tmp_path)
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        (tmp_path / "config.toml").write_text('[audio]\ndevice = "X"\n', encoding="utf-8")

        assert app_dir() == tmp_path
        assert config_path() == tmp_path / "config.toml"

    def test_appdata_is_used_when_there_is_no_local_file(self, tmp_path, monkeypatch):
        from whisperfree.config import app_dir

        monkeypatch.setattr("whisperfree.config.program_dir", lambda: tmp_path)
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))

        assert app_dir() == tmp_path / "roaming" / "WhisperFree"

    def test_history_and_logs_follow_the_config(self, tmp_path, monkeypatch):
        from whisperfree.config import history_path, log_path

        monkeypatch.setattr("whisperfree.config.program_dir", lambda: tmp_path)
        (tmp_path / "config.toml").write_text("[audio]\n", encoding="utf-8")

        assert history_path().parent == tmp_path
        assert log_path().parent == tmp_path / "logs"

    def test_a_directory_named_config_toml_does_not_count(self, tmp_path, monkeypatch):
        from whisperfree.config import app_dir

        monkeypatch.setattr("whisperfree.config.program_dir", lambda: tmp_path)
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        (tmp_path / "config.toml").mkdir()

        assert app_dir() == tmp_path / "roaming" / "WhisperFree"


class TestTemplateIsRaw:
    """Шаблон конфига содержит регулярки, и обратные слеши в нём принадлежат
    им, а не Python.

    Тест появился после того, как обычная (не raw) строка превратила \b в
    управляющий символ, и tomlkit отказался читать собственный шаблон
    программы — то есть конфиг молча откатывался на умолчания.
    """

    def test_no_control_characters(self):
        bad = [(i, ch) for i, ch in enumerate(DEFAULT_CONFIG_TOML) if ord(ch) < 0x20 and ch != "\n"]
        assert not bad, f"управляющий символ в шаблоне: {bad[:3]}"

    def test_template_parses_with_tomlkit(self):
        tomlkit.parse(DEFAULT_CONFIG_TOML)

    def test_regex_keys_survived_intact(self):
        cfg = parse(DEFAULT_CONFIG_TOML)
        keys = list(cfg.postprocess.replacements)
        assert any(k.startswith(r"re:\b") for k in keys), keys


class TestTermReplacements:
    """Термины должны переживать и дефис, и падежи.

    Правка моделью пишет «пул-реквест» через дефис, а на слух это два слова —
    ключ с пробелом мимо такого промахивается.
    """

    def build(self):
        from whisperfree.postprocess import Postprocessor

        return Postprocessor(parse(DEFAULT_CONFIG_TOML).postprocess)

    @pytest.mark.parametrize(
        "said, expected",
        [
            ("почему пул реквест не проходит", "почему pull request не проходит"),
            ("почему пул-реквест не проходит", "почему pull request не проходит"),
            ("откатить коммит", "откатить commit"),
            ("сделать деплой на прод", "сделать deploy на прод"),
            ("в докере всё работает", "в Docker всё работает"),
            ("гит-хаб лежит", "GitHub лежит"),
            ("гитхаб лежит", "GitHub лежит"),
            ("запусти клод код", "запусти Claude Code"),
            ("проверь через джемини", "проверь через Gemini"),
        ],
    )
    def test_terms_become_latin(self, said, expected):
        assert self.build().process(said).rstrip() == expected

    @pytest.mark.parametrize("text", ["кодировка не меняется", "покером не увлекаюсь"])
    def test_similar_words_are_left_alone(self, text):
        assert self.build().process(text).rstrip() == text


class TestLegacyMigration:
    r"""Программа звалась VoiceFlow и хранила данные в %APPDATA%\VoiceFlow.

    Переименование не должно выглядеть как потеря настроек: пользователь
    просто обновился и вправе увидеть свой конфиг и свою историю на месте.
    """

    @pytest.fixture
    def dirs(self, tmp_path, monkeypatch):
        old = tmp_path / "VoiceFlow"
        new = tmp_path / "WhisperFree"
        old.mkdir()
        new.mkdir()
        monkeypatch.setattr(config_mod, "legacy_app_dir", lambda: old)
        monkeypatch.setattr(config_mod, "app_dir", lambda: new)
        return old, new

    def test_config_and_history_are_carried_over(self, dirs):
        old, new = dirs
        (old / "config.toml").write_text('[audio]\ndevice = "старый"\n', encoding="utf-8")
        (old / "history.jsonl").write_text('{"text": "привет"}\n', encoding="utf-8")

        assert sorted(config_mod.migrate_legacy_data()) == ["config.toml", "history.jsonl"]
        assert "старый" in (new / "config.toml").read_text(encoding="utf-8")
        assert "привет" in (new / "history.jsonl").read_text(encoding="utf-8")

    def test_existing_files_are_left_alone(self, dirs):
        # Иначе обновление затёрло бы свежие настройки прошлогодними.
        old, new = dirs
        (old / "config.toml").write_text("старое\n", encoding="utf-8")
        (new / "config.toml").write_text("новое\n", encoding="utf-8")

        assert config_mod.migrate_legacy_data() == []
        assert (new / "config.toml").read_text(encoding="utf-8") == "новое\n"

    def test_old_directory_survives(self, dirs):
        # Копируем, а не переносим: откатиться можно в любой момент.
        old, new = dirs
        (old / "config.toml").write_text("данные\n", encoding="utf-8")

        config_mod.migrate_legacy_data()
        assert (old / "config.toml").is_file()

    def test_nothing_to_migrate_is_not_an_error(self, dirs):
        assert config_mod.migrate_legacy_data() == []

    def test_no_legacy_directory_at_all(self, monkeypatch):
        monkeypatch.setattr(config_mod, "legacy_app_dir", lambda: None)
        assert config_mod.migrate_legacy_data() == []

    def test_portable_mode_does_not_copy_onto_itself(self, tmp_path, monkeypatch):
        # В переносимом режиме каталоги могут совпасть — копирование файла
        # в самого себя обрушило бы запуск.
        same = tmp_path / "рядом"
        same.mkdir()
        (same / "config.toml").write_text("данные\n", encoding="utf-8")
        monkeypatch.setattr(config_mod, "legacy_app_dir", lambda: same)
        monkeypatch.setattr(config_mod, "app_dir", lambda: same)

        assert config_mod.migrate_legacy_data() == []

    def test_unreadable_source_does_not_stop_startup(self, dirs, monkeypatch):
        old, new = dirs
        (old / "config.toml").write_text("данные\n", encoding="utf-8")
        (old / "history.jsonl").write_text("данные\n", encoding="utf-8")

        def refuse(source, target):
            if source.name == "config.toml":
                raise OSError("нет доступа")
            return target

        monkeypatch.setattr(config_mod.shutil, "copy2", refuse)
        assert config_mod.migrate_legacy_data() == ["history.jsonl"]

    def test_legacy_dir_is_none_without_appdata(self, monkeypatch):
        monkeypatch.delenv("APPDATA", raising=False)
        assert config_mod.legacy_app_dir() is None


class TestShippedExample:
    """config.example.toml обязан совпадать с шаблоном в коде.

    Файл в репозитории — единственное, что видит человек до первого запуска.
    Он уже отставал от шаблона на три ключа: настройки были, а в примере их
    не было, и найти их можно было только чтением исходников.
    """

    def test_example_matches_the_template(self):
        from pathlib import Path

        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        assert example.is_file(), "config.example.toml пропал из репозитория"
        assert example.read_text(encoding="utf-8") == DEFAULT_CONFIG_TOML

    def test_example_parses(self):
        from pathlib import Path

        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        cfg = parse(example.read_text(encoding="utf-8"))
        assert cfg.provider.model == "whisper-large-v3-turbo"

    def test_example_carries_no_key(self):
        from pathlib import Path

        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        text = example.read_text(encoding="utf-8")
        assert "gsk_" not in text
        assert "sk-" not in text

    def test_example_keeps_unix_line_endings(self):
        # .gitattributes объявляет для .toml перевод строки LF. Файл с CRLF
        # на диске означал бы, что git при каждом клоне видит правку.
        from pathlib import Path

        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        raw = example.read_bytes()
        assert b"\r\n" not in raw


class TestPersonalFilesStayOutOfTheRepository:
    """Всё, что программа пишет о своём хозяине, обязано быть в .gitignore.

    В переносимом режиме эти файлы ложатся не в %APPDATA%, а прямо рядом с
    программой — то есть внутрь рабочей копии. Репозиторий публичный, и один
    незакрытый файл выкладывает наружу личное: history.jsonl хранит все
    диктовки целиком, lexicon.json — слова, которые человек говорил и правил,
    logs при --debug тоже пишут текст диктовок и абсолютные пути.

    Проверка структурная нарочно: она поймёт и следующий такой файл, если он
    появится, а не только те три, что есть сейчас.
    """

    @pytest.fixture
    def ignored(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        return {line.strip() for line in text.splitlines() if line.strip()}

    @pytest.mark.parametrize(
        "helper",
        ["config_path", "history_path", "lexicon_path", "log_path", "audio_cache_dir"],
    )
    def test_every_written_file_is_ignored(self, helper, ignored, monkeypatch, tmp_path):
        monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
        path = getattr(config_mod, helper)()
        # Каталог закрывают записью со слешем, файл — своим именем.
        assert (
            path.name in ignored
            or f"{path.name}/" in ignored
            or path.parent.name + "/" in ignored
        ), f"{path.name} пишется программой, но не закрыт в .gitignore"

    def test_the_key_file_is_ignored(self, ignored):
        assert ".env" in ignored
        assert "!.env.example" in ignored, "пример .env должен остаться в репозитории"
