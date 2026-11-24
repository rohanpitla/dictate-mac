"""Dictate for Windows self-test.

    python -m dictate_win.selftest            (on Windows: everything)
    python -m dictate_win.selftest            (on a Mac: the parts that
                                               aren't Windows-specific)

Each check prints PASS, FAIL or SKIP. SKIP means the check could not run
here (e.g. no microphone on a test server), never that it passed. The exit
code is 1 if anything failed.

Uses a throw-away data folder, a fake Groq server on 127.0.0.1, and fake
API keys. It never touches the real settings, history, or saved key.
"""

import contextlib
import faulthandler
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WINDOWS = sys.platform == "win32"
_TMP = Path(tempfile.mkdtemp(prefix="dictate-selftest-"))
os.environ["DICTATE_HOME"] = str(_TMP / "home")  # before importing dictate_win.paths

import numpy as np  # noqa: E402

results: dict[str, int] = {"PASS": 0, "FAIL": 0, "SKIP": 0}
SECRET = "gsk_SELFTEST_" + uuid.uuid4().hex


def report(status: str, name: str, detail: str = "") -> None:
    results[status] += 1
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    report("PASS" if ok else "FAIL", name, detail)


def skip(name: str, why: str) -> None:
    report("SKIP", name, why)


SECTION_TIMEOUT_S = 240


def section(title: str):
    def wrap(fn):
        def run():
            print(f"\n== {title} ==", flush=True)
            # A hung section prints every thread's stack and ends the run,
            # so CI shows where it stuck instead of timing out silently.
            faulthandler.dump_traceback_later(SECTION_TIMEOUT_S, exit=True)
            try:
                fn()
            except Exception as exc:
                report("FAIL", f"{title}: crashed", repr(exc))
                traceback.print_exc()
            finally:
                faulthandler.cancel_dump_traceback_later()
        run.__name__ = fn.__name__
        return run
    return wrap


