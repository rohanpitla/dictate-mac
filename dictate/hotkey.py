"""Global tap-to-toggle hotkey listener (pynput).

A "tap" is a press and release of the hotkey with no other key pressed in
between, so shortcuts that use the key as a modifier (Option+letter for é,
Fn+arrow for Page Up) never trigger dictation.

Requires Input Monitoring permission — without it, callbacks silently
never fire (see permissions.py).
"""

from collections.abc import Callable

import Quartz
from pynput import keyboard

_FN_VK = 63  # kVK_Function

_HOTKEYS = {
    "right_option": keyboard.Key.alt_r,
    "left_option": keyboard.Key.alt_l,
    "left_control": keyboard.Key.ctrl_l,
    "fn": keyboard.KeyCode.from_vk(_FN_VK),
    "f19": keyboard.Key.f19,
}

HOTKEY_LABELS = {
    "right_option": "Right Option",
    "left_option": "Left Option",
    "left_control": "Left Control",
    "fn": "Fn / Globe",
    "f19": "F19",
}


class _Listener(keyboard.Listener):
    """pynput 1.8.2 has no Fn entry in its modifier-flag table, so it reports
    every Fn flags-changed event (press and release) as a release. Read the
    Fn flag off the event itself instead."""

    def _handle_message(self, proxy, event_type, event, refcon, injected):
        if (event_type == Quartz.kCGEventFlagsChanged
                and Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode) == _FN_VK):
            key = keyboard.KeyCode.from_vk(_FN_VK)
            if Quartz.CGEventGetFlags(event) & Quartz.kCGEventFlagMaskSecondaryFn:
                self.on_press(key, injected)
            else:
                self.on_release(key, injected)
            self._flags = Quartz.CGEventGetFlags(event)
            return
        super()._handle_message(proxy, event_type, event, refcon, injected)


class HotkeyListener:
    def __init__(self, hotkey: str, on_tap: Callable[[], None]) -> None:
        self._key = _HOTKEYS.get(hotkey)
        if self._key is None:
            raise ValueError(f"Unsupported hotkey: {hotkey!r}")
        self._on_tap = on_tap
        self._held = False
        self._combo = False  # another key was pressed while the hotkey was down
        self._listener = _Listener(
            on_press=self._handle_press,
            on_release=self._handle_release,
        )

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def _handle_press(self, key) -> None:
        if key == self._key:
            if not self._held:  # macOS auto-repeats held keys
                self._held = True
                self._combo = False
        elif self._held:
            self._combo = True

    def _handle_release(self, key) -> None:
        if key == self._key and self._held:
            self._held = False
            if not self._combo:
                self._on_tap()
