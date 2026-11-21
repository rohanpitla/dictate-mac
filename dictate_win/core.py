"""The dictation controller: tap -> record -> transcribe -> clean up ->
lay out -> paste, with retry for dictations that couldn't be transcribed.

Kept free of any UI so it can be tested anywhere with stand-ins.

Rules carried over from the Mac version (each fixed a real bug there):
- A tap always toggles recording, even while earlier dictations are still
  being transcribed; those wait in a queue and are pasted in order.
- The hotkey thread only posts events; one controller thread does the
  microphone work, so a stuck audio device can never freeze the hotkey.
- Audio is on disk while recording; a dictation is only marked done once
  its text is in History. Nothing is lost to a crash or a network outage.
- Nothing that was said is ever logged.
"""

import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from dictate import history, layout, transcriber
from dictate import recorder as rec
from dictate.cleanup import apply_vocabulary
from dictate.recorder import MicrophoneError
from dictate_win.appdetect import STYLE_HINTS, resolve_mode
from dictate_win.engine_groq import EngineError

MAX_RECORDING_S = 15 * 60
MUTE_DELAY_S = 0.25          # let the start sound play before muting
KEEP_DONE = 5                # finished recordings kept for "Re-transcribe"
KEEP_UNFINISHED_DAYS = 7     # failed ones are kept this long for retry
RETRY_DELAYS_S = (20, 60, 120, 300)
MAX_OTHER_TRIES = 3

MIC_HELP = ("Dictate can't use the microphone. Open Windows Settings > "
            "Privacy & security > Microphone and turn on \"Microphone access\" "
            "and \"Let desktop apps access your microphone\". Also check that "
            "a microphone is connected.")
_FAILURE_TEXT = {
    "network": "No internet connection (or Groq is down). Your dictation is "
               "saved and will be transcribed automatically; it will appear "
               "in History.",
    "rate": "Groq's free limit is reached for the moment. Your dictation is "
            "saved and will be retried shortly; it will appear in History.",
    "auth": "Groq rejected the API key, or none is saved. Use the tray menu > "
            "Set Groq API key… Your dictation is saved and will be "
            "transcribed once the key works.",
    "model": "Groq no longer offers the speech model in settings.toml. "
             "Update it (see the README); your dictation is saved.",
    "other": "This dictation couldn't be transcribed. It is saved; use the "
             "tray menu > Retry failed dictations.",
}


def prune(directory: Path, keep_done: int = KEEP_DONE,
          keep_unfinished_days: float = KEEP_UNFINISHED_DAYS) -> None:
    """Delete old recordings, but never one still waiting to be transcribed
    unless it is older than keep_unfinished_days."""
    done = [p for p in rec.saved_recordings(directory) if rec.is_done(p)]
    for old in done[:-keep_done] if keep_done else done:
        old.unlink(missing_ok=True)
    cutoff = time.time() - keep_unfinished_days * 86400
    for path in rec.unfinished_recordings(directory):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def mic_problem_text(error: str) -> str:
    return f"{MIC_HELP}\n\n(Details: {error[:160]})"


