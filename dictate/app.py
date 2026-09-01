"""Dictate menu bar app: wires hotkey -> recorder -> transcriber -> paster."""

import atexit
import subprocess
import sys
import threading
import time
import traceback

import rumps
from PyObjCTools import AppHelper

from dictate import (__version__, appdetect, cleanup, history, latency, paster,
                    sounds, transcriber)
from dictate import config as config_mod
from dictate.ducking import OutputMuter
from dictate.hotkey import HOTKEY_LABELS, HotkeyListener
from dictate.recorder import MicrophoneError, Recorder

ICON_IDLE = "🎤"
ICON_RECORDING = "🔴"
ICON_HANDSFREE = "🎙️"
ICON_BUSY = "⏳"

# States
IDLE, RECORDING, PROCESSING = "idle", "recording", "processing"

# Two presses within this window = double-tap (hands-free latch toggle)
DOUBLE_TAP_S = 0.4
# Ignore a stop tap this soon after latching (debounces sloppy taps)
LATCH_MIN_S = 1.0

MODEL_CHOICES = [
    ("Large v3 Turbo (best)", "mlx-community/whisper-large-v3-turbo"),
    ("Medium", "mlx-community/whisper-medium-mlx"),
    ("Small", "mlx-community/whisper-small-mlx"),
    ("Base (fastest)", "mlx-community/whisper-base-mlx"),
]

LAUNCH_AT_LOGIN_TEXT = """Dictate runs from a terminal for now. To start it at login:

1. Open Automator > New Document > Application.
2. Add a "Run Shell Script" action with:
   cd ~/Developer/dictate && .venv/bin/python -m dictate
3. Save it as "Dictate" in /Applications.
4. System Settings > General > Login Items > add Dictate.
5. Grant Accessibility, Input Monitoring and Microphone
   to the new Dictate app on first run.

Full details are in TESTING.md."""


