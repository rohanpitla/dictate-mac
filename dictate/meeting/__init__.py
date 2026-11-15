"""Meeting Mode: a private, on-device live meeting assistant.

Security rules every module here follows:
- Meeting audio and transcript live in memory only. Nothing is written to
  disk unless the user turns on `save_transcript`.
- Nothing said in a meeting is ever printed or logged (stdout is mirrored to
  ~/Library/Logs/Dictate/dictate.log). Log counts and timings, never text.
- The only network destination is the chat endpoint the user configured,
  over HTTPS, and only transcript text is sent. Audio never leaves the Mac.
- The API key lives in the macOS Keychain, never in config.toml or a log.
- Model output and transcript text are displayed as plain text and never
  executed, opened or followed.
"""
