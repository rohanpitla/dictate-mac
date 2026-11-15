"""Cut live audio into utterances and hold the meeting transcript in memory."""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
_FRAME = 480  # 30 ms


class Chunker:
    """Feed it audio as it arrives; it calls `on_chunk(samples, started_at)`
    each time the speaker pauses, or every `max_s` if they don't.

    Audio with no speech in it is thrown away, so silence costs nothing and
    Whisper is never asked to invent words for it.
    """

    def __init__(self, on_chunk: Callable[[np.ndarray, float], None],
                 base_threshold: float = 180.0, pause_s: float = 0.75,
                 max_s: float = 20.0, min_speech_s: float = 0.35) -> None:
        self._on_chunk = on_chunk
        self._base = base_threshold
        self._pause_frames = int(pause_s * SAMPLE_RATE / _FRAME)
        self._max_frames = int(max_s * SAMPLE_RATE / _FRAME)
        self._min_speech = int(min_speech_s * SAMPLE_RATE / _FRAME)
        self._floor = base_threshold / 3  # running estimate of room noise
        self._rest = np.zeros(0, dtype=np.int16)
        self._pre: deque = deque(maxlen=10)  # 300 ms kept before speech starts
        self._frames: list[np.ndarray] = []
        self._speech = 0
        self._silence = 0
        self._started_at = 0.0
        self.last_loud_at = 0.0  # when this stream last carried speech

    def feed(self, samples: np.ndarray, muted: bool = False) -> None:
        """samples: int16 mono. `muted` treats the audio as silence (used to
        stop the speakers' sound being transcribed again from the mic)."""
        data = np.concatenate([self._rest, samples.reshape(-1)])
        usable = len(data) // _FRAME * _FRAME
        self._rest = data[usable:]
        for start in range(0, usable, _FRAME):
            self._frame(data[start:start + _FRAME], muted)

    def _frame(self, frame: np.ndarray, muted: bool) -> None:
        rms = 0.0 if muted else float(
            np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        if rms < self._floor:
            self._floor = max(rms, 1.0)
        else:
            # Capped, so long unbroken speech (or music) can never raise the
            # bar until the speech itself counts as silence.
            self._floor = min(self._floor + (rms - self._floor) * 0.002,
                              self._base)
        loud = rms > max(self._base, self._floor * 3)
        if loud:
            self.last_loud_at = time.monotonic()
        if not self._frames:
            if not loud:
                self._pre.append(frame)
                return
            self._started_at = time.time()
            self._frames = list(self._pre)
            self._pre.clear()
        self._frames.append(frame)
        if loud:
            self._speech += 1
            self._silence = 0
        else:
            self._silence += 1
        if (self._silence >= self._pause_frames
                or len(self._frames) >= self._max_frames):
            self._emit()

    def _emit(self) -> None:
        frames, speech = self._frames, self._speech
        self._frames, self._speech, self._silence = [], 0, 0
        if speech < self._min_speech:
            return  # a cough or a click, not speech
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        self._on_chunk(audio, self._started_at)

    def flush(self) -> None:
        if self._frames:
            self._emit()


@dataclass
class Line:
    at: float
    speaker: str  # "You" or "Them"
    text: str


class Transcript:
    """The meeting so far. Memory only; `wipe()` clears it when the meeting ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[Line] = []

    def add(self, speaker: str, text: str, at: float | None = None) -> None:
        with self._lock:
            self._lines.append(Line(at or time.time(), speaker, text))
            self._lines.sort(key=lambda line: line.at)

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)

    def lines(self, start: int = 0) -> list[Line]:
        with self._lock:
            return list(self._lines[start:])

    def render(self, start: int = 0, max_chars: int | None = None,
               stamps: bool = False) -> str:
        rows = []
        for line in self.lines(start):
            stamp = time.strftime("[%H:%M] ", time.localtime(line.at)) if stamps else ""
            rows.append(f"{stamp}{line.speaker}: {line.text}")
        text = "\n".join(rows)
        if max_chars and len(text) > max_chars:
            text = text[-max_chars:]
            text = text[text.find("\n") + 1:]  # start on a whole line
        return text

    def wipe(self) -> None:
        with self._lock:
            self._lines.clear()
