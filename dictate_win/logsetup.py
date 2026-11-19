"""Mirror what Dictate prints into %LOCALAPPDATA%\\Dictate\\logs\\dictate.log.

The app runs without a console (pythonw), so the log is the only record of
what happened. It records taps, timings and errors, never what was said.
Capped at ~1 MB plus one older file.
"""

import sys
import threading
import time

from dictate_win import paths

MAX_BYTES = 1_000_000


class _Tee:
    def __init__(self, stream, file, lock) -> None:
        self._stream = stream  # None under pythonw
        self._file = file
        self._lock = lock
        self._line_start = True

    def write(self, text: str) -> int:
        if self._stream is not None:
            try:
                self._stream.write(text)
            except (OSError, ValueError):
                pass
        try:
            with self._lock:
                for line in text.splitlines(keepends=True):
                    if self._line_start:
                        self._file.write(time.strftime("%Y-%m-%d %H:%M:%S "))
                    self._file.write(line)
                    self._line_start = line.endswith("\n")
                self._file.flush()
        except (OSError, ValueError):
            pass
        return len(text)

    def flush(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False


def install() -> None:
    try:
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        if paths.LOG_FILE.exists() and paths.LOG_FILE.stat().st_size > MAX_BYTES:
            paths.LOG_FILE.replace(paths.LOG_FILE.with_suffix(".log.1"))
        file = open(paths.LOG_FILE, "a", encoding="utf-8")
    except OSError:
        return
    lock = threading.Lock()
    sys.stdout = _Tee(sys.stdout, file, lock)
    sys.stderr = _Tee(sys.stderr, file, lock)
