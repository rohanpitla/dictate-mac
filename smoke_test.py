"""Dictate Phase 1 smoke test — no microphone or permissions needed.

Run:  .venv/bin/python smoke_test.py

Checks:
  1. All modules import and config.toml loads
  2. macOS TTS generates a known phrase -> whisper transcribes it correctly
  3. Clipboard save/restore round-trips
  4. Empty / too-short recordings pass through the pipeline without crashing
Prints per-stage latency.
"""

import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("Dictate Phase 1 smoke test\n")

    # --- 1. imports & config -------------------------------------------------
    print("1) Imports & config")
    try:
        from dictate import app, config, hotkey, paster, permissions, recorder, sounds, transcriber  # noqa: F401
        cfg = config.load()
        check("all modules import", True)
        check("config.toml loads", cfg["general"]["hotkey"] in hotkey.HOTKEY_LABELS,
              f"hotkey={cfg['general']['hotkey']}, model={cfg['whisper']['model']}")
    except Exception as exc:
        check("imports/config", False, repr(exc))
        _summary()
        return 1

    # --- 2. TTS -> whisper ---------------------------------------------------
    print("2) Transcription (macOS TTS clip, fully local)")
    phrase = "hello world this is a test of dictate dictation"
    wav = Path(tempfile.gettempdir()) / "dictate_smoke.wav"
    subprocess.run(
        ["say", "-o", str(wav), "--file-format=WAVE",
         "--data-format=LEI16@16000", phrase],
        check=True,
    )
    with wave.open(str(wav)) as w:
        ok_fmt = w.getframerate() == 16_000 and w.getnchannels() == 1
        check("say produced 16 kHz mono WAV", ok_fmt,
              f"{w.getframerate()} Hz, {w.getnchannels()} ch, "
              f"{w.getnframes() / w.getframerate():.1f}s")

    t0 = time.monotonic()
    text = transcriber.transcribe(str(wav), cfg["whisper"]["model"])
    dt = time.monotonic() - t0
    normalized = "".join(c for c in text.lower() if c.isalpha() or c == " ")
    hits = sum(word in normalized for word in ["hello", "world", "test", "dictation"])
    check("transcript matches phrase", hits >= 3, f"{dt:.1f}s -> {text!r}")

    # one Whisper failure must not disable transcription
    real_run, fails = transcriber._run_whisper, []

    def flaky(*args):
        if not fails:
            fails.append(1)
            raise RuntimeError("simulated out-of-memory")
        return real_run(*args)

    transcriber._run_whisper = flaky
    try:
        retried = transcriber.transcribe(str(wav), cfg["whisper"]["model"])
    except Exception as exc:
        retried = f"<{exc!r}>"
    transcriber._run_whisper = real_run
    check("Whisper error is retried, not fatal",
          fails == [1] and "test" in retried.lower(), repr(retried[:60]))
    wav.unlink(missing_ok=True)

    # --- 3. clipboard round-trip --------------------------------------------
    print("3) Clipboard save/restore")
    original = paster.get_clipboard_text()
    marker = f"dictate-smoke-{time.time()}"
    paster.set_clipboard_text(marker)
    ok_set = paster.get_clipboard_text() == marker
    if original is not None:
        paster.set_clipboard_text(original)
        ok_restore = paster.get_clipboard_text() == original
    else:
        ok_restore = True  # clipboard was empty/non-text; nothing to restore
    check("set clipboard", ok_set)
    check("restore clipboard", ok_restore)

    # --- 4. edge cases: empty + tiny audio never crash -----------------------
    print("4) Edge cases (never crash)")
    tiny = Path(tempfile.gettempdir()) / "dictate_smoke_tiny.wav"
    with wave.open(str(tiny), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * 3200)  # 0.2 s of silence
    try:
        out = transcriber.transcribe(str(tiny), cfg["whisper"]["model"])
        check("0.2s silent WAV transcribes without crash", True, f"-> {out!r}")
    except Exception as exc:
        check("0.2s silent WAV transcribes without crash", False, repr(exc))
    tiny.unlink(missing_ok=True)

    rec = recorder.Recorder()
    check("Recorder.stop() before start returns None", rec.stop() is None)

    # --- 4b. Recording reliability -------------------------------------------
    print("4b) Recording can't hang or be lost")
    import shutil
    import threading

    import numpy as np

    rec_dir = Path(tempfile.mkdtemp(prefix="dictate_smoke_rec_"))

    class FakeStream:
        """A mic whose shutdown never returns, like a device that vanished."""
        def __init__(self, callback, hang_open=False):
            self.callback, self.hang_open = callback, hang_open
            self.feeding = True

        def start(self):
            if self.hang_open:
                threading.Event().wait()
            def feed():
                while self.feeding:
                    self.callback(np.full((1600, 1), 1000, dtype=np.int16),
                                  1600, None, None)
                    time.sleep(0.02)
            threading.Thread(target=feed, daemon=True).start()

        def abort(self):
            threading.Event().wait()  # blocks forever

        def close(self):
            pass

    streams = []

    def factory(callback):
        streams.append(FakeStream(callback))
        return streams[-1]

    rec = recorder.Recorder(directory=rec_dir, stream_factory=factory)
    rec.start()
    time.sleep(0.4)
    on_disk_while_recording = sum(p.stat().st_size for p in rec_dir.glob("*.pcm"))
    t0 = time.monotonic()
    saved = rec.stop()
    stop_s = time.monotonic() - t0
    check("audio is on disk while still recording", on_disk_while_recording > 0,
          f"{on_disk_while_recording} bytes")
    check("stop returns at once even if the mic never shuts down",
          saved is not None and stop_s < 0.5, f"{stop_s * 1000:.0f}ms")
    check("saved recording loads as audio",
          saved is not None and len(transcriber._load_wav(str(saved))) > 1600)

    rec.start()
    time.sleep(0.2)
    streams[-1].feeding = False
    real_stall, recorder.STALL_S = recorder.STALL_S, 0.3
    time.sleep(0.5)
    check("a mic that stops sending audio is detected", rec.stalled)
    recorder.STALL_S = real_stall
    rec.stop()

    real_timeout, recorder.OPEN_TIMEOUT_S = recorder.OPEN_TIMEOUT_S, 0.3
    hung = recorder.Recorder(
        directory=rec_dir,
        stream_factory=lambda cb: FakeStream(cb, hang_open=True))
    try:
        hung.start()
        check("a mic that never opens gives an error, not a freeze", False)
    except recorder.MicrophoneError:
        check("a mic that never opens gives an error, not a freeze", True)
    recorder.OPEN_TIMEOUT_S = real_timeout

    for p in rec_dir.glob("*.pcm"):
        p.unlink()
    for i in range(8):
        (rec_dir / f"2026010{i}-000000-000.pcm").write_bytes(b"\0" * 64000)
    done = recorder.mark_done(rec_dir / "20260107-000000-000.pcm")
    recorder.prune(rec_dir)
    left = recorder.saved_recordings(rec_dir)
    check("only the 5 newest recordings are kept",
          len(left) == 5 and left[0].name.startswith("20260103"))
    check("unfinished recordings are found for recovery",
          done not in recorder.unfinished_recordings(rec_dir)
          and len(recorder.unfinished_recordings(rec_dir)) == 4)
    shutil.rmtree(rec_dir, ignore_errors=True)

    import httpx
    real_send, net_calls = httpx.Client.send, []

    def spy(self, request, *a, **k):
        net_calls.append(str(request.url))
        return real_send(self, request, *a, **k)

    httpx.Client.send = spy
    transcriber._on_mlx_thread(transcriber._unload)
    transcriber.warmup(cfg["whisper"]["model"])
    httpx.Client.send = real_send
    check("reloading Whisper makes no network calls", not net_calls,
          f"{len(net_calls)} calls")

    # --- 5. Phase 2: cleanup guardrails, vocabulary, Ollama ------------------
    print("5) Cleanup pass (Phase 2)")
    from dictate import cleanup

    raw = "um so basically the uh the client needs like a demo"
    check("guardrail: empty output rejected",
          not cleanup.passes_guardrails(raw, ""))
    check("guardrail: >2x length rejected",
          not cleanup.passes_guardrails(raw, raw * 3))
    check("guardrail: <0.3x length rejected",
          not cleanup.passes_guardrails(raw, "demo"))
    check("guardrail: meta-text rejected",
          not cleanup.passes_guardrails(raw, "Here is the cleaned text: demo"))
    check("guardrail: good output accepted",
          cleanup.passes_guardrails(raw, "So basically, the client needs a demo."))
    ask = "Give me a detailed summary of this video and what exactly he's trying to say."
    check("guardrail: LLM answering the dictation rejected",
          not cleanup.passes_guardrails(ask, "I'm unable to provide a summary as you've "
                                        "only provided the input text."))
    check("guardrail: instruction-shaped dictation passes through",
          cleanup.passes_guardrails(ask, ask))
    spoken = ("I need something that would make the recruiter feel like this "
              "person is capable of the job and credible enough to get hired "
              "for the automation engineer position at the company")
    check("guardrail: cleanup that drops a clause is rejected",
          not cleanup.passes_guardrails(
              spoken, "I need something capable of the job and credible "
                      "enough to get hired."))
    from dictate import layout
    listed = layout.format_layout(
        "I want you to do three things for me. First, go through the resume "
        "and fix any grammar mistakes. Second, make the summary shorter. "
        "Third, add the new project. Keep it to one page.")
    check("layout: 'first… second… third…' becomes a numbered list",
          "\n1. Go through the resume" in listed
          and "\n3. Add the new project." in listed
          and listed.endswith("\n\nKeep it to one page."), repr(listed[:60]))
    long_text = " ".join(
        f"This is sentence number {i} about the project and what we should "
        f"do with it next week." for i in range(8))
    laid = layout.format_layout(long_text)
    check("layout: long text is split into paragraphs, words unchanged",
          "\n\n" in laid and " ".join(laid.split()) == long_text)
    check("layout: short text and stray ordinals left alone",
          layout.format_layout("Let's meet at 4 pm.") == "Let's meet at 4 pm."
          and "\n" not in layout.format_layout(
              "The second half was great. First of all the team played well."))
    check("guardrail: short self-correction still allowed",
          cleanup.passes_guardrails("let's meet at 3 no wait 4 pm on thursday",
                                    "Let's meet at 4 pm on Thursday."))

    vocab_in = "I built it with n 8 n and fast api on a vps for cloud code"
    vocab_out = cleanup.apply_vocabulary(vocab_in, cfg["vocabulary"])
    check("vocabulary replacement",
          all(term in vocab_out for term in ["n8n", "FastAPI", "VPS", "Claude Code"]),
          repr(vocab_out))

    # Graceful degradation: unreachable Ollama must return raw, never raise
    bad_cfg = {"ollama": {"model": "qwen2.5:3b", "fallback_model": None},
               "vocabulary": {}}
    real_url, real_start = cleanup.OLLAMA_URL, cleanup.start_ollama
    starts = []
    cleanup.start_ollama = lambda: starts.append(1)
    cleanup.OLLAMA_URL = "http://localhost:1"  # nothing listens here
    t0 = time.monotonic()
    degraded, stage = cleanup.clean(raw, bad_cfg)
    dt = time.monotonic() - t0
    cleanup.OLLAMA_URL, cleanup.start_ollama = real_url, real_start
    check("ollama down -> raw transcript, fast, no crash",
          degraded == raw and stage == "raw" and dt < 2, f"{dt * 1000:.0f}ms")
    check("ollama down -> Dictate starts it for the next dictation", starts == [1])

    # Live cleanup if Ollama is actually up (the Phase 2 deliverable)
    t0 = time.monotonic()
    cleaned, stage = cleanup.clean(raw, cfg)
    dt = time.monotonic() - t0
    if stage == "llm":
        lowered = cleaned.lower()
        ok = ("um" not in lowered.split() and "uh" not in lowered.split()
              and "client" in lowered and "demo" in lowered)
        check("live cleanup removes fillers, keeps content", ok,
              f"{dt:.1f}s -> {cleaned!r}")
    else:
        check("live cleanup (ollama not running: raw fallback ok)",
              cleaned == raw, f"stage={stage}")

    # --- 6. Phase 3: history log + config writer -----------------------------
    print("6) Settings & history (Phase 3)")
    from dictate import config as config_mod
    from dictate import history

    real_path = history.HISTORY_PATH
    history.HISTORY_PATH = Path(tempfile.gettempdir()) / "dictate_smoke_hist.jsonl"
    history.HISTORY_PATH.unlink(missing_ok=True)
    for i in range(25):
        history.append(raw=f"raw {i}", text=f"text {i}", stage="llm",
                       latency_ms={"transcribe": 900.0})
    entries = history.last(20)
    check("history: last 20 of 25, newest first",
          len(entries) == 20 and entries[0]["text"] == "text 24"
          and entries[0]["ts"][:4].isdigit())
    history.HISTORY_PATH.write_text("not json\n" + history.HISTORY_PATH.read_text())
    check("history: corrupt line tolerated", len(history.last(20)) == 20)
    history.HISTORY_PATH.unlink()
    history.HISTORY_PATH = real_path

    original_hotkey = cfg["general"]["hotkey"]
    config_mod.save_setting("general", "hotkey", "f19")
    changed = config_mod.load()
    ok_write = (changed["general"]["hotkey"] == "f19"
                and changed["ollama"]["model"] == cfg["ollama"]["model"])
    config_mod.save_setting("general", "hotkey", original_hotkey)
    restored = config_mod.load()["general"]["hotkey"] == original_hotkey
    check("config writer: section-aware update + restore", ok_write and restored)

    from dictate.hotkey import HotkeyListener
    ok_hk = True
    for hk in ("right_option", "left_option", "left_control", "fn", "f19"):
        try:
            HotkeyListener(hk, on_tap=lambda: None)
        except Exception:
            ok_hk = False
    check("all hotkey choices construct", ok_hk)

    # Fn press/release is read from the real CGEvent flags (pynput alone
    # reports both as releases), and a tap fires only without another key.
    import Quartz
    from pynput import keyboard as kb
    taps = []
    fn_listener = HotkeyListener("fn", on_tap=lambda: taps.append(1))

    def fn_event(down: bool):
        ev = Quartz.CGEventCreateKeyboardEvent(None, 63, down)
        Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskSecondaryFn if down else 0)
        fn_listener._listener._handle_message(None, Quartz.kCGEventFlagsChanged,
                                              ev, None, False)

    fn_event(True)
    fn_event(False)
    one_tap = len(taps) == 1
    fn_event(True)
    fn_listener._handle_press(kb.Key.down)  # Fn+Down = Page Down, not a tap
    fn_event(False)
    check("Fn tap fires once; Fn+key combo does not", one_tap and len(taps) == 1,
          f"taps={len(taps)}")

    fn_event(True)   # press whose release macOS never delivered...
    fn_event(True)   # ...then a normal tap must still count
    fn_event(False)
    check("a missed Fn release doesn't swallow the next tap", len(taps) == 2,
          f"taps={len(taps)}")

    # A raising handler must not kill the listener (pynput stops it for good).
    def boom():
        raise RuntimeError("handler bug")
    fn_listener._on_tap = boom
    try:
        fn_event(True)
        fn_event(False)
        survived = True
    except Exception:
        survived = False
    check("hotkey handler exception is contained", survived)

    import numpy as np
    from dictate import ducking, transcriber as tr
    rng = np.random.default_rng(0)
    tone = (0.1 * np.sin(np.arange(32000) * 0.1)).astype(np.float32)
    padded = np.concatenate([rng.normal(0, 0.003, 16000 * 8), tone,
                             rng.normal(0, 0.003, 16000 * 40)]).astype(np.float32)
    trimmed = len(tr.trim_silence(padded)) / 16000
    check("long silences trimmed before Whisper", 2.0 <= trimmed < 4.5,
          f"50s -> {trimmed:.1f}s")

    garbage = [{"avg_logprob": float("nan"), "compression_ratio": 17.8},
               {"avg_logprob": -0.39, "compression_ratio": 30.7},
               {"avg_logprob": -1.35, "compression_ratio": 0.7}]
    speech = {"avg_logprob": -0.3, "compression_ratio": 1.0}
    check("Whisper garbage on coughs/clicks is dropped, speech kept",
          all(tr.is_garbage_segment(s) for s in garbage)
          and not tr.is_garbage_segment(speech))
    check("long run-ons split into even chunks",
          [len(c.split()) for c in cleanup.split_chunks("w " * 125)] == [63, 62])

    real_osa = ducking._osascript
    ducking._osascript = lambda e: "false" if "get volume" in e else time.sleep(0.02)
    muter = ducking.OutputMuter()
    for _ in range(10):
        muter.mute_async()
        muter.unmute_async()
    time.sleep(1.0)
    ducking._osascript = real_osa
    check("quick start/stop never leaves the Mac muted", not muter._we_muted)

    # --- 7. Phase 4: app modes, style hints, pasteboard, latency stats ------
    print("7) Per-app modes & polish (Phase 4)")
    from dictate import appdetect, latency
    from dictate import paster as paster_mod

    cases = {
        "com.apple.mail": "email",
        "com.tinyspeck.slackmacgap": "casual",
        "com.microsoft.VSCode": "code",
        "com.jetbrains.pycharm": "code",
        "com.some.unknown.app": "default",
        None: "default",
    }
    ok_modes = all(appdetect.resolve_mode(b, cfg)[0] == m
                   for b, m in cases.items())
    check("bundle-id -> mode mapping", ok_modes)
    override_cfg = dict(cfg)
    override_cfg["app_modes"] = {"com.some.unknown.app": "casual"}
    check("config [app_modes] override",
          appdetect.resolve_mode("com.some.unknown.app", override_cfg)[0] == "casual")
    _, email_hint = appdetect.resolve_mode("com.apple.mail", cfg)
    prompt = cleanup._system_prompt(email_hint)
    check("style hint injected, rules intact",
          "writing an email" in prompt and "Remove filler words" in prompt
          and cleanup._system_prompt(None) == cleanup.SYSTEM_PROMPT)

    # full pasteboard snapshot: a non-text type must survive save/restore
    from AppKit import NSData, NSPasteboard, NSPasteboardItem
    fake_png = NSData.dataWithBytes_length_(b"\x89PNG-fake", 9)
    item = NSPasteboardItem.alloc().init()
    item.setData_forType_(fake_png, "public.png")
    item.setString_forType_("caption", "public.utf8-plain-text")
    pb = NSPasteboard.generalPasteboard()
    prior = paster_mod.snapshot_pasteboard()  # save the user's real clipboard
    pb.clearContents(); pb.writeObjects_([item])
    snap = paster_mod.snapshot_pasteboard()
    paster_mod.set_clipboard_text("dictate transcript overwrote it")
    paster_mod.restore_pasteboard(snap)
    restored = pb.pasteboardItems()[0]
    ok_pb = (restored.dataForType_("public.png") is not None
             and bytes(restored.dataForType_("public.png")) == b"\x89PNG-fake")
    paster_mod.restore_pasteboard(prior)  # put the user's clipboard back
    check("pasteboard snapshot: image data survives paste", ok_pb)

    from dictate import history as hist_mod
    hist_mod.HISTORY_PATH = Path(tempfile.gettempdir()) / "dictate_lat_test.jsonl"
    hist_mod.HISTORY_PATH.unlink(missing_ok=True)
    for i in range(1, 101):
        hist_mod.append(raw="r", text="t", stage="llm", latency_ms={
            "finalize": 10, "transcribe": float(i * 10), "cleanup": 500,
            "paste": 400})
    st = latency.stats()
    ok_lat = (st["transcribe"]["p50"] == 505 and st["transcribe"]["p95"] == 950
              and st["total"]["n"] == 100)
    check("latency p50/p95 math", ok_lat, f"transcribe={st['transcribe']}")
    hist_mod.HISTORY_PATH.unlink()
    hist_mod.HISTORY_PATH = real_path

    config_mod.save_setting("general", "raw_mode", True)
    ok_true = config_mod.load()["general"]["raw_mode"] is True
    config_mod.save_setting("general", "raw_mode", False)
    ok_false = config_mod.load()["general"]["raw_mode"] is False
    check("raw_mode persists as real TOML boolean", ok_true and ok_false)

    # --- 8. Glitch fixes: hallucination gate ---------------------------------
    print("8) Hallucination gate")
    quiet = Path(tempfile.gettempdir()) / "dictate_quiet.wav"
    with wave.open(str(quiet), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16_000)
        w.writeframes(b"\x05\x00" * 16_000)  # 1s of near-silence
    loud = Path(tempfile.gettempdir()) / "dictate_loud.wav"
    subprocess.run(["say", "-o", str(loud), "--file-format=WAVE",
                    "--data-format=LEI16@16000", "thank you"], check=True)
    check("'you' on quiet audio dropped",
          transcriber.looks_like_hallucination("you", str(quiet)))
    check("'Thank you.' on quiet audio dropped",
          transcriber.looks_like_hallucination("Thank you.", str(quiet)))
    check("'thank you' with real speech kept",
          not transcriber.looks_like_hallucination("thank you", str(loud)))
    check("normal sentence on quiet audio kept",
          not transcriber.looks_like_hallucination(
              "send the invoice to the client", str(quiet)))
    quiet.unlink(); loud.unlink()

    from dictate import layout
    follow_up = layout.format_layout(
        "I'm testing it now and seeing how it works. Still run a diagnostic "
        "and see if there's anything else that we need to work on to fix "
        "that model and make sure everything is right. What happened to the "
        "feature that we talked about, the one in the meetings? Is that "
        "possible? First we need to perfect the speaking feature and then we "
        "can work on the meetings feature.")
    check("a follow-up question stays with the sentence it refers to",
          "meetings? Is that possible?\n\nFirst we need" in follow_up, follow_up)

    _mic_helper_checks()
    _meeting_checks()
    _app_checks()
    return _summary()


