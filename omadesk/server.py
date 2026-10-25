"""Frame hub + aiortc video track + WebRTC signaling server (aiohttp)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import subprocess
import time
from fractions import Fraction

import numpy as np
from aiohttp import web

from omadesk.audio import AudioSource, AudioFeed

log = logging.getLogger("omadesk.server")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FrameHub:
    """Broadcasts the latest captured frame to peers."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.cond = asyncio.Condition(self.lock)
        self.arr: np.ndarray | None = None
        self.output_name: str | None = None
        self.seq = 0
        self.fps_counter = 0
        self.last_stats = time.monotonic()
        self.current_fps = 0.0

    def push_threadsafe(self, loop, arr: np.ndarray, output_name: str) -> None:
        loop.call_soon_threadsafe(self._push, arr, output_name)

    def _push(self, arr: np.ndarray, output_name: str) -> None:
        self.arr = arr
        self.output_name = output_name
        self.seq += 1
        self.fps_counter += 1
        now = time.monotonic()
        if now - self.last_stats >= 2.0:
            self.current_fps = self.fps_counter / (now - self.last_stats)
            self.fps_counter = 0
            self.last_stats = now
        async def _notify():
            async with self.cond:
                self.cond.notify_all()
        asyncio.ensure_future(_notify())

    async def wait_frame(self, last_seq: int, timeout: float = 0.5):
        """Wait until seq > last_seq; returns (arr, seq)."""
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(
                    lambda: self.arr is not None and self.seq > last_seq), timeout)
            except asyncio.TimeoutError:
                return None, last_seq
            return self.arr, self.seq


class FeedTrack:
    """Minimal-but-correct aiortc video track backed by the hub.

    We subclass lazily (aiortc imports are heavy); the class is created at
    module import time only if aiortc is importable.
    """

    def __init__(self, hub: FrameHub):
        from aiortc.mediastreams import VideoStreamTrack

        self._VideoStreamTrack = VideoStreamTrack
        self.hub = hub
        self._last_seq = 0
        self._t0 = time.monotonic()
        self.closed = False

        ob = self

        class _Track(VideoStreamTrack):  # type: ignore[valid-type]
            async def recv(self):  # type: ignore[override]
                import av

                arr, seq = await ob.hub.wait_frame(ob._last_seq)
                if arr is None or self.readyState != "live":
                    # idle: emit a repeat frame to keep the decode pipeline happy
                    arr, seq = ob.hub.arr, ob.hub.seq or ob._last_seq
                    if arr is None:
                        await asyncio.sleep(0.03)
                        return await self.recv()
                ob._last_seq = seq
                frame = av.VideoFrame.from_ndarray(arr, format="bgra")
                frame.pts = int((time.monotonic() - ob._t0) * 90000)
                frame.time_base = Fraction(1, 90000)
                return frame

            async def stop(self):
                ob.closed = True
                await super().stop()

        self.track = _Track()

    def as_track(self):
        return self.track


class Client:
    ids = 0

    def __init__(self, kind: str):
        Client.ids += 1
        self.id = Client.ids
        self.kind = kind
        self.pc = None
        self.channel = None
        self.feed = None
        self.feed_audio: "AudioFeed | None" = None
        self.audio_on = True
        self.created = time.monotonic()


