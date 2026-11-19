"""Dictate menu bar app: wires hotkey -> recorder -> transcriber -> paster."""

import atexit
import queue
import subprocess
import sys
import threading
import time
import traceback

import rumps
from PyObjCTools import AppHelper

from dictate import (__version__, appdetect, cleanup, history, latency, layout,
                    micstream, paster, recorder, sounds, transcriber)
from dictate import config as config_mod
from dictate.ducking import OutputMuter
from dictate.hotkey import HOTKEY_LABELS, HotkeyListener
from dictate import dialogs
from dictate.meeting import keychain
from dictate.meeting.brain import Brain, BrainError, brain_host
from dictate.meeting.overlay import Overlay
from dictate.meeting.session import MeetingError, MeetingSession
from dictate.recorder import MicrophoneError, Recorder

ICON_IDLE = "🎤"
ICON_RECORDING = "🔴"
ICON_BUSY = "⏳"

_alert = dialogs.alert  # rumps.alert opens behind other windows


MEETING_START_TEXT = """While Meeting Mode is on, Dictate listens to your microphone and to everything your Mac plays, and shows a live transcript in a panel that is hidden from screen sharing.

• Audio and transcript stay in memory on this Mac and are erased when you stop. Nothing is saved unless you set save_transcript = true.
• If an API key is saved, transcript text (never audio) is sent to the provider in config.toml to write catch-ups and talking points.
• Recording people without telling them is illegal in some places. Make sure you are allowed to do this in your meeting."""