def _app_checks() -> None:
    """The app's own wiring: hotkey taps and menu clicks -> start / stop.
    Uses a fake recorder; no microphone, sound, muting or model is touched."""
    import contextlib
    import io

    from dictate import app as app_mod
    from dictate import config
    from dictate.recorder import MicrophoneError

    print("\n== 10. App controls (hotkey and menu) ==")
    folder = Path(tempfile.mkdtemp())

    class FakeRecorder:
        def __init__(self):
            self.stalled, self.elapsed, self.fail, self.n = False, 0.0, False, 0

        def start(self):
            if self.fail:
                raise MicrophoneError("no response after 5s")

        def stop(self):
            self.n += 1
            path = folder / f"rec{self.n}.wav"
            path.write_bytes(b"x")
            return path

    originals = (app_mod.AppHelper.callAfter, app_mod.sounds.play,
                 app_mod.transcriber.preload, app_mod.cleanup.preload,
                 app_mod.appdetect.frontmost_bundle_id, app_mod.recorder.duration_s)
    app_mod.AppHelper.callAfter = lambda fn, *a: fn(*a)
    app_mod.sounds.play = lambda *_: None
    app_mod.transcriber.preload = lambda *_: None
    app_mod.cleanup.preload = lambda *_: None
    app_mod.appdetect.frontmost_bundle_id = lambda: "com.example"
    app_mod.recorder.duration_s = lambda _path: 1.0
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app = app_mod.DictateApp(config.load())
            app._muter = None
            app._recorder = rec = FakeRecorder()

            def stop_enabled() -> bool:
                return app._stop_item._menuitem.action() is not None

            idle_grey = not stop_enabled()
            app._handle_event("stop")
            stop_when_idle = not app._recording and app._jobs.empty()

            app._handle_event("toggle")
            started = app._recording and app.title == app_mod.ICON_RECORDING
            stop_live = stop_enabled()
            app._on_stop_dictation(None)             # the menu click
            app._handle_event(app._events.get_nowait())
            menu_stopped = (not app._recording and app._jobs.qsize() == 1
                            and not stop_enabled())

            app._handle_event("toggle")
            app._handle_event("toggle")              # second tap stops
            tap_stopped = not app._recording and app._jobs.qsize() == 2

            app._handle_event("toggle")
            app._on_toggle_enabled(app._enabled_item)  # switch off mid-recording
            app._handle_event(app._events.get_nowait())
            discarded = (not app._recording and app._jobs.qsize() == 2
                         and not (folder / f"rec{rec.n}.wav").exists())
            app._handle_event("toggle")
            ignored_while_off = not app._recording
            app._on_toggle_enabled(app._enabled_item)

            app._handle_event("toggle")
            rec.stalled = True
            app._handle_event("tick")
            stall_stopped = not app._recording and app._jobs.qsize() == 3
            rec.stalled = False

            rec.fail = True
            app._handle_event("toggle")
            mic_error = not app._recording and not stop_enabled()
    finally:
        (app_mod.AppHelper.callAfter, app_mod.sounds.play,
         app_mod.transcriber.preload, app_mod.cleanup.preload,
         app_mod.appdetect.frontmost_bundle_id,
         app_mod.recorder.duration_s) = originals

    check("'Stop Dictation' is greyed out when nothing is recording",
          idle_grey and stop_when_idle)
    check("a tap starts recording and enables 'Stop Dictation'",
          started and stop_live)
    check("the menu's 'Stop Dictation' stops and queues the recording",
          menu_stopped)
    check("a second tap stops the recording", tap_stopped)
    check("switching Dictate off mid-recording discards it; taps are then ignored",
          discarded and ignored_while_off)
    check("a stalled microphone stops the recording and keeps the audio",
          stall_stopped)
    check("a microphone error leaves the app idle, not stuck recording", mic_error)


