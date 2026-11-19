"""Microphone capture process: writes 16 kHz mono 16-bit PCM to stdout.

Dictate runs this as a separate process and stops a recording by killing
it, which always works at once and always releases the microphone (an
in-process audio stream can hang while closing).

Protocol (the same as the Mac helper):
  1. Starts, loads the audio library, prints "armed" on stderr. The
     microphone is still OFF.
  2. Waits for one byte on stdin, then switches the microphone on.
  3. Streams audio to stdout until stdin closes (or it is killed).

Windows microphones often refuse 16 kHz, so it tries, in order:
  a. WASAPI at 16 kHz with Windows' own converter (auto_convert)
  b. WASAPI at the microphone's own rate, converted here
  c. MME at 16 kHz (the older audio system, which converts itself)
  d. the default device at 16 kHz (also what macOS uses, for tests)

Nothing is written to disk and nothing that was said is printed.
"""

import os
import queue
import sys
import threading
import time

RATE = 16_000
BLOCK = 800  # 50 ms


def log(message: str) -> None:
    try:
        sys.stderr.write(f"[mic] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _writer(blocks: "queue.Queue[bytes]") -> None:
    out = sys.stdout.buffer
    while True:
        data = blocks.get()
        try:
            out.write(data)
            out.flush()
        except (OSError, ValueError):
            os._exit(0)  # Dictate went away


def _watch_stdin() -> None:
    while True:
        try:
            if not sys.stdin.buffer.read(1):
                os._exit(0)
        except (OSError, ValueError):
            os._exit(0)


def _fake_source(blocks: "queue.Queue[bytes]") -> None:
    """--fake: a 440 Hz tone, for tests on machines without a microphone."""
    import numpy as np

    t = 0
    while True:
        n = np.arange(t, t + BLOCK)
        blocks.put((np.sin(2 * np.pi * 440 * n / RATE) * 8000).astype(np.int16).tobytes())
        t += BLOCK
        time.sleep(BLOCK / RATE)


def _open(sd, blocks: "queue.Queue[bytes]", only: str | None):
    from dictate_win.resample import Resampler, to_int16, to_mono

    def direct(indata, frames, time_info, status) -> None:
        blocks.put(bytes(indata))

    def attempts():
        apis = {api["name"]: api for api in sd.query_hostapis()}
        wasapi = apis.get("Windows WASAPI")
        if wasapi and wasapi["default_input_device"] >= 0:
            device = wasapi["default_input_device"]
            yield "wasapi-16k", dict(
                device=device, samplerate=RATE, channels=1, dtype="int16",
                blocksize=BLOCK, callback=direct,
                extra_settings=sd.WasapiSettings(auto_convert=True))
            info = sd.query_devices(device)
            rate = int(info["default_samplerate"])
            channels = max(1, min(2, int(info["max_input_channels"])))
            resampler = Resampler(rate, RATE)

            def converted(indata, frames, time_info, status) -> None:
                out = resampler.process(to_mono(indata))
                if len(out):
                    blocks.put(to_int16(out).tobytes())

            yield f"wasapi-native-{rate}", dict(
                device=device, samplerate=rate, channels=channels,
                dtype="float32", blocksize=int(rate * 0.05), callback=converted)
        mme = apis.get("MME")
        if mme and mme["default_input_device"] >= 0:
            yield "mme-16k", dict(device=mme["default_input_device"],
                                  samplerate=RATE, channels=1, dtype="int16",
                                  blocksize=BLOCK, callback=direct)
        yield "default-16k", dict(samplerate=RATE, channels=1, dtype="int16",
                                  blocksize=BLOCK, callback=direct)
        if only == "native":  # test path: force our own conversion
            info = sd.query_devices(kind="input")
            rate = int(info["default_samplerate"])
            resampler = Resampler(rate, RATE)

            def converted(indata, frames, time_info, status) -> None:
                out = resampler.process(to_mono(indata))
                if len(out):
                    blocks.put(to_int16(out).tobytes())

            yield f"native-{rate}", dict(samplerate=rate, channels=1,
                                         dtype="float32", blocksize=int(rate * 0.05),
                                         callback=converted)

    errors = []
    for name, kwargs in attempts():
        if only and not name.startswith(only):
            continue
        try:
            stream = sd.InputStream(**kwargs)
            stream.start()
            return name, stream
        except Exception as exc:  # PortAudioError: busy, unsupported, denied
            errors.append(f"{name}: {exc}")
    raise RuntimeError("; ".join(errors) or "no microphone found")


def main() -> int:
    fake = "--fake" in sys.argv
    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1]
    if not fake:
        import sounddevice as sd  # the slow part: done before "armed"
    log("armed")
    try:
        if not sys.stdin.buffer.read(1):
            return 0  # Dictate exited before we were needed
    except (OSError, ValueError):
        return 0
    blocks: "queue.Queue[bytes]" = queue.Queue()
    threading.Thread(target=_writer, args=(blocks,), daemon=True).start()
    threading.Thread(target=_watch_stdin, daemon=True).start()
    if fake:
        log("capturing via fake")
        _fake_source(blocks)
        return 0
    try:
        name, _stream = _open(sd, blocks, only)
    except Exception as exc:
        log(f"could not open the microphone: {exc}")
        return 1
    log(f"capturing via {name}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
