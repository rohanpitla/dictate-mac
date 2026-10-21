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
    """Mute/unmute the system output, only undoing our own mute.

    osascript takes ~100 ms, so callers use the *_async methods. The wanted
    state is recorded before the thread starts: on a quick start/stop, the
    unmute thread could otherwise run before the mute thread and the Mac
    would stay muted after dictation.
    """

    def __init__(self) -> None:
        self._we_muted = False
        self._want_muted = False
        self._lock = threading.Lock()

    def mute_async(self) -> None:
        self._want_muted = True
        threading.Thread(target=self._apply, daemon=True,
                         name="dictate-mute").start()

    def unmute_async(self) -> None:
        self._want_muted = False
        threading.Thread(target=self._apply, daemon=True,
                         name="dictate-unmute").start()

    def unmute(self) -> None:
        self._want_muted = False
        self._apply()

    def _apply(self) -> None:
        with self._lock:
            if self._want_muted and not self._we_muted:
                if output_is_muted():
                    return  # the user muted it themselves — leave it alone
                _osascript("set volume output muted true")
                self._we_muted = True
            elif not self._want_muted and self._we_muted:
                _osascript("set volume output muted false")
                self._we_muted = False
