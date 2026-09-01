"""Load config.toml from the project root, plus targeted setting writes."""

import re
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"

DEFAULTS = {
    "general": {"hotkey": "right_option", "debug": True,
                "mute_while_dictating": True, "raw_mode": False},
    "app_modes": {},
    "whisper": {"model": "mlx-community/whisper-large-v3-turbo"},
    "ollama": {"model": "qwen2.5:3b", "fallback_model": "llama3.2:3b"},
    "vocabulary": {},
}


def load() -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
    return cfg


def save_setting(section: str, key: str, value: str | bool) -> None:
    """Rewrite one `key = value` line inside [section], keeping comments.

    (tomllib is read-only, so this is a targeted text edit. Section-aware
    because e.g. `model` exists in both [whisper] and [ollama].)
    """
    if isinstance(value, bool):
        rendered = "true" if value else "false"  # unquoted TOML boolean
    else:
        rendered = f'"{value}"'
    try:
        text = CONFIG_PATH.read_text()
        # the section body runs until the next [header] or end of file
        section_re = re.compile(
            rf"(?ms)^(\[{re.escape(section)}\].*?)(?=^\[|\Z)")
        match = section_re.search(text)
        if not match:
            raise ValueError(f"no [{section}] section in config.toml")
        body = match.group(1)
        key_re = re.compile(rf'(?m)^{re.escape(key)}\s*=.*$')
        if not key_re.search(body):
            raise ValueError(f"no {key!r} key in [{section}]")
        new_body = key_re.sub(f'{key} = {rendered}', body, count=1)
        CONFIG_PATH.write_text(text[:match.start(1)] + new_body
                               + text[match.end(1):])
    except (OSError, ValueError) as exc:
        print(f"[dictate] could not persist {section}.{key}: {exc}",
              file=sys.stderr)
