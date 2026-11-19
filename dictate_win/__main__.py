"""Start Dictate for Windows:  pythonw -m dictate_win"""

import atexit
import sys

from dictate_win import __version__, logsetup, paths


def _already_running() -> bool:
    from dictate_win import winapi as api

    # Held for the life of the process; Windows releases it on exit.
    _already_running.handle = api.CreateMutexW(None, False, "Local\\DictateForWindows")
    return api.last_error() == api.ERROR_ALREADY_EXISTS


def main() -> int:
    if sys.platform != "win32":
        print("dictate_win runs on Windows. On a Mac use: python -m dictate")
        return 1
    paths.ensure()
    logsetup.install()
    if _already_running():
        from dictate_win import dialogs

        dialogs.message("Dictate", "Dictate is already running: look for the "
                        "microphone icon next to the clock (it may be under ^).")
        return 0
    print(f"Dictate for Windows v{__version__}")
    from dictate_win import mic
    from dictate_win.app import TrayApp

    atexit.register(mic.shutdown)
    app = TrayApp()
    if app.muter is not None:
        atexit.register(app.muter.unmute)  # never leave the PC muted
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
