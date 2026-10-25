"""Hyprland IPC — thin client for hyprctl-style commands.

Used where the server shells out for `hyprctl`-style data (workspaces, window
lists). Prefers the real `hyprctl` binary when present; falls back to speaking
directly to the compositor's unix socket ($XDG_RUNTIME_DIR/hypr/$SIG/.socket.sock),
so the omadesk server works inside a container without the Hyprland CLI.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess

log = logging.getLogger("omadesk.hyprctl")


def _sig() -> str:
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or ""
    # accept either the full hex or the tidier hash-hex used in older versions
    if sig:
        return sig
    return ""


def _runtime() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def _socket_path() -> str:
    base = os.environ.get("HYPRLAND_SOCKET")  # manual override
    if base:
        return base
    runtime = _runtime()
    sig = _sig()
    if sig:
        cand = os.path.join(runtime, "hypr", sig, ".socket.sock")
        if os.path.exists(cand):
            return cand
    # fallback: at most one live compositor for this user session
    try:
        d = os.path.join(runtime, "hypr")
        for entry in sorted(os.listdir(d)):
            cand = os.path.join(d, entry, ".socket.sock")
            if os.path.exists(cand):
                return cand
    except Exception:
        pass
    return ""


def ipc(cmd: str) -> str:
    """Run a hyprctl-style command over the IPC socket; returns the text."""
    path = _socket_path()
    if not path:
        return ""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(4.0)
    try:
        s.connect(path)
        s.sendall((cmd + "\n").encode())
        chunks = []
        while True:
            d = s.recv(65536)
            if not d:
                break
            chunks.append(d)
        return b"".join(chunks).decode(errors="replace")
    finally:
        s.close()


def run(cmd: str) -> str:
    """`hyprctl <cmd>` equivalent — callable anywhere the env is right."""
    try:
        if shutil.which("hyprctl"):
            out = subprocess.run(["hyprctl", *cmd.split()],
                                 capture_output=True, text=True, timeout=4)
            return out.stdout
        return ipc(cmd)
    except Exception:
        log.debug("hyprctl/ipc failed for %r", cmd, exc_info=True)
        return ""
