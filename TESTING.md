# Dictate — Testing Guide (Phases 1–4)

## 1. Automated smoke test (no mic or permissions needed)

Open **Terminal.app**, then:

```bash
cd ~/Developer/dictate
.venv/bin/python smoke_test.py
```

Expect `SMOKE TEST PASSED` (imports, local TTS→whisper transcription,
clipboard round-trip, empty-audio edge cases).

## 2. First launch & permissions (one-time)

```bash
cd ~/Developer/dictate
.venv/bin/python -m dictate
```

On first launch macOS will prompt for permissions. Grant **all three** to
**Terminal**:

| Permission | Where | Needed for |
|---|---|---|
| Accessibility | System Settings → Privacy & Security → Accessibility → Terminal ON | pasting (Cmd+V simulation) |
| Input Monitoring | System Settings → Privacy & Security → Input Monitoring → Terminal ON | the Right Option hotkey |
| Microphone | prompt appears on your first dictation | recording |

**After granting Accessibility or Input Monitoring, quit Dictate (menu bar 🎤
→ Quit Dictate) and relaunch it** — macOS only applies these to a fresh
process.

## 3. Manual dictation test

1. Launch: `.venv/bin/python -m dictate` — look for the **🎤 icon** in the menu
   bar and `Permissions OK` in the terminal. Wait for
   `whisper model warmed up and ready` (first launch takes a few seconds).
2. Open **Notes**, click into a note.
3. **Tap Fn**, say *"this is a test of dictate dictation"*, **tap Fn again**.
   (First set System Settings → Keyboard → "Press 🌐 key to" → Do Nothing.)
   - Pop sound on the first tap, icon turns 🔴 while recording
   - Second sound on the second tap, icon shows ⏳
   - Text appears at your cursor within ~1–3 s, icon returns to 🎤
4. Copy something first (e.g. Cmd+C on a word), dictate again, then Cmd+V
   elsewhere — your **original clipboard text is still there**.
5. Repeat step 3 in **TextEdit**, a **Chrome textarea** (any comment box or
   claude.ai), and **iMessage**.
6. **Audio ducking**: play a YouTube video (or music), then tap Fn and
   dictate. The video's audio **mutes ~0.2 s after the first tap** and
   **unmutes the instant you tap to stop** — even before the text appears. If
   your Mac was already muted before dictating, it stays muted afterwards. To
   turn this feature off: set `mute_while_dictating = false` in `config.toml`.
7. **Shortcuts don't trigger dictation**: hold Fn and press an arrow key
   (Page Up/Down) → no sound, no recording. Typing normally while recording
   is fine.
8. Edge checks:
   - Tap Fn twice very quickly → nothing pastes (too short), no crash.
   - Tap, speak for 60 seconds, tap → still transcribes and pastes.
   - Terminal shows per-stage latency lines like
     `[dictate] latency ms — finalize: 12  transcribe: 900  paste: 400`.

### Troubleshooting

- **Tapping the hotkey does nothing** (no sound, no icon change): this is
  almost always missing **Input Monitoring** permission. Check that pane
  first, then quit and relaunch Dictate.
- **Tapping Fn opens the emoji picker or switches keyboard language**: set
  System Settings → Keyboard → "Press 🌐 key to" → Do Nothing.
- **Sounds play but nothing pastes**: missing **Accessibility** permission.
- **Error sound on press**: microphone permission denied — System Settings →
  Privacy & Security → Microphone → Terminal ON, then relaunch.

## 4. Phase 2: cleanup pass (Ollama)

Prereq: Ollama running (`ollama serve`, or the menu bar app) with
`qwen2.5:3b` pulled. Dictate warms it up at launch — look for
`ollama cleanup model warmed up and ready` in the terminal.

1. Open Notes, tap Fn and say, with deliberate filler (tap again to finish):
   *"um so basically the uh the client needs like a demo"*.
   Expect roughly: **"So basically, the client needs a demo."**
   The latency line shows the stage: `cleanup: 800 (llm)`.
