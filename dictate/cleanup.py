"""LLM cleanup of raw transcripts via local Ollama, plus vocabulary.

Contract (see docs/DESIGN.md):
- Ollama at localhost, temperature 0, primary qwen2.5:3b, fallback llama3.2:3b
- If Ollama isn't running or anything fails: return the raw transcript.
  The cleanup pass must never lose user content and never block dictation.
- Guardrails: discard LLM output if empty, >2x or <0.3x raw length, or it
  contains meta-text like "Here is".
- Custom vocabulary is applied AFTER cleanup, case-insensitively.
"""

import json
import re
import sys
import urllib.error
import urllib.request

OLLAMA_URL = "http://localhost:11434"
CONNECT_TIMEOUT_S = 30  # read timeout for generation; connection refusal fails instantly

SYSTEM_PROMPT = """You clean up raw speech-to-text transcripts. Rules:
- Remove filler words (um, uh, like, you know, I mean) and false starts
- Fix punctuation, capitalization, and obvious homophone errors
- Preserve the speaker's meaning, tone, and word choice — do NOT rephrase, summarize, shorten, or "improve" their writing
- Keep contractions and casual tone if the speaker used them
- Format lists as lists only if the speaker clearly dictated a list
- Output ONLY the cleaned text. No preamble, no quotes, no comments.

Example:
Input: um so basically the uh the client needs like a demo
Output: So basically, the client needs a demo."""

_META_PREFIXES = ("here is", "here's", "sure", "certainly", "the cleaned")


def _system_prompt(style_hint: str | None) -> str:
    if style_hint:
        return f"{SYSTEM_PROMPT}\n\nStyle hint for this transcript: {style_hint}"
    return SYSTEM_PROMPT


def _chat(model: str, raw: str, style_hint: str | None = None) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt(style_hint)},
            {"role": "user", "content": raw},
        ],
        "stream": False,
        "options": {"temperature": 0},
        # Keep the model resident; Ollama's 5-minute default unload made the
        # first dictation after a break stall for a multi-second reload.
        "keep_alive": -1,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_S) as resp:
        body = json.loads(resp.read())
    return body["message"]["content"].strip()


def passes_guardrails(raw: str, cleaned: str) -> bool:
    """The cleanup pass must never lose user content."""
    if not cleaned:
        return False
    ratio = len(cleaned) / max(len(raw), 1)
    if ratio > 2.0 or ratio < 0.3:
        return False
    if cleaned.lower().startswith(_META_PREFIXES):
        return False
    return True


def clean(raw: str, cfg: dict, style_hint: str | None = None) -> tuple[str, str]:
    """Return (text, stage) where stage is 'llm', 'guardrail' or 'raw'.

    Never raises; on any failure the raw transcript comes back unchanged.
    """
    if not raw.strip():
        return raw, "raw"
    for model in (cfg["ollama"]["model"], cfg["ollama"].get("fallback_model")):
        if not model:
            continue
        try:
            cleaned = _chat(model, raw, style_hint)
        except (urllib.error.URLError, OSError, TimeoutError, KeyError,
                json.JSONDecodeError) as exc:
            print(f"[dictate] ollama cleanup skipped ({model}: {exc})",
                  file=sys.stderr)
            continue
        if passes_guardrails(raw, cleaned):
            return cleaned, "llm"
        print(f"[dictate] cleanup output failed guardrails, using raw "
              f"transcript (got {cleaned[:60]!r})", file=sys.stderr)
        return raw, "guardrail"
    return raw, "raw"


def apply_vocabulary(text: str, vocabulary: dict[str, str]) -> str:
    """Case-insensitive replacement of misheard terms, applied after cleanup."""
    for wrong, right in vocabulary.items():
        pattern = re.escape(wrong)
        if wrong and wrong[0].isalnum():
            pattern = r"\b" + pattern
        if wrong and wrong[-1].isalnum():
            pattern = pattern + r"\b"
        text = re.sub(pattern, right, text, flags=re.IGNORECASE)
    return text


def clean_and_correct(raw: str, cfg: dict,
                      style_hint: str | None = None) -> tuple[str, str]:
    """Full cleanup pass: LLM (with guardrails) then vocabulary."""
    text, stage = clean(raw, cfg, style_hint)
    return apply_vocabulary(text, cfg.get("vocabulary", {})), stage


def warmup(cfg: dict) -> None:
    """Preload the model into Ollama's memory so the first dictation is fast."""
    try:
        _chat(cfg["ollama"]["model"], "hello")
        print("[dictate] ollama cleanup model warmed up and ready")
    except Exception as exc:
        print(f"[dictate] ollama not available, will paste raw transcripts "
              f"({exc})", file=sys.stderr)
