# Dictate for Windows

Tap a key, speak, tap again, and your words are typed wherever your cursor
is: email, chat, documents, AI prompts. "Um"s and false starts are cleaned
up, and long dictations come out as tidy bullet points.

## What you need

- Windows 10 (version 1809 or newer) or Windows 11, 64-bit. Intel, AMD and
  Snapdragon (ARM) laptops all work.
- About 600 MB of free disk space (about 1.5 GB with the optional local engine).
- A microphone (the laptop's built-in one is fine).
- An internet connection and a free Groq API key (step 2).

Memory: about 150 MB while idle, so an 8 GB laptop is fine.

## 1. Install

Open **PowerShell** (press the Windows key, type `PowerShell`, press Enter)
and paste:

```powershell
irm https://raw.githubusercontent.com/rohanpitla/dictate-mac/main/windows/install.ps1 | iex
```

It takes a few minutes the first time and needs no administrator rights.
At the end it asks whether Dictate should start when you sign in.

The installer only downloads from GitHub, and it checks every file against
a fingerprint (checksum) stored in the installer. A file that has been
tampered with is refused.

## 2. Get your free Groq key

Groq turns your voice into text using the same large Whisper model the Mac
version runs.

1. Go to **console.groq.com** and sign up (free).
2. **Recommended:** open **Settings → Data Controls** and switch on
   **Zero Data Retention**, so Groq keeps nothing you send.
3. Open **API Keys → Create API Key**, give it a name ("Dictate"), and copy it.
4. Dictate asks for the key the first time it starts. Click
   **Paste from clipboard and save**. Later you can change it from the
   tray menu: **Set Groq API key…**

Your key is stored in Windows Credential Manager, not in any file, and is
only ever sent to Groq. Use your own key; don't share it.

## 3. Use it

Look for the **microphone icon** next to the clock. It may be hidden under
the **^** arrow; drag it onto the taskbar to keep it visible.

| | |
|---|---|
| **Start** | Tap **Right Ctrl** (press and let go). You hear a short rising tone and the icon turns red. |
| **Stop** | Tap **Right Ctrl** again. A falling tone; the icon turns amber while it transcribes, then your text appears where your cursor is. |
| **Stop from the menu** | Tray icon → **Stop dictation** |

**Your music and videos go quiet while you speak.** Dictate mutes other
sound when you start, so the microphone only hears you, and turns it back
on the moment you stop. If you had muted your PC yourself, it stays muted.
To switch this off, set `mute_while_dictating = false` in the settings
file (tray menu → **Open settings file**) and restart Dictate.

**Bullet points:** long dictations (about 60 words or more) come out as one
bullet point per idea. Emails get paragraphs instead, and short chat
messages stay as one message. Say "first… second… third…" to get a
numbered list. Prefer paragraphs everywhere? Set
`layout_style = "paragraphs"` in the settings file.

**Writing style follows the app:** Outlook and Gmail get complete
sentences; Slack, Teams, WhatsApp and Discord get casual messages;
terminals and code editors get exactly what you said, with no clean-up.

**Other hotkeys:** tray menu → **Hotkey**: Right Ctrl, the Copilot key,
Scroll Lock or F9. Some new laptops have a Copilot key instead of a
Right Ctrl.

**History:** tray menu → **History** lists your last 20 dictations; click
one to copy it.

## If something goes wrong

| Problem | Fix |
|---|---|
| "Microphone unavailable" or "No sound from the microphone" | Windows Settings → Privacy & security → **Microphone**: turn on **Microphone access** and **Let desktop apps access your microphone**. |
| Nothing happens when you tap Right Ctrl | Check the microphone icon is next to the clock (start Dictate from the Start menu if not). Try another hotkey from the tray menu. |
| "Press Ctrl+V to paste" | The app you're typing into runs as administrator, and Windows doesn't let other apps type into it. Your text is on the clipboard: press Ctrl+V. |
| "Dictation saved for later" | No internet, or Groq was busy. Nothing is lost: it's transcribed automatically when the connection is back, and appears in **History**. |
| "Groq rejected the API key" | Tray menu → **Set Groq API key…** and paste the key again. |
| The Copilot key also opens Copilot | Windows Settings → Personalization → Text input → **Customize Copilot key**, or pick another hotkey. |
| Your work laptop blocks it | Company security software may not allow apps that listen for a hotkey. Ask your IT team, or use a personal laptop. |

The log is at `%LOCALAPPDATA%\Dictate\logs\dictate.log` (tray menu →
**Open log folder**). It records times and errors, **never what you said**,
so it's safe to send if you need help.

## Privacy

- **What leaves your laptop:** the audio of each dictation and its text,
  sent only to Groq, over an encrypted connection. Nothing else, to no one
  else.
- **Everything stays on the laptop instead** with the local engine:
  reinstall with `-WithLocal` (run the installer file with that option),
  then tray menu → **Speech engine → On this laptop**. It's slower, and less
  accurate on most laptops.
- Your dictations are kept out of Windows clipboard history (Win+V) and
  cloud clipboard sync.
- History, settings and the last few recordings stay in
  `%LOCALAPPDATA%\Dictate`, readable only by your Windows account.

## Uninstall

Tray menu → **Uninstall Dictate…**. This removes the app, its shortcuts and
your saved key, and asks whether to also delete your history.
