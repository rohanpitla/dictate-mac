# Dictate

A free, fully local voice dictation tool for macOS. Hold a hotkey anywhere
on your Mac, speak, release — clean, formatted text appears wherever your
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

1. Hold **Right Option** (configurable) anywhere — a sound plays, the mic
   records while it's held.
2. Release — the recording is transcribed locally with Whisper, then passed
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
- **Hands-free latch** — double-tap the hotkey to start continuous listening
  without holding the key down; tap once to stop.
- **Audio ducking** — mutes other system audio while you're dictating so it
  isn't picked up by the mic.
- **Clipboard-safe pasting** — your existing clipboard (including images and
  files, not just text) is restored right after the paste.
- **Menu bar settings** — enable/disable, hotkey choice, Whisper model size,
  a raw-mode toggle, dictation history, and p50/p95 latency stats.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture and the reasoning
behind the trickier parts (the cleanup prompt, guardrails, reliability
fixes).

## Setup

```bash
# clone, then from the project root:
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
