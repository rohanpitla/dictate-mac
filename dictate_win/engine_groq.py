"""Speech-to-text and clean-up through Groq's OpenAI-compatible API.

What leaves the laptop: the audio of one dictation (to /audio/transcriptions)
and its text (to /chat/completions, for clean-up), only to the host in
settings.toml, only over HTTPS. The key is only ever sent to that host.

Errors carry a `kind` so the app can react the right way:
  auth     - the key was rejected: ask the user to fix it
  model    - the model name is unknown (Groq retires models): update settings
  rate     - free-tier limit reached: retry later
  network  - no internet / Groq unreachable / Groq error: retry later
  other    - anything else (not retried automatically)
Error messages never contain the key or anything that was said.
"""

import io
import time
import wave
from collections.abc import Callable
from urllib.parse import urlsplit

import numpy as np
import requests

from dictate.transcriber import is_garbage_segment, trim_silence

RATE = 16_000
PIECE_S = 240            # one request carries at most 4 minutes (~7.7 MB WAV)
MIN_PIECE_S = 30
CONNECT_TIMEOUT_S = 6
MAX_PROMPT_CHARS = 600   # Groq caps the prompt at 224 tokens
_LOCAL = {"localhost", "127.0.0.1", "::1"}


class EngineError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in ("network", "rate")


def validate_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if not parts.hostname:
        raise EngineError("other", "groq.base_url in settings.toml is not a valid address")
    if parts.username or parts.password:
        raise EngineError("other", "groq.base_url must not contain a username or password")
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOCAL):
        return url
    raise EngineError("other", "groq.base_url must start with https://")


def host_of(base_url: str) -> str:
    return urlsplit(validate_base_url(base_url)).hostname.lower()


def split_audio(audio: np.ndarray, max_s: float = PIECE_S) -> list[np.ndarray]:
    """Cut long audio into pieces of at most max_s seconds, each cut at the
    quietest moment in the last fifth of the piece, never mid-word."""
    limit = int(max_s * RATE)
    pieces = []
    start = 0
    frame = 480  # 30 ms
    while len(audio) - start > limit:
        window_start = start + int(limit * 0.8)
        window = audio[window_start:start + limit]
        n = len(window) // frame
        energy = np.sqrt(np.mean(window[:n * frame].reshape(n, frame) ** 2, axis=1))
        cut = window_start + int(np.argmin(energy)) * frame + frame // 2
        pieces.append(audio[start:cut])
        start = cut
    pieces.append(audio[start:])
    return [p for p in pieces if len(p)]


