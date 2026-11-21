"""Checks that Groq still serves the models Dictate for Windows uses.

Groq retires models with a few weeks' notice. This runs on GitHub Actions
every week so a retirement is noticed before testers' clean-up stops working.

Reads the key from the GROQ_API_KEY environment variable (a GitHub Actions
secret). With no key it reports SKIP and exits 0. The key is never printed.

Usage: python windows/check_groq_models.py
"""

import ast
import json
import os
import sys
import urllib.request
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / "dictate_win" / "settings.py"


def wanted_models() -> list[str]:
    # Read DEFAULTS without importing the module (it expects Windows paths).
    tree = ast.parse(SETTINGS.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "DEFAULTS" for t in node.targets):
            groq = ast.literal_eval(node.value)["groq"]
            return [groq["transcribe_model"], groq["cleanup_model"]]
    raise SystemExit("DEFAULTS not found in dictate_win/settings.py")


def main() -> int:
    models = wanted_models()
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        print("SKIP: no GROQ_API_KEY secret set; cannot check", ", ".join(models))
        return 0
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {key}", "User-Agent": "dictate-ci"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            live = {m["id"] for m in json.load(resp)["data"]}
    except Exception as exc:  # never echo the request (it holds the key)
        print(f"FAIL: could not list Groq models ({type(exc).__name__})")
        return 1
    missing = [m for m in models if m not in live]
    for m in models:
        print(("FAIL: retired or renamed: " if m in missing else "PASS: available: ") + m)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
