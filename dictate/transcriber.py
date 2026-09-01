"""Local speech-to-text.

Primary: mlx-whisper (Metal, Apple Silicon).
Fallback per spec: faster-whisper small.en on CPU — used only if MLX fails.
"""

import sys
import threading

_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
_mlx_broken = False
_fallback_model = None
_lock = threading.Lock()


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


def _transcribe_mlx(wav_path: str, model: str) -> str:
    import mlx_whisper

    result = mlx_whisper.transcribe(_load_wav(wav_path), path_or_hf_repo=model)
    return result["text"].strip()


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


def transcribe(wav_path: str, model: str = _MLX_MODEL) -> str:
    """Transcribe a WAV file. Raises TranscriptionError only if both engines fail."""
    global _mlx_broken
    if not _mlx_broken:
        try:
            return _transcribe_mlx(wav_path, model)
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
    import tempfile
    import wave
    from pathlib import Path

    silence = Path(tempfile.gettempdir()) / "dictate_warmup.wav"
    with wave.open(str(silence), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16_000)
        wf.writeframes(b"\x00\x00" * 8000)  # 0.5 s of silence
    try:
        transcribe(str(silence), model)
        print("[dictate] whisper model warmed up and ready")
    except Exception as exc:
        print(f"[dictate] model warmup failed: {exc}", file=sys.stderr)
