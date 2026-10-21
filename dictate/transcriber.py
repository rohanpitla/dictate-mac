"""Local speech-to-text.

Primary: mlx-whisper (Metal, Apple Silicon).
Fallback per spec: faster-whisper small.en on CPU — used only if MLX fails.
"""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
_mlx_broken = False
_fallback_model = None
_lock = threading.Lock()
# Every MLX call runs on this one thread. MLX GPU streams are per-thread: a
# model loaded on one thread and used on another aborts the whole process
# ("There is no Stream(gpu, N) in current thread"). One thread also
# serializes preload, transcribe and unload.
_mlx_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictate-mlx")
_last_used = 0.0
# The turbo model holds ~2.1 GB of GPU memory. Free it after this long idle;
# reloading takes ~0.4 s and preload() starts it when recording begins.
IDLE_UNLOAD_S = 600


class TranscriptionError(Exception):
    pass


def _load_wav(wav_path: str):
    """Decode a 16 kHz mono 16-bit WAV to float32 ourselves.

    mlx-whisper shells out to ffmpeg when given a file path; passing the
    samples directly removes that dependency. Dictate's recorder always
    produces this exact format.
    """
    import wave

    import numpy as np

    with wave.open(wav_path) as wf:
        if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (16_000, 1, 2):
            raise ValueError(
                f"expected 16kHz mono 16-bit WAV, got {wf.getframerate()} Hz "
                f"{wf.getnchannels()} ch {wf.getsampwidth() * 8}-bit"
            )
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


_FRAME = 480  # 30 ms at 16 kHz
_PAD_FRAMES = 10  # keep 300 ms of context around speech
_MAX_GAP_FRAMES = 27  # pauses longer than ~0.8 s get shortened to this


def trim_silence(samples):
    """Drop leading/trailing silence and shorten long pauses.

    Tap-to-toggle recordings often hold many seconds of silence; Whisper
    hallucinates and repeats itself on it, which made transcription take
    15 s+ and paste duplicated text.
    """
    import numpy as np

    n = len(samples) // _FRAME
    if n == 0:
        return samples
    frames = samples[:n * _FRAME].reshape(n, _FRAME)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    threshold = max(0.004, float(np.percentile(rms, 10)) * 3)
    voiced = rms > threshold
    if not voiced.any():
        # No speech at all. Whisper would invent text here ("Thank you.",
        # or words from the vocabulary prompt), so give it nothing.
        return samples[:0]
    keep = np.zeros(n, dtype=bool)
    for i in np.flatnonzero(voiced):
        keep[max(0, i - _PAD_FRAMES):i + _PAD_FRAMES + 1] = True
    out, gap = [], 0
    for i in range(n):
        if keep[i]:
            gap = 0
            out.append(frames[i])
        else:
            gap += 1
            if out and gap <= _MAX_GAP_FRAMES:
                out.append(frames[i])
    return np.concatenate(out)


def vocabulary_prompt(vocabulary: dict[str, str]) -> str | None:
    """Whisper spells names and jargon right far more often when they appear
    in its initial prompt ("Claude Coat" -> "Claude Code", "Fastapi" -> "FastAPI")."""
    terms = list(dict.fromkeys(v for v in vocabulary.values() if v))
    return f"Vocabulary: {', '.join(terms)}." if terms else None


def _on_mlx_thread(fn, *args):
    return _mlx_thread.submit(fn, *args).result()


def _transcribe_mlx(wav_path: str, model: str, prompt: str | None,
                    language: str | None) -> str:
    audio = trim_silence(_load_wav(wav_path))
    if len(audio) == 0:
        return ""
    return _on_mlx_thread(_run_whisper, audio, model, prompt, language)


def is_garbage_segment(seg: dict) -> bool:
    """Whisper's own failure signals. On a cough, breathing or keyboard
    clicks it emits loops ("!!!!", "Vocabulary, Vocabulary…", CJK runs) or
    stock phrases ("Closed Captioning by"); real speech scores around
    avg_logprob -0.3 and compression_ratio ~1."""
    logprob = seg.get("avg_logprob", 0.0)
    return (logprob != logprob  # NaN
            or logprob < -1.0
            or seg.get("compression_ratio", 0.0) > 2.4)


