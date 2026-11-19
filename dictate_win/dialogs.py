"""Windows dialogs: the API key prompt, messages and confirmations.

Learned on the Mac: a tray app's dialogs can open behind other windows or
without the keyboard, which looks like nothing happened. Here every dialog
is topmost and takes focus, and the key prompt has a button that reads the
key straight from the clipboard.

Each dialog runs on the calling thread and blocks it; the app calls them
from a fresh thread so the tray icon stays responsive.
"""

import sys
import threading

_MB_OK, _MB_OKCANCEL = 0x0, 0x1
_MB_ICONINFO, _MB_ICONWARNING, _MB_ICONQUESTION = 0x40, 0x30, 0x20
_MB_TOPMOST, _MB_SETFOREGROUND = 0x40000, 0x10000
_IDOK = 1
_one_at_a_time = threading.Lock()


def message(title: str, text: str, warning: bool = False) -> None:
    if sys.platform != "win32":
        print(f"[dialog] {title}: {text}")
        return
    from dictate_win import winapi as api

    with _one_at_a_time:
        api.MessageBoxW(None, text, title, _MB_OK | _MB_TOPMOST | _MB_SETFOREGROUND
                        | (_MB_ICONWARNING if warning else _MB_ICONINFO))


def confirm(title: str, text: str) -> bool:
    if sys.platform != "win32":
        return False
    from dictate_win import winapi as api

    with _one_at_a_time:
        return api.MessageBoxW(None, text, title, _MB_OKCANCEL | _MB_ICONQUESTION
                               | _MB_TOPMOST | _MB_SETFOREGROUND) == _IDOK


def ask_key(host: str, has_saved: bool, read_clipboard, _auto=None) -> str | None:
    """Ask for an API key. Returns the key (may be "" = remove the saved
    one) or None if cancelled. `_auto(root, entry, buttons)` drives the
    dialog in tests."""
    import tkinter as tk

    result: dict = {"value": None}
    with _one_at_a_time:
        root = tk.Tk()
        root.title(f"Dictate — API key for {host}")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        pad = {"padx": 14, "pady": 6}
        intro = (f"Paste your API key for {host}.\nIt is kept in Windows "
                 "Credential Manager, never in a file, and only sent to "
                 f"{host}.")
        intro += ("\n\nA key is already saved. Leave this empty and click Save "
                  "to remove it." if has_saved else "")
        tk.Label(root, text=intro, justify="left", anchor="w").pack(fill="x", **pad)
        entry = tk.Entry(root, show="•", width=52)
        entry.pack(fill="x", **pad)
        status = tk.Label(root, text="", fg="#b00020", anchor="w")
        status.pack(fill="x", padx=14)

        def save(_event=None) -> None:
            result["value"] = entry.get().strip()
            root.destroy()

        def paste_and_save() -> None:
            try:
                text = (read_clipboard() or "").strip()
            except Exception:
                text = ""
            if not text:
                status.config(text="The clipboard is empty. Copy the key first.")
                return
            result["value"] = text
            root.destroy()

        def cancel(_event=None) -> None:
            result["value"] = None
            root.destroy()

        row = tk.Frame(root)
        row.pack(fill="x", **pad)
        buttons = {
            "save": tk.Button(row, text="Save", width=10, command=save),
            "paste": tk.Button(row, text="Paste from clipboard and save",
                               command=paste_and_save),
            "cancel": tk.Button(row, text="Cancel", width=10, command=cancel),
        }
        buttons["save"].pack(side="left")
        buttons["paste"].pack(side="left", padx=8)
        buttons["cancel"].pack(side="right")
        root.bind("<Return>", save)
        root.bind("<Escape>", cancel)
        root.protocol("WM_DELETE_WINDOW", cancel)

        root.update_idletasks()
        x = (root.winfo_screenwidth() - root.winfo_width()) // 2
        y = (root.winfo_screenheight() - root.winfo_height()) // 3
        root.geometry(f"+{x}+{y}")

        def take_focus() -> None:
            try:
                root.deiconify()
                root.lift()
                root.focus_force()
                entry.focus_set()
            except tk.TclError:
                pass  # already closed

        for delay in (10, 200, 600):  # Windows may refuse the first request
            root.after(delay, take_focus)
        if _auto is not None:
            root.after(300, lambda: _auto(root, entry, buttons))
        root.mainloop()
    return result["value"]
