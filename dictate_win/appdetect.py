"""Pick a writing style from the app being typed into.

  email   -> complete sentences, paragraphs (Outlook, Gmail in a browser)
  casual  -> a chat message (Slack, Teams, WhatsApp, Discord, ...)
  code    -> paste exactly what was heard (terminals, code editors)
  default -> normal clean-up

On Windows a browser's window title includes the page title ("Inbox -
Gmail - Google Chrome"), so web apps are recognised without asking the
browser anything. Only the program name and window title are read, never
the window's contents.
"""

import ctypes

STYLE_HINTS = {
    "email": ("The speaker is writing an email: use complete sentences and "
              "proper punctuation, but add no greeting, sign-off, or words "
              "they did not say."),
    "casual": "The speaker is writing a casual chat message: keep it casual.",
}

PROGRAM_MODES = {
    "outlook.exe": "email", "olk.exe": "email", "thunderbird.exe": "email",
    "hxoutlook.exe": "email",
    "slack.exe": "casual", "ms-teams.exe": "casual", "teams.exe": "casual",
    "discord.exe": "casual", "whatsapp.exe": "casual", "telegram.exe": "casual",
    "signal.exe": "casual", "messenger.exe": "casual",
    "windowsterminal.exe": "code", "cmd.exe": "code", "powershell.exe": "code",
    "pwsh.exe": "code", "conhost.exe": "code", "wezterm-gui.exe": "code",
    "alacritty.exe": "code", "code.exe": "code", "cursor.exe": "code",
    "windsurf.exe": "code", "devenv.exe": "code", "idea64.exe": "code",
    "pycharm64.exe": "code", "webstorm64.exe": "code", "sublime_text.exe": "code",
    "notepad++.exe": "code", "zed.exe": "code",
}
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
            "vivaldi.exe", "arc.exe"}
TITLE_MODES = [("gmail", "email"), ("outlook", "email"), ("- mail", "email"),
               ("whatsapp", "casual"), ("messenger", "casual"), ("slack", "casual"),
               ("discord", "casual"), ("microsoft teams", "casual")]


def foreground() -> tuple[str, str]:
    """(program file name, window title) of the focused window, lowercase."""
    from ctypes import wintypes as w

    from dictate_win import winapi as api

    hwnd = api.GetForegroundWindow()
    if not hwnd:
        return "", ""
    title = ctypes.create_unicode_buffer(512)
    api.GetWindowTextW(hwnd, title, 512)
    pid = w.DWORD()
    api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    program = ""
    process = api.OpenProcess(api.PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if process:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = w.DWORD(1024)
            if api.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)):
                program = buf.value.rsplit("\\", 1)[-1]
        finally:
            api.CloseHandle(process)
    return program.lower(), title.value.lower()


def resolve_mode(program: str, title: str, overrides: dict | None = None) -> str:
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    if program in overrides and overrides[program] in ("email", "casual", "code", "default"):
        return overrides[program]
    if program in BROWSERS:
        for needle, mode in TITLE_MODES:
            if needle in title:
                return mode
        return "default"
    return PROGRAM_MODES.get(program, "default")
