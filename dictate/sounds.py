"""Start/stop/error feedback sounds via afplay (non-blocking)."""

import subprocess

_SOUNDS = {
    "start": "/System/Library/Sounds/Pop.aiff",
    "stop": "/System/Library/Sounds/Bottle.aiff",
    "latch": "/System/Library/Sounds/Purr.aiff",
    "error": "/System/Library/Sounds/Basso.aiff",
}


def play(name: str) -> None:
    path = _SOUNDS.get(name)
    if not path:
        return
    try:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass  # sound is nice-to-have; never let it break dictation
