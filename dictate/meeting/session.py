"""One meeting: two audio streams -> live transcript -> catch-ups and
talking points in the hidden panel.

    mic ("You") ─────────┐
                         ├─ Chunker ─ Whisper ─ Transcript ─ Brain ─ Overlay
    system audio ("Them")┘

Nothing said in the meeting is printed, logged or written to disk here (see
dictate/meeting/__init__.py). `stop()` wipes the transcript from memory.
"""

import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from dictate import cleanup, recorder, transcriber
from dictate.config import PROJECT_ROOT
from dictate.meeting.brain import Brain, BrainError, log_failure
from dictate.meeting.live import Chunker, Transcript
from dictate.meeting.sysaudio import SystemAudio, SystemAudioError

NOTES_PATH = PROJECT_ROOT / "meeting_notes.md"
SAVE_DIR = (Path.home() / "Library" / "Application Support" / "Dictate"
            / "meetings")
KEEP_SAVED = 10
MAX_MEETING_S = 4 * 60 * 60     # a forgotten session stops itself
MAX_BACKLOG = 8                 # utterances waiting for Whisper
TURN_COOLDOWN_S = 15            # between automatic "your turn" checks
ECHO_GATE_S = 0.5               # ignore the mic this long after speaker sound
HELPER_RESTARTS = 3


class MeetingError(Exception):
    """Message is written for the user."""


