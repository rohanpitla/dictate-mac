"""The floating panel only you can see.

It is marked `sharingType = none`, which keeps it out of screen shares,
screenshots and screen recordings. If macOS does not accept that setting the
panel refuses to open — it fails closed rather than risk being seen.

All methods may be called from any thread; the work runs on the main thread.
"""

from AppKit import (NSAttributedString, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSMutableAttributedString, NSPanel, NSScreen, NSTextField)
from Foundation import NSMakeRect
from PyObjCTools import AppHelper

WIDTH, HEIGHT = 380, 430
_SHARING_NONE = 0          # NSWindowSharingNone
_BORDERLESS_NONACTIVATING = 1 << 7
# every Space, over full-screen apps, absent from Mission Control and Cmd-`
_BEHAVIOR = (1 << 0) | (1 << 8) | (1 << 4) | (1 << 6)


class OverlayError(Exception):
    pass


class Overlay:
    def __init__(self) -> None:
        self._panel = None
        self._label = None
        self.failed = ""  # set when the panel could not be opened hidden

    # ------------------------------------------------------- main thread --
    def _build(self) -> None:
        screen = NSScreen.mainScreen().visibleFrame()
        rect = NSMakeRect(screen.origin.x + screen.size.width - WIDTH - 20,
                          screen.origin.y + screen.size.height - HEIGHT - 20,
                          WIDTH, HEIGHT)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, _BORDERLESS_NONACTIVATING, 2, False)
        panel.setSharingType_(_SHARING_NONE)
        if panel.sharingType() != _SHARING_NONE:
            panel.close()
            raise OverlayError("macOS would not hide the panel from screen "
                               "sharing, so it was not opened")
        panel.setLevel_(25)
        panel.setCollectionBehavior_(_BEHAVIOR)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.88))
        panel.setMovableByWindowBackground_(True)
        panel.setHasShadow_(True)
        panel.setReleasedWhenClosed_(False)
        content = panel.contentView()
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(12)
        content.layer().setMasksToBounds_(True)

        label = NSTextField.wrappingLabelWithString_("")
        label.setFrame_(NSMakeRect(14, 12, WIDTH - 28, HEIGHT - 24))
        label.setSelectable_(False)  # also lets a drag anywhere move the panel
        label.setAllowsEditingTextAttributes_(False)
        content.addSubview_(label)
        self._panel, self._label = panel, label

    def _show(self) -> None:
        self.failed = ""
        try:
            if self._panel is None:
                self._build()
            self._panel.orderFrontRegardless()
        except Exception as exc:
            self._panel = None
            self.failed = (str(exc) if isinstance(exc, OverlayError)
                           else f"the panel could not be opened ({type(exc).__name__})")

    def _hide(self) -> None:
        if self._panel is not None:
            self._label.setStringValue_("")  # don't keep meeting text around
            self._panel.orderOut_(None)

    def _render(self, sections: list[tuple[str, str]]) -> None:
        if self._panel is None:
            return
        out = NSMutableAttributedString.alloc().init()
        head = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(11),
                NSForegroundColorAttributeName:
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.8, 1, 1)}
        body = {NSFontAttributeName: NSFont.systemFontOfSize_(13),
                NSForegroundColorAttributeName: NSColor.whiteColor()}
        for title, text in sections:
            if not text:
                continue
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    title.upper() + "\n", head))
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    text.strip() + "\n\n", body))
        self._label.setAttributedStringValue_(out)

    # -------------------------------------------------------- any thread --
    def show(self) -> None:
        AppHelper.callAfter(self._show)

    def hide(self) -> None:
        AppHelper.callAfter(self._hide)

    def render(self, sections: list[tuple[str, str]]) -> None:
        """sections: (heading, plain text) pairs, top to bottom."""
        AppHelper.callAfter(self._render, list(sections))
