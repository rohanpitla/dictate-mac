"""Streaming sample-rate conversion to 16 kHz, numpy only.

Used when a microphone only runs at its own rate (usually 44.1 or 48 kHz).
A windowed-sinc low-pass filter removes what 16 kHz can't represent, then
linear interpolation picks the 16 kHz samples. State carries across blocks,
so a stream converted block by block equals the whole thing converted at
once (no clicks at block edges). Good enough for speech recognition, which
only needs up to 8 kHz.
"""

import numpy as np


class Resampler:
    def __init__(self, src_rate: int, dst_rate: int = 16_000, taps: int = 63) -> None:
        self.src_rate, self.dst_rate = int(src_rate), int(dst_rate)
        self.step = self.src_rate / self.dst_rate
        cutoff = 0.45 * self.dst_rate / self.src_rate  # cycles per source sample
        n = np.arange(taps) - (taps - 1) / 2
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        self._h = (h / h.sum()).astype(np.float32)
        self._history = np.zeros(taps - 1, dtype=np.float32)
        self._carry = np.zeros(0, dtype=np.float32)
        self._pos = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        """float32 mono at src_rate -> float32 mono at dst_rate."""
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        if self.src_rate == self.dst_rate:
            return block
        joined = np.concatenate([self._history, block])
        filtered = np.convolve(joined, self._h, mode="valid").astype(np.float32)
        self._history = joined[len(joined) - len(self._history):]
        y = np.concatenate([self._carry, filtered])
        last = len(y) - 1
        if last < self._pos:
            self._carry = y
            return np.zeros(0, dtype=np.float32)
        count = int((last - self._pos) // self.step) + 1
        positions = self._pos + self.step * np.arange(count)
        out = np.interp(positions, np.arange(len(y)), y).astype(np.float32)
        next_pos = self._pos + self.step * count
        # The next output can lie beyond the samples we have; then keep the
        # remaining distance in _pos rather than losing it.
        drop = min(int(next_pos), len(y))
        self._carry = y[drop:]
        self._pos = next_pos - drop
        return out


def to_int16(samples: np.ndarray) -> np.ndarray:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)


def to_mono(block: np.ndarray) -> np.ndarray:
    block = np.asarray(block, dtype=np.float32)
    return block.mean(axis=1) if block.ndim == 2 else block