2. Vocabulary: dictate *"I deployed the n eight n workflow with fast api
   on a v p s"* — expect **n8n**, **FastAPI**, **VPS** spelled correctly.
3. Tone preservation: dictate a casual sentence with contractions
   ("don't", "it's") — they must survive cleanup, not be formalized.
4. Graceful degradation: **quit Ollama**, dictate again. The raw transcript
   pastes (stage shows `raw`), no error, no hang. Restart Ollama and the
   next dictation is cleaned again — no app restart needed.

## 5. Phase 3: settings menu & history

Click the 🎤 menu bar icon:

1. **Enabled** — untick it, tap Fn → nothing happens (no sound, no
   recording). Tick it again → dictation works. Unticking while a recording
   is active aborts that recording.
2. **Hotkey** — pick **Left Option** or **F19** (if your keyboard has one),
   then switch back to **Fn / Globe**; the change applies instantly (no
   restart) and persists in config.toml.
3. **Whisper Model** — pick **Base (fastest)**, dictate (first use downloads
   the smaller model, watch the terminal), then switch back to
   **Large v3 Turbo**. Persists in config.toml.
4. **History (last 20)** — after a few dictations, open it: newest first,
   timestamped. Click an entry → it's copied to your clipboard, Cmd+V pastes
   it. Full log with raw transcripts + latencies lives in `history.jsonl`.
5. **Open Config File** — opens config.toml in your default editor.
6. **Launch at Login…** — shows the setup instructions below.

### Launch at login (recommended: Automator wrapper)

1. Open **Automator** → New Document → **Application**.
2. Add a **Run Shell Script** action containing:
   ```bash
   cd ~/Developer/dictate && .venv/bin/python -m dictate
   ```
3. Save as **Dictate** in /Applications.
4. **System Settings → General → Login Items** → **+** → add Dictate.
5. First launch: grant **Accessibility**, **Input Monitoring**, and
   **Microphone** to this new Dictate app (same three panes as before —
   macOS treats it as a separate app from Terminal).

## 6. Phase 4: per-app modes & polish

1. **Code apps paste raw**: focus a terminal or VS Code, dictate
   *"um pip install fast api"* → the raw transcript pastes untouched (stage
   shows `code-app` in the terminal — fillers kept, no cleanup).
2. **Chat apps stay casual**: dictate into **Messages/Slack/Discord** — the
   latency line shows `(llm+casual)`.
3. **Mail is email mode**: dictate into **Mail.app** → `(llm+email)`. In
   **Chrome/Safari with Gmail open** the same applies — the first browser
   dictation triggers a one-time "Terminal wants to control Chrome" prompt;
   click OK (this only reads the tab title, locally, to spot Gmail).
4. **Raw Mode toggle**: menu → tick **Raw Mode (skip cleanup)** — every app
   now gets raw transcripts (stage `raw-mode`); untick to restore. Persists
   across restarts in config.toml.
5. **Latency Stats…**: after several dictations, menu → Latency Stats — a
   dialog shows p50/p95 per stage (finalize/transcribe/cleanup/paste + total)
   over the last 100 dictations.
6. **Clipboard now fully survives**: copy an **image** (screenshot with
   Ctrl+Cmd+Shift+4), dictate somewhere, then Cmd+V into Preview/Notes — the
   image is still on your clipboard. Same for copied files in Finder.
7. **Custom app modes**: add a bundle ID to `[app_modes]` in config.toml
   (e.g. `"com.apple.Notes" = "casual"`), restart, and verify the stage line.

### Known limitations

- **Fn/Globe hotkey** detection varies by keyboard; Right Option and F19 are
  reliable.
- **Gmail detection** works in Chrome, Safari, Brave, and Edge (not Firefox —
  it has no AppleScript tab access); other webmail isn't special-cased.
