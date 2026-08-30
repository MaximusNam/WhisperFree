"""Точка входа WhisperFree.

Раскладка по потокам:

* главный поток   — цикл Tk (оверлей и окно истории);
* поток pynput    — хук клавиатуры, обработчики удержания выполняются в нём
                    и обязаны быть мгновенными;
* рабочий поток   — кодирование, запрос к провайдеру, вставка;
* поток pystray   — значок в трее;
* поток PortAudio — колбэк микрофона.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

from . import audio as audio_mod
from . import autostart, config as config_mod, inject
from .history import AudioCache, History, Record
from .history_window import HistoryWindow
from .hotkey import HotkeyManager
from .logutil import Stopwatch, setup_logging
from .overlay import Overlay
from .postprocess import Postprocessor
from .providers import TranscriptionError, TranscriptionRequest, build_provider
from .limits import report as report_limits
from .refine import Refiner
from .singleton import SingleInstance
from .tray import Tray

log = logging.getLogger(__name__)

# Запасное значение порога тишины, если в конфиге его нет.
# Отправлять тишину бессмысленно и вредно: Whisper на ней уверенно выдумывает
# титры из роликов, на которых учился.
DEFAULT_SILENCE_PEAK = 0.02


@dataclass
class Job:
    """Одна диктовка, ждущая обработки."""

    capture: audio_mod.Capture
    lang: str
    started_at: float


class App:
    def __init__(self, cfg: config_mod.Config, root: tk.Tk | None = None) -> None:
        self.cfg = cfg
        self.paused = False
        self._recording = False
        self._state_lock = threading.Lock()
        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._stopping = threading.Event()

        self.history = History(config_mod.history_path(), cfg.history)
        self.audio_cache = AudioCache(
            config_mod.audio_cache_dir(),
            cfg.history.keep_audio_count if cfg.history.keep_audio else 0,
        )
        self.postprocessor = Postprocessor(cfg.postprocess)
        self.refiner = Refiner(cfg.refine, cfg.provider.base_url, cfg.provider.api_key)
        self.injector = inject.Injector(cfg.inject)
        self.provider = build_provider(cfg.provider)
        self.recorder = audio_mod.Recorder(
            sample_rate=cfg.audio.sample_rate,
            preroll_ms=cfg.audio.preroll_ms,
            device=cfg.audio.device,
            max_seconds=cfg.audio.max_seconds,
            hold_open=cfg.audio.hold_open,
        )
        self.hotkeys = HotkeyManager(suppress=cfg.hotkeys.suppress)

        # Корень можно передать снаружи: Tk плохо переносит несколько корней в
        # одном процессе, и тестам нужен общий.
        self.root = root or tk.Tk()
        self.root.withdraw()
        self.overlay = Overlay(self.root, enabled=cfg.ui.overlay)
        self.history_window = HistoryWindow(
            self.root, self.history, self._paste_record, self._copy_record
        )
        self.tray: Tray | None = None
        self._worker: threading.Thread | None = None

    # --- запуск и остановка ----------------------------------------------------

    def run(self) -> int:
        # Первой же строкой пишем, что программа реально собирается делать.
        # Вопрос «почему берётся не тот микрофон» должен решаться чтением лога,
        # а не расследованием.
        log.info("настройки: %s", config_mod.describe(self.cfg))
        try:
            # Микрофон открываем и при hold_open=false тоже: неверное имя
            # устройства должно всплыть сразу, а не при первой диктовке.
            self.recorder.open()
            if not self.cfg.audio.hold_open:
                self.recorder.close()
                log.info("микрофон освобождён до первой диктовки (hold_open=false)")
        except audio_mod.AudioError as exc:
            log.error("%s", exc)
            self.overlay.error(str(exc))

        self._worker = threading.Thread(target=self._work, name="transcribe", daemon=True)
        self._worker.start()

        self._register_hotkeys()
        self.hotkeys.start()

        if self.cfg.ui.tray:
            self.tray = Tray(
                history=self.history,
                provider_cfg=self.cfg.provider,
                refine_cfg=self.cfg.refine,
                on_toggle_pause=self.toggle_pause,
                is_paused=lambda: self.paused,
                on_paste_record=self._paste_record,
                on_open_history=self.history_window.open,
                on_quit=self.quit,
                config_path=config_mod.config_path(),
                log_path=config_mod.log_path(),
            )
            self.tray.start()

        if self.cfg.ui.autostart and not autostart.is_enabled():
            autostart.enable()

        log.info(
            "WhisperFree готов. Диктовка: %s (%s), альтернативный язык: %s (%s), "
            "повторная вставка: %s",
            self.cfg.hotkeys.dictate,
            self.cfg.language.main,
            self.cfg.hotkeys.dictate_alt or "выключено",
            self.cfg.language.alt,
            self.cfg.hotkeys.paste_last,
        )
        if not self.cfg.provider.api_key:
            message = (
                f"нет ключа {self.cfg.provider.api_key_env} — "
                "положите его в .env рядом с программой"
            )
            log.error("%s", message)
            self.overlay.error(message)

        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    def quit(self) -> None:
        """Можно звать из любого потока."""
        self.root.after(0, self.root.quit)

    def _shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        log.info("завершаю работу")
        self.hotkeys.stop()
        self._jobs.put(None)
        if self.tray is not None:
            self.tray.stop()
        self.recorder.close()
        for closeable in (self.provider, self.refiner):
            try:
                closeable.close()
            except Exception:  # pragma: no cover
                pass

    # --- горячие клавиши -------------------------------------------------------

    def _warn_about_risky_keys(self) -> None:
        """Правый Shift как клавиша диктовки делает жизнь невыносимой.

        Он нужен для заглавных букв, поэтому каждая заглавная запускала бы
        запись. Конфиг мог остаться от старой версии, где такой умолчание было,
        поэтому предупреждаем явно, а не молча делаем странное.
        """
        cfg = self.cfg.hotkeys
        for field, value in (("dictate", cfg.dictate), ("dictate_alt", cfg.dictate_alt)):
            if value and "shift" in value.lower():
                message = (
                    f"[hotkeys].{field} = {value!r}: Shift нужен для заглавных букв, "
                    "каждая заглавная будет запускать запись. Возьмите scroll_lock или f13"
                )
                log.warning("%s", message)
                self.overlay.error(f"{field}: Shift — плохая клавиша для диктовки")

    def _register_hotkeys(self) -> None:
        self._warn_about_risky_keys()
        cfg = self.cfg.hotkeys
        self.hotkeys.register_hold(
            cfg.dictate,
            lambda: self._start_dictation(self.cfg.language.main),
            self._stop_dictation,
        )
        if cfg.dictate_alt and cfg.dictate_alt != cfg.dictate:
            self.hotkeys.register_hold(
                cfg.dictate_alt,
                lambda: self._start_dictation(self.cfg.language.alt),
                self._stop_dictation,
            )
        self.hotkeys.register_combo(cfg.paste_last, self._paste_last)
        if cfg.open_history:
            self.hotkeys.register_combo(cfg.open_history, self.history_window.open)

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        log.info("пауза: %s", "включена" if self.paused else "выключена")
        if self.paused:
            with self._state_lock:
                if self._recording:
                    self._recording = False
                    self.recorder.cancel()
            self.overlay.hide()
        if self.tray is not None:
            self.tray.refresh()

    # --- диктовка --------------------------------------------------------------

    def _start_dictation(self, lang: str) -> None:
        """Выполняется в потоке хука — только мгновенные операции."""
        if self.paused:
            return
        with self._state_lock:
            if self._recording:
                return
            # При hold_open=false закрытый поток — это норма, его откроет begin().
            if self.cfg.audio.hold_open and not self.recorder.is_open:
                # Микрофон могли переподключить, пока приложение работало.
                threading.Thread(target=self._try_reopen, daemon=True).start()
                return
            self._recording = True
            self._lang = lang
            self._press_at = time.monotonic()
            self.recorder.begin()
        self.overlay.recording()
        if self.tray is not None:
            self.tray.set_state(True)

    def _stop_dictation(self) -> None:
        """Тоже в потоке хука: забрать буфер и отдать рабочему потоку."""
        with self._state_lock:
            if not self._recording:
                return
            self._recording = False
            capture = self.recorder.end()
            lang = getattr(self, "_lang", self.cfg.language.main)
            started = getattr(self, "_press_at", time.monotonic())

        if self.tray is not None:
            self.tray.set_state(False)

        # Проверять ДО отсечки по длине. Мёртвый поток отдаёт один пре-ролл,
        # то есть заведомо меньше минимума, и отсечка уносила единственное
        # объяснение в отладочную строку: человек видел, что программа молчит,
        # и не мог понять почему.
        if capture.stalled:
            held = time.monotonic() - started
            log.error(
                "микрофон перестал отдавать звук: клавишу держали %.1f с, "
                "записано %.2f с. Переоткрываю поток.",
                held, capture.duration_s,
            )
            self.overlay.error("микрофон замолчал — переоткрываю, повторите")
            threading.Thread(target=self._try_reopen, daemon=True).start()
            return

        if capture.duration_s < self.cfg.audio.min_seconds:
            log.debug("запись %.2f с короче порога — игнорирую", capture.duration_s)
            self.overlay.hide()
            return

        peak = audio_mod.peak_level(capture)
        if peak < self.cfg.audio.silence_peak:
            # Уровень в сообщении обязателен: без него непонятно, микрофон
            # молчит совсем или просто не дотянул до порога.
            log.info(
                "в записи тишина (пик %.3f при пороге %.3f), ничего не отправляю",
                peak, self.cfg.audio.silence_peak,
            )
            self.overlay.error(f"тишина: уровень {peak:.3f} ниже порога {self.cfg.audio.silence_peak:.3f}")
            return
        log.debug("пиковый уровень записи %.3f", peak)

        if capture.truncated:
            log.warning("запись обрезана по лимиту %d с", self.cfg.audio.max_seconds)

        self.overlay.sending()
        self._jobs.put(Job(capture=capture, lang=lang, started_at=started))

    def _try_reopen(self) -> None:
        try:
            self.recorder.reopen()
            log.info("микрофон переоткрыт")
        except audio_mod.AudioError as exc:
            self.overlay.error(str(exc))

    # --- рабочий поток ---------------------------------------------------------

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._process(job)
            except Exception:
                log.exception("непредвиденная ошибка при обработке диктовки")
                self.overlay.error("внутренняя ошибка, подробности в логе")

    def _process(self, job: Job) -> None:
        watch = Stopwatch()
        self._check_for_dropped_audio(job)

        capture = job.capture
        if self.cfg.audio.normalize:
            capture, gain = audio_mod.normalize(capture)
            if gain > 1.05:
                log.debug("громкость поднята в %.1f раза", gain)

        data, filename = audio_mod.encode(capture, self.cfg.audio.encode)
        watch.mark("encode")

        audio_file = self.audio_cache.save(data, filename)

        record = Record(
            ts=time.time(),
            text="",
            lang=job.lang,
            provider=self.provider.name,
            model=self.provider.model,
            audio_sec=round(job.capture.duration_s, 2),
            audio_file=audio_file,
        )

        try:
            raw = self.provider.transcribe(
                TranscriptionRequest(
                    audio=data,
                    filename=filename,
                    language=job.lang,
                    prompt=self.cfg.language.prompt_for(job.lang),
                    duration_s=job.capture.duration_s,
                )
            )
        except TranscriptionError as exc:
            watch.mark("api")
            record.error = str(exc)
            self.history.add(record)
            self._refresh_tray()
            log.error("распознавание не удалось: %s (%s)", exc, watch.summary())
            self.overlay.error(str(exc))
            return
        watch.mark("api")

        cleaned = self.postprocessor.clean(raw)
        watch.mark("post")
        if not cleaned:
            log.info("после обработки текста не осталось, вставлять нечего")
            self.overlay.hide()
            return

        # Правка моделью идёт ПЕРЕД словарём замен: иначе она перепишет
        # термины по-своему и последнее слово останется за ней, а не за
        # тем, что пользователь задал в конфиге.
        refined = cleaned
        if self.refiner.enabled:
            self.overlay.refining()
            refined = self.refiner.refine(cleaned)
            record.refine_in, record.refine_out = getattr(
                self.refiner, "last_usage", (0, 0)
            )
            watch.mark("refine")
            if refined != cleaned:
                log.debug("правка: %r -> %r", cleaned[:60], refined[:60])

        text = self.postprocessor.finish(refined)
        if not text:
            self.overlay.hide()
            return

        record.raw = cleaned if refined != cleaned else ""
        record.text = text
        record.target_exe = inject.foreground_exe()

        # В историю пишем ДО вставки: если приложение упадёт на вставке,
        # продиктованное всё равно не потеряется.
        self.history.add(record)

        ok, reason = self.injector.paste(text, target_exe=record.target_exe)
        watch.mark("paste")

        if ok:
            self.overlay.ok(text)
        else:
            record.error = reason
            self.overlay.error(f"{reason} · {self.cfg.hotkeys.paste_last} — вставить снова")
            log.warning("вставка не прошла: %s", reason)

        self._refresh_tray()
        log.info(
            "готово: %.1f с звука -> %d симв., %s, окно=%s",
            job.capture.duration_s,
            len(text),
            watch.summary(),
            record.target_exe or "?",
        )

    def _check_for_dropped_audio(self, job: Job) -> None:
        """Сравнивает время удержания клавиши с длиной записи.

        Если звука пришло заметно меньше, чем клавишу держали, значит колбэк
        микрофона не успевал и блоки терялись. Снаружи это выглядит как
        «половину фразы не расслышало», и без этой строки в логе причину
        искать негде.
        """
        if not job.started_at:
            return
        held = time.monotonic() - job.started_at
        gap = held - job.capture.duration_s
        if gap > 0.3:
            log.warning(
                "потеряно ~%.1f с звука: клавишу держали %.1f с, записано %.1f с",
                gap, held, job.capture.duration_s,
            )

    def _refresh_tray(self) -> None:
        if self.tray is not None:
            self.tray.refresh()

    # --- повторная вставка -----------------------------------------------------

    def _paste_last(self) -> None:
        """Хоткей повторной вставки. Нажатия подряд идут вглубь истории."""
        record = self.history.next_for_paste()
        if record is None:
            self.overlay.error("история пуста")
            return
        self._paste_record(record, reset_cycle=False)

    def _paste_record(self, record: Record, reset_cycle: bool = True) -> None:
        if not record.text:
            self.overlay.error("в этой записи нет текста")
            return
        if reset_cycle:
            self.history.reset_cycle()
        ok, reason = self.injector.paste(record.text)
        if ok:
            self.overlay.ok(record.text)
        else:
            self.overlay.error(reason)

    def _copy_record(self, record: Record) -> None:
        if record.text and self.injector.put_in_clipboard(record.text):
            self.overlay.ok("скопировано в буфер")
        else:
            self.overlay.error("не удалось положить в буфер")


# --- вспомогательные режимы ----------------------------------------------------


def cmd_devices() -> int:
    devices = audio_mod.list_input_devices()
    if not devices:
        print("Микрофоны не найдены.")
        return 1
    print("Доступные микрофоны (индекс — имя):")
    for index, name in devices:
        print(f"  {index:3d}  {name}")
    print('\nВпишите индекс или имя в [audio].device в конфиге.')
    return 0


def cmd_check(cfg: config_mod.Config) -> int:
    """Быстрая проверка: конфиг, микрофон, провайдер."""
    print(f"Конфиг:    {config_mod.config_path()}")
    print(f"История:   {config_mod.history_path()}")
    print(f"Логи:      {config_mod.log_path()}")
    print(f"Провайдер: {cfg.provider.base_url}  модель {cfg.provider.model}")

    key = cfg.provider.api_key
    print(f"Ключ {cfg.provider.api_key_env}: {'найден' if key else 'НЕ НАЙДЕН'}")

    print(f"Микрофон:  {cfg.audio.device or 'системный по умолчанию'}")
    recorder = audio_mod.Recorder(
        sample_rate=cfg.audio.sample_rate,
        preroll_ms=cfg.audio.preroll_ms,
        device=cfg.audio.device,
    )
    try:
        recorder.open()
    except audio_mod.AudioError as exc:
        print(f"Микрофон:  ОШИБКА — {exc}")
        return 1

    print("Микрофон:  открыт. Говорите — слушаю 3 секунды…")
    recorder.begin()
    time.sleep(3.0)
    capture = recorder.end()
    recorder.close()
    peak = audio_mod.peak_level(capture)
    print(
        f"           записано {capture.duration_s:.1f} с, "
        f"пиковый уровень {peak:.3f} (порог тишины {cfg.audio.silence_peak:.3f})"
    )
    if peak < cfg.audio.silence_peak:
        print("           НИЖЕ ПОРОГА — такая запись отправлена не будет.")
        print("           Если вы говорили: уменьшите [audio].silence_peak или")
        print("           прибавьте громкость микрофона в параметрах звука Windows.")
        print("           Если не говорили: так и должно быть.")
    else:
        print("           уровень достаточный, речь будет распознаваться")

    if not key:
        print("\nБез ключа запрос к провайдеру не проверить.")
        return 1

    to_send = capture
    if cfg.audio.normalize:
        to_send, gain = audio_mod.normalize(capture)
        if gain > 1.05:
            print(f"           громкость поднята в {gain:.1f} раза перед отправкой")

    data, filename = audio_mod.encode(to_send, cfg.audio.encode)
    print(f"           {filename}: {len(data) / 1024:.1f} КБ")
    provider = build_provider(cfg.provider)
    watch = Stopwatch()
    try:
        text = provider.transcribe(
            TranscriptionRequest(
                audio=data,
                filename=filename,
                language=cfg.language.main,
                prompt=cfg.language.prompt_for(cfg.language.main),
                duration_s=capture.duration_s,
            )
        )
    except TranscriptionError as exc:
        print(f"Провайдер: ОШИБКА — {exc}")
        return 1
    finally:
        provider.close()

    print(f"Провайдер: ответил за {watch.total * 1000:.0f} мс")
    print(f"Распознано: {text!r}")
    print(f"После обработки: {Postprocessor(cfg.postprocess).process(text)!r}")
    return 0


def _force_utf8_output() -> None:
    """Кириллица в консоли.

    В настоящей консоли Windows Python пишет через юникодный API и всё хорошо,
    но при запуске из Git Bash или с перенаправлением он берёт системную
    кодировку, и русский текст превращается в кашу.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