def to_wav(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def _session() -> requests.Session:
    session = requests.Session()
    # Ignore proxy, certificate-bundle and .netrc settings from the
    # environment: nothing outside settings.toml can re-route a request.
    session.trust_env = False
    return session


class GroqClient:
    def __init__(self, cfg: dict, key_getter: Callable[[], str | None],
                 session: requests.Session | None = None) -> None:
        self._cfg = cfg
        self._key_getter = key_getter
        self._session = session or _session()

    @property
    def base(self) -> str:
        return validate_base_url(self._cfg.get("base_url", ""))

    @property
    def host(self) -> str:
        return host_of(self._cfg.get("base_url", ""))

    def has_key(self) -> bool:
        try:
            return bool(self._key_getter())
        except Exception:
            return False

    # ---------------------------------------------------------- requests --
    def _post(self, path: str, read_timeout: float, **kwargs) -> dict:
        key = self._key_getter()
        if not key:
            raise EngineError("auth", "no Groq API key saved (tray menu > Set Groq API key…)")
        url = f"{self.base}{path}"
        last: EngineError | None = None
        for attempt in (1, 2):
            try:
                response = self._session.post(
                    url, headers={"Authorization": f"Bearer {key}"},
                    timeout=(CONNECT_TIMEOUT_S, read_timeout),
                    allow_redirects=False,  # a redirect would carry the key elsewhere
                    **kwargs)
            except requests.RequestException as exc:
                last = EngineError("network", f"could not reach Groq ({type(exc).__name__})")
                if attempt == 1:
                    time.sleep(1.0)
                continue
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise EngineError("other", "Groq sent a reply that could not be read") from None
            if status in (401, 403):
                raise EngineError("auth", "Groq rejected the API key (check it in the tray menu)")
            if status == 404:
                raise EngineError("model", "Groq doesn't recognise the model name; it may "
                                           "have been retired. Update the model in settings.toml")
            if status == 413:
                raise EngineError("too_large", "the recording piece was too large for Groq")
            if 300 <= status < 400:
                raise EngineError("other", "Groq redirected the request; refused for safety")
            if status == 429:
                try:
                    wait = float(response.headers.get("retry-after", "0"))
                except ValueError:
                    wait = 0.0
                if attempt == 1 and 0 < wait <= 10:
                    time.sleep(wait)
                    last = EngineError("rate", "Groq's free limit was reached", wait)
                    continue
                raise EngineError("rate", "Groq's free limit was reached; it will retry", wait)
            if status >= 500:
                last = EngineError("network", f"Groq had a problem (HTTP {status})")
                if attempt == 1:
                    time.sleep(1.0)
                continue
            raise EngineError("other", f"Groq refused the request (HTTP {status})")
        raise last or EngineError("network", "could not reach Groq")

    # ------------------------------------------------------ transcription --
    def transcribe(self, samples: np.ndarray, prompt: str | None,
                   language: str | None) -> str:
        """float32 16 kHz mono -> text. Raises EngineError."""
        audio = trim_silence(np.asarray(samples, dtype=np.float32))
        if len(audio) == 0:
            return ""
        texts = []
        for piece in split_audio(audio):
            texts.append(self._transcribe_piece(piece, prompt, language, PIECE_S))
        return " ".join(t for t in texts if t).strip()

    def _transcribe_piece(self, piece, prompt, language, max_s) -> str:
        fields = {"model": self._cfg.get("transcribe_model", "whisper-large-v3-turbo"),
                  "response_format": "verbose_json", "temperature": "0"}
        if language:
            fields["language"] = language
        if prompt:
            fields["prompt"] = prompt[:MAX_PROMPT_CHARS]
        seconds = len(piece) / RATE
        try:
            body = self._post("/audio/transcriptions", 30 + seconds / 4,
                              files={"file": ("dictation.wav", to_wav(piece), "audio/wav")},
                              data=fields)
        except EngineError as exc:
            if exc.kind == "too_large" and max_s / 2 >= MIN_PIECE_S:
                return " ".join(self._transcribe_piece(p, prompt, language, max_s / 2)
                                for p in split_audio(piece, max_s / 2)).strip()
            if exc.kind == "too_large":
                raise EngineError("other", "the recording was too large for Groq") from exc
            raise
        segments = body.get("segments")
        if isinstance(segments, list) and segments:
            kept = [s.get("text", "") for s in segments
                    if isinstance(s, dict) and not is_garbage_segment(s)]
            return "".join(kept).strip()
        return str(body.get("text", "")).strip()

    # --------------------------------------------------------------- chat --
    def chat(self, messages: list[dict], max_tokens: int) -> tuple[str, str]:
        """-> (reply text, finish_reason)."""
        model = self._cfg.get("cleanup_model", "openai/gpt-oss-20b")
        body = {"model": model, "messages": messages, "temperature": 0,
                "max_completion_tokens": max_tokens, "stream": False}
        if model.startswith("openai/gpt-oss"):
            # Reasoning models think before answering; clean-up needs
            # little of it, and the thinking must not reach the text.
            body["reasoning_effort"] = "low"
            body["include_reasoning"] = False
        reply = self._post("/chat/completions", 30, json=body)
        try:
            choice = reply["choices"][0]
            return str(choice["message"].get("content") or ""), str(choice.get("finish_reason") or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise EngineError("other", "Groq sent a reply in an unexpected format") from None
