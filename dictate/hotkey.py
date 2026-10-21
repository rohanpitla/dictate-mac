"""Global tap-to-toggle hotkey listener (pynput).

A "tap" is a press and release of the hotkey with no other key pressed in
between, so shortcuts that use the key as a modifier (Option+letter for é,
Fn+arrow for Page Up) never trigger dictation.

Requires Input Monitoring permission — without it, callbacks silently
never fire (see permissions.py).
"""

import sys
import traceback
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


_TAP_DISABLED = (Quartz.kCGEventTapDisabledByTimeout,
                 Quartz.kCGEventTapDisabledByUserInput)


class _Listener(keyboard.Listener):
    """Fixes two pynput 1.8.2 gaps on macOS.

    - macOS switches the event tap off when the system is busy (e.g. while
      Whisper is loading) and pynput never switches it back on, so the
      hotkey went dead mid-recording and the recording could not be stopped.
    - It has no Fn entry in its modifier-flag table, so every Fn event
      (press and release) came through as a release.
    """

    _tap = None

    def _create_event_tap(self):
        self._tap = super()._create_event_tap()
        return self._tap

    def _handle_message(self, proxy, event_type, event, refcon, injected):
        if event_type in _TAP_DISABLED:
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
            print("[dictate] macOS paused the hotkey listener; re-enabled it",
                  file=sys.stderr)
            return
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
            if self._combo:
                return
            # pynput permanently stops the listener if a callback raises,
            # which would leave the hotkey dead until the app is restarted.
            try:
                self._on_tap()
            except Exception:
                print("[dictate] hotkey handler failed:", file=sys.stderr)
                traceback.print_exc()