class DictateApp(rumps.App):
    def __init__(self, config: dict) -> None:
        super().__init__("Dictate", title=ICON_IDLE, quit_button="Quit Dictate")
        self._config = config
        self._debug = config["general"].get("debug", True)
        self._model = config["whisper"]["model"]
        self._hotkey_name = config["general"].get("hotkey", "right_option")
        self._enabled = True
        self._raw_mode = config["general"].get("raw_mode", False)
        self._build_menu()
        self._recorder = Recorder()
        self._muter: OutputMuter | None = None
        if config["general"].get("mute_while_dictating", True):
            self._muter = OutputMuter()
            atexit.register(self._muter.unmute)  # never leave the Mac muted
        self._state = IDLE
        self._state_lock = threading.Lock()
        # Hands-free latch bookkeeping — only touched on the hotkey
        # listener thread (press and release run on the same thread).
        self._latched = False
        self._pending_latch = False
        self._last_press = 0.0
        self._latch_started = 0.0
        self._hotkey = self._make_listener(self._hotkey_name)

    def _make_listener(self, hotkey_name: str) -> HotkeyListener:
        return HotkeyListener(
            hotkey_name,
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
            on_cancel=self._on_hotkey_cancel,
        )

    # ------------------------------------------------------------- menu --
    def _build_menu(self) -> None:
        self._info_item = rumps.MenuItem("")
        self._update_info_item()

        self._enabled_item = rumps.MenuItem("Enabled",
                                            callback=self._on_toggle_enabled)
        self._enabled_item.state = 1

        self._hotkey_menu = rumps.MenuItem("Hotkey")
        for key, label in HOTKEY_LABELS.items():
            item = rumps.MenuItem(label, callback=self._on_choose_hotkey)
            item.state = 1 if key == self._hotkey_name else 0
            self._hotkey_menu.add(item)

        self._model_menu = rumps.MenuItem("Whisper Model")
        for label, repo in MODEL_CHOICES:
            item = rumps.MenuItem(label, callback=self._on_choose_model)
            item.state = 1 if repo == self._model else 0
            self._model_menu.add(item)

        self._history_menu = rumps.MenuItem("History (last 20)")
        self._history_texts: dict[str, str] = {}
        self._refresh_history_menu()

        self._raw_mode_item = rumps.MenuItem("Raw Mode (skip cleanup)",
                                             callback=self._on_toggle_raw_mode)
        self._raw_mode_item.state = 1 if self._raw_mode else 0

        self.menu = [
            self._info_item,
            None,
            self._enabled_item,
            self._raw_mode_item,
            self._hotkey_menu,
            self._model_menu,
            self._history_menu,
            rumps.MenuItem("Latency Stats…", callback=self._on_latency_stats),
            None,
            rumps.MenuItem("Open Config File", callback=self._on_open_config),
            rumps.MenuItem("Launch at Login…", callback=self._on_launch_at_login),
        ]

    def _on_toggle_raw_mode(self, item) -> None:
        self._raw_mode = not self._raw_mode
        item.state = 1 if self._raw_mode else 0
        config_mod.save_setting("general", "raw_mode", self._raw_mode)

    def _on_latency_stats(self, _item) -> None:
        report = latency.format_stats()
        print(f"[dictate]\n{report}")
        rumps.alert(title="Dictate Latency (p50 / p95)", message=report)

    def _update_info_item(self) -> None:
        label = HOTKEY_LABELS[self._hotkey_name]
        self._info_item.title = (f"Dictate v{__version__} — hold {label} "
                                 f"(double-tap = hands-free)")

    def _on_toggle_enabled(self, item) -> None:
        self._enabled = not self._enabled
        item.state = 1 if self._enabled else 0
        if self._enabled:
            return
        # If dictation is disabled mid-recording (incl. hands-free), abort it —
        # otherwise a latched recording could never be stopped by key.
        with self._state_lock:
            was_recording = self._state == RECORDING
            if was_recording:
                self._state = IDLE
                self._latched = False
                self._pending_latch = False
        if was_recording:
            self._recorder.stop()  # discard
            if self._muter is not None:
                threading.Thread(target=self._muter.unmute, daemon=True,
                                 name="dictate-unmute").start()
            self._set_icon(ICON_IDLE)

    def _on_choose_hotkey(self, item) -> None:
        new_key = next(k for k, lbl in HOTKEY_LABELS.items()
                       if lbl == item.title)
        if new_key == self._hotkey_name:
            return
        old = self._hotkey
        self._hotkey = self._make_listener(new_key)
        self._hotkey.start()
        old.stop()
        self._hotkey_name = new_key
        for other in self._hotkey_menu.values():
            other.state = 1 if other.title == item.title else 0
        self._update_info_item()
        config_mod.save_setting("general", "hotkey", new_key)

    def _on_choose_model(self, item) -> None:
        repo = next(r for lbl, r in MODEL_CHOICES if lbl == item.title)
        if repo == self._model:
            return
        self._model = repo
        for other in self._model_menu.values():
            other.state = 1 if other.title == item.title else 0
        config_mod.save_setting("whisper", "model", repo)
        print(f"[dictate] switching whisper model to {repo} "
              "(downloads on first use)")
        threading.Thread(target=transcriber.warmup, args=(repo,),
                         daemon=True, name="dictate-warmup").start()

    def _refresh_history_menu(self) -> None:
        if self._history_menu._menu is not None:  # NSMenu exists only after first add
            self._history_menu.clear()
        self._history_texts.clear()
        entries = history.last(20)
        if not entries:
            self._history_menu.add(rumps.MenuItem("(no transcriptions yet)"))
            return
        for i, entry in enumerate(entries):
            preview = entry["text"][:48]
            if len(entry["text"]) > 48:
                preview += "…"
            title = f"{i + 1}. [{entry['ts'][5:16]}]  {preview}"
            self._history_texts[title] = entry["text"]
            self._history_menu.add(
                rumps.MenuItem(title, callback=self._on_history_click))

    def _on_history_click(self, item) -> None:
        text = self._history_texts.get(item.title)
        if text:
            paster.set_clipboard_text(text)

    def _on_open_config(self, _item) -> None:
        subprocess.Popen(["open", str(config_mod.CONFIG_PATH)])

    def _on_launch_at_login(self, _item) -> None:
        rumps.alert(title="Launch Dictate at Login",
                    message=LAUNCH_AT_LOGIN_TEXT)

    def start_background_services(self) -> None:
        threading.Thread(target=transcriber.warmup, args=(self._model,),
                         daemon=True, name="dictate-warmup").start()
        threading.Thread(target=cleanup.warmup, args=(self._config,),
                         daemon=True, name="dictate-ollama-warmup").start()
        self._hotkey.start()

    # AppKit UI may only be touched from the main thread; hotkey callbacks
    # and the pipeline worker run on background threads.
    def _set_icon(self, icon: str) -> None:
        AppHelper.callAfter(lambda: setattr(self, "title", icon))

    def _on_hotkey_press(self) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        is_double_tap = (now - self._last_press) < DOUBLE_TAP_S
        self._last_press = now
        stop_handsfree = False
        with self._state_lock:
            if self._state == IDLE:
                self._state = RECORDING
                self._pending_latch = is_double_tap
            elif (self._state == RECORDING and self._latched
                  and now - self._latch_started > LATCH_MIN_S):
                # Hands-free: a single tap ends listening and transcribes
                self._latched = False
                self._state = PROCESSING
                stop_handsfree = True
            else:
                return  # busy, or a bounce right after latching

        if stop_handsfree:
            self._finish_recording()
            return

        try:
            self._recorder.start()
        except MicrophoneError as exc:
            with self._state_lock:
                self._state = IDLE
            self._pending_latch = False
            sounds.play("error")
            print(f"[dictate] microphone unavailable: {exc}\n"
                  "  If you just denied the mic prompt: System Settings -> "
                  "Privacy & Security -> Microphone -> enable Terminal, then relaunch.",
                  file=sys.stderr)
            return
        sounds.play("start")
        self._set_icon(ICON_RECORDING)
        if self._muter is not None:
            # osascript takes ~100ms; keep the hotkey listener thread snappy
            threading.Thread(target=self._muter.mute, daemon=True,
                             name="dictate-mute").start()

    def _on_hotkey_cancel(self) -> None:
        """Another key was typed while the hotkey was held — the user is using
        Option as a typing modifier (é, ™, shortcuts), not dictating. Discard
        the recording silently instead of transcribing keyboard noise."""
        with self._state_lock:
            if self._state != RECORDING or self._latched:
                return  # hands-free typing while dictating is fine
            self._state = IDLE
            self._pending_latch = False
        wav_path = self._recorder.stop()
        if wav_path is not None:
            wav_path.unlink(missing_ok=True)
        if self._muter is not None:
            threading.Thread(target=self._muter.unmute, daemon=True,
                             name="dictate-unmute").start()
        self._set_icon(ICON_IDLE)
        if self._debug:
            print("[dictate] recording cancelled (hotkey used as typing modifier)")

    def _on_hotkey_release(self) -> None:
        with self._state_lock:
            if self._state != RECORDING:
                return
            if self._latched:
                return  # hands-free: keep listening until the stop double-tap
            if self._pending_latch and self._recorder.elapsed < DOUBLE_TAP_S:
                # Second tap of a double-tap, released quickly -> latch on.
                # (Double-tap then HOLD falls through to normal push-to-talk.)
                self._pending_latch = False
                self._latched = True
                self._latch_started = time.monotonic()
                sounds.play("latch")
                self._set_icon(ICON_HANDSFREE)
                return
            self._pending_latch = False
            self._state = PROCESSING
        self._finish_recording()

    def _finish_recording(self) -> None:
        """Stop the recorder (fast) and hand off to the pipeline worker.

        Runs on the hotkey listener thread; recorder.stop() is milliseconds,
        and the cheap discard path must not block a quick second tap.
        """
        t0 = time.monotonic()
        wav_path = self._recorder.stop()
        record_ms = (time.monotonic() - t0) * 1000
        if wav_path is None:  # accidental tap / empty recording
            if self._muter is not None:
                threading.Thread(target=self._muter.unmute, daemon=True,
                                 name="dictate-unmute").start()
            with self._state_lock:
                self._state = IDLE
            self._set_icon(ICON_IDLE)
            return
        self._set_icon(ICON_BUSY)
        # Capture the app that will receive the paste NOW, while it's frontmost
        bundle_id = appdetect.frontmost_bundle_id()
        threading.Thread(target=self._process,
                         args=(wav_path, record_ms, bundle_id),
                         daemon=True, name="dictate-pipeline").start()

    def _process(self, wav_path, record_ms: float,
                 bundle_id: str | None = None) -> None:
        try:
            # Unmute first so the video/music resumes the moment dictation
            # ends — and so the stop sound below is audible.
            if self._muter is not None:
                self._muter.unmute()
            sounds.play("stop")

            t1 = time.monotonic()
            raw = transcriber.transcribe(str(wav_path), self._model)
            if raw and transcriber.looks_like_hallucination(raw, str(wav_path)):
                if self._debug:
                    print(f"[dictate] dropped whisper hallucination on quiet "
                          f"audio: {raw!r}")
                raw = ""
            transcribe_ms = (time.monotonic() - t1) * 1000

            t2 = time.monotonic()
            mode, style_hint = appdetect.resolve_mode(bundle_id, self._config)
            if self._raw_mode or mode == "code":
                # code editors/terminals and Raw Mode paste the raw transcript
                text, stage = raw, ("raw-mode" if self._raw_mode else "code-app")
            else:
                text, stage = cleanup.clean_and_correct(raw, self._config,
                                                        style_hint)
                if mode != "default":
                    stage = f"{stage}+{mode}"
            cleanup_ms = (time.monotonic() - t2) * 1000

            paste_ms = 0.0
            if text:
                t3 = time.monotonic()
                paster.paste(text)
                paste_ms = (time.monotonic() - t3) * 1000
                history.append(raw=raw, text=text, stage=stage, latency_ms={
                    "finalize": record_ms, "transcribe": transcribe_ms,
                    "cleanup": cleanup_ms, "paste": paste_ms,
                })
                AppHelper.callAfter(self._refresh_history_menu)
            elif self._debug:
                print("[dictate] empty transcript (silence?) — nothing to paste")

            if self._debug:
                print(f"[dictate] latency ms — finalize: {record_ms:.0f}  "
                      f"transcribe: {transcribe_ms:.0f}  "
                      f"cleanup: {cleanup_ms:.0f} ({stage})  "
                      f"paste: {paste_ms:.0f}  | text: {text[:80]!r}")
        except Exception:
            sounds.play("error")
            print("[dictate] transcription pipeline failed:", file=sys.stderr)
            traceback.print_exc()
        finally:
            wav_path.unlink(missing_ok=True)
            with self._state_lock:
                self._state = IDLE
            self._set_icon(ICON_IDLE)
