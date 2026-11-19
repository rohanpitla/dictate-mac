"""The Windows tray app: icon + menu around the dictation core."""

import os
import subprocess
import sys
import threading
import time

from dictate import history, recorder as rec
from dictate_win import (appdetect, credentials, dialogs, engine_local, mic,
                         paste, paths, settings, sounds)
from dictate_win.cleanup_cloud import CloudCleanup
from dictate_win.core import Core
from dictate_win.engine_groq import EngineError, GroqClient
from dictate_win.hotkey import HotkeyListener
from dictate_win.mute import OutputMuter


def _icon_image(state: str):
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    body = {"idle": (235, 235, 235, 255), "recording": (229, 57, 53, 255),
            "busy": (255, 179, 0, 255)}[state]
    d.ellipse((2, 2, 62, 62), fill=(32, 33, 36, 255))
    d.rounded_rectangle((24, 10, 40, 38), radius=8, fill=body)        # capsule
    d.arc((16, 22, 48, 46), start=0, end=180, fill=body, width=4)      # holder
    d.line((32, 46, 32, 54), fill=body, width=4)
    d.line((24, 54, 40, 54), fill=body, width=4)
    return image


class TrayApp:
    def __init__(self) -> None:
        paths.ensure()
        history.HISTORY_PATH = paths.HISTORY
        sounds.prepare()
        self.cfg = settings.load()
        general = self.cfg["general"]
        self.client = GroqClient(self.cfg["groq"], key_getter=self._get_key)
        self.local = engine_local.LocalEngine(self.cfg["local"].get("model", "small.en"))
        self.muter = OutputMuter() if general.get("mute_while_dictating", True) else None
        self.core = Core(
            self.cfg,
            recorder=rec.Recorder(directory=paths.RECORDINGS,
                                  stream_factory=mic.MicStream),
            recordings_dir=paths.RECORDINGS, client=self.client,
            cleaner=CloudCleanup(self.client), local=self.local,
            local_available=engine_local.available, muter=self.muter,
            paste_fn=paste.paste, foreground_fn=appdetect.foreground,
            notify=self.notify, sound=sounds.play, on_change=self._refresh)
        self.hotkey = HotkeyListener(general["hotkey"],
                                     on_tap=lambda: self.core.events.put("toggle"))
        self.core.hotkey_alive = self.hotkey.is_alive
        self.core.restart_hotkey = self._restart_hotkey
        self.icon = None
        self._state = None

    # -------------------------------------------------------------- key --
    def _get_key(self) -> str | None:
        try:
            return credentials.get_key(self.client.host)
        except (credentials.CredentialError, EngineError) as exc:
            print(f"[dictate] could not read the API key: {exc}", file=sys.stderr)
            return None

    def ask_for_key(self) -> None:
        threading.Thread(target=self._ask_for_key, daemon=True, name="dictate-key").start()

    def _ask_for_key(self) -> None:
        try:
            host = self.client.host
        except EngineError as exc:
            dialogs.message("Dictate", str(exc), warning=True)
            return
        saved = self._get_key() is not None
        try:
            value = dialogs.ask_key(host, saved, paste.get_text)
        except Exception as exc:
            print(f"[dictate] key dialog failed: {exc!r}", file=sys.stderr)
            return
        if value is None:
            return
        if value and (len(value) < 20 or any(c.isspace() for c in value)):
            dialogs.message("Dictate", "That doesn't look like an API key (too "
                            "short, or it has spaces). Nothing was changed.", warning=True)
            return
        try:
            if value:
                credentials.set_key(host, value)
                dialogs.message("Dictate", "Key saved. Tap "
                                f"{settings.HOTKEYS[self.cfg['general']['hotkey']]} "
                                "and speak to try it.")
                self.core.key_changed()
            elif saved:
                credentials.delete_key(host)
                dialogs.message("Dictate", "Key removed.")
        except credentials.CredentialError as exc:
            dialogs.message("Dictate", f"Not saved: {exc}", warning=True)

    # ------------------------------------------------------------- tray --
    def notify(self, title: str, text: str) -> None:
        print(f"[dictate] notice: {title}")
        icon = self.icon
        if icon is not None:
            try:
                icon.notify(text, title)
            except Exception:
                pass

    def _refresh(self) -> None:
        icon = self.icon
        if icon is None:
            return
        state = ("recording" if self.core.recording
                 else "busy" if self.core.pending else "idle")
        try:
            if state != self._state:
                self._state = state
                icon.icon = _icon_image(state)
                icon.title = {"idle": "Dictate", "recording": "Dictate — listening",
                              "busy": "Dictate — transcribing"}[state]
            icon.update_menu()
        except Exception:
            pass

    def _restart_hotkey(self) -> None:
        try:
            self.hotkey.stop()
        except Exception:
            pass
        self.hotkey = HotkeyListener(self.cfg["general"]["hotkey"],
                                     on_tap=lambda: self.core.events.put("toggle"))
        self.core.hotkey_alive = self.hotkey.is_alive
        try:
            self.hotkey.start()
        except RuntimeError as exc:
            print(f"[dictate] hotkey could not restart: {exc}", file=sys.stderr)

    def _menu(self):
        from pystray import Menu, MenuItem as Item

        hotkey_label = lambda: settings.HOTKEYS[self.cfg["general"]["hotkey"]]  # noqa: E731

        def set_hotkey(key):
            def action(icon, item):
                self.cfg["general"]["hotkey"] = key
                self.hotkey.set_hotkey(key)
                settings.save_setting("general", "hotkey", key)
                self._refresh()
            return action

        def set_engine(key):
            def action(icon, item):
                if key == "local" and not engine_local.available():
                    self.notify("Local engine not installed",
                                "Re-run the installer with -WithLocal to add it.")
                    return
                self.cfg["general"]["engine"] = key
                settings.save_setting("general", "engine", key)
                self._refresh()
            return action

        def toggle_enabled(icon, item):
            self.core.enabled = not self.core.enabled
            if not self.core.enabled:
                self.core.events.put("discard")
            self._refresh()

        def toggle_raw(icon, item):
            self.core.raw_mode = not self.core.raw_mode
            settings.save_setting("general", "raw_mode", self.core.raw_mode)
            self._refresh()

        def history_items():
            entries = history.last(20)
            if not entries:
                yield Item("(nothing yet)", None, enabled=False)
                return
            for i, entry in enumerate(entries, 1):
                text = entry.get("text", "")
                preview = text[:48].replace("\n", " ") + ("…" if len(text) > 48 else "")
                yield Item(f"{i}. [{entry.get('ts', '')[5:16]}]  {preview}",
                           lambda icon, item, t=text: paste.set_text(t))

        def retranscribe(icon, item):
            saved = rec.saved_recordings(paths.RECORDINGS)
            if self.core.recording:
                saved = saved[:-1]
            if saved:
                self.core.enqueue(saved[-1], 0.0, ("", ""), recovered=True)
            else:
                self.notify("Dictate", "No saved recordings yet.")

        def open_path(path):
            return lambda icon, item: os.startfile(str(path))  # noqa: S606

        return Menu(
            Item(lambda item: f"Dictate — tap {hotkey_label()} to start / stop", None,
                 enabled=False),
            Item("Stop dictation", lambda icon, item: self.core.events.put("stop"),
                 enabled=lambda item: self.core.recording),
            Item(lambda item: f"Retry failed dictations ({self.core.failed_count()})",
                 lambda icon, item: self.core.retry_due(force=True),
                 visible=lambda item: self.core.failed_count() > 0),
            Menu.SEPARATOR,
            Item("Enabled", toggle_enabled, checked=lambda item: self.core.enabled),
            Item("Raw mode (no clean-up)", toggle_raw,
                 checked=lambda item: self.core.raw_mode),
            Item("Hotkey", Menu(*[
                Item(label, set_hotkey(key), radio=True,
                     checked=lambda item, k=key: self.cfg["general"]["hotkey"] == k)
                for key, label in settings.HOTKEYS.items()])),
            Item("Speech engine", Menu(*[
                Item(label, set_engine(key), radio=True,
                     checked=lambda item, k=key: self.cfg["general"]["engine"] == k)
                for key, label in settings.ENGINES.items()])),
            Item("History (click to copy)", Menu(history_items)),
            Item("Re-transcribe last recording", retranscribe),
            Menu.SEPARATOR,
            Item("Set Groq API key…", lambda icon, item: self.ask_for_key()),
            Item("Open settings file", open_path(paths.SETTINGS)),
            Item("Open log folder", open_path(paths.LOGS)),
            Item("Uninstall Dictate…", lambda icon, item: threading.Thread(
                target=self._uninstall, daemon=True).start()),
            Menu.SEPARATOR,
            Item("Quit Dictate", lambda icon, item: self.quit()),
        )

    def _uninstall(self) -> None:
        script = paths.APP_ROOT / "windows" / "uninstall.ps1"
        if not script.exists():
            dialogs.message("Dictate", "The uninstaller was not found.", warning=True)
            return
        if not dialogs.confirm("Uninstall Dictate?",
                               "This removes Dictate, its shortcuts and your saved "
                               "API key. You'll be asked whether to keep your "
                               "dictation history."):
            return
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(script)], creationflags=0x00000010)  # new console
        self.quit()

    # ------------------------------------------------------------ start --
    def _setup(self, icon) -> None:
        icon.visible = True
        if self.muter is not None:
            self.muter.recover_after_crash()
        threading.Thread(target=mic.arm, daemon=True, name="dictate-mic-arm").start()
        try:
            self.hotkey.start()
        except RuntimeError as exc:
            dialogs.message("Dictate", f"The hotkey could not start: {exc}", warning=True)
        self.core.start()
        threading.Thread(target=self._idle_loop, daemon=True, name="dictate-idle").start()
        label = settings.HOTKEYS[self.cfg["general"]["hotkey"]]
        print(f"[dictate] ready. Tap {label} to start dictating, tap again to stop.")
        if self.cfg["general"]["engine"] == "groq" and not self.client.has_key():
            self.notify("Welcome to Dictate", "Add your free Groq API key to start.")
            self.ask_for_key()
        else:
            self.notify("Dictate is running", f"Tap {label} to start, and again to stop.")

    def _idle_loop(self) -> None:
        while True:
            time.sleep(60)
            if not self.core.recording and not self.core.pending:
                try:
                    self.local.unload_if_idle()
                except Exception:
                    pass

    def run(self) -> None:
        import pystray

        self.icon = pystray.Icon("Dictate", _icon_image("idle"), "Dictate", self._menu())
        self._state = "idle"
        self.icon.run(setup=self._setup)

    def quit(self) -> None:
        print("[dictate] quitting")
        try:
            if self.core.recording:
                self.core.events.put("stop")
                time.sleep(0.5)
            self.hotkey.stop()
        finally:
            mic.shutdown()
            if self.muter is not None:
                self.muter.unmute()
            if self.icon is not None:
                self.icon.stop()