def _mic_helper_checks() -> None:
    """The microphone runs in a helper process so that stopping can never
    hang. Tested with stand-in helpers: no real microphone is opened."""
    import os
    import stat

    from dictate import micstream

    print("\n== 8b. Microphone helper ==")
    folder = Path(tempfile.mkdtemp())

    def fake_helper(name: str, body: str) -> Path:
        path = folder / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(stat.S_IRWXU)
        return path

    real_binary, real_armed = micstream.BINARY, micstream._armed
    micstream._armed = None
    try:
        # a helper that sends audio once told to start, then never exits
        micstream.BINARY = fake_helper(
            "good", "head -c 1 >/dev/null; while :; do printf '%01600d' 0; sleep 0.05; done\n")
        got: list = []
        stream = micstream.MicStream(lambda indata, *_: got.append(len(indata)))
        stream.start()
        pid = stream._proc.pid
        t = time.monotonic()
        stream.abort()
        stop_ms = (time.monotonic() - t) * 1000
        time.sleep(0.3)
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
        check("mic helper delivers audio", bool(got) and got[0] == 800, f"{got[:1]}")
        check("stopping the mic returns at once and the helper is gone",
              stop_ms < 100 and not alive, f"{stop_ms:.1f} ms")

        # a helper that hangs without ever producing audio
        micstream.shutdown()
        micstream.BINARY = fake_helper("hung", "sleep 60\n")
        stream = micstream.MicStream(lambda *a: None)
        t = time.monotonic()
        try:
            stream.start()
            check("a mic helper that hangs gives an error, not a freeze", False)
        except RuntimeError:
            check("a mic helper that hangs gives an error, not a freeze",
                  time.monotonic() - t < micstream.FIRST_AUDIO_TIMEOUT_S + 1)
    finally:
        micstream.shutdown()
        micstream.BINARY, micstream._armed = real_binary, real_armed


