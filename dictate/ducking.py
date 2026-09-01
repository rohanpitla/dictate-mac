"""Mute system audio output while dictating, restore it afterwards.

Uses AppleScript's volume commands — no extra permissions needed. If the
user had output muted before dictating, we leave it muted afterwards.
"""

import subprocess
import threading


def _osascript(expr: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", expr],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def output_is_muted() -> bool:
    return _osascript("output muted of (get volume settings)") == "true"


class OutputMuter:
    """Mute/unmute the system output, only undoing our own mute."""

    def __init__(self) -> None:
        self._we_muted = False
        self._lock = threading.Lock()

    def mute(self) -> None:
        with self._lock:
            if self._we_muted or output_is_muted():
                return  # already muted (by us or by the user) — nothing to do
            _osascript("set volume output muted true")
            self._we_muted = True

    def unmute(self) -> None:
        with self._lock:
            if self._we_muted:
                _osascript("set volume output muted false")
                self._we_muted = False