class Core:
    def __init__(self, cfg: dict, recorder, recordings_dir: Path, client,
                 cleaner, local=None, local_available=lambda: False,
                 muter=None, paste_fn=None, foreground_fn=None,
                 notify=None, sound=None, on_change=None) -> None:
        self.cfg = cfg
        self.recorder = recorder
        self.dir = recordings_dir
        self.client = client
        self.cleaner = cleaner
        self.local = local
        self.local_available = local_available
        self.muter = muter
        self.paste_fn = paste_fn or (lambda text: "pasted")
        self.foreground_fn = foreground_fn or (lambda: ("", ""))
        self.notify = notify or (lambda title, text: print(f"[notice] {title}: {text}"))
        self.sound = sound or (lambda name: None)
        self.on_change = on_change or (lambda: None)

        self.enabled = True
        self.raw_mode = bool(cfg["general"].get("raw_mode", False))
        self.events: queue.Queue = queue.Queue()
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self.recording = False
        self.pending = 0
        self._session = 0
        # path -> {"next": monotonic time, "tries": n, "kind": str}
        self.failed: dict[Path, dict] = {}
        self._in_flight: set[Path] = set()
        self.hotkey_alive = lambda: True
        self.restart_hotkey = lambda: None

    # ------------------------------------------------------------ threads --
    def start(self) -> None:
        for target, name in ((self._controller, "dictate-controller"),
                             (self._worker, "dictate-pipeline"),
                             (self._retrier, "dictate-retry")):
            threading.Thread(target=target, daemon=True, name=name).start()
        for path in rec.unfinished_recordings(self.dir):
            print(f"[dictate] found an unfinished dictation ({rec.duration_s(path):.0f}s); "
                  "it will be transcribed")
            with self._lock:
                self.failed[path] = {"next": time.monotonic(), "tries": 0,
                                     "kind": "startup"}

    def _controller(self) -> None:
        while True:
            try:
                event = self.events.get(timeout=1.0)
            except queue.Empty:
                event = "tick"
            try:
                self.handle_event(event)
            except Exception:
                print(f"[dictate] controller failed on {event!r}:", file=sys.stderr)
                traceback.print_exc()

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                self.process(*job)
            except Exception:
                traceback.print_exc()
            finally:
                with self._lock:
                    self.pending -= 1
                    self._in_flight.discard(job[0])
                self.on_change()

    def _retrier(self) -> None:
        while True:
            time.sleep(2.0)
            try:
                self.retry_due()
            except Exception:
                traceback.print_exc()

    # ------------------------------------------------------------- events --
    def handle_event(self, event: str) -> None:
        with self._lock:
            recording = self.recording
        if event == "toggle":
            if not self.enabled:
                return
            if recording:
                self._finish("tap")
            else:
                self._start()
        elif event == "stop":
            if recording:
                self._finish("menu")
        elif event == "discard":
            if recording:
                self._finish("disabled", discard=True)
        elif event == "tick":
            if recording and self.recorder.stalled:
                self.sound("error")
                print("[dictate] the microphone stopped sending audio; stopping "
                      "and transcribing what was captured", file=sys.stderr)
                self._finish("mic stalled")
            elif recording and self.recorder.elapsed > MAX_RECORDING_S:
                self._finish("15-minute limit")
            if not self.hotkey_alive():
                print("[dictate] hotkey listener stopped; restarting it", file=sys.stderr)
                self.restart_hotkey()

    def _start(self) -> None:
        try:
            self.recorder.start()
        except MicrophoneError as exc:
            self.sound("error")
            print(f"[dictate] microphone unavailable: {exc}", file=sys.stderr)
            self.notify("Microphone unavailable", mic_problem_text(str(exc)))
            return
        with self._lock:
            self.recording = True
            self._session += 1
            session = self._session
        self.sound("start")
        self.on_change()
        print("[dictate] recording started")
        if self.muter is not None:
            timer = threading.Timer(MUTE_DELAY_S, self._delayed_mute, args=(session,))
            timer.daemon = True
            timer.start()
        if self.cfg["general"].get("engine") == "local" and self.local is not None:
            self.local.preload()

    def _delayed_mute(self, session: int) -> None:
        # Under the lock, so a stop can't slip in between the check and the
        # mute request (the unmute is then always queued after it).
        with self._lock:
            if self.recording and self._session == session:
                self.muter.mute_async()

    def _finish(self, reason: str, discard: bool = False) -> None:
        t0 = time.monotonic()
        path = self.recorder.stop()
        record_ms = (time.monotonic() - t0) * 1000
        with self._lock:
            self.recording = False
        if self.muter is not None:
            self.muter.unmute_async()
        if path is None or discard:
            if path is not None:
                path.unlink(missing_ok=True)
            self.on_change()
            print(f"[dictate] recording discarded ({reason}, too short or disabled)")
            return
        self.sound("stop")
        print(f"[dictate] recording stopped ({reason}): {rec.duration_s(path):.1f}s of audio")
        try:
            target = self.foreground_fn()  # the app the text goes to, captured now
        except Exception:
            target = ("", "")
        self.enqueue(path, record_ms, target, recovered=False)

    def enqueue(self, path: Path, record_ms: float, target, recovered: bool) -> None:
        with self._lock:
            if path in self._in_flight:
                return
            self._in_flight.add(path)
            self.pending += 1
        self.on_change()
        self._jobs.put((path, record_ms, target, recovered))

    # -------------------------------------------------------------- retry --
    def retry_due(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            busy = self.recording
            due = sorted(p for p, info in self.failed.items()
                         if (force or info["next"] <= now) and p not in self._in_flight)
        if busy and not force:
            return
        for path in due:
            if not path.exists():
                self._forget(path)
                continue
            self.enqueue(path, 0.0, ("", ""), recovered=True)

    def _forget(self, path: Path) -> None:
        with self._lock:
            self.failed.pop(path, None)

    def failed_count(self) -> int:
        with self._lock:
            return len(self.failed)

    def key_changed(self) -> None:
        """A new key was saved: retry what failed for lack of one."""
        with self._lock:
            for info in self.failed.values():
                if info["kind"] in ("auth", "model"):
                    info["next"] = time.monotonic()

    def _record_failure(self, path: Path, kind: str, retry_after: float = 0.0) -> None:
        with self._lock:
            info = self.failed.get(path, {"tries": 0})
        tries = info["tries"] + 1
        if kind in ("auth", "model"):
            wait = float("inf")  # until the key or settings are fixed
        elif kind == "other" and tries >= MAX_OTHER_TRIES:
            wait = float("inf")  # stays listed; "Retry failed dictations" forces it
        else:
            wait = max(retry_after, RETRY_DELAYS_S[min(tries - 1, len(RETRY_DELAYS_S) - 1)])
        with self._lock:
            self.failed[path] = {"next": time.monotonic() + wait, "tries": tries,
                                 "kind": kind}

    # ----------------------------------------------------------- pipeline --
    def _transcribe(self, samples) -> tuple[str, str]:
        prompt = transcriber.vocabulary_prompt(self.cfg.get("vocabulary", {}))
        language = self.cfg["general"].get("language") or None
        use_local = self.cfg["general"].get("engine") == "local"
        if use_local:
            if self.local is None or not self.local_available():
                raise EngineError("other", "the local engine is not installed; "
                                           "switch the speech engine to Groq")
            return self.local.transcribe(samples, prompt, language), "local"
        try:
            return self.client.transcribe(samples, prompt, language), "groq"
        except EngineError as exc:
            if exc.retryable and self.local is not None and self.local_available():
                print(f"[dictate] {exc}; transcribing on this laptop instead",
                      file=sys.stderr)
                return self.local.transcribe(samples, prompt, language), "local-fallback"
            raise

    def process(self, path: Path, record_ms: float, target, recovered: bool) -> None:
        """Transcribe -> clean up -> lay out -> history -> paste.

        Recovered/retried dictations go to History (and a notice) instead of
        being typed: the cursor is no longer where the user was speaking.
        """
        try:
            t1 = time.monotonic()
            samples = transcriber._load_wav(str(path))
            if len(samples) and not np.any(samples):
                # Exactly zero audio: Windows' microphone privacy switch
                # blocks desktop apps this way, rather than with an error.
                self.sound("error")
                self.notify("No sound from the microphone", MIC_HELP)
                path.unlink(missing_ok=True)
                self._forget(path)
                return
            try:
                raw, engine = self._transcribe(samples)
            except EngineError as exc:
                self._record_failure(path, exc.kind, exc.retry_after)
                print(f"[dictate] transcription failed ({exc.kind}: {exc})", file=sys.stderr)
                if not recovered or exc.kind in ("auth", "model"):
                    self.sound("error")
                    self.notify("Dictation saved for later", _FAILURE_TEXT.get(exc.kind, str(exc)))
                return
            if raw and transcriber.looks_like_hallucination(raw, str(path)):
                print("[dictate] dropped a phantom phrase on quiet audio")
                raw = ""
            transcribe_ms = (time.monotonic() - t1) * 1000

            t2 = time.monotonic()
            program, title = target if target else ("", "")
            mode = resolve_mode(program, title, self.cfg.get("app_modes"))
            general = self.cfg["general"]
            if self.raw_mode or mode == "code":
                text, stage = raw, ("raw-mode" if self.raw_mode else "code-app")
            else:
                can_clean = (self.cfg["groq"].get("cleanup", True)
                             and engine != "local-fallback" and self.client.has_key())
                if can_clean:
                    text, stage = self.cleaner.clean(raw, STYLE_HINTS.get(mode))
                else:
                    text, stage = raw, "raw"
                text = apply_vocabulary(text, self.cfg.get("vocabulary", {}))
                if mode != "default":
                    stage = f"{stage}+{mode}"
                if general.get("smart_formatting", True):
                    style = general.get("layout_style", "points")
                    if mode == "email":
                        style = "paragraphs"
                    text = layout.format_layout(text, style, chat=(mode == "casual"),
                                                email=(mode == "email"))
            stage = f"{engine}/{stage}" + ("+recovered" if recovered else "")
            cleanup_ms = (time.monotonic() - t2) * 1000

            paste_ms, result = 0.0, "none"
            if text:
                # History first: once it's there the dictation can't be lost.
                history.append(raw=raw, text=text, stage=stage, latency_ms={
                    "finalize": record_ms, "transcribe": transcribe_ms,
                    "cleanup": cleanup_ms, "paste": 0})
                t3 = time.monotonic()
                if recovered:
                    self.sound("stop")
                    self.notify("Dictation recovered",
                                "A saved dictation was transcribed. Find it in the "
                                "tray menu > History (click it to copy).")
                    result = "history"
                else:
                    result = self.paste_fn(text)
                    if result == "clipboard":
                        self.notify("Press Ctrl+V to paste",
                                    "This app runs as administrator, so Windows "
                                    "doesn't let Dictate type into it. Your text "
                                    "is on the clipboard.")
                paste_ms = (time.monotonic() - t3) * 1000
                rec.mark_done(path)
            else:
                print("[dictate] nothing was said (silence) — nothing to paste")
                path.unlink(missing_ok=True)
            self._forget(path)
            print(f"[dictate] latency ms — finalize: {record_ms:.0f}  "
                  f"transcribe: {transcribe_ms:.0f}  cleanup: {cleanup_ms:.0f} "
                  f"({stage})  paste: {paste_ms:.0f} [{result}]  | {len(text)} chars")
        except Exception:
            self.sound("error")
            print("[dictate] the dictation could not be processed; the audio is "
                  "kept for a retry:", file=sys.stderr)
            traceback.print_exc()
            self._record_failure(path, "other")
        finally:
            prune(self.dir)
