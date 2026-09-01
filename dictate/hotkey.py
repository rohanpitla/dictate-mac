"""Global push-to-talk hotkey listener (pynput).

Requires Input Monitoring permission — without it, callbacks silently
never fire (see permissions.py).
"""

from collections.abc import Callable

from pynput import keyboard

_HOTKEYS = {
    "right_option": keyboard.Key.alt_r,
    # Fn/Globe arrives as a flags-changed event with vk 63 (kVK_Function).
    # Detection can vary by keyboard — Right Option and F19 are the safe picks.
    "fn": keyboard.KeyCode.from_vk(63),
    "f19": keyboard.Key.f19,
}

HOTKEY_LABELS = {
    "right_option": "Right Option",
    "fn": "Fn / Globe",
    "f19": "F19",
}


class HotkeyListener:
    def __init__(self, hotkey: str, on_press: Callable[[], None],
                 on_release: Callable[[], None],
                 on_cancel: Callable[[], None] | None = None) -> None:
        self._key = _HOTKEYS.get(hotkey)
        if self._key is None:
            raise ValueError(f"Unsupported hotkey: {hotkey!r}")
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._held = False
        self._listener = keyboard.Listener(
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
                self._on_press()
        elif self._held and self._on_cancel is not None:
            # Another key while the hotkey is held: the user is typing an
            # Option+letter special character, not dictating.
            self._on_cancel()

    def _handle_release(self, key) -> None:
        if key == self._key and self._held:
            self._held = False
            self._on_release()
