"""Phase 4: detect the frontmost app and pick a dictation mode.

Modes:
  email    -> style hint: professional but warm email prose
  casual   -> style hint: casual and concise
  code     -> skip the LLM pass entirely, paste raw
  default  -> normal cleanup, no hint

Built-in bundle-ID mappings can be extended/overridden in config.toml's
[app_modes] table (prefix match, longest prefix wins). Gmail in a browser is
detected by asking the browser for its active tab title via AppleScript —
the first use triggers a one-time Automation permission prompt; if denied,
browsers just get default cleanup.
"""

import subprocess
import sys

from AppKit import NSWorkspace

STYLE_HINTS = {
    "email": "Format as professional but warm email prose.",
    "casual": "Keep it casual and concise.",
}

# bundle-id prefix -> mode
DEFAULT_APP_MODES = {
    "com.apple.mail": "email",
    "com.tinyspeck.slackmacgap": "casual",
    "com.hnc.Discord": "casual",
    "com.apple.MobileSMS": "casual",  # Messages
    "com.apple.Terminal": "code",
    "com.googlecode.iterm2": "code",
    "com.mitchellh.ghostty": "code",
    "net.kovidgoyal.kitty": "code",
    "com.github.wez.wezterm": "code",
    "com.microsoft.VSCode": "code",
    "com.vscodium": "code",
    "com.apple.dt.Xcode": "code",
    "com.jetbrains.": "code",
    "dev.zed.Zed": "code",
    "com.sublimetext": "code",
}

# bundle id -> AppleScript application name (all speak Chrome's dictionary,
# except Safari which has its own)
_BROWSERS = {
    "com.google.Chrome": "Google Chrome",
    "com.brave.Browser": "Brave Browser",
    "com.microsoft.edgemac": "Microsoft Edge",
    "com.apple.Safari": "Safari",
}


def frontmost_bundle_id() -> str | None:
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.bundleIdentifier() if app else None
    except Exception:
        return None


# Browsers whose tab-title check failed (Automation permission denied, script
# error, timeout). Remembered for the session so ONE slow/failed check can't
# stall every subsequent dictation in that browser.
_tab_check_broken: set[str] = set()


def _active_tab_title(bundle_id: str) -> str:
    if bundle_id in _tab_check_broken:
        return ""
    app_name = _BROWSERS[bundle_id]
    if bundle_id == "com.apple.Safari":
        script = f'tell application "{app_name}" to get name of front document'
    else:
        script = (f'tell application "{app_name}" to get title of '
                  'active tab of front window')
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=1.5)
        if result.returncode != 0:
            raise OSError(result.stderr.strip()[:120])
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        _tab_check_broken.add(bundle_id)
        print(f"[dictate] Gmail detection disabled for {app_name} this session "
              f"({exc}). Grant Terminal automation access to {app_name} in "
              "System Settings -> Privacy & Security -> Automation to enable it.",
              file=sys.stderr)
        return ""


def resolve_mode(bundle_id: str | None, cfg: dict) -> tuple[str, str | None]:
    """Return (mode, style_hint) for the app that will receive the paste."""
    if not bundle_id:
        return "default", None
    modes = dict(DEFAULT_APP_MODES)
    modes.update(cfg.get("app_modes", {}))
    best = ""
    mode = "default"
    for prefix, m in modes.items():
        if bundle_id.startswith(prefix) and len(prefix) > len(best):
            best, mode = prefix, m
    if mode == "default" and bundle_id in _BROWSERS:
        if "gmail" in _active_tab_title(bundle_id).lower():
            mode = "email"
    if mode not in ("email", "casual", "code", "default"):
        print(f"[dictate] unknown app mode {mode!r} for {bundle_id}, "
              "using default", file=sys.stderr)
        mode = "default"
    return mode, STYLE_HINTS.get(mode)
