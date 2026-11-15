"""Mirror everything Dictate prints into a small log file.

When something goes wrong mid-dictation the terminal output is usually
gone; ~/Library/Logs/Dictate/dictate.log says what happened and when.
Capped at ~1 MB plus one older file.
"""

import logging
import sys
import threading
import time
from pathlib import Path

LOG_PATH = Path.home() / "Library" / "Logs" / "Dictate" / "dictate.log"
MAX_BYTES = 1_000_000


class _Tee:
    def __init__(self, stream, file, lock) -> None:
        self._stream = stream
        self._file = file
        self._lock = lock
        self._at_line_start = True

    def write(self, text: str) -> int:
        self._stream.write(text)
        try:
            with self._lock:
                for line in text.splitlines(keepends=True):
                    if self._at_line_start:
                        self._file.write(time.strftime("%Y-%m-%d %H:%M:%S "))
                    self._file.write(line)
                    self._at_line_start = line.endswith("\n")
                self._file.flush()
        except (OSError, ValueError):
            pass  # logging must never break dictation
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install() -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.parent.chmod(0o700)  # the log can quote dictated text
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
        file = open(LOG_PATH, "a", encoding="utf-8")
        LOG_PATH.chmod(0o600)
        old = LOG_PATH.with_suffix(".log.1")
        if old.exists():
            old.chmod(0o600)
    except OSError as exc:
        print(f"[dictate] could not open log file: {exc}", file=sys.stderr)
        return
    lock = threading.Lock()
    sys.stdout = _Tee(sys.stdout, file, lock)
    sys.stderr = _Tee(sys.stderr, file, lock)
    # pynput reports listener failures through `logging`
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="[%(name)s] %(message)s")
