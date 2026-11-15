# Dictate

A free, fully local voice dictation tool for macOS. Tap a hotkey anywhere
on your Mac, speak, tap again — clean, formatted text appears wherever your
cursor is. No subscriptions, no cloud, no audio ever leaves your machine.

## Why

I was paying for Wispr Flow to get good voice dictation and didn't love
handing over a monthly subscription for something that's fundamentally just
speech-to-text plus a bit of cleanup. Once local Whisper models and small
local LLMs got fast enough to run comfortably on Apple Silicon, and once the
big AI apps started shipping their own built-in voice dictation, it was
clear this didn't need to be a paid, cloud-dependent product — it could just
run entirely on-device. So I built my own.

## Requirements

- **macOS on Apple Silicon.** Transcription runs on [MLX](https://github.com/ml-explore/mlx),
  Apple's on-device ML framework — this will not run on Intel Macs or Linux.
- [Ollama](https://ollama.com) installed and running, for the local cleanup pass.
- Python 3.13.

## What it does

1. Tap **Fn / Globe** (configurable) anywhere — a sound plays and the mic
   starts recording. No need to hold the key.
2. Tap it again — the recording is transcribed locally with Whisper, then passed
   through a local LLM that strips filler words and fixes punctuation
   without changing your meaning or tone.
3. The cleaned text is pasted at your cursor, in whatever app is focused.

If Ollama isn't running, or the cleanup output looks wrong, it just pastes
the raw transcript — you never lose what you said.

## Features

- **Per-app modes** — automatically switches style depending on the focused
  app: professional-but-warm for Mail/Gmail, casual for Slack/Discord/
  Messages, raw passthrough (no LLM at all) for code editors and terminals.
- **Custom vocabulary** — a configurable list of misheard → correct term
  replacements for names, acronyms, and jargon Whisper reliably gets wrong.
- **Modifier-safe hotkey** — using the key in a shortcut (Fn+arrow,
  Option+letter) never starts a recording; only a clean tap does. If you use
  Fn, set System Settings → Keyboard → "Press 🌐 key to" → Do Nothing.
- **Audio ducking** — mutes other system audio while you're dictating so it
  isn't picked up by the mic.
- **Dictations can't be lost** — audio is saved to disk while you speak. If
  the app freezes or is restarted, the recording is transcribed on the next
  launch and copied to your clipboard. Only the 5 newest recordings are
  kept; "Re-transcribe Last Recording" in the menu redoes the latest one.
- **Clipboard-safe pasting** — your existing clipboard (including images and
  files, not just text) is restored right after the paste.
- **Menu bar settings** — enable/disable, hotkey choice, Whisper model size,
  a raw-mode toggle, dictation history, and p50/p95 latency stats.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture and the reasoning
behind the trickier parts (the cleanup prompt, guardrails, reliability
fixes).

## Setup

```bash
git clone https://github.com/rohanpitla/dictate-mac.git && cd dictate-mac

# from the project root:
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# pull the cleanup model
ollama pull qwen2.5:3b

# run it
.venv/bin/python -m dictate
```

On first launch, macOS will ask for three permissions — grant all of them
to your terminal (or the packaged app, once you set up launch-at-login):

| Permission | Where | Needed for |
|---|---|---|
| Accessibility | System Settings → Privacy & Security → Accessibility | simulating Cmd+V to paste |
| Input Monitoring | System Settings → Privacy & Security → Input Monitoring | the global hotkey |
| Microphone | prompt appears on first dictation | recording |

Full manual test script (including per-phase checks) is in
[TESTING.md](TESTING.md).

## Tech stack

Python · [MLX](https://github.com/ml-explore/mlx) + Whisper
(`whisper-large-v3-turbo`) for on-device transcription · [Ollama](https://ollama.com)
(`qwen2.5:3b`) for local cleanup · `pynput` for the global hotkey ·
`sounddevice` for audio capture · PyObjC / Quartz for macOS system
integration (paste simulation, clipboard, frontmost-app detection) ·
`rumps` for the menu bar UI.

## Non-goals

No cloud/API transcription option, no account system, no auto-update, no
Electron or Swift rewrite, no real-time streaming transcription, no UI
beyond the menu bar.

## Meeting Mode (experimental, off by default)

A private live assistant for calls (Google Meet, Zoom, Teams, anything your
Mac plays). It has passed its automated tests but has not been used in real
calls yet. To try it, set `enabled = true` under `[meeting]` in `config.toml`,
restart Dictate, then menu bar > **Start Meeting Mode…**

- **Live transcript** of you ("You") and everyone else ("Them"). No bot joins the call.
- **Catch-up** every few minutes: what was decided, what is open, what was asked of you.
- **Talking points** when someone asks you a question or says your name, or
  on demand by tapping Right Option.
- **A panel only you can see**: it is excluded from screen sharing,
  screenshots and recordings. The menu bar icon does not change, so nothing
  in a shared screen shows that Meeting Mode is on; the panel is your sign
  that it is listening. If the panel cannot be opened hidden, Meeting Mode
  stops.

### Setup

1. Install Apple's command line tools once: `xcode-select --install`
   (Dictate builds its small audio helper from `helper/sysaudio.swift` on first use).
2. System Settings > Privacy & Security > **Screen & System Audio Recording**:
   turn on the app you start Dictate from (e.g. Terminal), then reopen it.
3. In `config.toml` under `[meeting]`, set `your_name`, and `base_url` /
   `model` for your provider (any OpenAI-compatible API).
4. Menu bar > **Set Meeting API Key…** and paste your own key.
   Without a key you still get the live transcript, with no suggestions.
5. Optional: put your agenda and facts in `meeting_notes.md` next to
   `config.toml`; they are used to make suggestions specific.

### Privacy and security

- **Audio never leaves your Mac** and is never written to disk in Meeting Mode.
- **The transcript lives in memory** and is erased when you stop. It is saved
  only if you set `save_transcript = true` (private to your account, last 10 kept).
- **Only transcript text and your notes are sent**, to the one HTTPS endpoint
  in `base_url`, and only if a key is saved. Plain `http://` is refused.
- **The API key is stored in the macOS Keychain**, never in a file or a log.
  Use your own key; never share one.
- **A key is tied to its provider's address** and is only ever sent there. If
  `base_url` changes, the old key is not used; you are asked for a new one.
- **The start dialog names the address** your transcript text will go to.
  Do not use a `config.toml` someone else gives you without reading `base_url`.
- **Nothing said in a meeting or a dictation is written to the log.**
- **Screen Recording permission is used for audio only**: the helper never
  reads screen pixels, opens no files and makes no network connections.
- **It never starts by itself**: every start asks for confirmation, and a
  session stops on its own after 4 hours.
- **Consent:** recording or transcribing people without telling them is
  illegal in some places. Check the rules that apply to your meeting.
