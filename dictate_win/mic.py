"""Microphone stream backed by the mic_child process.

Offers the part of sounddevice.InputStream that dictate.recorder.Recorder
uses: start(), abort(), close(), and a callback receiving int16 frames.

One child is kept "armed" (started, microphone off) so recording begins
without waiting for Python to launch, which takes about a second on Windows.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from dictate_win.paths import APP_ROOT

BLOCK_BYTES = 1600          # 50 ms of 16 kHz int16
# Bluetooth headsets switch to their microphone profile when it opens,
# which can take a couple of seconds.
FIRST_AUDIO_TIMEOUT_S = 4.0

_lock = threading.Lock()
_armed: subprocess.Popen | None = None
extra_args: list[str] = []  # tests: ["--fake"] or ["--only=native"]


def _python() -> str:
    exe = Path(sys.executable)
    # pythonw.exe has no console; the child must have working pipes, so
    # use python.exe and tell Windows not to show a console window.
    console = exe.with_name("python.exe")
    return str(console if exe.name.lower() == "pythonw.exe" and console.exists() else exe)


def _spawn() -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(APP_ROOT)
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    return subprocess.Popen(
        [_python(), "-s", "-m", "dictate_win.mic_child", *extra_args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(APP_ROOT), env=env, creationflags=flags, close_fds=True)


def arm() -> None:
    """Keep one child waiting with the microphone off."""
    global _armed
    with _lock:
        if _armed is not None and _armed.poll() is None:
            return
        try:
            _armed = _spawn()
        except OSError as exc:
            _armed = None
            print(f"[dictate] could not start the microphone process: {exc}",
                  file=sys.stderr)


def _take() -> subprocess.Popen:
    global _armed
    with _lock:
        proc, _armed = _armed, None
    if proc is None or proc.poll() is not None:
        proc = _spawn()
    return proc


def _end(proc: subprocess.Popen) -> None:
    for step in (proc.kill, proc.stdin.close):
        try:
            step()
        except (OSError, ValueError):
            pass


def shutdown() -> None:
    global _armed
    with _lock:
        proc, _armed = _armed, None
    if proc is not None:
        _end(proc)


class MicStream:
    def __init__(self, callback) -> None:
        self._callback = callback
        self._proc: subprocess.Popen | None = None
        self._first_audio = threading.Event()
        self.stderr: list[str] = []
        self.via = ""

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
            raise RuntimeError(f"the microphone process is not running ({exc})") from exc
        threading.Thread(target=arm, daemon=True, name="dictate-mic-arm").start()
        deadline = time.monotonic() + FIRST_AUDIO_TIMEOUT_S
        while not self._first_audio.wait(0.05):
            if proc.poll() is not None or time.monotonic() > deadline:
                time.sleep(0.1)  # let the last error line arrive
                detail = " ".join(self.stderr[-2:]) or "no audio arrived"
                self.abort()
                raise RuntimeError(f"microphone did not start: {detail}")

    def _read_audio(self, proc: subprocess.Popen) -> None:
        while True:
            data = proc.stdout.read(BLOCK_BYTES)
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
            line = raw.decode("utf-8", "replace").strip()
            self.stderr = (self.stderr + [line])[-5:]
            if line.startswith("[mic] capturing via "):
                self.via = line.rsplit(" ", 1)[-1]

    def abort(self) -> None:
        """Stop now. Never blocks: killing the process releases the mic.

        In a virtual environment on Windows, python.exe is a small launcher
        that runs the real interpreter as a second process. Killing the
        launcher normally takes the interpreter with it; closing its input
        as well makes sure: the child exits the moment its stdin closes.
        """
        proc, self._proc = self._proc, None
        if proc is not None:
            _end(proc)

    stop = abort

    def close(self) -> None:
        self.abort()
