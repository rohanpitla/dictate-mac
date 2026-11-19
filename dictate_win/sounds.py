"""Short start / stop / error sounds.

Windows has no built-in sounds that fit, so three tiny tones are generated
once into the data folder. Played asynchronously; a missing sound never
affects dictation.
"""

import math
import struct
import sys
import wave

from dictate_win import paths

_TONES = {
    "start": [(660, 0.06), (990, 0.08)],
    "stop": [(990, 0.06), (660, 0.08)],
    "error": [(220, 0.18)],
}
_RATE = 22_050


def _write(path, notes) -> None:
    frames = bytearray()
    for freq, seconds in notes:
        n = int(_RATE * seconds)
        for i in range(n):
            fade = min(1.0, i / 200, (n - i) / 200)  # no clicks
            frames += struct.pack("<h", int(9000 * fade * math.sin(2 * math.pi * freq * i / _RATE)))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_RATE)
        wf.writeframes(bytes(frames))


def prepare() -> None:
    try:
        paths.SOUNDS.mkdir(parents=True, exist_ok=True)
        for name, notes in _TONES.items():
            path = paths.SOUNDS / f"{name}.wav"
            if not path.exists():
                _write(path, notes)
    except OSError as exc:
        print(f"[dictate] could not prepare sounds: {exc}", file=sys.stderr)


def play(name: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import winsound

        path = paths.SOUNDS / f"{name}.wav"
        if path.exists():
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
    except Exception:
        pass
