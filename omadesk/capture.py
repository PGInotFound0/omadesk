"""Screen capture over Wayland (ext-image-copy-capture-v1).

Runs a dedicated thread that owns its own Wayland connection. When a target
output is set and at least one subscriber exists, it captures frames
(push-based: the compositor only wakes us when pixels change) and hands the
latest BGRA numpy frame to an asyncio callback via the engine thread.

Notes validated on Hyprland 0.56 (scripts/poc_*.py):
- session events must be drained with select + display.read() + dispatch
- shm format 0 (ARGB8888) works; pixel bytes are B,G,R,A
- damage_buffer(0,0,INT_MAX,INT_MAX) before capture() forces full fetch
- compositor may stop a session (output reconfigure); rebuild transparently
- options=1 paints the host cursor into frames.
"""
from __future__ import annotations

import logging
import os
import select
import threading
import time

import numpy as np

log = logging.getLogger("omadesk.capture")

FORMAT_ARGB8888 = 0


class Output:
    def __init__(self, name: str, width: int, height: int, x: int, y: int):
        self.name = name
        self.width = width
        self.height = height
        self.x = x
        self.y = y

    def as_dict(self):
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "x": self.x,
            "y": self.y,
        }


class CaptureEngine:
    """Wayland capture worker.

    on_frame(np_bgra_uint8, output: Output)   — called on engine thread
    on_outputs(list[Output])                  — called on engine thread
    """

    def __init__(self, display: str | None = None):
        self._display_name = display or os.environ.get("WAYLAND_DISPLAY", "wayland-1")
        self.on_frame = None
        self.on_outputs = None
        self._active = False
        self._want_output: str | None = None
        self._current: Output | None = None
        self._err: str | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="omadesk-capture", daemon=True)
        self._thread.start()

    def start(self) -> None:
        if not self._ready.wait(timeout=12):
            raise RuntimeError("capture engine did not start: " + (self._err or "timeout"))
        if self._err:
            raise RuntimeError("capture engine failed: " + self._err)
        log.info("capture engine ready (%d outputs)", len(self._outputs or []))

    def stop(self) -> None:
        self._stop.set()

    def set_target(self, output_name: str | None) -> None:
        self._want_output = output_name

    def set_active(self, active: bool) -> None:
        self._active = active

    def current_output(self) -> Output | None:
        return self._current

    def outputs(self) -> list[Output]:
        return list(self._outputs or [])

    _outputs: list[Output] | None = None

    def _run(self) -> None:
        try:
            from pywayland.client import Display
            from pywayland.protocol.wayland import WlOutput, WlShm
            from pywayland.protocol.ext_image_copy_capture_v1 import (
                ExtImageCopyCaptureManagerV1,
            )
            from pywayland.protocol.ext_image_capture_source_v1 import (
                ExtOutputImageCaptureSourceManagerV1,
            )

            d = Display(self._display_name)
            d.connect()
            g: dict = {}
            outs: dict[int, dict] = {}

            def on_global(reg, name, ifc, ver):
                if ifc == "wl_output":
                    out = reg.bind(name, WlOutput, min(ver, 4))
                    info = {"proxy": out, "name": None, "x": 0, "y": 0, "w": 0, "h": 0}
                    outs[name] = info

                    def on_name(proxy, nm, info=info):
                        info["name"] = nm

                    def on_geometry(proxy, x, y, *_a, info=info):
                        info["x"], info["y"] = x, y

                    def on_mode(proxy, flags, w, h, *_a, info=info):
                        info["w"], info["h"] = w, h

                    out.dispatcher["name"] = on_name
                    out.dispatcher["geometry"] = on_geometry
                    out.dispatcher["mode"] = on_mode
                elif ifc == "wl_shm":
                    g["shm"] = reg.bind(name, WlShm, min(ver, 1))
                elif ifc == "ext_image_copy_capture_manager_v1":
                    g["cap"] = reg.bind(name, ExtImageCopyCaptureManagerV1, min(ver, 1))
                elif ifc == "ext_output_image_capture_source_manager_v1":
                    g["src"] = reg.bind(name, ExtOutputImageCaptureSourceManagerV1, min(ver, 1))

            reg = d.get_registry()
            reg.dispatcher["global"] = on_global
            reg.dispatcher["global_remove"] = lambda *a: None
            d.roundtrip()
            d.roundtrip()
            for missing in ("cap", "src", "shm"):
                if missing not in g:
                    self._err = f"required wayland global missing: {missing}"
                    self._ready.set()
                    return

            self._outputs = self._collect(outs)
            if self.on_outputs:
                try:
                    self.on_outputs(self._outputs)
                except Exception:
                    log.exception("on_outputs callback failed")
            self._ready.set()

            memfd = os.memfd_create("omadesk-frame")
            pool = None
            buf = None
            session = None
            source = None
            stopped = False
            frame_ok = False
            frame_obj = None
            sess_ready = False
            cur_size = (0, 0)

            def drain(timeout=0.05):
                r, _, _ = select.select([d.get_fd()], [], [], timeout)
                if r:
                    d.read()
                    d.dispatch(block=False)

            def destroy_all():
                nonlocal session, source, buf, pool, stopped, frame_ok, sess_ready
                for obj in (frame_obj, session, source, buf):
                    if obj is not None:
                        try:
                            obj.destroy()
                        except Exception:
                            pass
                session = source = buf = pool = None
                stopped = False
                frame_ok = False
                sess_ready = False

            def valid_buf():
                return pool is not None and cur_size[0] > 0

            def rebuild():
                nonlocal session, source, buf, pool, stopped
                tgt = self.pick_target()
                if tgt is None:
                    return False
                destroy_all()
                for info in outs.values():
                    if info["name"] == tgt.name:
                        source = g["src"].create_source(info["proxy"])
                        break
                if source is None:
                    return False
                d.flush()
                session = g["cap"].create_session(source, 0)  # no cursor painting
                d.flush()

                def on_buf_size(_sess, w, h):
                    nonlocal buf, pool, cur_size, stopped, sess_ready
                    sess_ready = True
                    if (w, h) != cur_size:
                        if buf is not None:
                            buf.destroy()
                        if pool is not None:
                            pool.destroy()
                        cur_size = (w, h)
                        os.ftruncate(memfd, w * h * 4)
                        pool = g["shm"].create_pool(memfd, w * h * 4)
                        buf = pool.create_buffer(0, w, h, w * 4, FORMAT_ARGB8888)
                        d.flush()
                        log.info("capture buffer %dx%d", w, h)
                    if buf is not None:
                        stopped = False

                def on_stopped(_sess):
                    nonlocal stopped
                    stopped = True

                session.dispatcher["buffer_size"] = on_buf_size
                session.dispatcher["stopped"] = on_stopped
                self._current = tgt
                return True

            def make_frame_dbg():
                log.info("make_frame called")

            def set_frame_ok(v):
                nonlocal frame_ok
                frame_ok = v

            def kill_frame():
                nonlocal frame_obj
                if frame_obj is not None:
                    try:
                        frame_obj.destroy()
                    except Exception:
                        pass
                    frame_obj = None

            def make_frame():
                nonlocal frame_obj
                if session is None or buf is None:
                    return
                kill_frame()
                fr = session.create_frame()
                fr.dispatcher["ready"] = lambda *a: set_frame_ok(True)
                fr.dispatcher["failed"] = lambda *a: set_frame_ok(False)
                fr.damage_buffer(0, 0, 0x7FFFFFFF, 0x7FFFFFFF)
                fr.attach_buffer(buf)
                d.flush()
                fr.capture()
                d.flush()
                frame_obj = fr

            def publish():
                w, h = cur_size
                if not (self._active and self.on_frame and w and h):
                    return
                try:
                    n = w * h * 4
                    data = os.pread(memfd, n, 0)   # kernel copies; no tricky lifetimes
                    if len(data) < n:
                        log.warning("short frame read: %d/%d", len(data), n)
                        return
                    arr = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
                    self.on_frame(arr, self._current)
                except Exception:
                    log.exception("frame publish failed")

            while not self._stop.is_set():
                drain()
                self._outputs = self._collect(outs)
                auto = self.pick_target()
                log.debug("loop: sess=%s ready=%s frame_obj=%s cur=%s", session is not None, sess_ready, frame_obj is not None, self._current and self._current.name)
                need = (
                    session is None
                    or stopped
                    or self._current is None
                    or (self._want_output is not None
                        and self._current is not None
                        and self._current.name != self._want_output)
                    or (self._want_output is None and auto is not None
                        and self._current is not None
                        and auto.name != self._current.name)
                )
                if need:
                    if session is not None and not stopped:
                        destroy_all()
                    if not rebuild():
                        time.sleep(0.4)
                        continue
                    if valid_buf():
                        make_frame()
                if stopped:
                    destroy_all()
                    continue
                if sess_ready and frame_obj is None:
                    log.debug('dispatching make_frame; sess_ready=True')
                    make_frame()
                if frame_ok and frame_obj is not None:
                    set_frame_ok(False)
                    publish()
                    kill_frame()
                    make_frame()

            destroy_all()
            d.flush()
            d.disconnect()
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "dispatch" in str(exc):
                log.info("capture display closed")
            else:
                self._err = str(exc)
                log.exception("capture thread failed")

    def _collect(self, outs: dict) -> list[Output]:
        lst = []
        for info in outs.values():
            if info["name"] and info["w"] and info["h"]:
                lst.append(Output(info["name"], info["w"], info["h"], info["x"], info["y"]))
        return lst

    def pick_target(self) -> Output | None:
        lst = self._outputs or []
        if not lst:
            return None
        want = self._want_output
        if want:
            for o in lst:
                if o.name == want:
                    return o
        return max(lst, key=lambda o: o.width * o.height)
