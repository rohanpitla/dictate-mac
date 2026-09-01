"""Entry point: python -m dictate"""

import sys

from dictate import __version__, config, permissions
from dictate.app import DictateApp


def main() -> None:
    print(f"Dictate v{__version__} — local voice dictation (100% on-device)")
    cfg = config.load()

    if not permissions.check_all(prompt=True):
        print("\nDictate will keep running so you can grant permissions, "
              "but the hotkey/paste won't work until you relaunch it.\n")
    else:
        print("Permissions OK. Hold Right Option anywhere to dictate.")

    app = DictateApp(cfg)
    app.start_background_services()
    app.run()


if __name__ == "__main__":
    sys.exit(main())
