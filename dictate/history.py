"""Transcription history: plain JSONL log with timestamps, last 20 viewable."""

import json
import sys
import threading
from datetime import datetime

from dictate.config import PROJECT_ROOT

HISTORY_PATH = PROJECT_ROOT / "history.jsonl"
_lock = threading.Lock()


def append(raw: str, text: str, stage: str, latency_ms: dict[str, float]) -> None:
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "raw": raw,
        "text": text,
        "stage": stage,
        "latency_ms": {k: round(v) for k, v in latency_ms.items()},
    }
    try:
        with _lock, open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"[dictate] could not write history: {exc}", file=sys.stderr)


def last(n: int = 20) -> list[dict]:
    """Most recent n entries, newest first."""
    if not HISTORY_PATH.exists():
        return []
    entries = []
    with _lock, open(HISTORY_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a corrupt line rather than crash
    return entries[-n:][::-1]