class DeskServer:
    def __init__(self, token: str, port: int = 9983, bind: str = "0.0.0.0",
                 engine=None, vi=None, audio: "AudioSource | None" = None):
        self.token = token
        self.port = port
        self.bind = bind
        self.engine = engine
        self.vi = vi
        self.audio = audio
        self.hub = FrameHub()
        self.clients: dict[int, Client] = {}
        self._clip_task = None
        self._last_clip = None
        self.app = self._build_app()
        self._runner: web.AppRunner | None = None
        self._site = None
        self.loop: asyncio.AbstractEventLoop | None = None  # bound in start_async

    # ------------------ auth ------------------
    def _auth(self, request):
        tok = request.headers.get("X-Auth", "")
        if not secrets.compare_digest(tok, self.token):
            return web.Response(status=401, text="bad token")
        return None

    # ------------------ routes ------------------
    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/app.js", self._appjs)
        app.router.add_get("/app.css", self._appcss)
        app.router.add_get("/manifest.webmanifest", self._manifest)
        app.router.add_get("/icons/{name}", self._icon)
        app.router.add_get("/qr.svg", self._qr)
        app.router.add_get("/api/info", self._api_info)
        app.router.add_get("/api/status", self._api_status)
        app.router.add_post("/api/session", self._api_session)
        app.router.add_post("/api/disconnect", self._api_disconnect)
        return app

    async def _index(self, request) -> web.Response:
        return web.FileResponse(os.path.join(ROOT, "web", "index.html"),
                                headers={"Cache-Control": "no-store"})

    async def _appjs(self, request) -> web.Response:
        return web.FileResponse(os.path.join(ROOT, "web", "app.js"),
                                headers={"Cache-Control": "no-store"})

    async def _appcss(self, request) -> web.Response:
        return web.FileResponse(os.path.join(ROOT, "web", "app.css"),
                                headers={"Cache-Control": "no-cache"})

    async def _manifest(self, request) -> web.Response:
        return web.FileResponse(os.path.join(ROOT, "web", "manifest.webmanifest"),
                                headers={"Content-Type": "application/manifest+json"})

    async def _icon(self, request) -> web.Response:
        name = request.match_info["name"]
        path = os.path.join(ROOT, "web", "icons", os.path.basename(name))
        if not os.path.exists(path):
            return web.Response(status=404, text="")
        return web.FileResponse(path)

    async def _qr(self, request) -> web.Response:
        import io

        import segno
        req = request.rel_url.query
        url = req.get("url")
        if not url:
            return web.Response(status=400, text="url required")
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=5, dark="#111")
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")

    async def _api_info(self, request):
        verdict = self._auth(request)
        if verdict:
            return verdict
        outs = [o.as_dict() for o in self.engine.outputs()]
        cur = self.engine.current_output()
        return web.json_response({
            "version": "0.1.0",
            "outputs": outs,
            "current": cur.name if cur else None,
            "clients": len(self.clients),
        })

    async def _api_status(self, request):
        verdict = self._auth(request)
        if verdict:
            return verdict
        return web.json_response({
            "clients": len(self.clients),
            "fps": round(self.hub.current_fps, 1),
        })

    async def _api_session(self, request):
        verdict = self._auth(request)
        if verdict:
            return verdict
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        sdp_in = body.get("sdp") or {}
        if sdp_in.get("type") != "offer" or not sdp_in.get("sdp"):
            return web.json_response({"error": "offer required"}, status=400)
        if len(self.clients) >= 3:
            return web.json_response({"error": "too many viewers (3 max)"}, status=429)
        output = body.get("output")

        import aiortc  # noqa: F401  (validated earlier)
        from aiortc import RTCPeerConnection, RTCSessionDescription

        client = Client("webrtc")
        pc = RTCPeerConnection()
        client.pc = pc
        self.clients[client.id] = client

        # video: recv-only from us
        client.feed = FeedTrack(self.hub)
        pc.addTrack(client.feed.as_track())

        # audio: desktop sink monitor (if source exists and the client offered
        # an audio m-line — otherwise skip audio for that peer)
        if self.audio is not None and "m=audio" in (sdp_in.get("sdp") or ""):
            client.feed_audio = AudioFeed(self.audio)
            client.feed_audio.subscribe(client.id)
            pc.addTrack(client.feed_audio.as_track())

        @pc.on("datachannel")
        def _(channel):
            log.info("datachannel from %s", channel.label)
            client.channel = channel
            self._bind_channel(client)

            @channel.on("message")
            def on_message(msg):
                try:
                    self._on_channel_message(client, msg)
                except Exception:
                    log.exception("channel message failed")
                    with contextlib.suppress(Exception):
                        channel.send(json.dumps({
                            "t": "error", "msg": "internal error"}))

        @pc.on("connectionstatechange")
        async def _on_state():
            log.info("peer %s state=%s", client.id, pc.connectionState)
            if pc.connectionState in ("closed", "failed", "disconnected"):
                await self._close_client(client)

        await pc.setRemoteDescription(RTCSessionDescription(
            sdp=sdp_in["sdp"], type="offer"))
        self.engine.set_target(output)
        self._wake_capture()
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return web.json_response({"sdp": {
            "type": pc.localDescription.type,
            "sdp": pc.localDescription.sdp
        }})

    async def _api_disconnect(self, request):
        verdict = self._auth(request)
        if verdict:
            return verdict
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        cid = body.get("id")
        client = self.clients.get(cid)
        if client:
            await self._close_client(client)
        return web.json_response({"ok": True})

    # ------------------ channel protocol ------------------
    def _bind_channel(self, client: Client) -> None:
        self.engine.set_active(True)

    def _wake_capture(self) -> None:
        async def _wake():
            self.engine.set_active(len(self.clients) > 0)
        asyncio.ensure_future(_wake())

    async def _close_client(self, client: Client):
        self.clients.pop(client.id, None)
        with contextlib.suppress(Exception):
            if client.feed_audio:
                client.feed_audio.unsubscribe()
        with contextlib.suppress(Exception):
            if client.pc:
                await client.pc.close()
        async def _sleep_check():
            await asyncio.sleep(0.2)
            if not self.clients:
                self.engine.set_active(False)
                if self.audio is not None:
                    asyncio.ensure_future(self.audio.stop())
        asyncio.ensure_future(_sleep_check())
        log.info("client %s closed (%d left)", client.id, len(self.clients))

    def _on_channel_message(self, client: Client, msg: str) -> None:
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        t = data.get("t")
        vi = self.vi
        log.info("chan <- %s", t)
        if t == "ready":
            self._send(client, {"t": "outputs", "outs": [
                o.as_dict() for o in self.engine.outputs()],
                "audio": self.audio is not None})
            self._send_clipboard(client)
        elif t == "move" and vi:
            out = self.engine.current_output()
            if out and vi.layout_w and vi.layout_h:
                nx = max(0.0, min(1.0, float(data["x"])))
                ny = max(0.0, min(1.0, float(data["y"])))
                vi.pointer_move(nx, ny)
        elif t == "btn" and vi:
            b = {"left": "left", "right": "right", "middle": "middle"}.get(data.get("b"))
            if b:
                vi.pointer_button(b, bool(data.get("p")))
        elif t == "scroll" and vi:
            vi.pointer_scroll(float(data.get("dx", 0)), float(data.get("dy", 0)))
        elif t == "key" and vi:
            m = {k: bool(data.get("m", {}).get(k)) for k in ("shift", "ctrl", "alt", "meta")}
            vi.key(data["c"], bool(data.get("d")), m)
        elif t == "text" and vi:
            vi.type_text(str(data.get("s", ""))[:512])
        elif t == "clipSet":
            txt = str(data.get("s", ""))[:65536]
            self._set_clipboard(client, txt)
        elif t == "clipGet":
            self._send_clipboard(client)
        elif t == "quality":
            mbps = int(data.get("mbps", 4))
            asyncio.ensure_future(self._apply_quality(client, mbps))
        elif t == "outputs":
            out = data.get("name")
            self.engine.set_target(out or None)
            self._verify_output(client)
        elif t == "audio":
            log.info("audio toggle <- %s", data)
            on = bool(data.get("on", True))
            client.audio_on = on
            if client.feed_audio:
                client.feed_audio.set_enabled(on)
            if self.audio is not None and on:
                asyncio.ensure_future(self.audio.ensure_running())
        elif t == "ws":
            asyncio.ensure_future(self._ws_go(client, int(data.get("id", 1))))
        elif t == "wsnext":
            asyncio.ensure_future(self._ws_step(client, +1))
        elif t == "wsprev":
            asyncio.ensure_future(self._ws_step(client, -1))
        elif t == "wsformer":
            asyncio.ensure_future(self._ws_former(client))
        elif t == "wslist":
            self._push_wslist(client)
        elif t == "winlist":
            asyncio.ensure_future(self._push_winlist(client))
        elif t == "wingo":
            asyncio.ensure_future(self._focus_window(client, data.get("addr", "")))

    def _verify_output(self, client: Client) -> None:
        async def _push():
            await asyncio.sleep(0.3)
            self._send(client, {"t": "outputs", "outs": [
                o.as_dict() for o in self.engine.outputs()],
                "current": (self.engine.current_output().name
                            if self.engine.current_output() else None)})
        asyncio.ensure_future(_push())

    # ------------------ desktop navigation (Hyprland) ------------------
    def _hypr(self, *args):
        """Snapshot data via `hyprctl` CLI or the IPC socket fallback."""
        from omadesk import hyprctl

        return hyprctl.run(" ".join(args))

    async def _inject_key(self, code: str, mods: dict) -> None:
        """Synthesize a press+release through the virtual keyboard thread."""
        vi = self.vi
        vi.key(code, True, mods)
        await asyncio.sleep(0.05)
        vi.key(code, False, {})
        await asyncio.sleep(0.12)

    async def _ws_go(self, client: Client, wid: int) -> None:
        """Switch to workspace 1..10 via injected Super+Digit."""
        wid = max(1, min(10, int(wid)))
        digit = "Digit0" if wid == 10 else f"Digit{wid}"
        await self._inject_key(digit, {"meta": True})
        await asyncio.sleep(0.25)
        self._push_wslist(client)

    async def _ws_step(self, client: Client, delta: int) -> None:
        # next = Super+Tab ; prev = Super+Shift+Tab (Omarchy default binds)
        if delta > 0:
            await self._inject_key("Tab", {"meta": True})
        else:
            await self._inject_key("Tab", {"meta": True, "shift": True})
        await asyncio.sleep(0.2)
        self._push_wslist(client)

    async def _ws_former(self, client: Client) -> None:
        await self._inject_key("Tab", {"meta": True, "ctrl": True})
        await asyncio.sleep(0.2)
        self._push_wslist(client)

    def _ws_snapshot(self) -> dict:
        try:
            out = self._hypr("workspaces", "-j")
            lst = json.loads(out or "[]")
            cur = self._hypr("activeworkspace", "-j")
            curj = json.loads(cur or "{}")
            items = []
            for w in lst:
                items.append({
                    "id": w.get("id"),
                    "name": w.get("name") or str(w.get("id")),
                    "monitor": w.get("monitor"),
                    "windows": w.get("windows"),
                })
            items.sort(key=lambda w: (w["monitor"] or 0, w["id"] or 0))
            return {"list": items, "current": curj.get("id")}
        except Exception:
            log.exception("ws snapshot failed")
            return {"list": [], "current": None}

    def _push_wslist(self, client: Client) -> None:
        self._send(client, {"t": "wslist", **self._ws_snapshot()})

    def _win_snapshot(self) -> list[dict]:
        try:
            out = self._hypr("clients", "-j")
            lst = json.loads(out or "[]")
            wins = []
            for w in lst:
                if not w.get("mapped", True):
                    continue
                wins.append({
                    "addr": w.get("address"),
                    "title": (w.get("title") or "")[:64] or w.get("initialTitle") or "",
                    "class": w.get("initialClass") or w.get("class") or "",
                    "ws": w.get("workspace", {}).get("id"),
                    "at": w.get("at"),
                    "size": w.get("size"),
                })
            return wins
        except Exception:
            log.exception("win snapshot failed")
            return []

    async def _push_winlist(self, client: Client) -> None:
        try:
            cur = self._hypr("activeworkspace", "-j")
            curj = json.loads(cur or "{}")
        except Exception:
            curj = {}
        self._send(client, {"t": "winlist", "list": self._win_snapshot(),
                            "current": curj.get("id")})

    async def _focus_window(self, client: Client, addr: str) -> None:
        """Same-workspace => click its center. Other workspace => switch
        (workspace 1..10 via injected keys) then click."""
        import json as _json

        out = self._hypr("clients", "-j")
        target = None
        for w in _json.loads(out or "[]"):
            if w.get("address") == addr and w.get("mapped", True):
                target = w
                break
        if not target:
            self._send(client, {"t": "error", "msg": "window vanished"})
            return
        ws = (target.get("workspace") or {}).get("id")
        curj = _json.loads(self._hypr("activeworkspace", "-j") or "{}")
        if ws and curj.get("id") != ws:
            if not (1 <= int(ws) <= 10):
                self._send(client, {"t": "error",
                                    "msg": f"window is on workspace {ws} — switch there first"})
                return
            await self._ws_go(client, int(ws))
        # compute layout-relative normalized coords for the virtual pointer
        outs = self.engine.outputs()
        if not outs:
            return
        ox = min(o.x for o in outs)
        oy = min(o.y for o in outs)
        x1 = max(o.x + o.width for o in outs)
        y1 = max(o.y + o.height for o in outs)
        W, H = x1 - ox, y1 - oy
        at = target.get("at") or [0, 0]
        size = target.get("size") or [W, H]
        cx = at[0] + size[0] / 2
        cy = at[1] + size[1] / 2
        nx = (cx - ox) / max(1, W)
        ny = (cy - oy) / max(1, H)
        self.vi.pointer_move(nx, ny)
        await asyncio.sleep(0.15)
        self.vi.pointer_button("left", True)
        await asyncio.sleep(0.05)
        self.vi.pointer_button("left", False)
        await asyncio.sleep(0.25)
        await self._push_winlist(client)

    def _send(self, client: Client, obj: dict) -> None:
        with contextlib.suppress(Exception):
            if client.channel and client.channel.readyState == "open":
                client.channel.send(json.dumps(obj))

    def _send_clipboard(self, client: Client) -> None:
        if self._last_clip is not None:
            self._send(client, {"t": "clip", "s": self._last_clip})

    def _set_clipboard(self, client, txt: str) -> None:
        async def _do():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "wl-copy", stdin=asyncio.subprocess.PIPE)
                await proc.communicate(txt.encode())
                self._last_clip = txt
                # echo to other clients
                for other in self.clients.values():
                    if other is not client:
                        self._send(other, {"t": "clip", "s": txt})
            except Exception:
                log.exception("wl-copy failed")
        asyncio.ensure_future(_do())

    async def _apply_quality(self, client: Client, mbps: int) -> None:
        """Adjust the video send bitrate dynamically via RTP sender params."""
        with contextlib.suppress(Exception):
            sender = client.pc.getSenders()[0]
            params = sender.getParameters()
            enc = (params.get("encodings") or [{}])[0]
            enc["maxBitrate"] = int(mbps * 1_000_000)
            await sender.setParameters(params)

    # ------------------ clipboard watcher (server -> phone) --------------
    async def _clipboard_loop(self) -> None:
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "wl-paste", "-n", stdout=asyncio.subprocess.PIPE)
                out, _ = await proc.communicate()
                cur = out.decode(errors="replace")
                if cur != self._last_clip:
                    self._last_clip = cur
                    for c in list(self.clients.values()):
                        self._send(c, {"t": "clip", "s": cur})
            except FileNotFoundError:
                await asyncio.sleep(5)
                continue
            except Exception:
                log.exception("clipboard watch failed")
            await asyncio.sleep(1.0)

    # ------------------ lifecycle ------------------
    async def start_async(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.engine.on_frame = self._on_engine_frame
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.bind, self.port)
        await self._site.start()
        self._clip_task = asyncio.ensure_future(self._clipboard_loop())

    def _on_engine_frame(self, arr, out) -> None:
        self.hub.push_threadsafe(self.loop, arr, out.name)

    async def run_forever(self) -> None:
        await self.start_async()
        log.info("omadesk server listening on %s:%d", self.bind, self.port)
        while True:
            await asyncio.sleep(3600)
