"""Push-to-talk microphone capture: 16 kHz mono int16 -> temp WAV."""

import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
MIN_DURATION_S = 0.3  # discard accidental taps


class MicrophoneError(Exception):
    """Could not open the microphone (device missing or permission denied)."""


class Recorder:
    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._frames: list[np.ndarray] = []
        self._started_at: float = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at if self.recording else 0.0

    def start(self) -> None:
        if self._stream is not None:
            return
        self._frames = []

        def callback(indata, _frames, _time, _status):
            self._frames.append(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # PortAudioError, permission denied, no device
            self._stream = None
            raise MicrophoneError(str(exc)) from exc
        self._started_at = time.monotonic()

    def stop(self) -> Path | None:
        """Stop and return the WAV path, or None if too short / empty."""
        if self._stream is None:
            return None
        stream, self._stream = self._stream, None
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

        duration = time.monotonic() - self._started_at
        frames, self._frames = self._frames, []
        if duration < MIN_DURATION_S or not frames:
            return None

        audio = np.concatenate(frames)
        wav_path = Path(tempfile.gettempdir()) / f"dictate_{int(time.time() * 1000)}.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return wav_path
