"""Where Dictate keeps its files on Windows.

Everything personal lives in %LOCALAPPDATA%\\Dictate, which only this
Windows account can read, and which survives reinstalls and updates (the
app itself is in %LOCALAPPDATA%\\Dictate\\app and is replaced on update).

DICTATE_HOME overrides the folder, for tests.
"""

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def _home() -> Path:
    override = os.environ.get("DICTATE_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Dictate"


HOME = _home()
SETTINGS = HOME / "settings.toml"
HISTORY = HOME / "history.jsonl"
LOGS = HOME / "logs"
LOG_FILE = LOGS / "dictate.log"
RECORDINGS = HOME / "recordings"
SOUNDS = HOME / "sounds"
# Written while Dictate has the speakers muted; if Dictate dies before it
# unmutes, the next start finds this file and turns the sound back on.
MUTE_MARKER = HOME / "muted-by-dictate.json"


def ensure() -> None:
    for folder in (HOME, LOGS, RECORDINGS, SOUNDS):
        folder.mkdir(parents=True, exist_ok=True)
