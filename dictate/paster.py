"""Paste text into the focused app via clipboard + simulated Cmd+V.

Save clipboard -> write text -> Cmd+V (CGEvent) -> restore after 300 ms.

Phase 4: the full pasteboard is snapshotted item-by-item (every type's raw
data), so images and files on the clipboard survive a dictation, not just
plain text.
"""

import sys
import time

import Quartz
from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString

_KVK_ANSI_V = 9
RESTORE_DELAY_S = 0.3


def get_clipboard_text() -> str | None:
    return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)


def set_clipboard_text(text: str) -> None:
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def snapshot_pasteboard() -> list[list[tuple[str, object]]]:
    """Copy every item's data for every type it carries (text, images, files)."""
    items = []
    try:
        for item in NSPasteboard.generalPasteboard().pasteboardItems() or []:
            data = []
            for t in item.types():
                d = item.dataForType_(t)
                if d is not None:
                    data.append((str(t), d))
            if data:
                items.append(data)
    except Exception as exc:  # snapshot is best-effort, never blocks pasting
        print(f"[dictate] pasteboard snapshot failed: {exc}", file=sys.stderr)
    return items


def restore_pasteboard(items: list[list[tuple[str, object]]]) -> None:
    if not items:
        return
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        restored = []
        for data in items:
            item = NSPasteboardItem.alloc().init()
            for t, d in data:
                item.setData_forType_(d, t)
            restored.append(item)
        pb.writeObjects_(restored)
    except Exception as exc:
        print(f"[dictate] pasteboard restore failed: {exc}", file=sys.stderr)


def _press_cmd_v() -> None:
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for key_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(src, _KVK_ANSI_V, key_down)
        # Explicitly Command-only: the user may still be holding Right Option
        # (the dictation hotkey) — without this, the paste would become
        # Cmd+Opt+V or another shortcut.
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.01)


def paste(text: str) -> None:
    """Type `text` into the focused field; restores the prior clipboard
    (all items and types — text, images, files)."""
    saved = snapshot_pasteboard()
    set_clipboard_text(text)
    time.sleep(0.05)  # let the pasteboard settle before the app reads it
    _press_cmd_v()
    time.sleep(RESTORE_DELAY_S)
    restore_pasteboard(saved)
