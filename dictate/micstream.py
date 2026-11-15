"""Microphone capture in a small helper process (helper/micaudio.swift).

Why not open the mic inside Dictate: closing an in-process microphone can
deadlock in the audio system. That left the mic switched on and made every
later recording fail with "microphone unavailable" until Dictate was
restarted. A helper process cannot do that to us: stopping is `kill`, which
always returns at once and always releases the microphone.

One helper is kept "armed" (started, microphone still off) so a recording
begins without waiting for a process to launch.

MicStream offers the small part of sounddevice.InputStream that Dictate
uses: start(), abort(), close(), and a callback that receives int16 frames.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from dictate.config import PROJECT_ROOT
from dictate.meeting.sysaudio import SystemAudioError, build_helper

SOURCE = PROJECT_ROOT / "helper" / "micaudio.swift"
BINARY = PROJECT_ROOT / "helper" / "micaudio"
_BLOCK_BYTES = 1600        # 50 ms
FIRST_AUDIO_TIMEOUT_S = 3.0

_lock = threading.Lock()
_ready: bool | None = None            # None = not built yet
_armed: subprocess.Popen | None = None


def prepare() -> bool:
    """Build the helper if needed and arm one. Call once in the background at
    launch. Returns False when the helper cannot be built (no Apple command
    line tools); Dictate then uses the in-process microphone instead."""
    global _ready
    try:
        build_helper(SOURCE, BINARY)
        _ready = True
    except (SystemAudioError, OSError) as exc:
        _ready = False
        print(f"[dictate] microphone helper unavailable, using the built-in "
              f"recorder instead: {exc}", file=sys.stderr)
        return False
    _arm()
    return True


def available() -> bool:
    return bool(_ready)


def _spawn() -> subprocess.Popen:
    return subprocess.Popen(
        [str(BINARY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"}, close_fds=True)


def _arm() -> None:
    """Keep one helper waiting with the microphone off."""
    global _armed
    with _lock:
        if _armed is not None and _armed.poll() is None:
            return
        try:
            _armed = _spawn()
        except OSError as exc:
            _armed = None
            print(f"[dictate] could not start the microphone helper: {exc}",
                  file=sys.stderr)


def _take() -> subprocess.Popen:
    global _armed
    with _lock:
        proc, _armed = _armed, None
    if proc is None or proc.poll() is not None:
        proc = _spawn()
    return proc


def shutdown() -> None:
    """Kill the waiting helper (it also exits by itself when Dictate does)."""
    global _armed
    with _lock:
        proc, _armed = _armed, None
    if proc is not None:
        proc.kill()


class MicStream:
    def __init__(self, callback) -> None:
        self._callback = callback
        self._proc: subprocess.Popen | None = None
        self._first_audio = threading.Event()
        self._stderr: list[str] = []

    def start(self) -> None:
        proc = self._proc = _take()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name="dictate-mic-log").start()
        threading.Thread(target=self._read_audio, args=(proc,), daemon=True,
                         name="dictate-mic").start()
        try:
            proc.stdin.write(b"s")  # switch the microphone on
            proc.stdin.flush()
        except OSError as exc:
            self.abort()
            raise RuntimeError(f"microphone helper is not running ({exc})") from exc
        threading.Thread(target=_arm, daemon=True, name="dictate-mic-arm").start()
        deadline = time.monotonic() + FIRST_AUDIO_TIMEOUT_S
        while not self._first_audio.wait(0.05):
            if proc.poll() is not None or time.monotonic() > deadline:
                detail = " ".join(self._stderr[-2:]) or "no audio arrived"
                self.abort()
                raise RuntimeError(f"microphone did not start: {detail}")

    def _read_audio(self, proc: subprocess.Popen) -> None:
        while True:
            data = proc.stdout.read(_BLOCK_BYTES)
            if not data:
                return
            if proc is not self._proc:
                return  # stopped: nothing more may reach the recording
            self._first_audio.set()
            frames = np.frombuffer(data[:len(data) // 2 * 2], dtype=np.int16)
            try:
                self._callback(frames.reshape(-1, 1), len(frames), None, None)
            except Exception as exc:
                print(f"[dictate] audio handler failed: {exc!r}", file=sys.stderr)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            self._stderr = (self._stderr
                            + [raw.decode("utf-8", "replace").strip()])[-5:]

    def abort(self) -> None:
        """Stop now. Never blocks: the helper is killed, which releases the mic."""
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill()

    stop = abort

    def close(self) -> None:
        self.abort()


def helper_path() -> Path:
    return BINARY
