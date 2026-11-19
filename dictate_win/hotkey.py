"""Global tap-to-toggle hotkey, using Windows Raw Input.

Why not a keyboard hook (what pynput uses): Windows silently removes a
low-level keyboard hook whose callback is ever slower than its timeout
(e.g. while the PC is busy). The hotkey would then go dead with no error.
Raw Input has no such timeout: Windows queues key events to our hidden
window, and nothing is removed if we are slow. It also only reads keys, it
never intercepts or changes them.

A "tap" is a press and release of the hotkey with no other key pressed in
between, so Ctrl+C, Ctrl+V etc. never start dictation.
"""

import ctypes
import sys
import threading
import traceback
from collections.abc import Callable

# Virtual-key codes; (vk, needs the "extended key" flag)
_KEYS = {
    "right_ctrl": {(0x11, True), (0xA3, True), (0xA3, False)},
    "copilot": {(0x86, False), (0x86, True)},     # the Copilot key sends F23
    "scroll_lock": {(0x91, False), (0x91, True)},
    "f9": {(0x78, False), (0x78, True)},
}


class TapDetector:
    """Platform-neutral tap logic (tested on the Mac too)."""

    def __init__(self, hotkey: str, on_tap: Callable[[], None]) -> None:
        if hotkey not in _KEYS:
            raise ValueError(f"unsupported hotkey: {hotkey!r}")
        self.hotkey = hotkey
        self._on_tap = on_tap
        self._held = False
        self._combo = False

    def set_hotkey(self, hotkey: str) -> None:
        if hotkey not in _KEYS:
            raise ValueError(f"unsupported hotkey: {hotkey!r}")
        self.hotkey, self._held, self._combo = hotkey, False, False

    def key_event(self, vk: int, extended: bool, released: bool) -> None:
        is_hotkey = (vk, extended) in _KEYS[self.hotkey]
        if not released:
            if is_hotkey:
                if not self._held:  # Windows repeats held keys; ignore repeats
                    self._held, self._combo = True, False
            elif self._held:
                self._combo = True  # e.g. Ctrl+C: not a tap
            return
        if is_hotkey and self._held:
            self._held = False
            if self._combo:
                return
            try:
                self._on_tap()  # must only queue work and return
            except Exception:
                print("[dictate] hotkey handler failed:", file=sys.stderr)
                traceback.print_exc()


class HotkeyListener:
    """Runs a hidden window on its own thread that receives Raw Input."""

    def __init__(self, hotkey: str, on_tap: Callable[[], None]) -> None:
        self.detector = TapDetector(hotkey, on_tap)
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._proc = None  # keep the callback alive while the window exists

    def set_hotkey(self, hotkey: str) -> None:
        self.detector.set_hotkey(hotkey)

    def start(self, timeout: float = 5.0) -> None:
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="dictate-hotkey")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("the hotkey window did not start")
        if self._error:
            raise RuntimeError(self._error)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        from dictate_win import winapi as api

        if self._hwnd:
            api.PostMessageW(self._hwnd, api.WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(2)

    def _run(self) -> None:
        from ctypes import wintypes as w

        from dictate_win import winapi as api

        buffer = api.RAWINPUT_KEYBOARD()
        header_size = ctypes.sizeof(api.RAWINPUTHEADER)

        def window_proc(hwnd, msg, wparam, lparam):
            try:
                if msg == api.WM_INPUT:
                    size = w.UINT(ctypes.sizeof(buffer))
                    got = api.GetRawInputData(lparam, api.RID_INPUT,
                                              ctypes.byref(buffer),
                                              ctypes.byref(size), header_size)
                    if (got != 0xFFFFFFFF and got >= header_size
                            and buffer.header.dwType == api.RIM_TYPEKEYBOARD):
                        kb = buffer.keyboard
                        if kb.VKey != 0xFF:  # 0xFF: part of an escape sequence
                            self.detector.key_event(
                                kb.VKey, bool(kb.Flags & api.RI_KEY_E0),
                                bool(kb.Flags & api.RI_KEY_BREAK))
                elif msg == api.WM_DESTROY:
                    api.PostQuitMessage(0)
                    return 0
            except Exception:
                traceback.print_exc()
            return api.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = api.WNDPROC(window_proc)
        instance = api.GetModuleHandleW(None)
        class_name = f"DictateHotkey{id(self)}"
        wc = api.WNDCLASSEXW(cbSize=ctypes.sizeof(api.WNDCLASSEXW),
                             lpfnWndProc=self._proc, hInstance=instance,
                             lpszClassName=class_name)
        if not api.RegisterClassExW(ctypes.byref(wc)):
            self._error = f"could not register the hotkey window ({api.last_error()})"
            self._ready.set()
            return
        # A hidden top-level window (never shown, not in the taskbar).
        self._hwnd = api.CreateWindowExW(0x80, class_name, "Dictate hotkey", 0,
                                         0, 0, 0, 0, None, None, instance, None)
        device = api.RAWINPUTDEVICE(usUsagePage=0x01, usUsage=0x06,  # keyboard
                                    dwFlags=api.RIDEV_INPUTSINK,
                                    hwndTarget=self._hwnd)
        if not self._hwnd or not api.RegisterRawInputDevices(
                ctypes.byref(device), 1, ctypes.sizeof(device)):
            self._error = f"could not listen to the keyboard ({api.last_error()})"
            self._ready.set()
            return
        self._ready.set()
        msg = w.MSG()
        while api.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            api.TranslateMessage(ctypes.byref(msg))
            api.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = None
        api.UnregisterClassW(class_name, instance)
