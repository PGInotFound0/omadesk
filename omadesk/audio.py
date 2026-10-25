"""Desktop audio capture (PulseAudio/PipeWire sink monitor) -> Opus -> browser.

Grabbed from the *default sink's* `.monitor` device via `parecord` writing raw
s16le stereo PCM; fed to per-peer aiortc audio tracks as 20 ms chunks.
Spotify/discord/youtube on the desktop arrive at the phone in sync with video.

No hardware exposure, no portal dialogs: monitor-of-sink is a read-only
"desktop audio" mix, same class of data as the screen itself.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from fractions import Fraction

log = logging.getLogger("omadesk.audio")

RATE = 48000
CHUNK_MS = 20
SAMPLES = RATE * CHUNK_MS // 1000          # 960
FRAME_BYTES = SAMPLES * 2 * 2              # s16 stereo = 3840
SILENCE = b"\x00" * FRAME_BYTES
TIME_BASE = Fraction(1, RATE)


def _default_sink() -> str | None:
    try:
        out = subprocess.run(["pactl", "get-default-sink"],
                             capture_output=True, text=True, timeout=4)
        name = out.stdout.strip()
        return name or None
    except Exception:
        return None


class AudioSource:
    """Spawns `parecord` against the default sink monitor and splits the raw
    PCM into clean 20 ms frames, fanned out to subscriber queues."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self._tail = bytearray()
        self._subscribers: dict[int, asyncio.Queue] = {}
        self.enabled_for: set[int] = set()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._warned = False

    # ---------- lifecycle ----------
    async def ensure_running(self) -> bool:
        if self._task and not self._task.done():
            return self.proc is not None
        self._stopping.clear()
        self._task = asyncio.ensure_future(self._run())
        # give the subprocess a moment; report by probing output
        for _ in range(10):
            await asyncio.sleep(0.2)
            if self.proc and self.proc.returncode is None:
                return True
            if self._task.done():
                return False
        return self.proc is not None

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass

    def subscribe(self, client_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers[client_id] = q
        self.enabled_for.add(client_id)
        return q

    def unsubscribe(self, client_id: int) -> None:
        self._subscribers.pop(client_id, None)
        self.enabled_for.discard(client_id)

    def set_enabled(self, client_id: int, on: bool) -> None:
        if on:
            self.enabled_for.add(client_id)
        else:
            self.enabled_for.discard(client_id)

    def want_output(self) -> bool:
        return bool(self.enabled_for)

    # ---------- reader ----------
    async def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                if not self.want_output():
                    await asyncio.sleep(0.25)
                    continue
                sink = _default_sink()
                if sink is None:
                    if not self._warned:
                        self._warned = True
                        log.warning("audio: no default sink (pactl missing?)")
                    await asyncio.sleep(2.0)
                    continue
                dev = sink + ".monitor"
                cmd = ["pw-record",
                       "--format=s16",
                       "--rate", str(RATE),
                       "--channels", "2",
                       "--target", dev, "-"]
                try:
                    self.proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL)
                except FileNotFoundError:
                    if not self._warned:
                        self._warned = True
                        log.warning("audio: pw-record not available")
                    await asyncio.sleep(4.0)
                    continue
                log.info("audio: capturing %s", dev)
                assert self.proc.stdout is not None
                while not self._stopping.is_set() and self.want_output():
                    try:
                        buf = await self.proc.stdout.read(8192)
                    except Exception:
                        break
                    if not buf:
                        break
                    if not self._subscribers:
                        continue
                    data = bytes(self._tail) + buf if self._tail else buf
                    self._tail.clear()
                    whole = len(data) // FRAME_BYTES * FRAME_BYTES
                    if whole != len(data):
                        self._tail += data[whole:]
                    for off in range(0, whole, FRAME_BYTES):
                        chunk = data[off:off + FRAME_BYTES]
                        for cid, q in list(self._subscribers.items()):
                            if cid not in self.enabled_for:
                                continue
                            if q.qsize() >= q.maxsize:
                                try:
                                    q.get_nowait()   # drop oldest
                                except Exception:
                                    pass
                            try:
                                q.put_nowait(chunk)
                            except Exception:
                                self.unsubscribe(cid)
                # process exited — respawn after a beat
                try:
                    if self.proc and self.proc.returncode is None:
                        self.proc.terminate()
                except Exception:
                    pass
                self.proc = None
                if not self._stopping.is_set():
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("audio reader crashed")


class AudioFeed:
    """Per-peer aiortc audio track pulling 20 ms frames from AudioSource."""

    def __init__(self, source: AudioSource):
        from aiortc.mediastreams import MediaStreamTrack

        self.src = source
        self._sub_id: int | None = None
        self._queue: asyncio.Queue | None = None
        self._calls = 0
        self._pts = 0
        ob = self

        class _Track(MediaStreamTrack):  # type: ignore[valid-type,misc]
            kind = "audio"

            async def recv(self):  # type: ignore[override]
                import av

                if ob._queue is None:
                    await asyncio.sleep(0.05)
                    return await self.recv()
                if ob._calls < 5 or ob._calls % 50 == 0:
                    log.debug("audio recv call %d qsize %d enabled %s",
                              ob._calls, ob._queue.qsize(), ob.src.want_output())
                ob._calls += 1
                try:
                    chunk = await asyncio.wait_for(ob._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    chunk = SILENCE
                frame = av.AudioFrame(format="s16", layout="stereo",
                                      samples=SAMPLES)
                frame.sample_rate = RATE
                frame.pts = ob._pts
                frame.time_base = TIME_BASE
                ob._pts += SAMPLES
                frame.planes[0].update(chunk)
                return frame

        self._Track = _Track

    def subscribe(self, client_id: int) -> None:
        self._sub_id = client_id
        self._queue = self.src.subscribe(client_id)

    def unsubscribe(self) -> None:
        if self._sub_id is not None:
            self.src.unsubscribe(self._sub_id)
        self._sub_id = None
        self._queue = None

    def set_enabled(self, on: bool) -> None:
        if self._sub_id is not None:
            self.src.set_enabled(self._sub_id, on)

    def as_track(self):
        return self._Track()