MAX_RECORDING_S = 15 * 60  # a forgotten recording stops itself

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
        # A tap always toggles recording, even while earlier dictations are
        # still transcribing — those wait in _jobs and are pasted in order.
        # (Ignoring taps while busy let the app drift out of step with the
        # user: their "stop" tap would start a recording that never ended.)
        self._state_lock = threading.Lock()
        self._recording = False
        self._pending = 0
        self._jobs: queue.Queue = queue.Queue()
        # The hotkey thread and the menu only post events here; the
        # controller thread does the actual microphone work. A stuck audio
        # device can therefore never freeze the hotkey or the menu.
        self._events: queue.Queue = queue.Queue()
        self._hotkey = self._make_listener(self._hotkey_name)
        self._overlay = Overlay()
        self._meeting: MeetingSession | None = None
        self._meeting_starting = False

    def _make_listener(self, hotkey_name: str) -> HotkeyListener:
        return HotkeyListener(hotkey_name,
                              on_tap=lambda: self._events.put("toggle"))

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

        self._meeting_item = rumps.MenuItem("Start Meeting Mode…",
                                            callback=self._on_toggle_meeting)

        # Greyed out unless a dictation is being recorded, so it is never
        # mistaken for the way to stop Meeting Mode.
        self._stop_item = rumps.MenuItem("Stop Dictation")

        # Meeting Mode is experimental and off unless enabled in config.toml.
        meeting_items = []
        if self._config.get("meeting", {}).get("enabled", False):
            meeting_items = [
                self._meeting_item,
                rumps.MenuItem("Set Meeting API Key…",
                               callback=self._on_set_meeting_key),
                None,
            ]

        self.menu = [
            self._info_item,
            None,
            *meeting_items,
            self._stop_item,
            rumps.MenuItem("Re-transcribe Last Recording",
                           callback=self._on_retranscribe_last),
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

    # ----------------------------------------------------- meeting mode --
    def _meeting_active(self) -> bool:
        return self._meeting is not None and self._meeting.active

    def _on_toggle_meeting(self, _item) -> None:
        if self._meeting_active():
            threading.Thread(target=self._meeting.stop, daemon=True,
                             name="meeting-stop").start()
            return
        if self._meeting_starting:
            return  # a second click while starting must not start a second session
        # Pick up config.toml edits (name, provider) without a restart.
        try:
            self._config["meeting"] = config_mod.load()["meeting"]
        except Exception as exc:
            _alert(title="Meeting Mode",
                        message=f"config.toml could not be read: {exc}")
            return
        problem = Brain(self._config["meeting"]).problem()
        if problem:
            destination = f"Suggestions are OFF ({problem}). Nothing will leave this Mac."
        else:
            destination = ("Transcript text will be sent to: "
                           f"{brain_host(self._config['meeting']['base_url'])} "
                           f"(model {self._config['meeting']['model']}).")
        # Starting is always a deliberate, confirmed act — never automatic.
        if _alert(title="Start Meeting Mode?",
                       message=f"{MEETING_START_TEXT}\n\n{destination}",
                       ok="Start Listening", cancel="Cancel") != 1:
            return
        self._meeting_starting = True
        self._meeting_item.title = "Starting Meeting Mode…"
        threading.Thread(target=self._start_meeting, daemon=True,
                         name="meeting-start").start()

    def _start_meeting(self) -> None:
        def hotkey_factory(on_tap):
            name = self._config.get("meeting", {}).get("suggest_hotkey", "")
            if name not in HOTKEY_LABELS or name == self._hotkey_name:
                raise ValueError("meeting.suggest_hotkey is unset or the same "
                                 "as the dictation hotkey")
            return HotkeyListener(name, on_tap=on_tap)

        session = MeetingSession(self._config, self._model, self._overlay,
                                 on_stopped=self._on_meeting_stopped,
                                 # words dictated into another app are not
                                 # part of the meeting
                                 mic_paused=lambda: self._recording,
                                 hotkey_factory=hotkey_factory)
        self._meeting = session  # set first, so "stop" works from the first moment
        try:
            session.start()
        except Exception as exc:
            self._meeting_starting = False
            message = (str(exc) if isinstance(exc, MeetingError)
                       else f"unexpected error ({type(exc).__name__})")
            print(f"[meeting] could not start: {message}", file=sys.stderr)
            AppHelper.callAfter(self._meeting_ui, False)
            AppHelper.callAfter(
                lambda: _alert(title="Meeting Mode could not start",
                                    message=message))
            return
        self._meeting_starting = False
        AppHelper.callAfter(self._meeting_ui, True)

    def _on_meeting_stopped(self, reason: str) -> None:
        AppHelper.callAfter(self._meeting_ui, False)
        if reason:  # it stopped by itself: say why
            print(f"[meeting] stopped by itself: {reason}", file=sys.stderr)
            AppHelper.callAfter(
                lambda: _alert(title="Meeting Mode stopped", message=reason))

    def _meeting_ui(self, active: bool) -> None:
        # Only the menu item changes. The menu bar icon stays the same on
        # purpose: the menu bar is visible in a full-screen share, and the
        # hidden panel is what tells you Meeting Mode is listening.
        self._meeting_item.title = ("Stop Meeting Mode (listening now)" if active
                                    else "Start Meeting Mode…")

    def _on_set_meeting_key(self, _item) -> None:
        try:
            host = brain_host(config_mod.load()["meeting"]["base_url"])
        except (BrainError, Exception) as exc:
            _alert(title="Meeting API Key",
                        message=f"Fix meeting.base_url in config.toml first: {exc}")
            return
        try:
            saved = keychain.get_key(account=host) is not None
        except keychain.KeychainError:
            saved = False
        text = dialogs.ask_secret(
            f"API Key for {host}",
            f"Paste your API key for {host}. It is stored in your macOS "
            "Keychain, not in any file, and is only ever sent to that "
            "address.\n"
            + ("A key is already saved. Leave this empty and click Save to "
               "remove it." if saved else "No key is saved yet."))
        if text is None:
            return
        key = text.strip()
        if key and (len(key) < 8 or any(c.isspace() for c in key)):
            _alert(title="Meeting API Key",
                   message="That does not look like an API key (it has "
                           "spaces or is very short). Nothing was changed.")
            return
        try:
            if key:
                keychain.set_key(key, account=host)
                _alert(title="Meeting API Key", message="Saved to your Keychain.")
            elif saved:
                keychain.delete_key(account=host)
                _alert(title="Meeting API Key", message="Removed from your Keychain.")
        except (keychain.KeychainError, ValueError) as exc:
            _alert(title="Meeting API Key", message=f"Not saved: {exc}")

    def _on_toggle_raw_mode(self, item) -> None:
        self._raw_mode = not self._raw_mode
        item.state = 1 if self._raw_mode else 0
        config_mod.save_setting("general", "raw_mode", self._raw_mode)

    def _on_latency_stats(self, _item) -> None:
        report = latency.format_stats()
        print(f"[dictate]\n{report}")
        _alert(title="Dictate Latency (p50 / p95)", message=report)

    def _update_info_item(self) -> None:
        label = HOTKEY_LABELS[self._hotkey_name]
        self._info_item.title = (f"Dictate v{__version__} — tap {label} "
                                 f"to start / stop")

    def _on_toggle_enabled(self, item) -> None:
        self._enabled = not self._enabled
        item.state = 1 if self._enabled else 0
        if self._enabled:
            return
        # Disabling mid-recording aborts it — the hotkey is ignored while
        # disabled, so the recording could otherwise never be stopped.
        self._events.put("discard")

    def _on_retranscribe_last(self, _item) -> None:
        """Transcribe the newest saved recording again -> clipboard + history."""
        saved = recorder.saved_recordings()
        with self._state_lock:
            if self._recording:
                saved = saved[:-1]  # the newest file is still being written
        if not saved:
            _alert(title="Dictate", message="No saved recordings yet.")
            return
        self._enqueue(saved[-1], 0.0, None, recovered=True)

    def _on_choose_hotkey(self, item) -> None:
        new_key = next(k for k, lbl in HOTKEY_LABELS.items()
                       if lbl == item.title)
        if new_key == self._hotkey_name:
            return
        old = self._hotkey
        new = self._make_listener(new_key)
        new.start()
        self._hotkey = new  # assigned once alive, so the health check never sees a dead one
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
        _alert(title="Launch Dictate at Login",
                    message=LAUNCH_AT_LOGIN_TEXT)

    def start_background_services(self) -> None:
        threading.Thread(target=micstream.prepare, daemon=True,
                         name="dictate-mic-prepare").start()
        atexit.register(micstream.shutdown)
        threading.Thread(target=transcriber.warmup, args=(self._model,),
                         daemon=True, name="dictate-warmup").start()
        ollama_warmup = threading.Thread(
            target=cleanup.warmup, args=(self._config,), daemon=True,
            name="dictate-ollama-warmup")
        ollama_warmup.start()
        threading.Thread(target=self._worker, daemon=True,
                         name="dictate-pipeline").start()
        threading.Thread(target=self._idle_unloader, daemon=True,
                         name="dictate-idle-unload").start()
        threading.Thread(target=self._recover_unfinished, args=(ollama_warmup,),
                         daemon=True, name="dictate-recover").start()
        self._hotkey.start()
        threading.Thread(target=self._controller, daemon=True,
                         name="dictate-controller").start()

    def _recover_unfinished(self, ollama_warmup: threading.Thread) -> None:
        """Recordings whose text never reached history (the app froze, crashed
        or was restarted mid-dictation) are transcribed now."""
        unfinished = recorder.unfinished_recordings()
        if not unfinished:
            return
        ollama_warmup.join(40)  # so recovered text gets cleaned up too
        for path in unfinished:
            print(f"[dictate] recovering unfinished recording {path.name} "
                  f"({recorder.duration_s(path):.0f}s)")
            self._enqueue(path, 0.0, None, recovered=True)

    def _idle_unloader(self) -> None:
        while True:
            time.sleep(60)
            with self._state_lock:
                busy = self._recording or self._pending > 0
            if not busy:
                try:
                    transcriber.unload_if_idle()
                except Exception as exc:
                    print(f"[dictate] idle unload failed: {exc}", file=sys.stderr)

    # AppKit UI may only be touched from the main thread; the controller
    # and the pipeline worker run on background threads.
    def _refresh_icon(self) -> None:
        with self._state_lock:
            if self._recording:
                icon = ICON_RECORDING
            elif self._pending:
                icon = ICON_BUSY
            else:
                icon = ICON_IDLE
        stop = self._on_stop_dictation if icon == ICON_RECORDING else None

        def apply() -> None:
            self.title = icon
            self._stop_item.set_callback(stop)
        AppHelper.callAfter(apply)

    def _on_stop_dictation(self, _item) -> None:
        self._events.put("stop")

    # ------------------------------------------------------- controller --
    def _controller(self) -> None:
        while True:
            try:
                event = self._events.get(timeout=1.0)
            except queue.Empty:
                event = "tick"
            try:
                self._handle_event(event)
            except Exception:
                print(f"[dictate] controller failed on {event!r}:", file=sys.stderr)
                traceback.print_exc()

    def _handle_event(self, event: str) -> None:
        with self._state_lock:
            recording = self._recording
        if event == "toggle":
            if not self._enabled:
                return
            if recording:
                self._finish_recording("tap")
            else:
                self._start_recording()
        elif event == "stop":
            if recording:
                self._finish_recording("menu")
        elif event == "discard":
            if recording:
                self._finish_recording("disabled", discard=True)
        elif event == "tick":
            if recording and self._recorder.stalled:
                sounds.play("error")
                print("[dictate] microphone stopped sending audio — stopping "
                      "and transcribing what was captured", file=sys.stderr)
                self._finish_recording("mic stalled")
            elif recording and self._recorder.elapsed > MAX_RECORDING_S:
                self._finish_recording("15-minute limit")
            if not self._hotkey.is_alive():
                print("[dictate] hotkey listener died — restarting it",
                      file=sys.stderr)
                new = self._make_listener(self._hotkey_name)
                new.start()
                self._hotkey = new

    def _start_recording(self) -> None:
        try:
            self._recorder.start()
        except MicrophoneError as exc:
            sounds.play("error")
            print(f"[dictate] microphone unavailable: {exc}\n"
                  "  If this is the first run: System Settings -> Privacy & "
                  "Security -> Microphone -> enable Terminal, then relaunch.",
                  file=sys.stderr)
            return
        with self._state_lock:
            self._recording = True
        sounds.play("start")
        self._refresh_icon()
        print("[dictate] recording started")
        # Muting the Mac mid-meeting would silence the other people.
        if self._muter is not None and not self._meeting_active():
            self._muter.mute_async()
        # Both models are freed when idle; reload them while the user talks.
        transcriber.preload(self._model)
        if not self._raw_mode:
            threading.Thread(target=cleanup.preload, args=(self._config,),
                             daemon=True, name="dictate-preload").start()

    def _finish_recording(self, reason: str, discard: bool = False) -> None:
        """Stop the recorder and queue the audio for transcription."""
        t0 = time.monotonic()
        path = self._recorder.stop()
        record_ms = (time.monotonic() - t0) * 1000
        with self._state_lock:
            self._recording = False
        # Unmute now so the video/music resumes the moment dictation ends —
        # and so the stop sound is audible.
        if self._muter is not None:
            self._muter.unmute_async()
        if path is None or discard:  # accidental tap, or dictation disabled
            if path is not None:
                path.unlink(missing_ok=True)
            self._refresh_icon()
            print(f"[dictate] recording discarded ({reason}, too short or disabled)")
            return
        sounds.play("stop")
        print(f"[dictate] recording stopped ({reason}): "
              f"{recorder.duration_s(path):.1f}s of audio")
        # Capture the app that will receive the paste NOW, while it's frontmost
        self._enqueue(path, record_ms, appdetect.frontmost_bundle_id())

    # --------------------------------------------------------- pipeline --
    def _enqueue(self, path, record_ms: float, bundle_id: str | None,
                 recovered: bool = False) -> None:
        with self._state_lock:
            self._pending += 1
        self._refresh_icon()
        self._jobs.put((path, record_ms, bundle_id, recovered))

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                self._process(*job)
            except Exception:
                traceback.print_exc()
            finally:
                with self._state_lock:
                    self._pending -= 1
                self._refresh_icon()

    def _process(self, path, record_ms: float, bundle_id: str | None = None,
                 recovered: bool = False) -> None:
        """Transcribe -> clean up -> paste -> history.

        `recovered` recordings (left over from a crash, or re-transcribed
        from the menu) go to the clipboard instead of being pasted: the
        cursor is no longer where the user was dictating.
        """
        try:
            t1 = time.monotonic()
            raw = transcriber.transcribe(
                str(path), self._model,
                transcriber.vocabulary_prompt(self._config.get("vocabulary", {})),
                self._config["whisper"].get("language") or None)
            if raw and transcriber.looks_like_hallucination(raw, str(path)):
                print("[dictate] dropped a whisper hallucination on quiet audio")
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
                # chat apps (Slack, Messages) get one plain message
                if (mode != "casual"
                        and self._config["general"].get("smart_formatting", True)):
                    text = layout.format_layout(text)
            if recovered:
                stage += "+recovered"
            cleanup_ms = (time.monotonic() - t2) * 1000

            paste_ms = 0.0
            if text:
                # History first: once it's there the dictation can't be lost,
                # whatever happens to the paste.
                history.append(raw=raw, text=text, stage=stage, latency_ms={
                    "finalize": record_ms, "transcribe": transcribe_ms,
                    "cleanup": cleanup_ms, "paste": 0,
                })
                AppHelper.callAfter(self._refresh_history_menu)
                t3 = time.monotonic()
                if recovered:
                    paster.set_clipboard_text(text)
                    sounds.play("stop")
                    self._notify("Recovered a dictation",
                                 "Copied to your clipboard and saved in History.")
                else:
                    paster.paste(text)
                paste_ms = (time.monotonic() - t3) * 1000
                recorder.mark_done(path)
            else:
                print("[dictate] empty transcript (silence?) — nothing to paste")
                path.unlink(missing_ok=True)

            print(f"[dictate] latency ms — finalize: {record_ms:.0f}  "
                  f"transcribe: {transcribe_ms:.0f}  "
                  f"cleanup: {cleanup_ms:.0f} ({stage})  "
                  f"paste: {paste_ms:.0f}  | {len(text)} chars")
        except Exception:
            sounds.play("error")
            print("[dictate] transcription pipeline failed — the audio is kept; "
                  "use 'Re-transcribe Last Recording' in the menu:",
                  file=sys.stderr)
            traceback.print_exc()
            if recovered:
                recorder.mark_done(path)  # don't retry it on every launch
        finally:
            recorder.prune()

    def _notify(self, title: str, message: str) -> None:
        def show() -> None:
            try:
                rumps.notification("Dictate", title, message)
            except Exception as exc:  # needs an app bundle on some setups
                print(f"[dictate] {title}: {message} (notification failed: {exc})")
        AppHelper.callAfter(show)