def _meeting_checks() -> None:
    """Meeting Mode, with fake audio devices and a fake provider: no real
    microphone, network or Keychain item of the user's is touched."""
    import contextlib
    import io
    import os
    import re
    import stat

    import numpy as np

    from dictate import config
    from dictate.meeting import brain as brain_mod
    from dictate.meeting import keychain, session as session_mod
    from dictate.meeting.live import Chunker
    from dictate.meeting.overlay import Overlay

    print("\n== 9. Meeting Mode ==")

    def tone(seconds: float, amplitude: int = 6000):
        t = np.arange(int(seconds * 16000))
        return (np.sin(t * 0.2) * amplitude).astype(np.int16)

    def quiet_audio(seconds: float):
        return np.zeros(int(seconds * 16000), dtype=np.int16)

    # --- cutting audio into utterances
    chunks: list = []
    chunker = Chunker(lambda audio, at: chunks.append(audio))
    chunker.feed(quiet_audio(3))
    check("silence produces no utterance", not chunks)
    chunker.feed(tone(1.5)); chunker.feed(quiet_audio(1.2))
    chunker.feed(tone(1.0)); chunker.feed(quiet_audio(1.2))
    check("speech is cut into utterances at pauses", len(chunks) == 2,
          f"{len(chunks)} chunks")
    chunks.clear()
    chunker.feed(tone(45))
    check("non-stop speech is still cut every 20 s", len(chunks) == 2,
          f"{len(chunks)} chunks")
    chunks.clear()
    chunker = Chunker(lambda audio, at: chunks.append(audio))
    chunker.feed(tone(2), muted=True); chunker.feed(quiet_audio(1.2))
    check("echo gate: muted mic audio is not transcribed", not chunks)

    # --- what may leave the Mac, and how
    def url_ok(url: str) -> bool:
        try:
            brain_mod.validate_base_url(url)
            return True
        except brain_mod.BrainError:
            return False

    check("https endpoint accepted", url_ok("https://api.deepseek.com/v1"))
    check("plain http endpoint refused", not url_ok("http://api.deepseek.com/v1"))
    check("http allowed only to this Mac", url_ok("http://localhost:11434/v1")
          and not url_ok("http://localhost.evil.com/v1"))
    check("credentials inside the URL refused",
          not url_ok("https://user:pass@api.example.com/v1"))
    cleaned = brain_mod.clean_reply(
        "<think>secret plan</think>**Say** this: [click](https://evil.example) "
        "or http://evil.example/x\n- point two")
    check("model output is reduced to plain text",
          "evil" not in cleaned and "think" not in cleaned and "*" not in cleaned
          and "• point two" in cleaned, cleaned)

    secret = "sk-SMOKE-SECRET-42"
    sent: list = []

    class Reply:
        def __init__(self, status, text="• ok"):
            self.status_code, self._text = status, text

        def json(self):
            return {"choices": [{"message": {"content": self._text}}]}

    def make_brain(replies):
        def post(url, **kw):
            sent.append((url, kw))
            return replies.pop(0) if len(replies) > 1 else replies[0]
        return brain_mod.Brain(
            {"base_url": "https://api.example.com/v1", "model": "m",
             "your_name": "Rohan"}, key_getter=lambda: secret, post=post)

    for status, label in ((401, "a rejected key"), (302, "a redirect")):
        try:
            make_brain([Reply(status)]).what_to_say("", "Them: hi")
            check(f"{label} is an error", False)
        except brain_mod.BrainError as exc:
            check(f"{label} is an error that does not reveal the key",
                  secret not in str(exc), str(exc))
    try:
        make_brain([Reply(402)]).check()
        check("an account with no credit is reported in plain words", False)
    except brain_mod.BrainError as exc:
        check("an account with no credit is reported in plain words",
              "no credit" in str(exc), str(exc))
    sent.clear()
    extra_brain = make_brain([Reply(200)])
    extra_brain._cfg["request_extra"] = {"thinking": {"type": "disabled"},
                                         "model": "evil", "messages": []}
    extra_brain.what_to_say("", "Them: hi")
    body = sent[-1][1]["json"]
    check("request_extra is sent but cannot replace the model or the messages",
          body["thinking"] == {"type": "disabled"} and body["model"] == "m"
          and len(body["messages"]) == 2)
    try:
        make_brain([Reply(200, "")]).what_to_say("", "Them: hi")
        check("an empty reply is reported, not shown as blank", False)
    except brain_mod.BrainError as exc:
        check("an empty reply is reported, not shown as blank",
              "thinking" in str(exc))
    check("requests never follow redirects and carry the key only in the header",
          all(kw["allow_redirects"] is False and secret not in str(kw["json"])
              and url.startswith("https://api.example.com/v1/")
              for url, kw in sent))
    check("no key field exists in config.toml",
          not re.search(r"(?im)^\s*(api_?key|token|secret)\s*=",
                        config.CONFIG_PATH.read_text()))

    # --- Keychain (a throwaway item, removed again)
    service = "Dictate Meeting Mode (smoke test)"
    try:
        keychain.delete_key(service)
        keychain.set_key("sk-one", service)
        keychain.set_key("sk-two", service)
        stored = keychain.get_key(service)
        removed = keychain.delete_key(service)
        check("API key round-trips through the Keychain",
              stored == "sk-two" and removed and keychain.get_key(service) is None)
    except keychain.KeychainError as exc:
        check("API key round-trips through the Keychain", False, str(exc))

    # a key saved for one provider must never be sent to another address
    host_a, host_b = "smoke-a.example.com", "smoke-b.example.com"
    try:
        keychain.set_key("sk-for-a", account=host_a)
        cfg_a = {"base_url": f"https://{host_a}/v1", "model": "m"}
        cfg_b = {"base_url": f"https://{host_b}/v1", "model": "m"}
        posted: list = []
        swapped = brain_mod.Brain(cfg_b, post=lambda url, **kw: posted.append(url))
        try:
            swapped.what_to_say("", "Them: hi")
        except brain_mod.BrainError:
            pass
        check("a key saved for one provider is not sent to a different address",
              brain_mod.Brain(cfg_a).problem() is None
              and "no API key" in (brain_mod.Brain(cfg_b).problem() or "")
              and not posted)
    finally:
        keychain.delete_key(account=host_a)

    # --- the panel
    from AppKit import NSApplication
    NSApplication.sharedApplication()
    panel = Overlay()
    panel._build()
    check("panel is excluded from screen capture", panel._panel.sharingType() == 0)
    panel._panel.close()

    # --- a whole session
    class FakeSystemAudio:
        last = None

        def __init__(self, on_audio):
            self.on_audio, self.alive, self.starts = on_audio, False, 0
            self.failure = "stopped"
            FakeSystemAudio.last = self

        def start(self):
            self.alive, self.starts = True, self.starts + 1

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

    class FakeMic:
        last = None

        def __init__(self, callback):
            self.callback, self.closed = callback, False
            FakeMic.last = self

        def start(self): pass
        def abort(self): pass
        def close(self): self.closed = True

    class FakeOverlay:
        def __init__(self): self.sections, self.visible = {}, False
        def show(self): self.visible = True
        def hide(self): self.visible = False
        def render(self, sections): self.sections = dict(sections)

    heard = iter(["The launch moved to March fifteenth.",
                  "Rohan, where does the pricing page stand?",
                  "It goes live on Monday."])
    cfg = config.load()
    cfg["meeting"] = dict(cfg["meeting"], your_name="Rohan", save_transcript=False)
    sent.clear()
    brain = make_brain([Reply(200, "OK"), Reply(500), Reply(500),
                        Reply(200, "Asked: pricing page status?\n"
                                   "Context: Launch moved to March.\n"
                                   "Say: It goes live on Monday.")])
    overlay = FakeOverlay()
    session = session_mod.MeetingSession(
        cfg, "model", overlay, brain=brain,
        system_audio_factory=FakeSystemAudio, mic_factory=FakeMic,
        transcribe=lambda audio, *a: next(heard))
    saved_dir = Path(tempfile.mkdtemp())
    session_mod.SAVE_DIR = saved_dir / "meetings"

    def wait(condition, seconds=5.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def them(seconds=1.5):
        FakeSystemAudio.last.on_audio(
            np.concatenate([tone(seconds), quiet_audio(1.2)]).tobytes())

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        session.start()
        checked = wait(lambda: session._brain_ready)
        check_calls = len(sent)
        check_body = str(sent[0][1]["json"]) if sent else ""
        ready_shown = wait(lambda: "Suggestions on" in
                           dict(overlay.sections).get("Meeting mode", ""))
        them()
        wait(lambda: len(session.transcript) == 1)
        statement_calls = len(sent) - check_calls
        session_mod.TURN_COOLDOWN_S = 0
        them()  # the question: the provider fails twice (HTTP 500)
        wait(lambda: "paused" in session.note)
        note_after_failure = session.note
        lines_after_failure = len(session.transcript)
        session._last_turn_check = 0
        session._think("turn")  # the provider is back
        wait(lambda: session.say.startswith("Asked"))
        time.sleep(session_mod.ECHO_GATE_S + 0.1)  # the others stop talking
        FakeMic.last.callback(
            np.concatenate([tone(1.5), quiet_audio(1.2)]).reshape(-1, 1))
        wait(lambda: len(session.transcript) == 3)
        text = session.transcript.render()
        say = session.say
        FakeSystemAudio.last.alive = False  # the helper dies
        restarted = wait(lambda: FakeSystemAudio.last.starts == 2, 4)
        shown = dict(overlay.sections)
        session.stop()
    log = out.getvalue()

    check("starting a meeting tests the key and model once, and says so",
          checked and check_calls == 1 and ready_shown)
    check("a plain statement does not call the provider", statement_calls == 0)
    check("a question with my name asks the provider for talking points",
          say.startswith("Asked"), say)
    check("a provider outage keeps the transcript running and says so",
          "paused" in note_after_failure and lines_after_failure == 2,
          note_after_failure)
    check("speakers are labelled Them and You",
          text.count("Them:") == 2 and text.count("You:") == 1, text)
    check("a dead audio helper is restarted", restarted)

    failing = FakeOverlay()
    failing.failed = "macOS would not hide the panel"
    blind = session_mod.MeetingSession(
        cfg, "model", failing, brain=make_brain([Reply(200)]),
        system_audio_factory=FakeSystemAudio, mic_factory=FakeMic,
        transcribe=lambda audio, *a: "x")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        blind.start()
        stopped_itself = wait(lambda: not blind.active, 4)
        blind.stop()
    check("if the panel cannot be hidden, Meeting Mode stops listening",
          stopped_itself)
    bad_key = session_mod.MeetingSession(
        cfg, "model", FakeOverlay(), brain=make_brain([Reply(401)]),
        system_audio_factory=FakeSystemAudio, mic_factory=FakeMic,
        transcribe=lambda audio, *a: "x")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        bad_key.start()
        warned = wait(lambda: "rejected the API key" in bad_key.note)
        bad_key.stop()
    check("a wrong key is reported as soon as the meeting starts", warned)
    dictating = session_mod.MeetingSession(
        cfg, "model", FakeOverlay(), brain=make_brain([Reply(200)]),
        system_audio_factory=FakeSystemAudio, mic_factory=FakeMic,
        transcribe=lambda audio, *a: "Dictated words.", mic_paused=lambda: True)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        dictating.start()
        FakeMic.last.callback(
            np.concatenate([tone(1.5), quiet_audio(1.2)]).reshape(-1, 1))
        time.sleep(0.8)
        leaked = len(dictating.transcript)
        dictating.stop()
    check("words dictated during a meeting stay out of the meeting transcript",
          leaked == 0)
    check("the panel shows the live transcript", "pricing" in shown.get("Live", ""))
    check("stopping wipes the meeting from memory and hides the panel",
          len(session.transcript) == 0 and not session.say
          and not overlay.visible and FakeMic.last.closed
          and session._them is None and session._jobs.empty())
    check("nothing is saved to disk by default", not session_mod.SAVE_DIR.exists())
    check("nothing said in the meeting, and no key, is logged",
          not any(w in log for w in ("pricing", "March", "Monday", secret)), log)

    # opt-in saving
    cfg["meeting"]["save_transcript"] = True
    session = session_mod.MeetingSession(
        cfg, "model", FakeOverlay(), brain=make_brain([Reply(200)]),
        system_audio_factory=FakeSystemAudio, mic_factory=FakeMic,
        transcribe=lambda audio, *a: "Saved line.")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        session.start()
        them()
        wait(lambda: len(session.transcript) == 1)
        session.stop()
    files = list(session_mod.SAVE_DIR.glob("meeting-*.txt"))
    mode = stat.S_IMODE(os.stat(files[0]).st_mode) if files else 0
    dir_mode = stat.S_IMODE(os.stat(session_mod.SAVE_DIR).st_mode) if files else 0
    check("an opted-in transcript is saved readable by this account only",
          len(files) == 1 and mode == 0o600 and dir_mode == 0o700,
          f"{oct(mode)} in {oct(dir_mode)}")
    for f in files:
        f.unlink()


def _summary() -> int:
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    if failed:
        print("SMOKE TEST FAILED")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