def cmd_calibrate(cfg: config_mod.Config, seconds: float = 3.0) -> int:
    """Измеряет фон микрофона и подбирает порог тишины.

    Порог нельзя задать раз и навсегда: он зависит от усиления конкретного
    микрофона. Стоит прибавить громкость в Windows — и фон подскакивает
    вместе с речью. Живой случай: после прибавления громкости фон вырос с
    0.006 до 0.035, прежний порог 0.012 перестал его отсекать, и провайдер
    начал выдумывать текст на пустых записях.
    """
    print(f"Микрофон: {cfg.audio.device or 'системный по умолчанию'}")
    print(f"Текущий порог тишины: {cfg.audio.silence_peak:.3f}")
    print()
    print(f"Сейчас измерю фон. МОЛЧИТЕ {seconds:.0f} секунды…")

    recorder = audio_mod.Recorder(
        sample_rate=cfg.audio.sample_rate,
        preroll_ms=cfg.audio.preroll_ms,
        device=cfg.audio.device,
    )
    try:
        recorder.open()
    except audio_mod.AudioError as exc:
        print(f"ОШИБКА: {exc}")
        return 1

    recorder.begin()
    time.sleep(seconds)
    capture = recorder.end()
    recorder.close()

    noise = audio_mod.peak_level(capture)
    # Втрое выше фона: речь обычно на порядок громче, запас лишним не будет.
    threshold = min(0.30, max(0.010, round(noise * 3, 3)))

    print(f"           фон = {noise:.3f}")
    print(f"           рекомендуемый порог = {threshold:.3f}")

    if noise > 0.15:
        print()
        print("           Фон очень высокий. Возможно, стоит убавить усиление")
        print("           микрофона в параметрах звука Windows (sound.bat).")

    path = config_mod.config_path()
    try:
        import tomlkit

        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        doc.setdefault("audio", tomlkit.table())["silence_peak"] = threshold
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    except Exception as exc:
        print(f"\nНе удалось записать в конфиг ({exc}).")
        print(f"Впишите вручную в {path}:  [audio] silence_peak = {threshold}")
        return 1

    print()
    print(f"Записано в {path}")
    print("Проверьте: check.bat, говорите — уровень должен быть заметно выше порога.")
    print("Если жалуется на тишину, когда вы говорите, — уменьшите silence_peak.")
    return 0


