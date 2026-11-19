"""Build the release zip that install.ps1 downloads, and print the two
lines to put into install.ps1 ($ReleaseUrl and $ReleaseSha256).

    python windows/make_release.py v0.1.0

Then: commit install.ps1, tag, and upload dist/dictate-windows-<tag>.zip
to the GitHub release of that tag. The installer refuses any zip whose
checksum differs, so a swapped or tampered download can't be installed.
"""

import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["dictate", "dictate_win", "windows/uninstall.ps1", "windows/README.md",
           "requirements-windows.txt", "requirements-windows-local.txt", "LICENSE"]
SKIP_PARTS = {"__pycache__", ".DS_Store"}


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("v"):
        print(__doc__)
        return 1
    tag = sys.argv[1]
    out = ROOT / "dist" / f"dictate-windows-{tag}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            path = ROOT / item
            files = [path] if path.is_file() else sorted(path.rglob("*"))
            for f in files:
                if f.is_file() and not SKIP_PARTS & set(f.parts):
                    info = zipfile.ZipInfo(str(f.relative_to(ROOT)).replace("\\", "/"),
                                           date_time=(2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    zf.writestr(info, f.read_bytes())  # fixed dates: same input, same zip
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"built {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print(f'$ReleaseUrl = "https://github.com/rohanpitla/dictate-mac/releases/download/{tag}/{out.name}"')
    print(f'$ReleaseSha256 = "{digest}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
