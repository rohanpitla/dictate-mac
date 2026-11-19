"""Dialogs for a menu bar app.

A menu bar app is never the active app. Left alone, macOS opens its dialogs
behind other windows (or on another desktop when an app is full screen) and
gives them no keyboard focus, so they look like nothing happened or cannot
be typed into. Every dialog here is put on the screen the user is looking
at and given the keyboard, and the app gets an Edit menu so Cmd+V works.
"""

from AppKit import (NSAlert, NSApplication, NSMenu, NSMenuItem, NSPasteboard,
                    NSRunningApplication, NSSecureTextField)
from Foundation import NSMakeRect, NSRunLoop, NSTimer

_OK = 1000  # NSAlertFirstButtonReturn; later buttons are 1001, 1002


def _install_edit_menu(app) -> None:
    """Cmd+V/C/X/A only reach a text field through a menu that names them."""
    if app.mainMenu() is not None and app.mainMenu().itemWithTitle_("Edit"):
        return
    main = app.mainMenu() or NSMenu.alloc().init()
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"),
                               ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
    item.setSubmenu_(edit)
    main.addItem_(item)
    app.setMainMenu_(main)


def _run(alert, focus=None) -> int:
    app = NSApplication.sharedApplication()
    _install_edit_menu(app)
    window = alert.window()
    window.setLevel_(3)  # floating
    # move to the active Space | allowed over a full-screen app
    window.setCollectionBehavior_((1 << 1) | (1 << 8))
    if focus is not None:
        window.setInitialFirstResponder_(focus)

    def take_keyboard(_timer) -> None:
        # macOS only lets an app come forward once it has a window on
        # screen, so this runs just after the dialog appears. Asking before
        # (the usual way) is ignored on macOS 14 and later: the dialog shows
        # but cannot be typed into.
        if app.isActive() and window.isKeyWindow():
            return
        NSRunningApplication.currentApplication().activateWithOptions_(1 << 1)
        app.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)
        if focus is not None:
            window.makeFirstResponder_(focus)

    # macOS sometimes ignores the first request; later ones do nothing once
    # the dialog has the keyboard.
    timers = [NSTimer.timerWithTimeInterval_repeats_block_(delay, False, take_keyboard)
              for delay in (0.05, 0.3, 0.7, 1.2, 2.0, 3.5)]
    for timer in timers:
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, "NSModalPanelRunLoopMode")
    try:
        return alert.runModal()
    finally:
        # A timer left over after the dialog closed would fire inside the
        # next dialog and put this closed one back on the screen.
        for timer in timers:
            timer.invalidate()


def alert(title: str = "", message: str = "", ok: str | None = None,
          cancel: str | None = None) -> int:
    """Returns 1 when the first button was clicked, else 0."""
    box = NSAlert.alloc().init()
    box.setMessageText_(title)
    box.setInformativeText_(str(message))
    box.addButtonWithTitle_(ok or "OK")
    if cancel:
        box.addButtonWithTitle_(cancel)
    return 1 if _run(box) == _OK else 0


def ask_secret(title: str, message: str, pasteboard=None) -> str | None:
    """Ask for a secret such as an API key. Returns the text (possibly
    empty), or None when cancelled. The text can be typed, pasted with
    Cmd+V, or taken straight from the clipboard with one click."""
    box = NSAlert.alloc().init()
    box.setMessageText_(title)
    box.setInformativeText_(message)
    box.addButtonWithTitle_("Save")
    box.addButtonWithTitle_("Cancel")
    box.addButtonWithTitle_("Paste from Clipboard and Save")
    field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 24))
    box.setAccessoryView_(field)
    clicked = _run(box, focus=field)
    if clicked == _OK:
        return str(field.stringValue())
    if clicked == _OK + 2:
        board = pasteboard or NSPasteboard.generalPasteboard()
        text = str(board.stringForType_("public.utf8-plain-text") or "").strip()
        if not text:
            alert(title, "The clipboard is empty. Copy the key first, then "
                         "try again. Nothing was changed.")
            return None
        return text
    return None
