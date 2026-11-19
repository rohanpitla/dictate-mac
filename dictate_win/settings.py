"""settings.toml: created with sensible defaults on first run, edited by
the tray menu or by hand. The API key is never stored here (it lives in
Windows Credential Manager)."""

import re
import sys
import tomllib

from dictate_win import paths

HOTKEYS = {
    "right_ctrl": "Right Ctrl",
    "copilot": "Copilot key",
    "scroll_lock": "Scroll Lock",
    "f9": "F9",
}
ENGINES = {"groq": "Groq (cloud, best quality)",
           "local": "On this laptop (private, slower)"}

DEFAULTS = {
    "general": {
        "hotkey": "right_ctrl",
        "engine": "groq",
        "language": "en",
        "mute_while_dictating": True,
        "raw_mode": False,
        "smart_formatting": True,
        "layout_style": "points",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "transcribe_model": "whisper-large-v3-turbo",
        "cleanup": True,
        "cleanup_model": "openai/gpt-oss-20b",
    },
    "local": {"model": "small.en"},
    "app_modes": {},
    "vocabulary": {},
}

TEMPLATE = """# Dictate settings. Most of these can also be changed from the tray menu.
# Your API key is NOT stored here: use the tray menu > "Set Groq API key…".

[general]
# Tap once to start dictating, tap again to stop:
# "right_ctrl", "copilot", "scroll_lock" or "f9"
hotkey = "right_ctrl"
# "groq": your voice is sent to Groq, which returns the text (best quality,
#         needs internet and a free Groq key)
# "local": everything stays on this laptop (needs the optional local engine;
#          slower and less accurate on most laptops)
engine = "groq"
# The language you speak ("en", "hi", "es", ...); "" detects it each time
language = "en"
# Mute other sound (music, videos) while you dictate; it comes back after
mute_while_dictating = true
# true pastes exactly what was heard, with no clean-up
raw_mode = false
# Lay out long dictations; "points" = bullet points, "paragraphs" = paragraphs
smart_formatting = true
layout_style = "points"

[groq]
base_url = "https://api.groq.com/openai/v1"
transcribe_model = "whisper-large-v3-turbo"
# Remove "um"s and fix small slips with a Groq chat model
cleanup = true
cleanup_model = "openai/gpt-oss-20b"

[local]
# "small.en" (better) or "base.en" (faster); downloaded on first use
model = "small.en"

# Program name (e.g. "notion.exe") -> "email", "casual", "code" or "default"
[app_modes]

# Words it mishears -> the right spelling, e.g.  "n 8 n" = "n8n"
[vocabulary]
"""


def load() -> dict:
    cfg = {section: dict(values) for section, values in DEFAULTS.items()}
    try:
        if not paths.SETTINGS.exists():
            paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            paths.SETTINGS.write_text(TEMPLATE, encoding="utf-8")
        with open(paths.SETTINGS, "rb") as f:
            user = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # A typo in settings.toml must not stop Dictate from starting.
        print(f"[dictate] settings.toml could not be read, using defaults "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return cfg
    for section, values in user.items():
        if isinstance(values, dict):
            cfg.setdefault(section, {}).update(values)
    if cfg["general"].get("hotkey") not in HOTKEYS:
        cfg["general"]["hotkey"] = "right_ctrl"
    if cfg["general"].get("engine") not in ENGINES:
        cfg["general"]["engine"] = "groq"
    return cfg


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_setting(section: str, key: str, value: str | bool) -> None:
    """Change one `key = value` line inside [section], keeping comments."""
    rendered = ("true" if value else "false") if isinstance(value, bool) \
        else _quote(str(value))
    # Keys like "n 8 n" (vocabulary) must be quoted, or the file stops
    # being valid TOML and every setting falls back to its default.
    shown = key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _quote(key)
    try:
        text = paths.SETTINGS.read_text(encoding="utf-8")
        match = re.search(rf"(?ms)^(\[{re.escape(section)}\].*?)(?=^\[|\Z)", text)
        if not match:
            text = text.rstrip("\n") + f"\n\n[{section}]\n{shown} = {rendered}\n"
        else:
            body = match.group(1)
            key_re = re.compile(rf"(?m)^{re.escape(shown)}\s*=.*$")
            if key_re.search(body):
                body = key_re.sub(lambda _m: f"{shown} = {rendered}", body, count=1)
            else:
                body = body.rstrip("\n") + f"\n{shown} = {rendered}\n\n"
            text = text[:match.start(1)] + body + text[match.end(1):]
        paths.SETTINGS.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"[dictate] could not save {section}.{key}: {exc}", file=sys.stderr)
