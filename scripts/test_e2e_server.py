#!/usr/bin/env python3
"""omadesk end-to-end verification against the live Hyprland session.

Brings the whole stack up (capture, input, WebRTC server), then connects an
aiortc "browser" peer and verifies:
  1. auth rejects a bad token                 (http 401)
  2. webrtc session + video decoding          (framesDecoded >= 20)
  3. pointer move lands exactly               (hyprctl cursorpos)
  4. pointer drag lands exactly
  5. datachannel typing lands in a fresh      (bash reads a line + Enter)
  6. clipboard phone -> PC                    (wl-paste)
  7. clipboard PC -> phone                    (server message)

Run: .venv/bin/python scripts/test_e2e_server.py   (on the Omarchy session)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LOG = []

async def boot_server(port: int):
    from omadesk.server_cli import token_rotate
    token = token_rotate()
    from omadesk.capture import CaptureEngine
    from omadesk.input import VirtualInput
    from omadesk.server import DeskServer
    from omadesk.audio import AudioSource

    engine = CaptureEngine(os.environ.get("WAYLAND_DISPLAY", "wayland-1"))
    engine.start()
    engine.set_active(False)
    vi = VirtualInput(os.environ.get("WAYLAND_DISPLAY", "wayland-1"))
    vi.start()
    audio = AudioSource()
    srv = DeskServer(token=token, port=port, bind="127.0.0.1", engine=engine, vi=vi, audio=audio)
    asyncio.ensure_future(srv.run_forever())
    await asyncio.sleep(0.9)
    return token, srv, engine, vi, audio


W = 1680
H = 1050


async def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("omadesk.audio").setLevel(logging.DEBUG)
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        line = ("OK " if ok else "FAIL ") + name + (" — " + detail if detail else "")
        LOG.append(line)
        print("CHECK:", line, flush=True)

    port = 9976
    token, srv, engine, vi, audio = await boot_server(port)

    import aiortc
    from aiortc import RTCPeerConnection, RTCSessionDescription

    import aiohttp

    async with aiohttp.ClientSession() as http:
        # 1) auth gate
        async with http.post(
            f"http://127.0.0.1:{port}/api/session",
            headers={"X-Auth": "wrong", "Content-Type": "application/json"},
            json={},
        ) as resp:
            check("auth rejects bad token", resp.status == 401, f"status={resp.status}")

        # fresh bash that reads one line then echoes it to a file
        sink = "/tmp/opencode/e2e_sink.txt"
        subprocess.run(["sh", "-c", f"rm -f {sink}"])
        foot = subprocess.Popen(
            ["foot", "-e", "bash", "-c",
             f'read -r L; printf %s "$L" > {sink}'],
        )
        await asyncio.sleep(2.4)
        aij = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout or "[]")
        feet = [w for w in aij if w.get("class") == "foot" and w.get("pid") == foot.pid]
        assert feet, "fresh foot window not found"

        # 2..7 via one webrtc session
        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")

        deco = {"n": 0}
        audio_frames = {"n": 0}
        @pc.on("track")
        async def on_track(track):
            print("[dbg] track event:", track.kind, flush=True)
            if track.kind == "video":
                async def eat():
                    while deco["n"] < 200:
                        try:
                            await asyncio.wait_for(track.recv(), timeout=2)
                            deco["n"] += 1
                        except asyncio.TimeoutError:
                            return
                        except Exception:
                            return
                asyncio.ensure_future(eat())
            elif track.kind == "audio":
                async def eat_audio():
                    try:
                        while audio_frames["n"] < 24:
                            try:
                                f = await asyncio.wait_for(track.recv(), timeout=1.5)
                                audio_frames["n"] += 1
                                if audio_frames["n"] == 1:
                                    print("[dbg] first audio frame received", flush=True)
                            except asyncio.TimeoutError:
                                continue  # audio may not be enabled yet; keep waiting
                        print("[dbg] audio consumer ended with", audio_frames["n"], flush=True)
                    except Exception as exc:
                        print("[dbg] audio consumer crashed:", repr(exc), flush=True)
                asyncio.ensure_future(eat_audio())

        channel = pc.createDataChannel("omadesk", ordered=True)
        open_evt = asyncio.Event()
        server_msgs = []

        @channel.on("open")
        async def _o():
            open_evt.set()

        @channel.on("message")
        def on_msg(m):
            print("[dbg] server msg:", m[:80], flush=True)
            try:
                server_msgs.append(json.loads(m))
            except Exception:
                pass

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        async with http.post(
            f"http://127.0.0.1:{port}/api/session",
            headers={"X-Auth": token, "Content-Type": "application/json"},
            json={"sdp": {"type": offer.type, "sdp": offer.sdp}},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print("SESSION 500 BODY:", body[-1500:], flush=True)
                raise SystemExit(1)
            ans = await resp.json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"]["sdp"], type="answer"))
        await asyncio.wait_for(open_evt.wait(), 10)
        check("datachannel + webrtc handshake", True)
        # enable audio right away, like the web client does
        channel.send(json.dumps({"t": "audio", "on": True}))
        # focus = move the real cursor into the window (input:follow_mouse=1)
        w_ = feet[0]
        cx = w_["at"][0] + w_["size"][0] // 2
        cy = w_["at"][1] + w_["size"][1] // 2
        channel.send(json.dumps({"t": "move", "x": cx / W, "y": cy / H}))
        await asyncio.sleep(0.8)
        st = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True).stdout or "{}")
        focused = st.get("address") == w_["address"]
        check("focus via pointer", focused, f"active={st.get('address')} want={w_['address']}")

        # 2) video decoding
        t0 = time.time()
        while deco["n"] < 20 and time.time() - t0 < 10:
            await asyncio.sleep(0.3)
        check("video decodes", deco["n"] >= 20, f"frames={deco['n']}")

        # 3) pointer move (exact)
        channel.send(json.dumps({"t": "move", "x": 0.25, "y": 0.75}))
        await asyncio.sleep(0.5)
        pos = subprocess.run(["hyprctl", "cursorpos"], capture_output=True, text=True).stdout.strip()
        x, y = (int(v) for v in pos.split(","))
        check("pointer move exact", abs(x - W // 4) < 30 and abs(y - H * 3 // 4) < 30, pos)

        # 4) drag
        channel.send(json.dumps({"t": "btn", "b": "left", "p": True}))
        channel.send(json.dumps({"t": "move", "x": 0.35, "y": 0.75}))
        channel.send(json.dumps({"t": "btn", "b": "left", "p": False}))
        await asyncio.sleep(0.4)
        pos = subprocess.run(["hyprctl", "cursorpos"], capture_output=True, text=True).stdout.strip()
        x, y = (int(v) for v in pos.split(","))
        check("pointer drag exact", abs(x - int(W * 0.35)) < 30, pos)

        # 5) typing via text channel (re-focus first: pointer tests walked off)
        cx = w_["at"][0] + w_["size"][0] // 2
        cy = w_["at"][1] + w_["size"][1] // 2
        channel.send(json.dumps({"t": "move", "x": cx / W, "y": cy / H}))
        await asyncio.sleep(0.7)
        st = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True).stdout or "{}")
        focused = st.get("address") == w_["address"]
        print("CHECK: re-focus:", focused, st.get("address"), w_["address"], flush=True)
        channel.send(json.dumps({"t": "text", "s": "from-omadesk phone"}))
        await asyncio.sleep(0.5)
        channel.send(json.dumps({"t": "key", "c": "Enter", "d": True, "m": {}}))
        await asyncio.sleep(0.06)
        channel.send(json.dumps({"t": "key", "c": "Enter", "d": False, "m": {}}))
        await asyncio.sleep(1.2)
        got = open(sink).read() if os.path.exists(sink) else ""
        check("typing lands in app", "from-omadesk" in got, repr(got[:40]))

        # 6) clipboard phone -> pc
        channel.send(json.dumps({"t": "clipSet", "s": "CLIP-OK"}))
        await asyncio.sleep(1.0)
        clip = subprocess.run(["wl-paste", "-n"], capture_output=True, text=True).stdout.strip()
        check("clipboard phone->pc", clip == "CLIP-OK", clip[:30])

        # 7) clipboard pc -> phone (server pushes on change)
        priv = "PC-SAYS-HELLO"
        subprocess.Popen("printf %s " + priv + " | wl-copy", shell=True)
        await asyncio.sleep(2.5)
        got_clip = any(m.get("t") == "clip" and m.get("s") == priv for m in server_msgs)
        check("clipboard pc->phone", got_clip, json.dumps(server_msgs[-2:])[:80])

        # 8) audio: consumer started at track event; verify received frames by now
        check("desktop audio streams (opus frames decoded)", audio_frames["n"] >= 20,
              f"frames={audio_frames['n']}")

        # 9) workspace switch via datachannel (foot's own ws known)
        wins0 = srv._win_snapshot()
        footwin = [w for w in wins0 if w["class"] == "foot" and w.get("ws")]
        foot_ws = footwin[0]["ws"] if footwin else 2
        before = srv._ws_snapshot().get("current")
        want = foot_ws if foot_ws != before else (2 if before != 2 else 1)
        channel.send(json.dumps({"t": "ws", "id": want}))
        await asyncio.sleep(1.2)
        now = srv._ws_snapshot().get("current")
        check("workspace switch (Super+digit)", now == want, f"{before} -> {now} (wanted {want})")
        wslist = srv._ws_snapshot()
        check("wslist snapshot sane", all("id" in w for w in wslist["list"]) and len(wslist["list"]) >= 1,
              str(wslist["list"])[:80])

        # 10) window list + click-to-focus: find the foot window on the target ws
        wins = srv._win_snapshot()
        footwin = [w for w in wins if w["ws"] == want and w["class"] == "foot"]
        if not footwin:
            footwin = [w for w in srv._win_snapshot() if w["class"] == "foot"]
        wins_check = footwin
        check("window list contains test foot", bool(wins_check),
              str([w["title"][:20] for w in srv._win_snapshot()[:2]]))
        if footwin:
            channel.send(json.dumps({"t": "wingo", "addr": footwin[0]["addr"]}))
            await asyncio.sleep(1.4)
            focus = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True).stdout
            foc = json.loads(focus or "{}")
            check("click-to-focus window", foc.get("class") == "foot", foc.get("class"))

        await pc.close()
        foot.terminate()
        subprocess.run(["sh", "-c", "pkill -f 'read -r L; printf' 2>/dev/null; true"], capture_output=True)

    engine.stop()
    vi.stop()
    audio.stop()

    aurel = [r for r in results]
    print("\n".join(l for l in map(str, LOG)))
    ok_all = all(ok for _, ok, _ in aurel)
    print("E2E RESULT:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