def _run_whisper(audio, model: str, prompt: str | None,
                 language: str | None) -> str:
    import mlx.core as mx
    import mlx_whisper

    global _last_used
    try:
        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=model, initial_prompt=prompt,
            language=language,  # skipping detection halves the time
            # Feeding each window's text into the next lets one
            # hallucination snowball into the same sentence repeated.
            condition_on_previous_text=False,
            # No re-decoding at higher temperatures: on noise it retried
            # five times (11-16 s) only to produce garbage we drop anyway.
            temperature=0.0,
        )
    finally:
        _last_used = time.monotonic()
        mx.clear_cache()  # MLX otherwise keeps ~0.5 GB of buffers around
    kept = [s["text"] for s in result["segments"] if not is_garbage_segment(s)]
    return "".join(kept).strip()


def preload(model: str = _MLX_MODEL) -> None:
    """Load the Whisper model while the user is still speaking. Returns at
    once; the load runs on the MLX thread ahead of the transcription."""
    if not _mlx_broken:
        future = _mlx_thread.submit(_load_model, model)
        future.add_done_callback(
            lambda f: f.exception() and print(
                f"[dictate] whisper preload failed: {f.exception()}",
                file=sys.stderr))


def _load_model(model: str) -> None:
    import mlx.core as mx
    from mlx_whisper.transcribe import ModelHolder

    global _last_used
    ModelHolder.get_model(model, mx.float16)
    _last_used = time.monotonic()


def unload_if_idle() -> None:
    """Free the Whisper model's memory if it hasn't been used for a while."""
    _on_mlx_thread(_unload_if_idle)


def _unload_if_idle() -> None:
    import gc

    import mlx.core as mx
    from mlx_whisper.transcribe import ModelHolder

    if (ModelHolder.model is None
            or time.monotonic() - _last_used < IDLE_UNLOAD_S):
        return
    ModelHolder.model = None
    ModelHolder.model_path = None
    gc.collect()
    mx.clear_cache()
    print("[dictate] whisper model unloaded after idle (reloads on next tap)")


def _transcribe_fallback(wav_path: str) -> str:
    global _fallback_model
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "MLX failed and faster-whisper (the spec fallback) is not installed. "
            "Run: .venv/bin/pip install faster-whisper"
        ) from exc
    with _lock:
        if _fallback_model is None:
            _fallback_model = WhisperModel("small.en", device="cpu", compute_type="int8")
    segments, _info = _fallback_model.transcribe(wav_path)
    return " ".join(seg.text.strip() for seg in segments).strip()


def transcribe(wav_path: str, model: str = _MLX_MODEL,
               prompt: str | None = None, language: str | None = None) -> str:
    """Transcribe a WAV file. Raises TranscriptionError only if both engines fail.

    language: e.g. "en"; None auto-detects (slower, and noise can come out
    as another language).
    """
    global _mlx_broken
    if not _mlx_broken:
        try:
            return _transcribe_mlx(wav_path, model, prompt, language)
        except Exception as exc:
            _mlx_broken = True
            print(f"[dictate] mlx-whisper failed ({exc!r}); falling back to "
                  f"faster-whisper small.en on CPU", file=sys.stderr)
    try:
        return _transcribe_fallback(wav_path)
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(str(exc)) from exc


# Whisper's classic hallucinations on (near-)silent audio. Only dropped when
# the audio is also quiet — a real, audible "thank you" always pastes.
_HALLUCINATIONS = {"you", "thank you", "thanks for watching",
                   "thank you for watching", "bye", "the"}
_QUIET_RMS = 150.0  # int16 units; normal speech is several times this


def looks_like_hallucination(text: str, wav_path: str) -> bool:
    normalized = text.lower().strip(" .!?")
    if normalized not in _HALLUCINATIONS:
        return False
    import numpy as np

    samples = _load_wav(wav_path)  # float32 in [-1, 1]
    rms = float(np.sqrt(np.mean(samples**2))) * 32768.0
    return rms < _QUIET_RMS


def warmup(model: str = _MLX_MODEL) -> None:
    """Download + load the model ahead of the first dictation (call in a thread)."""
    try:
        _on_mlx_thread(_load_model, model)
        print("[dictate] whisper model warmed up and ready")
    except Exception as exc:
        print(f"[dictate] model warmup failed: {exc}", file=sys.stderr)
