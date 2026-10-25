#!/usr/bin/env python3
"""PoC: capture one frame via ext-image-copy-capture-v1 (thegrim protocol)."""
from __future__ import annotations

import mmap
import os
import shutil
import struct
import sys
import time
import zlib

import select

from pywayland.client import Display
from pywayland.protocol.wayland import WlOutput, WlShm
from pywayland.protocol.ext_image_copy_capture_v1 import (
    ExtImageCopyCaptureManagerV1,
)
from pywayland.protocol.ext_image_capture_source_v1 import (
    ExtOutputImageCaptureSourceManagerV1,
)

FORMAT_XRGB8888 = 0


def write_png(path, w, h, data, stride):
    rows = []
    for y in range(h):
        row = bytearray(data[y * stride : (y + 1) * stride])
        for x in range(w):
            i = x * 4
            row[i], row[i + 2] = row[i + 2], row[i]
        rows.append(b"\x00" + bytes(row[: w * 3]))
    raw = b"".join(rows)
    idat = zlib.compress(raw, 1)
    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(
            ">I", zlib.crc32(tag + payload) & 0xFFFFFFFF
        )
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def main() -> None:
    expr = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
    display = Display(expr)
    display.connect()
    print("connected", flush=True)

    state = {"outs": {}, "shm": None, "cap": None, "src": None, "formats": set()}

    def on_global(registry, idn, ifc, ver):
        if ifc == "wl_output":
            out = registry.bind(idn, WlOutput, min(ver, 4))
            info = {"proxy": out, "name": f"out{idn}", "mode": None}
            state["outs"][idn] = info
            out.dispatcher["name"] = lambda proxy, nm: info.__setitem__("name", nm)
            out.dispatcher["mode"] = lambda proxy, *a: info.__setitem__("mode", (a[1], a[2]))
        elif ifc == "wl_shm":
            state["shm"] = registry.bind(idn, WlShm, min(ver, 1))
        elif ifc == "ext_image_copy_capture_manager_v1":
            state["cap"] = registry.bind(idn, ExtImageCopyCaptureManagerV1, min(ver, 1))
        elif ifc == "ext_output_image_capture_source_manager_v1":
            state["src"] = registry.bind(idn, ExtOutputImageCaptureSourceManagerV1, min(ver, 1))

    registry = display.get_registry()
    registry.dispatcher["global"] = on_global
    registry.dispatcher["global_remove"] = lambda *a: None
    display.roundtrip()
    display.roundtrip()

    outs = {info["name"]: info for info in state["outs"].values()}
    print("outputs:", outs, flush=True)
    assert state["cap"] and state["src"], "missing ext capture globals"

    results = []
    for name, info in outs.items():
        w, h = info["mode"]
        size = w * 4 * h
        fd = os.memfd_create("omadesk-ext-poc")
        os.ftruncate(fd, size)

        source = state["src"].create_source(info["proxy"])
        display.flush()

        done = {"ready": False, "stopped": False}

        def sess_buffer_size(sess, sw, sh):
            print(f"  session buffer_size {sw}x{sh}", flush=True)
            nonlocal size, w, h
            # size only settles here
            w, h, size = sw, sh, sw * 4 * sh
            os.ftruncate(fd, size)

        def sess_shm_format(sess, fmt):
            state["formats"].add(fmt)

        def sess_stopped(sess):
            done["stopped"] = True

        sess = state["cap"].create_session(source, 0)
        display.flush()
        sess.dispatcher["buffer_size"] = sess_buffer_size
        sess.dispatcher["shm_format"] = sess_shm_format
        sess.dispatcher["stopped"] = sess_stopped
        display.flush()

        # wait for session done event
        t0 = time.time()
        while time.time() - t0 < 3:
            r, _, _ = select.select([display.get_fd()], [], [], 0.1)
            if r:
                display.read()
                display.dispatch(block=False)
        pool = state["shm"].create_pool(fd, size)
        display.flush()
        buf = pool.create_buffer(0, w, h, w * 4, FORMAT_XRGB8888)
        display.flush()

        frame = sess.create_frame()
        display.flush()
        captured = {"ok": False}

        def fr_transform(fr, tr):
            print("  transform:", tr, flush=True)

        def fr_damage(fr, x, y, dw, dh):
            pass

        def fr_ready(fr):
            captured["ok"] = True

        def fr_failed(fr, reason):
            print("  frame failed:", reason, flush=True)

        def fr_attach_ok(fr, buffer):
            pass

        frame.dispatcher["transform"] = fr_transform
        frame.dispatcher["damage"] = fr_damage
        frame.dispatcher["ready"] = fr_ready
        frame.dispatcher["failed"] = fr_failed
        frame.attach_buffer(buf)
        display.flush()
        frame.capture()
        display.flush()

        t0 = time.time()
        while not captured["ok"] and time.time() - t0 < 5:
            r, _, _ = select.select([display.get_fd()], [], [], 0.1)
            if r:
                display.read()
                display.dispatch(block=False)
        print(f"  capture ok={captured['ok']} after {time.time()-t0:.2f}s", flush=True)
        if captured["ok"]:
            with mmap.mmap(fd, size, access=mmap.ACCESS_READ) as mm:
                data = mm[:]
            open(f"/tmp/opencode/raw_{name}.bin", "wb").write(data)
            path = f"/tmp/opencode/ext_{name}.png"
            write_png(path, w, h, data, w * 4)
            results.append((name, (w, h), path))
        frame.destroy()
        sess.destroy()
        source.destroy()
        os.close(fd)

    print("captured:", results, flush=True)
    display.disconnect()


if __name__ == "__main__":
    main()
