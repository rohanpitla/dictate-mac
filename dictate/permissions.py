"""Check Accessibility and Input Monitoring permissions.

Both are required:
- Accessibility     -> CGEvent Cmd+V paste (and pynput on some macOS versions)
- Input Monitoring  -> pynput's global key listener. Without it the listener
  fails SILENTLY: the hotkey just never fires.
"""

import ctypes

from ApplicationServices import AXIsProcessTrustedWithOptions

# IOHIDCheckAccess / IOHIDRequestAccess (IOKit, macOS 10.15+)
_IOKIT = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
_KIOHID_REQUEST_TYPE_LISTEN_EVENT = 1  # kIOHIDRequestTypeListenEvent
_KIOHID_ACCESS_TYPE_GRANTED = 0  # kIOHIDAccessTypeGranted

ACCESSIBILITY_HELP = """\
*** Accessibility permission missing (needed to paste text) ***
  System Settings -> Privacy & Security -> Accessibility
  -> toggle ON "Terminal" (or the app you launched Dictate from)
  Then quit and relaunch Dictate."""

INPUT_MONITORING_HELP = """\
*** Input Monitoring permission missing (needed for the hotkey) ***
  Without it, holding Right Option will silently do NOTHING.
  System Settings -> Privacy & Security -> Input Monitoring
  -> toggle ON "Terminal" (or the app you launched Dictate from)
  Then quit and relaunch Dictate."""


def check_accessibility(prompt: bool = True) -> bool:
    return bool(AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": prompt}))


def check_input_monitoring(prompt: bool = True) -> bool:
    status = _IOKIT.IOHIDCheckAccess(_KIOHID_REQUEST_TYPE_LISTEN_EVENT)
    if status == _KIOHID_ACCESS_TYPE_GRANTED:
        return True
    if prompt:
        # Triggers the system prompt / adds this app to the Input Monitoring pane
        _IOKIT.IOHIDRequestAccess(_KIOHID_REQUEST_TYPE_LISTEN_EVENT)
    return False


def check_all(prompt: bool = True) -> bool:
    """Check both permissions; print exact fix steps for any that are missing."""
    ok = True
    if not check_accessibility(prompt):
        print(ACCESSIBILITY_HELP)
        ok = False
    if not check_input_monitoring(prompt):
        print(INPUT_MONITORING_HELP)
        ok = False
    return ok
