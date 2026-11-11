"""Microphone capture: 16 kHz mono int16, written to disk while recording.

Audio goes straight to a file as it arrives, so a freeze, crash or restart
never loses a dictation — the file is picked up again on the next launch.
Only the newest KEEP_RECORDINGS files are kept.
"""

import threading
import time
from pathlib import Path

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
MIN_DURATION_S = 0.3  # discard accidental taps
KEEP_RECORDINGS = 5
OPEN_TIMEOUT_S = 5.0
STALL_S = 3.0  # no audio for this long while recording = the mic died

RECORDINGS_DIR = (Path.home() / "Library" / "Application Support" / "Dictate"
                  / "recordings")
# A recording is "<timestamp>.pcm" until its text is safely in history, then
# "<timestamp>.done.pcm". Anything not marked done at launch gets recovered.
_DONE_SUFFIX = ".done.pcm"


class MicrophoneError(Exception):
    """Could not open the microphone (device missing or permission denied)."""


def _default_stream(callback):
    import sounddevice as sd

    # 50 ms blocks: ~20 callbacks a second instead of ~1,000
    return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                          blocksize=800, callback=callback)


def duration_s(path: Path) -> float:
    return path.stat().st_size / BYTES_PER_SECOND


def is_done(path: Path) -> bool:
    return path.name.endswith(_DONE_SUFFIX)


def mark_done(path: Path) -> Path:
    """Mark a recording as fully processed (its text is in history)."""
    if is_done(path) or not path.exists():
        return path
    done = path.with_name(path.name[:-len(".pcm")] + _DONE_SUFFIX)
    path.rename(done)
    return done


def saved_recordings(directory: Path | None = None) -> list[Path]:
    """All saved recordings, oldest first."""
    return sorted((directory or RECORDINGS_DIR).glob("*.pcm"))


def unfinished_recordings(directory: Path | None = None) -> list[Path]:
    return [p for p in saved_recordings(directory) if not is_done(p)]


def prune(directory: Path | None = None, keep: int = KEEP_RECORDINGS) -> None:
    """Delete all but the newest `keep` recordings."""
    for old in saved_recordings(directory)[:-keep]:
        old.unlink(missing_ok=True)


class Recorder:
    def __init__(self, directory: Path | None = None,
                 stream_factory=_default_stream) -> None:
        self._dir = directory or RECORDINGS_DIR
        self._stream_factory = stream_factory
        self._lock = threading.Lock()
        self._stream = None
        self._file = None
        self._path: Path | None = None
        self._token: object | None = None
        self._started_at = 0.0
        self._last_audio_at = 0.0
        self._closer: threading.Thread | None = None

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at if self.recording else 0.0

    @property
    def stalled(self) -> bool:
        """True if the mic has stopped delivering audio mid-recording."""
        return (self.recording
                and time.monotonic() - self._last_audio_at > STALL_S)

    def _on_audio(self, token, indata) -> None:
        with self._lock:
            # The token ties a stream to its own recording: an old stream
            # that is slow to shut down must not write into the next file.
            if self._file is not None and token is self._token:
                self._file.write(indata.tobytes())
                self._file.flush()
                self._last_audio_at = time.monotonic()

    def start(self) -> None:
        if self._stream is not None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        path = self._dir / f"{stamp}.pcm"

        # Opening the mic can hang if the audio system is in a bad state
        # (device vanished, previous stream still shutting down), so it runs
        # on its own thread with a deadline.
        result: dict = {}

        def open_stream() -> None:
            try:
                stream = self._stream_factory(
                    lambda indata, *_: self._on_audio(token, indata))
                stream.start()
                if result.get("gave_up"):  # opened too late; don't hold the mic
                    stream.abort()
                    stream.close()
                    return
                result["stream"] = stream
            except Exception as exc:  # PortAudioError, permission denied, no device
                result["error"] = exc

        token = object()
        with self._lock:
            self._file = open(path, "wb")
            self._path = path
            self._token = token
            self._started_at = self._last_audio_at = time.monotonic()
        opener = threading.Thread(target=open_stream, daemon=True,
                                  name="dictate-mic-open")
        opener.start()
        opener.join(OPEN_TIMEOUT_S)
        if "stream" not in result:
            result["gave_up"] = True
            self._discard_file()
            error = result.get("error") or f"no response after {OPEN_TIMEOUT_S:.0f}s"
            raise MicrophoneError(str(error))
        self._stream = result["stream"]

    def _discard_file(self) -> None:
        with self._lock:
            file, self._file = self._file, None
            path, self._path = self._path, None
        if file is not None:
            file.close()
        if path is not None:
            path.unlink(missing_ok=True)

    def stop(self) -> Path | None:
        """Stop and return the recording's path, or None if too short / empty.

        Never blocks: the audio is already on disk, and the stream is shut
        down on a background thread (a vanished mic can make that hang).
        """
        if self._stream is None:
            return None
        stream, self._stream = self._stream, None
        with self._lock:
            file, self._file = self._file, None
            path, self._path = self._path, None
        file.close()

        def close_stream() -> None:
            try:
                stream.abort()  # unlike stop(), doesn't wait for the device
                stream.close()
            except Exception as exc:
                print(f"[dictate] closing the mic stream failed: {exc}")

        self._closer = threading.Thread(target=close_stream, daemon=True,
                                        name="dictate-mic-close")
        self._closer.start()

        if duration_s(path) < MIN_DURATION_S:
            path.unlink(missing_ok=True)
            return None
        return path
