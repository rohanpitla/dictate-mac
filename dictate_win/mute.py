"""Mute other sound (music, videos) while dictating; bring it back after.

- Only undoes its own mute: if the sound was already muted, it is left
  alone and stays muted.
- Remembers exactly which speaker/headphone device it muted, and unmutes
  that one, even if Windows switched to another device meanwhile
  (e.g. Bluetooth headphones connected mid-dictation).
- Writes a small marker file while muted. If Dictate is killed while the
  sound is muted, the next start finds the marker and unmutes. The laptop
  can't stay silent because of us.

All audio-system calls run on one worker thread that set up COM itself
(Windows requires COM to be initialised on the thread that uses it).
"""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from dictate_win import paths


class PycawBackend:
    """Windows Core Audio through pycaw. Create it on the worker thread."""

    def __init__(self) -> None:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        comtypes.CoInitialize()
        self._clsctx = comtypes.CLSCTX_ALL
        self._iface = IAudioEndpointVolume
        self._enum = AudioUtilities.GetDeviceEnumerator()

    def default_id(self) -> str | None:
        device = self._enum.GetDefaultAudioEndpoint(0, 1)  # render, multimedia
        return device.GetId() if device else None

    def _volume(self, device_id: str):
        device = self._enum.GetDevice(device_id)
        raw = device.Activate(self._iface._iid_, self._clsctx, None)
        return raw.QueryInterface(self._iface)

    def is_muted(self, device_id: str) -> bool:
        return bool(self._volume(device_id).GetMute())

    def set_mute(self, device_id: str, muted: bool) -> None:
        self._volume(device_id).SetMute(1 if muted else 0, None)


class OutputMuter:
    def __init__(self, backend_factory=PycawBackend) -> None:
        self._factory = backend_factory
        self._backend = None
        self._broken = False
        self._want_muted = False
        self._muted_device: str | None = None
        # one thread, so mute/unmute requests run in the order they were made
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dictate-mute")

    def _get_backend(self):
        if self._backend is None and not self._broken:
            try:
                self._backend = self._factory()
            except Exception as exc:
                self._broken = True
                print(f"[dictate] muting is unavailable on this PC ({exc!r})",
                      file=sys.stderr)
        return self._backend

    def mute_async(self) -> None:
        self._want_muted = True
        self._worker.submit(self._apply)

    def unmute_async(self) -> None:
        self._want_muted = False
        self._worker.submit(self._apply)

    def unmute(self, timeout: float = 3.0) -> None:
        self._want_muted = False
        try:
            self._worker.submit(self._apply).result(timeout)
        except Exception:
            pass

    def recover_after_crash(self, timeout: float = 3.0) -> None:
        try:
            self._worker.submit(self._recover).result(timeout)
        except Exception:
            pass

    def _apply(self) -> None:
        backend = self._get_backend()
        if backend is None:
            return
        try:
            if self._want_muted and self._muted_device is None:
                device = backend.default_id()
                if not device or backend.is_muted(device):
                    return  # no speakers, or the user muted it themselves
                _write_marker(device)
                backend.set_mute(device, True)
                self._muted_device = device
            elif not self._want_muted and self._muted_device is not None:
                device, self._muted_device = self._muted_device, None
                try:
                    backend.set_mute(device, False)
                finally:
                    _clear_marker()
        except Exception as exc:
            print(f"[dictate] mute/unmute failed: {exc!r}", file=sys.stderr)

    def _recover(self) -> None:
        try:
            device = json.loads(paths.MUTE_MARKER.read_text(encoding="utf-8"))["device"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        backend = self._get_backend()
        if backend is None:
            return
        try:
            backend.set_mute(device, False)
            print("[dictate] turned the sound back on (Dictate had stopped "
                  "while it was muted)")
        except Exception as exc:
            print(f"[dictate] could not unmute after a crash: {exc!r}", file=sys.stderr)
        finally:
            _clear_marker()


_marker_lock = threading.Lock()


def _write_marker(device: str) -> None:
    with _marker_lock:
        try:
            paths.MUTE_MARKER.write_text(json.dumps({"device": device}), encoding="utf-8")
        except OSError:
            pass


def _clear_marker() -> None:
    with _marker_lock:
        try:
            paths.MUTE_MARKER.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
