"""Put text where the cursor is: clipboard + Ctrl+V, then give the user
their old clipboard back.

Details that matter on Windows:
- The clipboard needs an owner window whose thread runs a message loop.
  Owning it from a thread that doesn't pump messages can freeze OTHER apps
  when they next clear the clipboard. So a small hidden window on its own
  thread owns it.
- Windows' clipboard history (Win+V) and cloud clipboard keep everything
  that passes through the clipboard. Our writes are marked so they are left
  out of both, so dictations don't pile up there or sync to other devices.
- If Shift/Alt/Win is still held, Ctrl+V becomes another shortcut, so we
  wait (briefly) for the keyboard to be free.
- An app running as administrator can't receive keystrokes from a normal
  app. Then the text is left on the clipboard and the caller tells the user
  to press Ctrl+V.
"""

import ctypes
import threading
import time

RESTORE_DELAY_S = 0.5
MODIFIER_WAIT_S = 1.0
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_OPEN_TIMEOUT_S = 1.0

# Formats that are not plain memory blocks (GDI handles, metafiles, owner
# display) or are synthesized by Windows from the ones we do copy.
_SKIP = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LWIN, _VK_RWIN, _VK_V = (
    0x10, 0x11, 0x12, 0x5B, 0x5C, 0x56)


class ClipboardError(Exception):
    pass


class _OwnerWindow:
    """A hidden window with its own message loop, to own the clipboard."""

    def __init__(self) -> None:
        self.hwnd = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True,
                         name="dictate-clipboard-owner").start()
        if not self._ready.wait(5) or not self.hwnd:
            raise ClipboardError("could not create the clipboard window")

    def _run(self) -> None:
        from ctypes import wintypes as w

        from dictate_win import winapi as api

        self.hwnd = api.CreateWindowExW(0, "STATIC", "Dictate clipboard", 0,
                                        0, 0, 0, 0, api.HWND_MESSAGE, None,
                                        api.GetModuleHandleW(None), None)
        self._ready.set()
        msg = w.MSG()
        while api.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            api.TranslateMessage(ctypes.byref(msg))
            api.DispatchMessageW(ctypes.byref(msg))


_owner: _OwnerWindow | None = None
_owner_lock = threading.Lock()


def _owner_hwnd():
    global _owner
    with _owner_lock:
        if _owner is None:
            _owner = _OwnerWindow()
        return _owner.hwnd


class _Clipboard:
    """`with _Clipboard():` opens the clipboard, retrying while another app
    holds it (they only hold it for moments)."""

    def __enter__(self):
        from dictate_win import winapi as api

        hwnd = _owner_hwnd()
        deadline = time.monotonic() + _OPEN_TIMEOUT_S
        while not api.OpenClipboard(hwnd):
            if time.monotonic() > deadline:
                raise ClipboardError("another app is holding the clipboard")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc) -> None:
        from dictate_win import winapi as api

        api.CloseClipboard()


def _put(fmt: int, data: bytes) -> None:
    from dictate_win import winapi as api

    handle = api.GlobalAlloc(api.GMEM_MOVEABLE, max(len(data), 1))
    if not handle:
        raise ClipboardError("out of memory")
    pointer = api.GlobalLock(handle)
    if not pointer:
        api.GlobalFree(handle)
        raise ClipboardError("could not lock clipboard memory")
    ctypes.memmove(pointer, data, len(data))
    api.GlobalUnlock(handle)
    if not api.SetClipboardData(fmt, handle):
        api.GlobalFree(handle)  # still ours if Windows didn't take it
        raise ClipboardError(f"clipboard refused format {fmt}")


