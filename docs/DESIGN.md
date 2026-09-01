# Design notes

Notes on how Dictate is put together and why. See the [README](../README.md) for what it does and how to run it.

## The contract

Hold a hotkey (default: Right Option) in any app. A subtle sound plays; the
mic records while the key is held (push-to-talk). On release: audio → local
Whisper transcription → local LLM cleanup → cleaned text is pasted into the
focused field at the cursor.

Target latency: under 3 seconds for a 15-second utterance. If anything in
the cleanup path fails, paste the raw transcript rather than nothing — the
one thing this app must never do is silently lose what you said. If
transcription itself fails, play an error sound and log it.

## Stack

- **Menu bar**: `rumps`
- **Audio capture**: `sounddevice`, 16kHz mono to a temp WAV
- **Hotkey**: `pynput` global listener (needs Accessibility permission)
- **Transcription**: `mlx-whisper` with `whisper-large-v3-turbo` on Apple
  Silicon/Metal. Falls back to `faster-whisper` (`small.en`, CPU) if MLX
  fails to load.
- **Cleanup LLM**: local Ollama running `qwen2.5:3b` (fallback
  `llama3.2:3b`), temperature 0. If Ollama isn't reachable, cleanup is
  skipped entirely and the raw transcript pastes — the app never blocks
  waiting on it.
- **Paste**: snapshot the current clipboard → write cleaned text to it →
  simulate Cmd+V via a Quartz `CGEvent` → restore the original clipboard
  shortly after. This preserves whatever you had copied before, images and
  files included, not just plain text.
- **Config**: a single `config.toml` — hotkey, whisper model, ollama model,
  custom vocabulary, per-app modes.

No cloud calls anywhere, no API keys, no account system.

## The cleanup prompt

This is the part that actually makes the output usable. The system prompt
sent to Ollama:

```
You clean up raw speech-to-text transcripts. Rules:
- Remove filler words (um, uh, like, you know, I mean) and false starts
- Fix punctuation, capitalization, and obvious homophone errors
- Preserve the speaker's meaning, tone, and word choice — do NOT
  rephrase, summarize, shorten, or "improve" their writing
- Keep contractions and casual tone if the speaker used them
- Format lists as lists only if the speaker clearly dictated a list
- Output ONLY the cleaned text. No preamble, no quotes, no comments.
```

Small local models like `qwen2.5:3b` tend to over-edit — dropping words
like "basically" or "I think" that aren't actually filler, or adding a
"Here's the cleaned text:" preamble. A one-shot example in the prompt
(`"um so basically the uh the client needs like a demo"` →
`"So basically, the client needs a demo."`) fixed most of that.

**Guardrails** on top of the prompt, because a model will occasionally
ignore its instructions: if the LLM output is empty, is more than 2x or
less than 0.3x the length of the raw transcript, or starts with meta-text
like "Here is", it gets discarded and the raw transcript is used instead.
The cleanup pass must never lose user content — worst case, you get
unpolished text, never no text.

## Custom vocabulary

`config.toml` holds a `[vocabulary]` table of misheard → correct terms,
applied by case-insensitive regex replacement *after* cleanup — useful for
product names, acronyms, and jargon Whisper reliably mishears (e.g. "n eight
n" → "n8n", "h vac" → "HVAC").

## Per-app modes

Before the cleanup pass, the frontmost app's bundle ID (via `NSWorkspace`)
picks a style hint injected into the system prompt:

- Mail / Gmail in a browser → "Format as professional but warm email prose."
- Slack / Discord / Messages → "Keep it casual and concise."
- Code editors / terminals → skip the LLM pass entirely, paste raw.
- Everything else → default cleanup, no hint.

Gmail-in-browser detection reads the active tab title via AppleScript
(Chrome/Safari/Brave/Edge); a denied Automation prompt just falls back to
default cleanup for that browser rather than retrying every dictation.

## Reliability details worth remembering

- **Whisper hallucination gate**: on very quiet/silent audio, Whisper likes
  to invent short phrases ("Thank you.", "you"). These get filtered before
  they can pollute a paste.
- **Ollama `keep_alive: -1`**: Ollama's default 5-minute model unload made
  the first dictation after a break stall for several seconds while it
  reloaded. Keeping the model resident fixes that.
- **Cancel on other keypress**: since the hotkey (Right Option) is also a
  typing modifier, pressing another key while it's held cancels the
  recording instead of transcribing a mixed signal.
- **Hands-free latch**: double-tapping the hotkey toggles continuous
  listening without holding the key down; a double-tap that's held instead
  behaves like normal push-to-talk. Small state machine on the press/release
  handlers.
- **Audio ducking**: system audio output mutes for the duration of a
  dictation and restores afterward, so a recording isn't picked up over
  whatever else is playing.

## Non-goals

- No cloud/API transcription option, no account system, no auto-update.
- No Electron, no Swift rewrite, no App Store packaging.
- No real-time streaming transcription — batch on key-release is fine for
  this use case.
- No UI beyond the menu bar.
