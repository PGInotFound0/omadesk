"""omadesk CLI — run the desktop viewer server, manage install/auth."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import secrets
import socket
import subprocess
import sys

LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

CONFIG_DIR = os.path.expanduser("~/.config/omadesk")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token")
DEFAULT_PORT = 9983

UNIT = """\
[Unit]
Description=omadesk — Omarchy remote desktop viewer
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart={venv_python} -m omadesk.server_cli run
Restart=on-failure
RestartSec=3
Slice=user.slice
# Hyprland session env
Environment=WAYLAND_DISPLAY={wayland}
Environment=XDG_RUNTIME_DIR={runtime_dir}

[Install]
WantedBy=graphical-session.target
"""


def token_read() -> str:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(TOKEN_PATH):
        tok = open(TOKEN_PATH).read().strip()
        if re.fullmatch(r"[0-9a-f]{32}", tok):
            return tok
    return token_rotate()


def token_rotate() -> str:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tok = secrets.token_hex(16)
    with open(TOKEN_PATH, "w") as f:
        f.write(tok)
    os.chmod(TOKEN_PATH, 0o600)
    return tok


def get_lan_ips() -> list[str]:
    ips = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
        seen = set()
        for info in infos:
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                ips.append(ip)
    except Exception:
        pass
    return ips


def tailscale_ip() -> str | None:
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else None
        return ip
    except Exception:
        return None


def print_connect_banner(port: int, tok: str) -> None:
    urls = []
    for ip in get_lan_ips():
        urls.append(f"http://{ip}:{port}/#{tok}")
    ts = tailscale_ip()
    if ts:
        u = f"http://{ts}:{port}/#{tok}"
        if u not in urls:
            urls.append(u)

    print("\n===================== omadesk =====================")
    print("Open this on your phone (same network):")
    for u in urls:
        print("  ", u, flush=True)
    primary = urls[0] if urls else f"http://127.0.0.1:{port}/#{tok}"
    try:
        import segno

        qr = segno.make(primary, error="m")
        try:
            qr.terminal(compact=True, border=1)
        except TypeError:
            qr.terminal(compact=True, border=1)
        print("(scan the code above)\n")
        sys.stdout.flush()
    except Exception:
        pass
    print("Token lives in", TOKEN_PATH, "— run `omadesk token-rotate` to change it")
    print("==================================================\n")


def ensure_wayland_display() -> str:
    d = os.environ.get("WAYLAND_DISPLAY")
    if d:
        return d
    # running under systemd without WAYLAND_DISPLAY? look at the runtime dir
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    for name in sorted(os.listdir(runtime)) if os.path.isdir(runtime) else []:
        if name.startswith("wayland-"):
            return name
    return "wayland-1"


def cmd_run(args) -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    tok = token_read()
    display = ensure_wayland_display()

    from omadesk.capture import CaptureEngine
    from omadesk.input import VirtualInput
    from omadesk.server import DeskServer
    from omadesk.audio import AudioSource

    print_connect_banner(args.port, tok)

    engine = CaptureEngine(display)
    engine.start()
    engine.set_active(False)

    vi = VirtualInput(display)
    vi.start()

    audio = AudioSource()
    server = DeskServer(token=tok, port=args.port, bind=args.bind,
                        engine=engine, vi=vi, audio=audio)

    async def main():
        # forward input layout extents to input: outputs change on monitors change
        def on_outputs(outs):
            vi.refresh_layout(outs)
        engine.on_outputs = on_outputs
        await server.run_forever()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        engine.stop()
        vi.stop()
    return 0


def cmd_token_rotate(_args) -> int:
    tok = token_rotate()
    print("new token:", tok)
    return 0


def cmd_service_install(args) -> int:
    venv_python = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable
    import pwd

    uid = os.getuid()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    wayland = ensure_wayland_display()
    unit = UNIT.format(venv_python=venv_python, wayland=wayland, runtime_dir=runtime_dir)
    systemd_path = os.path.expanduser("~/.config/systemd/user/omadesk.service")
    os.makedirs(os.path.dirname(systemd_path), exist_ok=True)
    with open(systemd_path, "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "omadesk"], check=False)
    print("installed systemd user service:", systemd_path)
    print("status:")
    subprocess.run(["systemctl", "--user", "status", "omadesk", "--no-pager"], check=False)
    return 0


def cmd_service_uninstall(_args) -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", "omadesk"], check=False)
    path = os.path.expanduser("~/.config/systemd/user/omadesk.service")
    with contextlib_suppress():
        os.remove(path)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("service removed")
    return 0


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("omadesk — AnyDesk-style viewer for Omarchy")
    sub = ap.add_subparsers(dest="cmd")

    runp = sub.add_parser("run", help="run the capture engine + WebRTC server")
    runp.add_argument("--port", type=int, default=DEFAULT_PORT)
    runp.add_argument("--bind", default="0.0.0.0")

    sub.add_parser("token-rotate", help="generate a new access token")

    ip = sub.add_parser("service-install", help="install and start systemd user service")
    sub.add_parser("service-uninstall", help="disable + remove the systemd service")

    args = ap.parse_args(argv)
    # module cheese: quick way to reuse token logic in server unit
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "token-rotate":
        return cmd_token_rotate(args)
    if args.cmd == "service-install":
        return cmd_service_install(args)
    if args.cmd == "service-uninstall":
        return cmd_service_uninstall(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