def tone(seconds: float, freq: float = 300.0, amp: float = 0.3, rate: int = 16000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def speechlike(seconds: float):
    """Bursts of tone with pauses, so silence trimming keeps it."""
    parts = []
    for i in range(int(seconds)):
        parts.append(tone(0.7, 200 + 40 * (i % 5)))
        parts.append(np.zeros(4800, dtype=np.float32))
    return np.concatenate(parts)[: int(seconds * 16000)]


# --------------------------------------------------------------- fake Groq --
class FakeGroq:
    """A local stand-in for api.groq.com. `script` is a list of responses
    (status, json, headers) used in order; the last one repeats."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.script: list[tuple] = [(200, {"text": "ok", "segments": []}, {})]
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                fake.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": body})
                status, payload, headers = (fake.script.pop(0) if len(fake.script) > 1
                                            else fake.script[0])
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/openai/v1"

    def respond(self, *responses) -> None:
        self.requests.clear()
        self.script = list(responses)


def multipart_fields(body: bytes) -> dict:
    fields = {}
    for part in body.split(b"--")[1:]:
        if b'name="' not in part:
            continue
        name = part.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        value = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0] if b"\r\n\r\n" in part else b""
        fields[name] = value
    return fields


# ------------------------------------------------------------------ checks --
@section("1. Modules load")
def test_imports():
    import importlib

    neutral = ["dictate_win.paths", "dictate_win.settings", "dictate_win.resample",
               "dictate_win.engine_groq", "dictate_win.cleanup_cloud",
               "dictate_win.engine_local", "dictate_win.core", "dictate_win.hotkey",
               "dictate_win.appdetect", "dictate_win.mute", "dictate_win.sounds",
               "dictate_win.mic", "dictate_win.dialogs", "dictate_win.logsetup",
               "dictate_win.credentials", "dictate_win.paste"]
    windows_only = ["dictate_win.winapi", "dictate_win.app", "pystray", "pycaw.pycaw",
                    "sounddevice", "tkinter"]
    for name in neutral:
        try:
            importlib.import_module(name)
            check(f"import {name}", True)
        except Exception as exc:
            check(f"import {name}", False, repr(exc))
    for name in windows_only:
        if not WINDOWS:
            skip(f"import {name}", "Windows only")
            continue
        try:
            importlib.import_module(name)
            check(f"import {name}", True)
        except Exception as exc:
            check(f"import {name}", False, repr(exc))


@section("2. Settings")
def test_settings():
    from dictate_win import paths, settings

    cfg = settings.load()
    check("settings.toml is created with defaults on first run",
          paths.SETTINGS.exists() and cfg["general"]["hotkey"] == "right_ctrl"
          and cfg["general"]["engine"] == "groq")
    settings.save_setting("general", "hotkey", "f9")
    settings.save_setting("general", "raw_mode", True)
    settings.save_setting("vocabulary", "n 8 n", "n8n")
    cfg = settings.load()
    check("tray changes are saved and read back",
          cfg["general"]["hotkey"] == "f9" and cfg["general"]["raw_mode"] is True,
          str(cfg["general"]))
    text = paths.SETTINGS.read_text(encoding="utf-8")
    check("comments in settings.toml survive edits", "# Tap once to start" in text)
    paths.SETTINGS.write_text("[general\nhotkey = ", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        cfg = settings.load()
    check("a broken settings.toml falls back to defaults instead of crashing",
          cfg["general"]["hotkey"] == "right_ctrl" and "could not be read" in out.getvalue())
    paths.SETTINGS.write_text('[general]\nhotkey = "alt"\nengine = "x"\n', encoding="utf-8")
    cfg = settings.load()
    check("an unknown hotkey or engine falls back to the default",
          cfg["general"]["hotkey"] == "right_ctrl" and cfg["general"]["engine"] == "groq")
    paths.SETTINGS.unlink()


@section("3. Hotkey tap logic")
def test_taps():
    from dictate_win.hotkey import TapDetector

    taps = []
    d = TapDetector("right_ctrl", lambda: taps.append(1))
    d.key_event(0x11, True, False)
    d.key_event(0x11, True, False)  # Windows auto-repeat while held
    d.key_event(0x11, True, True)
    check("a Right Ctrl tap fires once, even with key repeat", taps == [1])
    taps.clear()
    d.key_event(0x11, True, False)
    d.key_event(0x43, False, False)  # C
    d.key_event(0x43, False, True)
    d.key_event(0x11, True, True)
    check("Right Ctrl + C (copy) does not start dictation", taps == [])
    d.key_event(0x11, False, False)
    d.key_event(0x11, False, True)
    check("Left Ctrl is not the hotkey", taps == [])
    d.set_hotkey("copilot")
    d.key_event(0x5B, False, False)  # Win  (the Copilot key sends Win+Shift+F23)
    d.key_event(0x10, False, False)  # Shift
    d.key_event(0x86, False, False)  # F23
    d.key_event(0x86, False, True)
    check("the Copilot key (Win+Shift+F23) counts as a tap", taps == [1])
    taps.clear()

    def boom():
        raise RuntimeError("handler bug")

    d2 = TapDetector("f9", boom)
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        d2.key_event(0x78, False, False)
        d2.key_event(0x78, False, True)
        d2.key_event(0x78, False, False)
        d2.key_event(0x78, False, True)
    check("an error in the tap handler is contained (the hotkey keeps working)",
          out.getvalue().count("hotkey handler failed") == 2)


@section("4. Microphone format conversion")
def test_resample():
    from dictate_win.resample import Resampler

    rng = np.random.default_rng(7)
    ok = True
    for rate in (48000, 44100, 32000, 22050, 96000, 16000):
        x = rng.standard_normal(rate * 2).astype(np.float32) * 0.1
        whole = Resampler(rate).process(x)
        r, out, i = Resampler(rate), [], 0
        while i < len(x):
            n = int(rng.integers(1, 6000))
            out.append(r.process(x[i:i + n]))
            i += n
        y = np.concatenate(out)
        ok &= len(y) == len(whole) == 32000 and np.allclose(y, whole, atol=1e-5)
    check("44.1/48/96 kHz microphones convert to 16 kHz exactly, block by block", ok)
    t = np.arange(48000) / 48000
    y = Resampler(48000).process((0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32))
    peak = int(np.argmax(np.abs(np.fft.rfft(y[:16000]))))
    check("a 1 kHz tone stays 1 kHz", peak == 1000, f"{peak} Hz")
    y = Resampler(48000).process((0.5 * np.sin(2 * np.pi * 12000 * t)).astype(np.float32))
    check("sound above 8 kHz is filtered out, not folded into speech",
          float(np.sqrt(np.mean(y[200:] ** 2))) < 0.01)


@section("5. Groq client (fake server)")
def test_groq():
    import requests

    from dictate_win.engine_groq import (EngineError, GroqClient, split_audio,
                                         validate_base_url)

    fake = FakeGroq()
    cfg = {"base_url": fake.url, "transcribe_model": "whisper-large-v3-turbo",
           "cleanup_model": "openai/gpt-oss-20b"}
    client = GroqClient(cfg, key_getter=lambda: SECRET)
    good_segment = {"text": " Send the invoice today.", "avg_logprob": -0.2,
                    "compression_ratio": 1.1}
    junk_segment = {"text": " !!!!!!!!", "avg_logprob": -1.5, "compression_ratio": 3.0}
    fake.respond((200, {"text": "x", "segments": [good_segment, junk_segment]}, {}))
    text = client.transcribe(speechlike(5), "Vocabulary: n8n, Twilio.", "en")
    req = fake.requests[0]
    fields = multipart_fields(req["body"])
    wav_ok = False
    if b"RIFF" in fields.get("file", b""):
        with wave.open(io.BytesIO(fields["file"][fields["file"].index(b"RIFF"):])) as wf:
            wav_ok = (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) == (16000, 1, 2)
    check("sends a 16 kHz mono WAV with model, language and vocabulary",
          wav_ok and fields.get("model") == b"whisper-large-v3-turbo"
          and fields.get("language") == b"en" and b"n8n" in fields.get("prompt", b""))
    check("the key goes in the Authorization header only",
          req["headers"].get("Authorization") == f"Bearer {SECRET}"
          and SECRET.encode() not in req["body"])
    check("Whisper's noise segments are dropped, speech kept",
          text == "Send the invoice today.", repr(text))

    fake.respond((200, {"text": "part", "segments": []}, {}))
    long = speechlike(15 * 60)
    client.transcribe(long, None, "en")
    sizes = [len(r["body"]) for r in fake.requests]
    check("a 15-minute dictation is sent in pieces under Groq's 25 MB limit",
          len(sizes) >= 4 and max(sizes) < 25 * 1024 * 1024,
          f"{len(sizes)} pieces, largest {max(sizes) / 1e6:.1f} MB")
    pieces = split_audio(long)
    check("pieces add up to the whole recording, each at most 4 minutes",
          sum(len(p) for p in pieces) == len(long)
          and max(len(p) for p in pieces) <= 240 * 16000)

    def error_of(*responses):
        fake.respond(*responses)
        try:
            client.transcribe(speechlike(3), None, "en")
            return None
        except EngineError as exc:
            return exc

    exc = error_of((401, {"error": {"message": "Invalid API Key"}}, {}))
    check("a wrong key is reported as 'auth' and never echoed",
          exc is not None and exc.kind == "auth" and SECRET not in str(exc))
    exc = error_of((404, {}, {}))
    check("a retired model is reported as 'model'", exc is not None and exc.kind == "model")
    exc = error_of((500, {}, {}), (500, {}, {}))
    check("Groq errors are retried once, then reported as retryable",
          exc is not None and exc.kind == "network" and exc.retryable
          and len(fake.requests) == 2)
    fake.respond((429, {}, {"retry-after": "1"}), (200, {"text": "after wait", "segments": []}, {}))
    check("a short rate-limit wait is waited out and retried",
          client.transcribe(speechlike(3), None, "en") == "after wait")
    exc = error_of((302, {}, {"Location": "https://evil.example/steal"}))
    check("a redirect is refused, so the key can't be carried elsewhere",
          exc is not None and len(fake.requests) == 1)
    fake.respond((413, {}, {}), (200, {"text": "half", "segments": []}, {}))
    check("a piece that is too large is split smaller and retried",
          client.transcribe(speechlike(70), None, "en").startswith("half"))

    dead = GroqClient({"base_url": "http://127.0.0.1:9/openai/v1"}, lambda: SECRET)
    try:
        dead.transcribe(speechlike(2), None, "en")
        check("no internet is a retryable error", False)
    except EngineError as exc:
        check("no internet is a retryable error", exc.retryable, exc.kind)

    no_key = GroqClient(cfg, lambda: None)
    try:
        no_key.transcribe(speechlike(2), None, "en")
        check("no saved key is reported, not sent", False)
    except EngineError as exc:
        check("no saved key is reported, not sent", exc.kind == "auth")

    for bad in ("http://api.groq.com/openai/v1", "https://user:pw@api.groq.com", "nonsense"):
        try:
            validate_base_url(bad)
            check(f"rejects unsafe address {bad!r}", False)
        except EngineError:
            check(f"rejects unsafe address {bad!r}", True)

    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://127.0.0.1:9"
    try:
        fake.respond((200, {"text": "direct", "segments": []}, {}))
        check("proxy settings from the environment are ignored",
              client.transcribe(speechlike(2), None, "en") == "direct")
    except EngineError as exc:
        check("proxy settings from the environment are ignored", False, str(exc))
    finally:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("http_proxy", None)
    del requests


@section("6. Clean-up (fake server)")
def test_cleanup():
    from dictate_win.cleanup_cloud import CloudCleanup
    from dictate_win.engine_groq import GroqClient

    fake = FakeGroq()
    client = GroqClient({"base_url": fake.url, "cleanup_model": "openai/gpt-oss-20b"},
                        lambda: SECRET)
    raw = ("um so basically the uh the client needs like a demo by friday and "
           "we should send them the deck")

    def chat(content, finish="stop", status=200):
        return (status, {"choices": [{"message": {"content": content},
                                      "finish_reason": finish}]}, {})

    fake.respond(chat("So basically, the client needs a demo by Friday and we "
                      "should send them the deck."))
    text, stage = CloudCleanup(client).clean(raw)
    body = json.loads(fake.requests[0]["body"])
    check("a good clean-up is used", stage == "llm" and text.startswith("So basically"))
    check("reasoning is set low and kept out of the reply",
          body.get("reasoning_effort") == "low" and body.get("include_reasoning") is False
          and body.get("temperature") == 0)
    fake.respond(chat("Sure! Here's a demo plan for Friday: 1. Book a room 2. Prepare slides"))
    text, stage = CloudCleanup(client).clean(raw)
    check("a reply that answers instead of cleaning is rejected", text == raw and stage == "guardrail")
    fake.respond(chat("So basically the client", finish="length"))
    text, stage = CloudCleanup(client).clean(raw)
    check("a cut-off reply is rejected", text == raw)
    fake.respond((404, {}, {}))
    cleaner = CloudCleanup(client)
    text, stage = cleaner.clean(raw)
    check("a retired clean-up model falls back to the words as heard",
          text == raw and stage == "raw" and cleaner.disabled_reason)


class FakeRecorder:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.stalled, self.elapsed, self.fail = False, 0.0, False
        self.audio = speechlike(3)
        self.n = 0
        self.started = False

    def start(self):
        from dictate.recorder import MicrophoneError

        if self.fail:
            raise MicrophoneError("Error opening InputStream: Unanticipated host error")
        self.started = True

    def stop(self):
        self.n += 1
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"20260101-0000{self.n:02d}-000.pcm"
        path.write_bytes((np.clip(self.audio, -1, 1) * 32767).astype("<i2").tobytes())
        return path


class FakeClient:
    def __init__(self) -> None:
        self.text = "send the invoice today and call the client tomorrow morning"
        self.error = None
        self.key = True

    def transcribe(self, samples, prompt, language):
        if self.error:
            raise self.error
        return self.text

    def has_key(self):
        return self.key


class FakeCleaner:
    def clean(self, raw, style_hint=None):
        return raw[0].upper() + raw[1:] + ".", "llm"


class FakeMuter:
    def __init__(self) -> None:
        self.calls = []

    def mute_async(self):
        self.calls.append("mute")

    def unmute_async(self):
        self.calls.append("unmute")


def wait_for(cond, seconds=5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@section("7. Dictation pipeline (stand-ins for mic, Groq, paste)")
def test_core():
    from dictate import history
    from dictate import recorder as rec
    from dictate_win import core as core_mod, settings
    from dictate_win.engine_groq import EngineError

    folder = _TMP / "core"
    history.HISTORY_PATH = folder / "history.jsonl"
    cfg = settings.load()
    pasted, notices = [], []
    client, muter, recorder = FakeClient(), FakeMuter(), FakeRecorder(folder / "rec")
    target = [("notepad.exe", "untitled - notepad")]
    core = core_mod.Core(cfg, recorder, folder / "rec", client, FakeCleaner(),
                         muter=muter, paste_fn=lambda t: pasted.append(t) or "pasted",
                         foreground_fn=lambda: target[0],
                         notify=lambda title, text: notices.append((title, text)))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.start()
        core.handle_event("toggle")
        recording = core.recording
        time.sleep(core_mod.MUTE_DELAY_S + 0.1)
        core.handle_event("toggle")
        wait_for(lambda: pasted and not core.pending)
    check("tap, speak, tap: the text is pasted", recording and pasted
          and pasted[-1].startswith("Send the invoice"), repr(pasted[-1:]))
    check("the dictation is saved in History first, then marked done",
          history.last(1) and history.last(1)[0]["text"] == pasted[-1]
          and rec.unfinished_recordings(folder / "rec") == [])
    check("other sound is muted while speaking and restored after",
          muter.calls == ["mute", "unmute"], str(muter.calls))

    muter.calls.clear()
    with contextlib.redirect_stdout(out):
        core.handle_event("toggle")
        core.handle_event("toggle")  # stopped before the mute delay ran out
        time.sleep(core_mod.MUTE_DELAY_S + 0.2)
        wait_for(lambda: not core.pending)
    check("a very quick tap-tap never leaves the sound muted",
          muter.calls[-1:] == ["unmute"] and muter.calls.count("mute") == 0, str(muter.calls))

    pasted.clear()
    notices.clear()
    client.error = EngineError("network", "could not reach Groq")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("toggle")
        core.handle_event("toggle")
        wait_for(lambda: core.failed_count() == 1 and not core.pending)
    kept = rec.unfinished_recordings(folder / "rec")
    check("with no internet the dictation is kept and the user told",
          core.failed_count() == 1 and len(kept) == 1 and not pasted
          and notices and "saved" in notices[-1][1])
    client.error = None
    before = len(history.last(50))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.retry_due(force=True)
        wait_for(lambda: core.failed_count() == 0 and not core.pending)
    check("when the connection is back it is transcribed into History, not typed",
          core.failed_count() == 0 and len(history.last(50)) == before + 1
          and not pasted and "+recovered" in history.last(1)[0]["stage"]
          and notices[-1][0] == "Dictation recovered")

    client.error = EngineError("auth", "Groq rejected the API key")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("toggle")
        core.handle_event("toggle")
        wait_for(lambda: core.failed_count() == 1 and not core.pending)
        core.retry_due()
        time.sleep(0.2)
    check("a rejected key is not retried in a loop", core.failed_count() == 1
          and not core.pending)
    client.error = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.key_changed()
        core.retry_due()
        wait_for(lambda: core.failed_count() == 0 and not core.pending)
    check("saving a new key retries it", core.failed_count() == 0)

    class FakeLocal:
        def transcribe(self, samples, prompt, language):
            return "transcribed on the laptop instead"

        def preload(self):
            pass

    core.local, core.local_available = FakeLocal(), lambda: True
    client.error = EngineError("network", "offline")
    pasted.clear()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("toggle")
        core.handle_event("toggle")
        wait_for(lambda: pasted and not core.pending)
    check("with no internet and the local engine installed, it falls back to it",
          pasted and "laptop instead" in pasted[-1] and core.failed_count() == 0)
    core.local, core.local_available, client.error = None, lambda: False, None

    pasted.clear()
    notices.clear()
    recorder.audio = np.zeros(48000, dtype=np.float32)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("toggle")
        core.handle_event("toggle")
        wait_for(lambda: notices and not core.pending)
    check("all-silent audio (mic blocked by Windows privacy) explains the fix",
          not pasted and notices and "Privacy" in notices[-1][1])
    recorder.audio = speechlike(3)

    for program, title, expect in (("windowsterminal.exe", "", "raw"),
                                   ("chrome.exe", "inbox - gmail - google chrome", "email")):
        target[0] = (program, title)
        client.text = ("first point is that the build is green and the tests pass on "
                       "windows and mac. second we still need the installer. third "
                       "the readme needs a section on privacy and on muting, and "
                       "then we ship it to the testers next week if all goes well")
        pasted.clear()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            core.handle_event("toggle")
            core.handle_event("toggle")
            wait_for(lambda: pasted and not core.pending)
        if expect == "raw":
            check("terminals and code editors get exactly what was heard",
                  pasted and pasted[-1] == client.text)
        else:
            check("Gmail gets paragraphs, not bullet points",
                  pasted and "• " not in pasted[-1])
    target[0] = ("notepad.exe", "")

    notices.clear()
    recorder.fail = True
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("toggle")
    check("a microphone error leaves Dictate idle and explains the fix",
          not core.recording and notices and "Privacy" in notices[-1][1])
    recorder.fail = False

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("toggle")
        recorder.stalled = True
        core.handle_event("tick")
        recorder.stalled = False
        wait_for(lambda: not core.pending)
    check("a microphone that stops sending audio ends the recording (audio kept)",
          not core.recording)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("toggle")
        recorder.elapsed = core_mod.MAX_RECORDING_S + 1
        core.handle_event("tick")
        recorder.elapsed = 0
        wait_for(lambda: not core.pending)
    check("a forgotten recording stops itself at 15 minutes", not core.recording)

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        core.handle_event("stop")
    check("'Stop dictation' does nothing when nothing is recording", not core.recording)
    core.enabled = False
    with contextlib.redirect_stdout(out):
        core.handle_event("toggle")
    check("taps are ignored while Dictate is switched off", not core.recording)
    core.enabled = True

    words = ["invoice", "tomorrow", "readme", "installer", "testers"]
    leaked = [line for line in out.getvalue().splitlines()
              if any(w in line.lower() for w in words)]
    check("nothing that was said appears in the log", not leaked, " | ".join(leaked)[:300])

    d = _TMP / "prune"
    d.mkdir()
    for i in range(8):
        (d / f"2026010{i}-000000-000.done.pcm").write_bytes(b"x")
    old = d / "20250101-000000-000.pcm"
    new = d / "20260109-000000-000.pcm"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 9 * 86400,) * 2)
    core_mod.prune(d)
    left = sorted(p.name for p in d.iterdir())
    check("clean-up keeps the 5 newest finished recordings and every recent "
          "unfinished one", len([n for n in left if n.endswith(".done.pcm")]) == 5
          and new.name in left and old.name not in left, str(left))


@section("8. Microphone process")
def test_mic_process():
    from dictate_win import mic

    mic.extra_args = ["--fake"]
    try:
        mic.shutdown()
        mic.arm()
        time.sleep(1.5 if WINDOWS else 0.5)
        got = []
        stream = mic.MicStream(lambda indata, *_: got.append(len(indata)))
        t0 = time.monotonic()
        stream.start()
        started = time.monotonic() - t0
        time.sleep(0.5)
        proc = stream._proc
        t1 = time.monotonic()
        stream.abort()
        stopped = time.monotonic() - t1
        proc.wait(5)
        check("audio flows from the microphone process", sum(got) > 4000,
              f"{sum(got)} samples, started in {started * 1000:.0f} ms")
        check("stopping returns at once and the process is gone",
              stopped < 0.2 and proc.poll() is not None, f"{stopped * 1000:.1f} ms")
        armed = mic._armed
        mic.shutdown()
        if armed is not None:
            armed.wait(5)
        check("the waiting process is cleaned up on quit",
              armed is None or armed.poll() is not None)
        if WINDOWS:
            import psutil

            time.sleep(1.0)
            left = [p for p in psutil.process_iter(["cmdline"])
                    if any("mic_child" in part for part in (p.info["cmdline"] or []))]
            check("no microphone process is left running (incl. behind the venv launcher)",
                  not left, f"{len(left)} left")
    finally:
        mic.extra_args = []
        mic.shutdown()


@section("9. Small parts")
def test_small():
    from dictate_win import appdetect, paths, sounds

    sounds.prepare()
    check("start/stop/error sounds are created",
          all((paths.SOUNDS / f"{n}.wav").exists() for n in ("start", "stop", "error")))
    cases = {("outlook.exe", ""): "email", ("slack.exe", ""): "casual",
             ("ms-teams.exe", ""): "casual", ("code.exe", ""): "code",
             ("msedge.exe", "chats - whatsapp - microsoft edge"): "casual",
             ("chrome.exe", "inbox (3) - gmail - google chrome"): "email",
             ("chrome.exe", "youtube - google chrome"): "default",
             ("notion.exe", ""): "default"}
    check("apps and web apps get the right writing style",
          all(appdetect.resolve_mode(p, t) == m for (p, t), m in cases.items()))
    check("settings.toml can override an app's style",
          appdetect.resolve_mode("notion.exe", "", {"Notion.exe": "casual"}) == "casual")

    from dictate import layout
    mail = layout.format_layout(
        "Hi Sam, thanks for the update on the launch. I looked over the plan and it "
        "looks good to me. Could you send the final numbers by Friday? Thank you, Rohan",
        "paragraphs", email=True)
    check("an email gets its greeting and sign-off on their own lines",
          mail.startswith("Hi Sam,\n") and mail.endswith("Thank you,\nRohan"), repr(mail))
    todo = layout.format_layout(
        "The list of tasks that I have are hitting the gym, and then studying for "
        "a midterm, and also working on three assignments.", "points")
    check("a spoken list becomes bullet points",
          todo.count("\n• ") == 3 and "Studying for a midterm" in todo, repr(todo))


# ------------------------------------------------------------ Windows only --
@section("10. Windows: Credential Manager")
def test_credentials():
    if not WINDOWS:
        return skip("Credential Manager", "Windows only")
    from dictate_win import credentials, paths

    host = f"selftest-{uuid.uuid4().hex[:8]}.invalid"
    try:
        check("no key is saved for a new host", credentials.get_key(host) is None)
        credentials.set_key(host, SECRET)
        check("a key is saved and read back", credentials.get_key(host) == SECRET)
        files = [p for p in paths.HOME.rglob("*") if p.is_file()]
        check("the key is in no file Dictate writes",
              not any(SECRET.encode() in p.read_bytes() for p in files))
        check("the key is removed", credentials.delete_key(host)
              and credentials.get_key(host) is None)
    finally:
        try:
            credentials.delete_key(host)
        except Exception:
            pass


@section("11. Windows: clipboard")
def test_clipboard():
    if not WINDOWS:
        return skip("clipboard", "Windows only")
    import ctypes

    from dictate_win import paste
    from dictate_win import winapi as api

    original = paste.snapshot()
    try:
        text = "Héllo — 你好 👋\nline two\n" + "x" * 100_000
        paste.set_text(text)
        check("long Unicode text round-trips through the clipboard",
              paste.get_text() == text)
        with paste._Clipboard():
            hidden = all(api.IsClipboardFormatAvailable(api.RegisterClipboardFormatW(n))
                         for n in ("ExcludeClipboardContentFromMonitorProcessing",
                                   "CanIncludeInClipboardHistory"))
        check("dictated text is kept out of clipboard history and cloud sync", hidden)

        # an image (8x8 32-bit DIB) plus text, as if the user had copied them
        header = (40).to_bytes(4, "little") + (8).to_bytes(4, "little", signed=True) \
            + (8).to_bytes(4, "little", signed=True) + (1).to_bytes(2, "little") \
            + (32).to_bytes(2, "little") + bytes(24)
        dib = header + bytes(range(256))
        with paste._Clipboard():
            api.EmptyClipboard()
            paste._put(8, dib)  # CF_DIB
            paste._put(api.CF_UNICODETEXT, "user's text\0".encode("utf-16-le"))
        saved = paste.snapshot()
        paste.set_text("dictated")
        paste.restore(saved)
        with paste._Clipboard():
            handle = api.GetClipboardData(8)
            pointer = api.GlobalLock(handle) if handle else None
            restored = ctypes.string_at(pointer, len(dib)) if pointer else b""
            if pointer:
                api.GlobalUnlock(handle)
        check("an image and text on the clipboard survive a dictation",
              restored == dib and paste.get_text() == "user's text")
        check("the elevation check runs", isinstance(paste.foreground_is_elevated(), bool))
    finally:
        paste.restore(original)


def _tk_window():
    """A focused Tk text box to type into; None if Windows won't focus it."""
    import tkinter as tk

    from dictate_win import winapi as api

    root = tk.Tk()
    root.title("Dictate self-test")
    root.attributes("-topmost", True)
    box = tk.Text(root, width=40, height=4)
    box.pack()
    root.update()
    for _ in range(20):
        root.lift()
        root.focus_force()
        box.focus_set()
        root.update()
        hwnd = int(root.wm_frame(), 16)
        if api.GetForegroundWindow() == hwnd:
            return root, box
        time.sleep(0.1)
    root.destroy()
    return None


@section("12. Windows: typing into an app")
def test_paste_into_app():
    if not WINDOWS:
        return skip("paste into an app", "Windows only")
    from dictate_win import paste

    window = _tk_window()
    if window is None:
        return skip("paste into an app", "this machine has no interactive desktop focus")
    root, box = window
    original = paste.snapshot()
    try:
        paste.set_text("the user's own clipboard")
        result: dict = {}

        def worker():
            result["status"] = paste.paste("Dictated words • with a bullet")

        t = threading.Thread(target=worker)
        t.start()
        end = time.monotonic() + 5
        while t.is_alive() and time.monotonic() < end:
            root.update()
            time.sleep(0.02)
        for _ in range(20):
            root.update()
            time.sleep(0.02)
        typed = box.get("1.0", "end").strip()
        check("Ctrl+V types the text into the focused app",
              result.get("status") == "pasted" and typed == "Dictated words • with a bullet",
              f"{result} / {typed!r}")
        check("the user's clipboard is given back afterwards",
              paste.get_text() == "the user's own clipboard")
    finally:
        root.destroy()
        paste.restore(original)


@section("13. Windows: hotkey (Raw Input)")
def test_hotkey():
    if not WINDOWS:
        return skip("Raw Input hotkey", "Windows only")
    import ctypes

    from dictate_win import winapi as api
    from dictate_win.hotkey import HotkeyListener

    taps = []
    listener = HotkeyListener("right_ctrl", lambda: taps.append(time.monotonic()))
    listener.start()

    def send(*keys):
        events = (api.INPUT * len(keys))()
        for i, (vk, up, extended) in enumerate(keys):
            flags = (api.KEYEVENTF_KEYUP if up else 0) | (api.KEYEVENTF_EXTENDEDKEY if extended else 0)
            events[i] = api.INPUT(type=api.INPUT_KEYBOARD)
            events[i].ki = api.KEYBDINPUT(wVk=vk, wScan=api.MapVirtualKeyW(vk, 0),
                                          dwFlags=flags, time=0, dwExtraInfo=0)
        return api.SendInput(len(keys), events, ctypes.sizeof(api.INPUT))

    try:
        if send((0xA3, False, True), (0xA3, True, True)) != 2:
            return skip("Raw Input hotkey", "this machine refuses synthetic key presses")
        got = wait_for(lambda: len(taps) == 1, 3)
        check("a Right Ctrl tap is heard from any app", got, f"{len(taps)} taps")
        taps.clear()
        send((0xA3, False, True), (0x43, False, False), (0x43, True, False), (0xA3, True, True))
        time.sleep(0.5)
        check("Right Ctrl + C does not trigger dictation", taps == [])
        send((0xA2, False, False), (0xA2, True, False))  # Left Ctrl
        time.sleep(0.5)
        check("Left Ctrl does not trigger dictation", taps == [])
        listener.set_hotkey("f9")
        send((0x78, False, False), (0x78, True, False))
        check("switching the hotkey takes effect at once", wait_for(lambda: len(taps) == 1, 3))
        check("the listener is alive", listener.is_alive())
    finally:
        listener.stop()
    check("the listener stops cleanly", not listener.is_alive())


@section("14. Windows: microphone")
def test_real_mic():
    if not WINDOWS:
        return skip("real microphone", "Windows only")
    import sounddevice as sd

    from dictate_win import mic

    apis = [a["name"] for a in sd.query_hostapis()]
    print(f"  audio systems: {apis}")
    try:
        sd.query_devices(kind="input")
    except Exception:
        return skip("real microphone", "this machine has no microphone")
    mic.shutdown()
    mic.arm()
    time.sleep(2)
    got = []
    stream = mic.MicStream(lambda indata, *_: got.append(len(indata)))
    try:
        stream.start()
        time.sleep(1)
        check("the microphone opens and delivers 16 kHz audio",
              sum(got) > 8000, f"via {stream.via}, {sum(got)} samples")
    except RuntimeError as exc:
        check("the microphone opens and delivers 16 kHz audio", False, str(exc))
    finally:
        stream.abort()
        mic.shutdown()


@section("15. Windows: mute and unmute")
def test_mute():
    if not WINDOWS:
        return skip("mute", "Windows only")
    from dictate_win import mute, paths

    try:
        backend_probe = []
        muter = mute.OutputMuter()
        muter._worker.submit(lambda: backend_probe.append(
            muter._get_backend() and muter._backend.default_id())).result(10)
    except Exception as exc:
        return skip("mute", f"no audio output device ({exc!r})")
    if not backend_probe or not backend_probe[0]:
        return skip("mute", "no audio output device")
    device = backend_probe[0]
    backend = muter._backend
    was_muted = muter._worker.submit(backend.is_muted, device).result(5)
    try:
        if was_muted:
            muter._worker.submit(backend.set_mute, device, False).result(5)
        muter.mute_async()
        muter._worker.submit(lambda: None).result(5)
        muted = muter._worker.submit(backend.is_muted, device).result(5)
        marker = paths.MUTE_MARKER.exists()
        muter.unmute()
        unmuted = not muter._worker.submit(backend.is_muted, device).result(5)
        check("other sound is muted during dictation and comes back",
              muted and unmuted and marker and not paths.MUTE_MARKER.exists())
        muter._worker.submit(backend.set_mute, device, True).result(5)
        muter.mute_async()
        muter.unmute()
        still = muter._worker.submit(backend.is_muted, device).result(5)
        check("sound the user muted themselves stays muted", still)
        muter._worker.submit(backend.set_mute, device, False).result(5)
        # crash: muted with the marker left behind
        muter._worker.submit(backend.set_mute, device, True).result(5)
        mute._write_marker(device)
        fresh = mute.OutputMuter()
        fresh.recover_after_crash(10)
        recovered = not fresh._worker.submit(
            lambda: fresh._get_backend().is_muted(device)).result(5)
        check("after a crash, the next start turns the sound back on",
              recovered and not paths.MUTE_MARKER.exists())
    finally:
        muter._worker.submit(backend.set_mute, device, was_muted).result(5)


@section("16. Windows: API key dialog")
def test_key_dialog():
    if not WINDOWS:
        return skip("key dialog", "Windows only")
    from dictate_win import dialogs

    def type_and_save(root, entry, buttons):
        entry.insert(0, SECRET)
        buttons["save"].invoke()

    value = dialogs.ask_key("api.groq.com", False, lambda: "", _auto=type_and_save)
    check("the key dialog opens, accepts a key and saves", value == SECRET)

    def paste_button(root, entry, buttons):
        buttons["paste"].invoke()

    value = dialogs.ask_key("api.groq.com", True, lambda: "  " + SECRET + "\n",
                            _auto=paste_button)
    check("'Paste from clipboard and save' takes the key from the clipboard", value == SECRET)

    def empty_clipboard(root, entry, buttons):
        buttons["paste"].invoke()
        root.after(200, buttons["cancel"].invoke)

    value = dialogs.ask_key("api.groq.com", False, lambda: "", _auto=empty_clipboard)
    check("an empty clipboard is refused, nothing is saved", value is None)


@section("17. Windows: tray icon, single instance, app detection")
def test_tray():
    if not WINDOWS:
        return skip("tray", "Windows only")
    import pystray

    from dictate_win import appdetect
    from dictate_win import winapi as api
    from dictate_win.app import _icon_image

    icon = pystray.Icon("DictateSelfTest", _icon_image("idle"), "Dictate self-test",
                        pystray.Menu(pystray.MenuItem("x", lambda: None)))
    icon.run_detached()
    time.sleep(1)
    try:
        for state in ("recording", "busy", "idle"):
            icon.icon = _icon_image(state)
        icon.update_menu()
        check("the tray icon appears and changes state", True)
    finally:
        icon.stop()
    name = f"Local\\DictateSelfTest{uuid.uuid4().hex}"
    first = api.CreateMutexW(None, False, name)
    second_exists = api.CreateMutexW(None, False, name) and api.last_error() == api.ERROR_ALREADY_EXISTS
    check("a second copy of Dictate is detected", bool(first) and second_exists)
    program, title = appdetect.foreground()
    check("the focused app is detected", isinstance(program, str), program or "(none)")


@section("18. Local engine (if installed)")
def test_local_engine():
    from dictate_win import engine_local

    if not engine_local.available():
        return skip("local engine", "not installed (optional)")
    wav = _TMP / "speech.wav"
    phrase = "Please send the invoice to the client before Friday."
    if WINDOWS:
        import subprocess

        script = ("Add-Type -AssemblyName System.Speech; "
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
                  "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
                  "[System.Speech.AudioFormat.AudioChannel]::Mono); "
                  f"$s.SetOutputToWaveFile('{wav}', $f); $s.Speak('{phrase}'); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], timeout=60,
                       check=False)
    if not wav.exists():
        return skip("local engine", "no text-to-speech voice to make a test clip")
    with wave.open(str(wav)) as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    engine = engine_local.LocalEngine("base.en")
    t0 = time.monotonic()
    text = engine.transcribe(audio, None, "en")
    took = time.monotonic() - t0
    check("speech is transcribed on this machine", "invoice" in text.lower()
          and "friday" in text.lower(), f"{text!r} in {took:.1f}s")


def main() -> int:
    print(f"Dictate for Windows self-test on {sys.platform}, Python {sys.version.split()[0]}")
    try:
        tests = (test_imports, test_settings, test_taps, test_resample, test_groq,
                 test_cleanup, test_core, test_mic_process, test_small,
                 test_credentials, test_clipboard, test_paste_into_app, test_hotkey,
                 test_real_mic, test_mute, test_key_dialog, test_tray,
                 test_local_engine)
        # Optional: run only some tests, e.g.  -m dictate_win.selftest core mic_process
        wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
        for test in tests:
            if not wanted or test.__name__.removeprefix("test_") in wanted:
                test()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{results['PASS']} passed, {results['FAIL']} failed, "
          f"{results['SKIP']} skipped (could not run here)")
    return 1 if results["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