def cmd_paste_test(cfg: config_mod.Config, delay: float = 5.0) -> int:
    """Проверка вставки без микрофона и без сети.

    Нужна для обхода по приложениям из плана: блокнот, адресная строка,
    терминал, Electron, Word. Механика вставки везде разная, и единственный
    честный способ проверить — вставить в каждое.
    """
    sample = "Проверка WhisperFree: поставь Docker и проверь через Gemini — раз, два, три."
    print("Переключитесь в окно, куда нужно вставить текст.")
    for remaining in range(int(delay), 0, -1):
        print(f"  {remaining}…", end="\r", flush=True)
        time.sleep(1.0)

    exe = inject.foreground_exe()
    combo = inject.paste_key_for(exe, cfg.inject.default_paste, cfg.inject.paste_overrides)
    watch = Stopwatch()
    ok, reason = inject.Injector(cfg.inject).paste(sample)

    print(f"Окно:      {exe or 'не определено'}")
    print(f"Клавиша:   {combo}")
    print(f"Результат: {'вставлено' if ok else 'НЕ вставлено — ' + reason}")
    print(f"Время:     {watch.total * 1000:.0f} мс")
    if not ok:
        print("\nТекст остался в буфере обмена — нажмите Ctrl+V руками.")
    elif combo == cfg.inject.default_paste and exe:
        print(
            "\nЕсли в этом окне текст не появился, добавьте его в конфиг:\n"
            f'  [inject.paste_overrides]\n  "{exe}" = "ctrl+shift+v"'
        )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="whisperfree", description="Голосовой ввод в любое окно Windows"
    )
    parser.add_argument("--config", type=Path, help="путь к config.toml")
    parser.add_argument("--cwd", type=Path, help="рабочий каталог (нужен для .env при автозапуске)")
    parser.add_argument("--devices", action="store_true", help="показать список микрофонов")
    parser.add_argument("--check", action="store_true", help="проверить микрофон и провайдера")
    parser.add_argument(
        "--calibrate",
        nargs="?",
        const=3.0,
        type=float,
        metavar="СЕК",
        help="измерить фон микрофона и подобрать порог тишины",
    )
    parser.add_argument(
        "--paste-test",
        nargs="?",
        const=5.0,
        type=float,
        metavar="СЕК",
        help="вставить тестовую фразу в активное окно через N секунд",
    )
    parser.add_argument(
        "--limits", action="store_true", help="остаток бесплатных лимитов провайдера"
    )
    parser.add_argument("--debug", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as exc:
            print(f"не удалось перейти в {args.cwd}: {exc}", file=sys.stderr)

    # Перенос до чтения конфига: иначе первый запуск после переименования
    # создал бы пустой конфиг и настройки выглядели бы потерянными.
    moved = config_mod.migrate_legacy_data()

    cfg = config_mod.load_config(args.config)
    setup_logging(
        config_mod.log_path(),
        level=logging.DEBUG if args.debug else logging.INFO,
        console=sys.stderr is not None,
    )
    if moved:
        # Логгер до этой строки ещё не настроен, поэтому сообщаем здесь.
        log.info("перенесено из каталога прежней версии: %s", ", ".join(moved))

    if args.devices:
        return cmd_devices()
    if args.check:
        return cmd_check(cfg)
    if args.calibrate is not None:
        return cmd_calibrate(cfg, args.calibrate)
    if args.limits:
        return report_limits(cfg)
    if args.paste_test is not None:
        return cmd_paste_test(cfg, args.paste_test)

    # Два работающих экземпляра вешают хук на одну клавишу, и на отпускание
    # срабатывают оба: текст вставляется дважды, а микрофон держат обе копии.
    guard = SingleInstance()
    if not guard.acquire():
        message = (
            "WhisperFree уже запущен — значок есть в трее. "
            "Второй экземпляр вставлял бы текст дважды."
        )
        log.error("%s", message)
        print(message, file=sys.stderr)
        return 1
    try:
        return App(cfg).run()
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
