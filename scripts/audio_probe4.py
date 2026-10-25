import asyncio, json, os, sys, time
sys.path.insert(0, "/home/pginotfound/omadesk")
os.environ.setdefault("WAYLAND_DISPLAY", "wayland-1")
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("aiortc.rtcrtpsender").setLevel(logging.DEBUG)

async def m():
    from omadesk.server_cli import token_rotate
    token = token_rotate()
    from omadesk.capture import CaptureEngine
    from omadesk.input import VirtualInput
    from omadesk.server import DeskServer
    from omadesk.audio import AudioSource
    eng = CaptureEngine("wayland-1"); eng.start(); eng.set_active(False)
    vi = VirtualInput("wayland-1"); vi.start()
    audio = AudioSource()
    srv = DeskServer(token=token, port=9991, bind="127.0.0.1", engine=eng, vi=vi, audio=audio)
    asyncio.ensure_future(srv.run_forever())
    await asyncio.sleep(0.6)
    from aiortc import RTCPeerConnection, RTCSessionDescription
    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")
    ch = pc.createDataChannel("omadesk")
    openevt = asyncio.Event()
    @ch.on("open")
    async def _o(): openevt.set()

    audio_on = asyncio.Event()
    @pc.on("track")
    async def on_track(track):
        print("TRACK:", track.kind, flush=True)
        if track.kind == "audio":
            async def eat():
                while True:
                    try:
                        f = await asyncio.wait_for(track.recv(), timeout=2)
                        print("audio frame pts", f.pts, flush=True)
                    except Exception as e:
                        print("audio track recv err:", repr(e), flush=True)
                        return
            asyncio.ensure_future(eat())

    offer = await pc.createOffer(); await pc.setLocalDescription(offer)
    import aiohttp
    async with aiohttp.ClientSession() as h:
        async with h.post("http://127.0.0.1:9991/api/session",
            headers={"X-Auth": token, "Content-Type": "application/json"},
            json={"sdp": {"type": offer.type, "sdp": offer.sdp}}) as r:
            ans = await r.json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"]["sdp"], type="answer"))
    await asyncio.wait_for(openevt.wait(), 8)
    ch.send(json.dumps({"t": "audio", "on": True}))
    await asyncio.sleep(7)
    print("END", flush=True)
    os._exit(0)

asyncio.run(m())
