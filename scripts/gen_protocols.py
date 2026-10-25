#!/usr/bin/env python3
"""Generate pywayland bindings for the protocols omadesk needs.

Run with the omadesk venv's python. Outputs into omadesk/protocol_gen/ so
that at runtime we can `from omadesk.protocol_gen import ...`.

Protocol XMLs are vendored under omadesk/protocols/ (MIT-licensed, see the
copyright headers inside each file).
"""
from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
XML_DIR = os.path.join(ROOT, "omadesk", "protocols")
OUT_DIR = os.path.join(ROOT, "omadesk", "protocol_gen")

PROTOCOLS = [
    "wlr-screencopy-unstable-v1.xml",
    "wlr-virtual-pointer-unstable-v1.xml",
    "virtual-keyboard-unstable-v1.xml",
]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR)

    # pass wayland.xml first so the scanner resolves core interfaces
    # (wl_output, wl_keyboard, ...) — see pywayland scanner docs
    wayland_xml = "/usr/share/wayland/wayland.xml"
    if not os.path.exists(wayland_xml):
        import subprocess

        try:
            wayland_xml = subprocess.check_output(
                ["pkg-config", "--variable=pkgdatadir", "wayland-scanner"], text=True
            ).strip() + "/wayland.xml"
        except Exception as exc:  # pragma: no cover
            sys.exit(f"cannot find wayland.xml ({exc})")

    inputs = [
        os.path.join(XML_DIR, xml) for xml in PROTOCOLS
    ]
    cmd = [
        sys.executable, "-m", "pywayland.scanner",
        "-o", OUT_DIR,
        "-i", wayland_xml,
        *inputs,
    ]
    log = subprocess.run(cmd, capture_output=True, text=True)
    if log.returncode != 0 or not (log.stdout or "").strip():
        sys.stderr.write(log.stdout or "")
        sys.stderr.write(log.stderr or "")
        sys.exit(f"scanner failed (rc={log.returncode})")
    print("scanner ok:", log.stdout.strip()[-200:])

    # Generated modules do `from .wayland import WlOutput` etc, so provide a
    # shim re-exporting pywayland's own wayland bindings.
    shim = os.path.join(OUT_DIR, "wayland.py")
    with open(shim, "w") as f:
        f.write(
            "# shim: re-export pywayland's wayland core bindings so generated\n"
            "# protocol modules can use relative imports\n"
            "from pywayland.protocol.wayland import *  # noqa: F401,F403\n"
        )
    with open(os.path.join(OUT_DIR, "__init__.py"), "w") as f:
        f.write("# generated pywayland bindings used by omadesk\n")
    print("generated into", OUT_DIR)


if __name__ == "__main__":
    main()