def _put_private_markers() -> None:
    """Keep this clipboard content out of Win+V history and cloud sync."""
    from dictate_win import winapi as api

    zero = (0).to_bytes(4, "little")
    for name in ("ExcludeClipboardContentFromMonitorProcessing",
                 "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
        fmt = api.RegisterClipboardFormatW(name)
        if fmt:
            _put(fmt, zero)


def set_text(text: str) -> int:
    """Replace the clipboard with `text` (kept out of clipboard history).
    Returns the clipboard sequence number after the write."""
    from dictate_win import winapi as api

    with _Clipboard():
        if not api.EmptyClipboard():
            raise ClipboardError("could not clear the clipboard")
        _put(api.CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        _put_private_markers()
    return api.GetClipboardSequenceNumber()


def get_text() -> str | None:
    from dictate_win import winapi as api

    with _Clipboard():
        handle = api.GetClipboardData(api.CF_UNICODETEXT)
        if not handle:
            return None
        pointer = api.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            api.GlobalUnlock(handle)


def snapshot() -> list[tuple[int, bytes]] | None:
    """Copy every memory-based format on the clipboard (text, images,
    files, rich text). None if it can't be copied safely."""
    from dictate_win import winapi as api

    items: list[tuple[int, bytes]] = []
    total = 0
    try:
        with _Clipboard():
            fmt = api.EnumClipboardFormats(0)
            while fmt:
                if fmt not in _SKIP and not 0x200 <= fmt <= 0x3FF:
                    handle = api.GetClipboardData(fmt)
                    size = api.GlobalSize(handle) if handle else 0
                    if size:
                        total += size
                        if total > MAX_SNAPSHOT_BYTES:
                            return None
                        pointer = api.GlobalLock(handle)
                        if pointer:
                            try:
                                items.append((fmt, ctypes.string_at(pointer, size)))
                            finally:
                                api.GlobalUnlock(handle)
                fmt = api.EnumClipboardFormats(fmt)
    except ClipboardError:
        return None
    return items


def restore(items: list[tuple[int, bytes]] | None) -> None:
    from dictate_win import winapi as api

    if items is None:
        return
    with _Clipboard():
        api.EmptyClipboard()
        for fmt, data in items:
            try:
                _put(fmt, data)
            except ClipboardError:
                continue  # one odd format must not lose the rest
        _put_private_markers()


def _modifiers_held() -> bool:
    from dictate_win import winapi as api

    return any(api.GetAsyncKeyState(vk) & 0x8000
               for vk in (_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LWIN, _VK_RWIN))


def foreground_is_elevated() -> bool:
    """True if the focused app runs as administrator (and we don't), so
    Windows will drop our keystrokes."""
    from ctypes import wintypes as w

    from dictate_win import winapi as api

    def elevated(process) -> bool | None:
        token = w.HANDLE()
        if not api.OpenProcessToken(process, api.TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            value, size = w.DWORD(), w.DWORD()
            if not api.GetTokenInformation(token, api.TokenElevation,
                                           ctypes.byref(value), 4, ctypes.byref(size)):
                return None
            return bool(value.value)
        finally:
            api.CloseHandle(token)

    if elevated(api.GetCurrentProcess()):
        return False  # we are elevated too: Windows lets us through
    hwnd = api.GetForegroundWindow()
    if not hwnd:
        return False
    pid = w.DWORD()
    api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = api.OpenProcess(api.PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return False
    try:
        # Reading an administrator app's token is itself refused: that
        # refusal means it is elevated.
        result = elevated(process)
        return True if result is None else result
    finally:
        api.CloseHandle(process)


def press_ctrl_v() -> bool:
    """True if Windows accepted all four key events."""
    from dictate_win import winapi as api

    def key(vk: int, up: bool) -> api.INPUT:
        event = api.INPUT(type=api.INPUT_KEYBOARD)
        event.ki = api.KEYBDINPUT(wVk=vk, wScan=api.MapVirtualKeyW(vk, 0),
                                  dwFlags=api.KEYEVENTF_KEYUP if up else 0,
                                  time=0, dwExtraInfo=0)
        return event

    events = (api.INPUT * 4)(key(_VK_CONTROL, False), key(_VK_V, False),
                             key(_VK_V, True), key(_VK_CONTROL, True))
    sent = api.SendInput(4, events, ctypes.sizeof(api.INPUT))
    return sent == 4


def paste(text: str) -> str:
    """Type `text` into the focused app. Returns "pasted", or "clipboard"
    when Windows wouldn't let us press Ctrl+V (the text is then left on the
    clipboard for the user to paste)."""
    from dictate_win import winapi as api

    deadline = time.monotonic() + MODIFIER_WAIT_S
    while _modifiers_held() and time.monotonic() < deadline:
        time.sleep(0.02)
    saved = snapshot()
    ours = set_text(text)
    if foreground_is_elevated() or not press_ctrl_v():
        return "clipboard"
    time.sleep(RESTORE_DELAY_S)
    # Only restore if nobody else has used the clipboard in the meantime.
    if api.GetClipboardSequenceNumber() == ours:
        try:
            restore(saved)
        except ClipboardError:
            pass
    return "pasted"
