"""Run the Swift helper that captures what the Mac is playing.

The helper (helper/sysaudio.swift) writes 16 kHz mono 16-bit PCM to a pipe.
It opens no files and no network connections, reads no screen pixels, and
exits the moment Dictate closes the pipe or dies.
"""

import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from dictate.config import PROJECT_ROOT

HELPER_SOURCE = PROJECT_ROOT / "helper" / "sysaudio.swift"
HELPER_BINARY = PROJECT_ROOT / "helper" / "sysaudio"
_BLOCK_BYTES = 1600  # 50 ms

PERMISSION_HELP = (
    "Screen Recording permission is missing. Open System Settings > Privacy "
    "& Security > Screen & System Audio Recording, turn it on for the app you "
    "start Dictate from (e.g. Terminal), then quit and reopen that app.")


class SystemAudioError(Exception):
    pass


def _check_not_tampered(path: Path) -> None:
    """Refuse to build or run a helper another account could have replaced."""
    for target in (path, path.parent):
        info = target.stat()
        if info.st_uid != os.getuid():
            raise SystemAudioError(f"{target} is not owned by you — refusing to run it")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SystemAudioError(
                f"{target} is writable by other accounts — refusing to run it "
                f"(fix with: chmod go-w '{target}')")


def ensure_built() -> Path:
    return build_helper(HELPER_SOURCE, HELPER_BINARY)


def build_helper(source: Path, binary: Path) -> Path:
    """Compile a helper from source when missing or out of date.

    Each person builds it on their own Mac from readable source, so no
    unsigned binary is ever passed around.
    """
    HELPER_SOURCE, HELPER_BINARY = source, binary
    if not HELPER_SOURCE.exists():
        raise SystemAudioError(f"missing {HELPER_SOURCE}")
    _check_not_tampered(HELPER_SOURCE)
    stale = (not HELPER_BINARY.exists()
             or HELPER_BINARY.stat().st_mtime < HELPER_SOURCE.stat().st_mtime)
    if stale:
        print(f"[dictate] building helper {binary.name} (one time)…")
        try:
            result = subprocess.run(
                ["/usr/bin/swiftc", "-O", "-swift-version", "5",
                 str(HELPER_SOURCE), "-o", str(HELPER_BINARY)],
                capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemAudioError(
                "could not build the audio helper. Install Apple's command "
                f"line tools with: xcode-select --install ({exc})") from exc
        if result.returncode != 0:
            raise SystemAudioError(
                "could not build the audio helper. Install Apple's command "
                "line tools with: xcode-select --install\n"
                + result.stderr[-400:])
        HELPER_BINARY.chmod(0o700)
    _check_not_tampered(HELPER_BINARY)
    return HELPER_BINARY


class SystemAudio:
    def __init__(self, on_audio: Callable[[bytes], None],
                 command: list[str] | None = None) -> None:
        self._on_audio = on_audio
        self._command = command
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: list[str] = []
        self.got_audio = False

    def start(self) -> None:
        command = self._command or [str(ensure_built())]
        self._stderr_tail = []
        self.got_audio = False
        # A minimal environment: the helper needs nothing from ours.
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"},
            close_fds=True)
        proc = self._proc
        threading.Thread(target=self._read_audio, args=(proc,), daemon=True,
                         name="meeting-sysaudio").start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name="meeting-sysaudio-log").start()

    def _read_audio(self, proc: subprocess.Popen) -> None:
        while True:
            data = proc.stdout.read(_BLOCK_BYTES)
            if not data:
                return
            if proc is not self._proc:
                return  # a stopped helper must not feed a newer session
            self.got_audio = True
            try:
                self._on_audio(data)
            except Exception as exc:
                print(f"[meeting] audio handler failed: {exc!r}", file=sys.stderr)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            self._stderr_tail = (self._stderr_tail + [line])[-5:]
            print(f"[meeting] {line}")

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def failure(self) -> str:
        """Why the helper stopped, in words the user can act on."""
        tail = " ".join(self._stderr_tail)
        if "declined TCC" in tail or "not authorized" in tail.lower():
            return PERMISSION_HELP
        return tail or "the audio helper stopped unexpectedly"

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # the helper exits when its stdin closes
        except OSError:
            pass
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
