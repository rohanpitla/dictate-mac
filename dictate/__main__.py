"""Entry point: python -m dictate"""

import sys

from dictate import __version__, config, permissions
from dictate.app import DictateApp
from dictate.hotkey import HOTKEY_LABELS


def main() -> None:
    print(f"Dictate v{__version__} — local voice dictation (100% on-device)")
    cfg = config.load()

    if not permissions.check_all(prompt=True):
        print("\nDictate will keep running so you can grant permissions, "
              "but the hotkey/paste won't work until you relaunch it.\n")
    else:
        label = HOTKEY_LABELS.get(cfg["general"]["hotkey"], "the hotkey")
        print(f"Permissions OK. Tap {label} to start dictating, tap again "
              "to stop.")

    app = DictateApp(cfg)
    app.start_background_services()
    app.run()


if __name__ == "__main__":
    sys.exit(main())