class MeetingSession:
    def __init__(self, config: dict, model: str, overlay,
                 brain: Brain | None = None, system_audio_factory=SystemAudio,
                 mic_factory=recorder._default_stream,
                 transcribe=transcriber.transcribe_samples,
                 on_stopped=None, hotkey_factory=None,
                 mic_paused=None) -> None:
        self._config = config
        self._cfg = config.get("meeting", {})
        self._model = model
        self._overlay = overlay
        self._brain = brain or Brain(self._cfg)
        self._system_audio_factory = system_audio_factory
        self._mic_factory = mic_factory
        self._transcribe = transcribe
        self._on_stopped = on_stopped
        self._hotkey_factory = hotkey_factory
        self._mic_paused = mic_paused or (lambda: False)
        self._lock = threading.Lock()
        self._run: object | None = None  # identity of the live session
        self.transcript = Transcript()
        self.summary = ""
        self.say = ""
        self.note = ""

    @property
    def active(self) -> bool:
        return self._run is not None

    # -------------------------------------------------------------- start --
    def start(self) -> None:
        with self._lock:
            if self._run is not None:
                return
            run = object()
            self.transcript = Transcript()
            self.summary = self.say = self.note = ""
            self._jobs: queue.Queue = queue.Queue()
            self._thoughts: queue.Queue = queue.Queue()
            self._summarized = 0
            self._started = time.monotonic()
            self._next_catch_up = self._started + self._summary_s()
            self._last_turn_check = 0.0
            self._restarts = 0
            self._dropped = 0
            self._notes = self._read_notes()
            self._brain_problem = self._brain.problem()
            self._brain_ready = False
            name = (self._cfg.get("your_name") or "").strip()
            self._name_re = (re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
                             if name else None)
            terms = list(dict.fromkeys(
                [v for v in self._config.get("vocabulary", {}).values() if v]
                + ([name] if name else [])))
            self._prompt = f"Vocabulary: {', '.join(terms)}." if terms else None
            self._headphones = bool(self._cfg.get("headphones", False))

            self._them = Chunker(lambda audio, at: self._queue("Them", audio, at, run),
                                 base_threshold=60.0)
            self._you = Chunker(lambda audio, at: self._queue("You", audio, at, run))
            self._system_audio = self._system_audio_factory(
                lambda data: self._on_system_audio(data, run))
            try:
                self._system_audio.start()
            except (SystemAudioError, OSError) as exc:
                raise MeetingError(str(exc)) from exc
            try:
                self._mic = self._mic_factory(
                    lambda indata, *_: self._on_mic(indata, run))
                self._mic.start()
            except Exception as exc:
                self._system_audio.stop()
                raise MeetingError(f"could not open the microphone "
                                   f"({type(exc).__name__})") from exc
            self._hotkey = None
            if self._hotkey_factory is not None:
                try:
                    self._hotkey = self._hotkey_factory(lambda: self.ask_now())
                    self._hotkey.start()
                except Exception as exc:
                    print(f"[meeting] suggestion hotkey unavailable: {exc!r}",
                          file=sys.stderr)
            self._run = run
        for target, name_ in ((self._transcriber, "meeting-transcribe"),
                              (self._thinker, "meeting-think"),
                              (self._ticker, "meeting-tick")):
            threading.Thread(target=target, args=(run,), daemon=True,
                             name=name_).start()
        self._overlay.show()
        self._think("check")  # proves the key and model work before they are needed
        self._render()
        print("[meeting] started"
              + ("" if not self._brain_problem
                 else f" (suggestions off: {self._brain_problem})"))

    def _summary_s(self) -> float:
        try:
            minutes = float(self._cfg.get("summary_minutes", 3))
        except (TypeError, ValueError):
            minutes = 3
        return max(1.0, minutes) * 60

    def _read_notes(self) -> str:
        try:
            return NOTES_PATH.read_text(encoding="utf-8")[:4000]
        except OSError:
            return ""

    # -------------------------------------------------------------- audio --
    def _on_system_audio(self, data: bytes, run: object) -> None:
        them = self._them
        if run is self._run and them is not None:
            them.feed(np.frombuffer(data[:len(data) // 2 * 2], dtype=np.int16))

    def _on_mic(self, indata, run: object) -> None:
        if run is not self._run:
            return
        # Without headphones the mic hears the other people through the
        # speakers; ignore it while they are talking so their words are not
        # transcribed twice and credited to "You".
        them, you = self._them, self._you
        if them is None or you is None:
            return
        echo = (not self._headphones
                and time.monotonic() - them.last_loud_at < ECHO_GATE_S)
        you.feed(np.array(indata, dtype=np.int16).reshape(-1),
                 muted=echo or self._mic_paused())

    def _queue(self, speaker: str, audio, at: float, run: object) -> None:
        if run is not self._run:
            return
        # Staying live matters more than completeness: if Whisper falls
        # behind, the oldest waiting utterance is dropped.
        while self._jobs.qsize() >= MAX_BACKLOG:
            try:
                self._jobs.get_nowait()
                self._dropped += 1
            except queue.Empty:
                break
        self._jobs.put((speaker, audio, at))

    # ------------------------------------------------------------ workers --
    def _transcriber(self, run: object) -> None:
        language = self._config.get("whisper", {}).get("language") or None
        vocabulary = self._config.get("vocabulary", {})
        while run is self._run:
            try:
                speaker, audio, at = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                text = self._transcribe(audio, self._model, self._prompt,
                                        language).strip()
            except Exception as exc:
                # type only: an error message must never carry meeting text
                print(f"[meeting] transcription failed: {type(exc).__name__}",
                      file=sys.stderr)
                continue
            if run is not self._run or not text:
                continue
            if (text.lower().strip(" .!?") in transcriber._HALLUCINATIONS
                    or text.lower().startswith("vocabulary")):
                continue  # Whisper's inventions on noise, not speech
            text = cleanup.apply_vocabulary(text, vocabulary)
            self.transcript.add(speaker, text, at)
            if speaker == "Them" and self._needs_answer(text):
                now = time.monotonic()
                if now - self._last_turn_check >= TURN_COOLDOWN_S:
                    self._last_turn_check = now
                    self._think("turn")
            self._render()

    def _needs_answer(self, text: str) -> bool:
        if "?" in text:
            return True
        return bool(self._name_re and self._name_re.search(text))

    def _think(self, kind: str) -> None:
        if self._brain_problem is None and kind not in list(self._thoughts.queue):
            self._thoughts.put(kind)

    def _thinker(self, run: object) -> None:
        while run is self._run:
            try:
                kind = self._thoughts.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                recent = self.transcript.render(max_chars=3000)
                if kind == "check":
                    self._brain.check()
                elif kind == "catchup":
                    count = len(self.transcript)
                    new = self.transcript.render(start=self._summarized)
                    if not new.strip():
                        continue
                    result = self._brain.catch_up(self.summary, new, self._notes)
                    if run is self._run and result:
                        self.summary, self._summarized = result, count
                elif kind == "turn":
                    result = self._brain.your_turn(self.summary, recent, self._notes)
                    if run is self._run and result:
                        self.say = result
                elif kind == "ask":
                    self.say = "Thinking…"
                    self._render()
                    result = self._brain.what_to_say(self.summary, recent, self._notes)
                    if run is self._run:
                        self.say = result
                self.note = ""
                self._brain_ready = True  # any answer proves the service works
            except BrainError as exc:
                log_failure(kind, exc)
                self.note = f"Suggestions paused: {exc}"
                if self.say == "Thinking…":
                    self.say = ""
            except Exception as exc:
                log_failure(kind, exc)
            if run is self._run:
                self._render()

    def _ticker(self, run: object) -> None:
        last_render = 0.0
        while run is self._run:
            time.sleep(1.0)
            now = time.monotonic()
            if run is not self._run:
                return
            # The panel is the only sign that Meeting Mode is listening. If
            # it cannot be shown (hidden from sharing), don't listen at all.
            if getattr(self._overlay, "failed", ""):
                self.stop(self._overlay.failed)
                return
            if now - self._started > MAX_MEETING_S:
                self.stop("4-hour limit")
                return
            if not self._system_audio.is_alive():
                failure = self._system_audio.failure
                if "permission" in failure.lower() or self._restarts >= HELPER_RESTARTS:
                    self.stop(failure)
                    return
                self._restarts += 1
                print(f"[meeting] audio helper stopped — restarting "
                      f"({self._restarts}/{HELPER_RESTARTS})", file=sys.stderr)
                try:
                    self._system_audio.start()
                except (SystemAudioError, OSError) as exc:
                    self.stop(str(exc))
                    return
            if now >= self._next_catch_up:
                self._next_catch_up = now + self._summary_s()
                if len(self.transcript) > self._summarized:
                    self._think("catchup")
            if now - last_render >= 10:
                last_render = now
                self._render()

    # ------------------------------------------------------------ display --
    def ask_now(self) -> None:
        """The "what should I say?" hotkey."""
        if not self.active:
            return
        if self._brain_problem:
            self.note = f"Suggestions are off: {self._brain_problem}"
            self._render()
            return
        self._think("ask")

    def _render(self) -> None:
        if not self.active:
            return
        elapsed = int(time.monotonic() - self._started)
        status = f"Listening {elapsed // 60}:{elapsed % 60:02d} · hidden from screen share"
        if self._brain_problem:
            status += f"\nTranscript only — {self._brain_problem}"
        elif self._brain_ready:
            status += "\nSuggestions on"
        elif not self.note:
            status += "\nChecking the suggestion service…"
        if self._dropped:
            status += f"\nSkipped {self._dropped} utterances to stay live"
        if self.note:
            status += f"\n{self.note}"
        live = self.transcript.render(max_chars=420)
        self._overlay.render([
            ("Meeting mode", status),
            ("Say this", self.say),
            ("Catch-up", self.summary),
            ("Live", live or "Waiting for someone to speak…"),
        ])

    # --------------------------------------------------------------- stop --
    def stop(self, reason: str = "") -> None:
        with self._lock:
            if self._run is None:
                return
            self._run = None  # every worker and audio callback now stands down
            system_audio, mic, hotkey = self._system_audio, self._mic, self._hotkey
        system_audio.stop()

        def close_mic() -> None:  # can block if the device vanished
            try:
                mic.abort()
                mic.close()
            except Exception:
                pass
        threading.Thread(target=close_mic, daemon=True,
                         name="meeting-mic-close").start()
        if hotkey is not None:
            try:
                hotkey.stop()
            except Exception:
                pass
        lines = len(self.transcript)
        try:
            if self._cfg.get("save_transcript", False) and lines:
                self._save()
        except OSError as exc:
            print(f"[meeting] could not save the transcript: "
                  f"{type(exc).__name__}", file=sys.stderr)
        self.transcript.wipe()
        self.summary = self.say = self.note = self._notes = ""
        # buffered audio goes too: queued utterances and partial chunks
        self._jobs = queue.Queue()
        self._thoughts = queue.Queue()
        self._them = self._you = None
        self._overlay.hide()
        print(f"[meeting] stopped ({reason or 'by user'}); {lines} lines "
              "discarded from memory")
        if self._on_stopped is not None:
            try:
                self._on_stopped(reason)
            except Exception:
                traceback.print_exc()

    def _save(self) -> None:
        """Only when the user opted in. Private to this account (0600)."""
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        SAVE_DIR.chmod(0o700)
        path = SAVE_DIR / time.strftime("meeting-%Y%m%d-%H%M%S.txt")
        body = self.transcript.render(stamps=True)
        if self.summary:
            body = f"SUMMARY\n{self.summary}\n\nTRANSCRIPT\n{body}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body + "\n")
        for old in sorted(SAVE_DIR.glob("meeting-*.txt"))[:-KEEP_SAVED]:
            old.unlink(missing_ok=True)
