"""Speech-to-text on this laptop with faster-whisper (optional).

Nothing leaves the laptop. Slower and less accurate than Groq on most
laptops (the small model can mishear whole phrases), so it is the choice
for privacy, and the automatic fallback when there is no internet.

Installed only when asked for (install.ps1 -WithLocal), because it adds
about 200 MB of libraries plus a ~500 MB model downloaded on first use.
"""

import importlib.util
import os
import sys
import threading
import time

import numpy as np

from dictate.transcriber import is_garbage_segment, trim_silence

IDLE_UNLOAD_S = 600


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


class LocalEngine:
    def __init__(self, model_name: str = "small.en") -> None:
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()
        self._last_used = 0.0

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            threads = max(1, min(4, (os.cpu_count() or 2) // 2))
            print(f"[dictate] loading the local speech model {self.model_name} "
                  "(downloads once, ~500 MB)")
            self._model = WhisperModel(self.model_name, device="cpu",
                                       compute_type="int8", cpu_threads=threads)
        return self._model

    def preload(self) -> None:
        threading.Thread(target=self._preload, daemon=True,
                         name="dictate-local-preload").start()

    def _preload(self) -> None:
        try:
            with self._lock:
                self._load()
                self._last_used = time.monotonic()
        except Exception as exc:
            print(f"[dictate] local model could not load: {exc!r}", file=sys.stderr)

    def transcribe(self, samples: np.ndarray, prompt: str | None,
                   language: str | None) -> str:
        audio = trim_silence(np.asarray(samples, dtype=np.float32))
        if len(audio) == 0:
            return ""
        with self._lock:
            model = self._load()
            segments, _info = model.transcribe(
                audio, language=language or None, initial_prompt=prompt,
                condition_on_previous_text=False, beam_size=1, temperature=0.0,
                vad_filter=False)
            found = [{"text": s.text, "avg_logprob": s.avg_logprob,
                      "compression_ratio": s.compression_ratio} for s in segments]
            self._last_used = time.monotonic()
        kept = [s["text"] for s in found if not is_garbage_segment(s)]
        if len(kept) < len(found):
            print(f"[dictate] dropped {len(found) - len(kept)} noise segment(s)")
        return "".join(kept).strip()

    def unload_if_idle(self) -> None:
        with self._lock:
            if self._model is not None and time.monotonic() - self._last_used > IDLE_UNLOAD_S:
                self._model = None
                print("[dictate] local speech model unloaded (reloads when needed)")
