"""Clean-up (remove "um"s, fix small slips) with a Groq chat model.

The prompt, examples, chunking and guardrails are the Mac version's
(dictate/cleanup.py), so both behave the same. Any doubt -> the words as
heard are pasted: clean-up must never lose, invent, or answer what was said.
"""

import re
import sys

from dictate.cleanup import _messages, passes_guardrails, split_chunks
from dictate_win.engine_groq import EngineError, GroqClient

_REASONING_HEADROOM = 1024  # reasoning tokens count toward the reply limit


class CloudCleanup:
    def __init__(self, client: GroqClient) -> None:
        self._client = client
        self.disabled_reason = ""  # set when the model is gone, until restart

    def clean(self, raw: str, style_hint: str | None = None) -> tuple[str, str]:
        """-> (text, stage); stage is llm / guardrail / raw / llm-partial.
        Never raises."""
        if not raw.strip() or self.disabled_reason:
            return raw, "raw"
        parts, stages = [], []
        for chunk in split_chunks(raw):
            if stages and stages[-1] == "raw":
                text, stage = chunk, "raw"  # the service is down: don't retry each chunk
            else:
                text, stage = self._clean_one(chunk, style_hint)
            parts.append(text)
            stages.append(stage)
        stage = stages[0] if len(set(stages)) == 1 else "llm-partial"
        return " ".join(parts), stage

    def _clean_one(self, raw: str, style_hint: str | None) -> tuple[str, str]:
        try:
            reply, finish = self._client.chat(
                _messages(raw, style_hint), len(raw) // 2 + 64 + _REASONING_HEADROOM)
        except EngineError as exc:
            if exc.kind in ("model", "auth"):
                self.disabled_reason = str(exc)
            print(f"[dictate] clean-up skipped ({exc})", file=sys.stderr)
            return raw, "raw"
        if finish == "length":
            print("[dictate] clean-up reply was cut off; using the words as heard",
                  file=sys.stderr)
            return raw, "guardrail"
        cleaned = re.sub(r"</?transcript>", "", reply).strip()
        if passes_guardrails(raw, cleaned):
            return cleaned, "llm"
        print(f"[dictate] clean-up failed the safety checks, using the words as "
              f"heard ({len(cleaned)} chars rejected)", file=sys.stderr)
        return raw, "guardrail"
