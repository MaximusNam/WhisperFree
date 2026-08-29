# WhisperFree

Voice typing for Windows: hold a key, talk, release — the text appears where the
caret was.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)
[![tests](https://github.com/MaximusNam/WhisperFree/actions/workflows/tests.yml/badge.svg)](https://github.com/MaximusNam/WhisperFree/actions/workflows/tests.yml)

Русская версия: [README.md](README.md)

---

## What it looks like

```
hold Right Ctrl  →  a "listening" bar shows up at the bottom of the screen
speak            →  "put docker on the server and restart the container"
release          →  Put Docker on the server and restart the container.
```

Works in any window: browser, messenger, Word, a terminal running Claude Code.

The interesting case is dictating in your own language with English technical
terms mixed in — that is what the replacement dictionary is for, see
[Mixed-language dictation](#mixed-language-dictation).

---

## Features

- Push-to-talk on Right Ctrl — text lands where the caret was, in any Windows window.
- Transcription through Groq, model `whisper-large-v3-turbo`: 204–336 ms per request — measured on live dictations, 9.2 seconds of speech on average, August 2026.
- An editor model pass with `openai/gpt-oss-120b`: punctuation, grammar, agreement, 293–480 ms.
- A regex-backed term dictionary — every inflected form of a word maps to one canonical spelling, edited in a single file.
- 250 ms pre-roll: recording starts before the key goes down, so the first syllable is not clipped.
- The API key is read only from `.env` or an environment variable, and never written into `config.toml`.
- Transcript history: Ctrl+Alt+V re-pastes the last one, Ctrl+Alt+H opens a searchable window.
- Paste key is picked per app: terminals get Ctrl+Shift+V instead of Ctrl+V, and the clipboard is restored.
- One dictation costs $0.000201 on the paid tier — about $0.60 a month at a hundred dictations a day.

---

## Why I left Wispr Flow

I paid Wispr Flow $15 a month, and the money was the smallest of my problems.
It is a good product, but three things got in the way every day: the
subscription, maintaining the dictionary by hand, and transcription that
regularly did not understand what I said. That last one is my personal
experience, not a measured comparison — I ran no accuracy benchmarks against it
and I am not going to quote any.

The dictionary part is easier to show. Wispr Flow has a Dictionary, and you fill
it in by hand, word by word, or your terms get mangled: `Gemini` comes out as
"Джемини", `Docker` as "Докер". In WhisperFree the replacement dictionary lives
in `config.toml` and understands regular expressions: one line
`re:\bдокер\w*` = `"Docker"` covers every case ending and hyphenation at once.
The keys are plain regex, so the same trick works for whatever language you
dictate in. On top of that runs an editor-model pass — and the dictionary is
applied *after* it, so the last word on terminology belongs to your file, not to
the model.

Everything I can claim about speed and cost comes from my own measurements on
live requests, August 2026: transcription 204–336 ms, refinement 293–480 ms, an
average dictation of 9.2 seconds of speech and 104 characters of output. The
Groq free tier at that same moment gave 2000 transcription requests and 1000
chat-model requests per day — a ceiling of roughly 1000 dictations a day, and
the refinement pass is what you hit first. Providers change their limits;
`limits.bat` shows the current ones, read straight from the API response
headers.

| | WhisperFree | Wispr Flow |
|---|---|---|
| Price | the provider's free tier; on the paid tier $0.000201 per dictation | $15 a month |
| Term dictionary | `config.toml`, regex covering every inflected form, editor model on top | Dictionary, filled in by hand one word at a time |
| Where the key lives | your `.env` or an environment variable, never written to the config | no key of your own, requests go through the service |
| Open source | yes, MIT | no |

---

## Table of contents

- [What it looks like](#what-it-looks-like)
- [Features](#features)
- [Why I left Wispr Flow](#why-i-left-wispr-flow)
- [Quick start](#quick-start)
- [Keys](#keys)
- [When the paste does not land](#when-the-paste-does-not-land)
- [Mixed-language dictation](#mixed-language-dictation)
- [Model-based text refinement](#model-based-text-refinement)
- [What it costs](#what-it-costs)
- [A different provider](#a-different-provider)
- [Settings worth knowing](#settings-worth-knowing)
- [Files next to the program](#files-next-to-the-program)
- [When something is wrong](#when-something-is-wrong)
- [Building a standalone exe](#building-a-standalone-exe)
- [How it works](#how-it-works)
- [Measurements](#measurements)
- [License](#license)
- [Contributing](#contributing)

---

## Quick start

**1. Environment**

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -e .
```

**2. Provider key**

Get a key at [console.groq.com/keys](https://console.groq.com/keys) — there is a
free tier and no card is required. Copy `.env.example` to `.env` and paste the key in:

```
GROQ_API_KEY=gsk_your_key
```

The key is read only from `.env` or an environment variable. It never ends up in
`config.toml`.

**3. Language**

The config that ships here dictates Russian on Right Ctrl, because that is what
the author uses. Set your own language in `[language]`:

```toml
[language]
main = "en"
alt = "ru"
```

`main` is the language of the dictation key, and for a single-language setup it
is the only one you need. `alt` belongs to an optional second key — and that key
is not assigned in the shipped config: `[hotkeys].dictate_alt = ""`, so `alt` on
its own changes nothing. To switch the second key on, give it a key nobody else
uses:

```toml
[hotkeys]
dictate_alt = "scroll_lock"
```

Right Ctrl then dictates in `main`, Scroll Lock in `alt`. Details under
[Mixed-language dictation](#mixed-language-dictation).

The values are ISO 639-1 codes, passed straight through to the provider's
`language` parameter.

**4. Check**

Double-click **`check.bat`**. It reports whether the key was found, opens the
microphone, records three seconds, sends them to the provider and prints what
came back.

If it picked the wrong microphone, **`devices.bat`** lists them. Put the name
from that list into the config:

```toml
[audio]
device = "Logitech StreamCam"
```

A name is safer than an index: indices shift as devices are plugged in and out.
A fragment of the name is enough and case does not matter. One piece of hardware
usually shows up under four audio APIs at once (MME, DirectSound, WASAPI,
WDM-KS) — the one belonging to the default API is used.

While you are here, set the silence threshold. Below `[audio].silence_peak` no
request is sent at all, and the right value depends on the microphone and on its
volume in Windows — on the same hardware it moves once you drag the volume
slider. Do not guess it. **`calibrate.bat`** listens for three seconds while you
stay quiet, measures the noise floor, and writes the resulting threshold into
the config itself.

**5. Paste check**

Pasting works differently in different applications, so it is worth running
through every window you plan to dictate into. Run **`paste-test.bat`**: you get
five seconds to switch to the target window, a test phrase is pasted there, and
the console prints which window was detected and which key was used.

Go through Notepad, the browser address bar, a terminal running Claude Code,
Telegram or VS Code, and a field in Word.

**6. Run**

`run.vbs` — silent, an icon appears in the tray. `run.bat` — with a console and
a verbose log.

To start it at logon: tray icon → "Run at startup". The program drops
`WhisperFree.vbs` into the startup folder — open it with `shell:startup` in
Explorer, and turn autostart off by deleting that file.

The startup folder was chosen over the usual `HKCU\...\Run` registry key on
purpose. The registry gets virtualized: a process inside an app container writes
to its own hive, reads back from the same place and looks perfectly happy, while
the real user session never sees the entry. A file in the startup folder you can
see with your own eyes.

---

## Keys

| Key | What it does |
|---|---|
| **Right Ctrl** (hold) | Record and paste |
| **Ctrl+Alt+V** | Paste the last transcript again |
| **Ctrl+Alt+H** | Open the history window |

Right Ctrl was picked because almost nobody uses it, and `Ctrl+Win` is already
taken by Wispr Flow. Change it in `[hotkeys]`.

Do not put right Shift in `dictate`: it is needed for capital letters, and every
capital would start a recording.

---

## When the paste does not land

Pasting into someone else's window is unreliable by nature: the window may have
lost focus, the app may have swallowed Ctrl+V, the field may not be editable.
On Windows you generally cannot verify that a paste actually happened — so the
program does not try to detect failure, it makes failure harmless instead.

- **The text is on the clipboard before the paste is attempted.** Even if nothing
  landed, a plain manual Ctrl+V works.
- **Every transcript is written to** `%APPDATA%\WhisperFree\history.jsonl` along
  with the timestamp, language, duration and the name of the receiving app. The
  record is written **before** the paste: even a crash during the paste loses
  nothing.
- **Ctrl+Alt+V pastes the last transcript.** Repeated presses walk backwards: the
  first gives you the last one, the second the one before it, and so on. A
  three-second pause resets the counter back to the newest.
- **Tray → "Recent transcripts"** — the last ten, click to paste.
- **Ctrl+Alt+H** opens the history window with search. Failed attempts are
  highlighted in red — that is usually what you are looking for.
- **Errors are always visible.** The bar at the bottom of the screen turns red and
  says what happened. Silently losing a dictated paragraph is the worst possible
  outcome, so it does not happen.

The clipboard is restored to its previous contents 300 ms after the paste. If you
want the last transcript to stay on the clipboard, the way Wispr Flow does it,
set `restore_clipboard = false`.

---

## Mixed-language dictation

This is the hard part. Most people do not dictate in one clean language: you
speak your own, and the technical vocabulary in the middle of the sentence is
English. Speech models transliterate those terms into the surrounding language —
in Russian, `Gemini` comes out as "Джемини" and `Docker` as "Докер"; in your
language it will be whatever the local spelling of the sound is. Even
English-only dictation has the same shape of problem with product names the model
has never heard.

Three layers handle it, each of them configurable.

**1. A prompt seed for the model** — `[language].prompt_ru` and
`[language].prompt_en`. A short string showing your punctuation style and your
terms spelled the way you want them. The model reads it as an example of the
desired format. Add your own terms; the limit is 224 tokens.

**2. The replacement dictionary** — `[postprocess.replacements]`. Deterministic,
instant and free, it cleans up whatever the seed did not:

```toml
"джемини" = "Gemini"
"пул реквест" = "pull request"
```

Left side is what you hear, right side is how it should be written — in any pair
of languages, since this is a plain substitution table over the recognizer's
output. Matching is case-insensitive and happens on word boundaries. A capital
at the start of a sentence is preserved.

For inflection there are regexes — a key with the `re:` prefix:

```toml
"re:\\bдокер\\w*" = "Docker"   # докера, докером, докеру — every case form
```

The same works for English-only setups, where the job is usually canonical
spelling rather than grammar:

```toml
"re:\\bpy ?torch\\b" = "PyTorch"
"re:\\bpost gres\\w*" = "PostgreSQL"
```

The rules that ship in `config.example.toml` are Russian → English, because that
is the author's pair. Replace them with yours; nothing in the code assumes a
particular language.

**3. A separate key for the second language.** `[hotkeys].dictate_alt` is empty
in the shipped config, so out of the box there is exactly one dictation key and
`language.alt` is not used by anything. Assign the key and it starts dictating
in `language.alt`:

```toml
[hotkeys]
dictate_alt = "scroll_lock"
```

`scroll_lock`, `pause` or `f13`..`f24` are the good candidates: nothing else
claims them.

---

## Model-based text refinement

Transcription gives you words but not grammar: no punctuation, endings guessed
by ear, agreement all over the place. Set `[refine].enabled = true` and the text
goes through a fast model before being pasted.

A real dictation, Russian — no punctuation and broken agreement going in, clean
sentences coming out:

```
before: запустил диктую пробую запись эти слова набраны уже из нового
        единственное что я хотел бы чтобы он еще проверял
after : Запустил диктую, пробую запись. Эти слова уже набраны из нового.
        Единственное, что я хотел бы, чтобы он ещё проверял...
```

Measured on Groq over a warm connection: **293 ms on average**. The whole cycle
comes to about 1.3 s. Wispr Flow took 2–4 s for the same cycle when I used it —
that is an observation from daily use, not a controlled benchmark.

**The model was picked by measurement.** `openai/gpt-oss-120b` with
`reasoning_effort = "low"` gives the best quality and does not over-edit: it
leaves "Раз, два, три, проверка." alone, whereas 20b swaps the comma for a dash
for no reason. The `qwen` models dump their reasoning into the answer and are
unusable here.

**The real risk is not quality, it is the model going off-script.** It can answer
the text instead of correcting it, add things of its own, or drop a chunk. So the
result is checked, and on any doubt the original is used:

- the response is empty, or traces of reasoning are left in it;
- the text grew by more than `max_growth` times — meaning the model started answering;
- the text shrank by half — meaning part of what you said was lost;
- the request failed or did not finish within `timeout_s`.

Losing what you dictated is worse than leaving it rough. If the refinement did
distort the meaning, the original sits next to it in history — the `Ctrl+Alt+H`
window.

**The replacement dictionary is applied AFTER the refinement**, so the last word
on terminology belongs to your config and not to the model.

---

## What it costs

The numbers below are not estimates, they come from measurements on live
dictations: average length **9.2 seconds and 104 characters**, refinement usage
**218 + 96 tokens**.

**One dictation — 0.02 cents:**

| | per dictation |
|---|---|
| Transcription (`whisper-large-v3-turbo`, $0.04/hour) | $0.000111 |
| Text refinement (`gpt-oss-120b`, $0.15/$0.60 per million tokens) | $0.000090 |
| **Total** | **$0.000201** |

Refinement costs almost as much as transcription — turning it off roughly halves
the bill.

**Per month:**

| Dictations per day | Roughly | With refinement | Without |
|---|---|---|---|
| 20 | 3 min of speech | $0.12 | $0.07 |
| 50 | 8 min of speech | $0.30 | $0.17 |
| 100 | 15 min of speech, ~10k characters | $0.60 | $0.33 |
| 200 | 31 min of speech, ~20k characters | $1.21 | $0.67 |
| 500 | 77 min of speech | $3.02 | $1.67 |
| **Wispr Flow** | — | **$15.00** | **$15.00** |

To reach $15 a month you would have to dictate **more than six hours a day, every
day**. In practice the bill closes under a dollar.

You may not pay anything at all: on the Groq free tier, as measured in August
2026, the ceiling was 2000 transcription requests and 1000 chat-model requests a
day. Providers change limits, so check yours with `limits.bat` — it reads the
current values from the API response headers.

**Where you overpay.** Groq bills a minimum of 10 seconds. A 3-second utterance
is billed as ten seconds, three times its own length. Three short utterances cost
as much as one thirty-second one — longer phrases are cheaper per word, though at
these amounts it is a matter of principle rather than money.

Current spend is visible in the tray: count and total for today and for the
month, split between transcription and refinement.

If you need better recognition quality, use `model = "whisper-large-v3"`
($0.111/hour, roughly a quarter fewer errors, but slower).

---

## A different provider

The endpoint is any OpenAI-compatible one, so switching providers is three lines
in `[provider]` and no code changes:

```toml
# OpenAI
base_url = "https://api.openai.com/v1"
model = "gpt-4o-transcribe"
api_key_env = "OPENAI_API_KEY"
```

Proxies and local servers hook up the same way. If `api.groq.com` is not
reachable from your network, the program says so in plain words ("access denied
(403) — the provider may be unreachable from your network"), and changing
`base_url` is enough.

---

## Settings worth knowing

The config is looked up in two places, in this order:

1. **`config.toml` next to the program** — portable mode. `history.jsonl`,
   `logs/` and `audio/` then live there too, and the whole folder can be moved
   as one piece.
2. `%APPDATA%\WhisperFree\config.toml` — if there is no file next to the program.

Portable mode is the one to prefer, and not only for convenience. `%APPDATA%`
gets substituted: apps from the Microsoft Store and programs in MSIX containers
see their own `LocalCache` instead of `C:\Users\<name>\AppData\Roaming`, **while
printing exactly the same path**. A config edit made from such a program never
reaches a normal run, and it looks like "my settings are being ignored". A file
next to the program knows no such substitution.

When that is what you are seeing — the edit sits in the file, the program acts
as if it never happened — run **`diag.bat`**. It prints the size, the mtime and the
SHA-256 of the `config.toml` this particular process actually opened, plus the
`[audio].device` line read out of it. Run it both ways, double-clicked from
Explorer and from your terminal, and compare: different hashes mean the two
launches are reading different files.

A fully commented sample is in `config.example.toml`. The current one can be
opened from the tray.

| Setting | What it means |
|---|---|
| `[audio].device` | Microphone. Empty — the system default, otherwise part of a name or an index from `devices.bat` |
| `[audio].preroll_ms` | How much audio from **before** the keypress goes into the recording. 250 ms saves the first syllable |
| `[audio].min_seconds` | Shorter than this counts as an accidental press and is ignored |
| `[audio].silence_peak` | Silence threshold, 0..1. Below it no request is sent. Default `0.02`, but it depends on the microphone and its volume — measure it with `calibrate.bat` instead of guessing |
| `[audio].hold_open` | `false` releases the microphone between dictations — the tray icon goes dark, but the pre-roll is gone |
| `[inject].paste_overrides` | Paste key for specific programs. `ctrl+shift+v` for terminals |
| `[inject].method` | `unicode` instead of `clipboard`, if the clipboard must not be touched. Slower |
| `[history].keep_audio` | Keep the audio of recent dictations, to re-transcribe without saying it again |
| `[hotkeys].suppress` | Hide the dictation key from applications. Only works for dedicated keys |

---

## Files next to the program

Everything you double-click sits in the project root. These files pick up the
`.venv` beside them and say so plainly when the environment is missing — the one
exception is `sound.bat`, which needs no Python at all.

| File | What it does |
|---|---|
| `run.vbs` | Silent start: no console, the icon appears in the tray |
| `run.bat` | The same with a console and a verbose log |
| `check.bat` | Checks the key, the microphone and the provider: records three seconds and prints what came back |
| `devices.bat` | Lists microphones — the name from here goes into `[audio].device` |
| `calibrate.bat` | Measures the noise floor over three seconds of silence and writes the chosen `[audio].silence_peak` into the config itself |
| `paste-test.bat` | Gives you five seconds to switch windows, pastes a test phrase there and prints which key it used |
| `limits.bat` | Remaining plan limits, read out of the API response headers |
| `sound.bat` | Opens the classic Sound panel on the Recording tab: the microphone level slider and Microphone Boost live there, not in modern Settings |
| `diag.bat` | Prints the size, mtime and SHA-256 of the `config.toml` this particular process actually opened |
| `build.bat` | Builds the standalone `dist\WhisperFree` folder |

Worth remembering about `calibrate.bat`: the silence threshold depends not only
on the microphone but on its volume in Windows. Raise the slider and the noise
floor rises with it, and the old threshold stops catching it. So do not guess the
threshold — measure it again after every change to the volume.

`diag.bat` answers exactly one question: which config file is the program really
reading. Run it both ways — by double-clicking from Explorer and from your own
terminal — and compare the SHA-256 lines. Different hashes mean the two processes
open different files, which is how a redirected `%APPDATA%` gets caught.

---

## When something is wrong

**SmartScreen or the antivirus complains.** Expected: keyboard capture uses the
same Win32 API that keyloggers use, and the build is unsigned.

**It does not work over a window running as administrator.** A Windows
limitation: a hook from a non-elevated process does not see events in elevated
windows. The only fix is running WhisperFree as administrator.

**It stopped responding after sleep.** It should not: a watchdog thread detects
the wake by the drift between wall-clock and monotonic time and rebuilds the
hook. If it happens anyway, the log will contain a "rebuilding keyboard hook"
line — attach it to the report.

**In a terminal the text goes to the wrong place, or nowhere.** Add your terminal
to `[inject.paste_overrides]` with `ctrl+shift+v`. Entries already ship for
Windows Terminal, conhost, mintty and PuTTY.

**"Silence: level below threshold".** The message and the log both show the
measured level and the threshold, which tells you what to do right away.

- Level near zero (below 0.001) — there is no audio at all. Look for the
  "settings" line in the log: if it says the microphone is the system default,
  the system default is being used, and that is often not the one you want. Put
  yours in via `devices.bat`.
- Level noticeable but below the threshold (say 0.015 against a threshold of
  0.02) — the microphone works but is quiet. Raise the volume in the Windows
  sound settings: **`sound.bat`** opens the classic panel straight on the
  Recording tab, where the level slider and Microphone Boost live. Or lower
  `[audio].silence_peak`.

`check.bat` prints the level, the threshold and what follows from both — start
there, but do **speak** during the three seconds it listens.

The opposite failure exists too: a threshold below your own noise floor lets
empty recordings through, and the provider answers them with invented captions.
Either way the fix is the same — **`calibrate.bat`** measures the noise floor
over three silent seconds and writes the threshold into the config.

**The microphone icon in the tray stays lit.** By design: the microphone stream
is held open so the pre-roll works — those 250 ms before the keypress, without
which the first syllable is cut off. If that bothers you, set
`[audio].hold_open = false`: the device will be busy only during dictation, but
the pre-roll is gone and the start of a phrase can be lost.

**The text is pasted twice.** That means two copies are running. As of this
version the second instance refuses to start and says so, but old processes may
have survived — check Task Manager for `python.exe` with `whisperfree` in the
command line.

**Ctrl+C with the right Ctrl turns into the letter "c".** This should not happen —
modifiers are deliberately never suppressed, even with `suppress = true`. If you
see it, it is a bug, and the log will show which key is being suppressed.

Logs: `%APPDATA%\WhisperFree\logs\whisperfree.log`, openable from the tray. Every
dictation writes a line with per-stage timings — it is immediately obvious what
is slow.

---

## Building a standalone exe

Not needed for everyday use — `run.vbs` and autostart work as they are. It is
useful for moving the program to another machine that has no Python.

```bash
build.bat
```

You get a `dist\WhisperFree` folder (74 MB) that can be moved as a whole. Put the
key in a `.env` next to `WhisperFree.exe` — the program looks for it there too,
not only in the working directory. The order is: the working directory, then the
folder holding the exe, then `%APPDATA%\WhisperFree`; the first `.env` found
wins. So a shortcut, or a launch from some other folder, still finds the key.

The build is tested and runs. Two pitfalls are already worked around and
documented in `build.bat` and `whisperfree.spec`, so nobody steps on them again:

- PyInstaller inside a venv does not find Tcl/Tk and **silently drops tkinter**
  from the build — the exe dies with `ModuleNotFoundError: tkinter`. That is why
  `build.bat` sets `TCL_LIBRARY` and `TK_LIBRARY` before starting the build.
- The entry point is `whisperfree_app.py`, not `whisperfree/__main__.py`:
  PyInstaller runs the given script as `__main__` with no package context, and
  relative imports inside the package fail.

---

## How it works

```
whisperfree/
  __main__.py        entry point, thread layout
  audio.py           always-open microphone stream, ring buffer for the pre-roll
  hotkey.py          keyboard hook, layout independence, sleep watchdog
  inject.py          clipboard and SendInput, paste key chosen per program
  providers/         adapter for an OpenAI-compatible endpoint
  postprocess.py     replacement dictionary, hallucination filtering on silence
  history.py         transcript log and re-pasting
  overlay.py         status bar on top of every window
  tray.py            icon, menu, spend counter
```

Threads: the main one runs the Tk loop, the keyboard hook lives in its own and
its handlers must return instantly, all the slow work goes to a worker thread,
and the tray and the microphone spin separately.

Tests. pytest lives in the `dev` extras, which the quick start does not install:

```bash
.venv\Scripts\python -m pip install -e ".[dev]"
```

```bash
.venv\Scripts\python -m pytest -q
```

---

## Measurements

What has already been verified on the machine this was written on (i7-4820K,
Windows 10 Pro 19045):

- **FLAC instead of WAV — 78% less** data to send (5 seconds of audio: 34.7 KB
  against 156.3 KB), lossless, bit for bit. That comes straight off the latency.
- **The clipboard** survives Cyrillic, multi-line text, guillemets, dashes and
  non-BMP emoji; the previous contents come back.
- **The silence cutoff** works: peak level of an empty recording is 0.0000
  against the default threshold of 0.02.
- **Active window detection** works — the program sees the exe name, so it can
  pick the paste key and record where the text went.
- **Groq answers in 270–340 ms** on a single two-second test fragment, network
  included. That is a different measurement from the 204–336 ms in
  [Features](#features): this one is a short probe of the endpoint, that one is
  live dictations averaging 9.2 seconds of speech. With encoding and pasting on
  top, the cycle fits inside the 1.5 s target with room to spare — the same
  cycle took 2–4 s for me on Wispr Flow.
- **383 automated tests** pass, including an end-to-end run of the pipeline
  against a stub provider: the request goes out with the right language and
  prompt seed, the replacement dictionary is applied before the paste, the record
  reaches history before the paste, and after a failed paste the text is
  retrievable by hotkey.

What still needs measuring in real use and writing down here:

- per-stage latency from the log — the target is under 1.5 s from key release to paste;
- real daily spend from the tray counter, checked against the $0.40–1.50/month estimate;
- the share of technical terms written in Latin script before and after the
  replacement dictionary: dictate twenty phrases with terms in them and count.

These checks have already caught two bugs:

- Clipboard size was counted in code points instead of UTF-16 units, so an emoji
  was pasted truncated to half a surrogate pair. On ordinary text this would
  never have shown up.
- The hallucination filter matched specific surnames from a list. On the very
  first real request Groq produced, out of clean tone, "Редактор субтитров
  А.Семкин Корректор А.Егорова" — new surnames, and the text would have gone
  straight through. Now the whole pattern is matched, not a roster of names.

---

## License

MIT — full text in [LICENSE](LICENSE). Use it, change it, embed it in your own
projects; the only condition is keeping the license text.

---

## Contributing

Bug reports, measurements on other hardware, rules for the replacement dictionary
and support for new terminals are all welcome. The process is in
[CONTRIBUTING.md](CONTRIBUTING.md).

Before a pull request it is worth running the tests, currently 383 of them.
pytest comes with the `dev` extras, which the quick start does not install:

```bash
.venv\Scripts\python -m pip install -e ".[dev]"
```

```bash
.venv\Scripts\python -m pytest
```

A bug report is much more useful with a line from
`%APPDATA%\WhisperFree\logs\whisperfree.log` attached — it has the per-stage
timings and the name of the receiving window.
