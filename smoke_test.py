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
        check("config.toml loads", cfg["general"]["hotkey"] == "right_option",
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

    vocab_in = "I built it with n 8 n and fast api on a vps for cloud code"
    vocab_out = cleanup.apply_vocabulary(vocab_in, cfg["vocabulary"])
    check("vocabulary replacement",
          all(term in vocab_out for term in ["n8n", "FastAPI", "VPS", "Claude Code"]),
          repr(vocab_out))

    # Graceful degradation: unreachable Ollama must return raw, never raise
    bad_cfg = {"ollama": {"model": "qwen2.5:3b", "fallback_model": None},
               "vocabulary": {}}
    real_url = cleanup.OLLAMA_URL
    cleanup.OLLAMA_URL = "http://localhost:1"  # nothing listens here
    t0 = time.monotonic()
    degraded, stage = cleanup.clean(raw, bad_cfg)
    dt = time.monotonic() - t0
    cleanup.OLLAMA_URL = real_url
    check("ollama down -> raw transcript, fast, no crash",
          degraded == raw and stage == "raw" and dt < 2, f"{dt * 1000:.0f}ms")

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
    for hk in ("right_option", "fn", "f19"):
        try:
            HotkeyListener(hk, on_press=lambda: None, on_release=lambda: None)
        except Exception:
            ok_hk = False
    check("all three hotkey choices construct", ok_hk)

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
          "professional but warm" in prompt and "Remove filler words" in prompt
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

    return _summary()


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
