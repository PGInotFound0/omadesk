#!/usr/bin/env python3
"""Spy: an xdg toplevel that prints every wl_keyboard event it receives."""
from __future__ import annotations
import os, struct, time, zlib
import select, sys

from pywayland.client import Display
from pywayland.protocol.wayland import WlCompositor, WlSeat, WlShm, WlOutput
from pywayland.protocol.xdg_shell import XdgWmBase

# connection semantics: we make a tiny 64x48 RGB window

def make_shm_buffer(shm, w, h, bgr_data):
    stride = w * 4
    size = stride * h
    fd = os.memfd_create("spy")
    os.ftruncate(fd, size)
    os.write(fd, bgr_data)
    os.lseek(fd, 0, 0)
    pool = shm.create_pool(fd, size)
    buf = pool.create_buffer(0, w, h, stride, 0)  # ARGB8888
    return buf, fd


def main():
    d = Display("wayland-1")
    d.connect()
    state: dict = {}

    def on_global(reg, name, ifc, ver):
        if ifc == "wl_compositor" and "comp" not in state:
            state["comp"] = reg.bind(name, WlCompositor, min(ver, 6))
        elif ifc == "xdg_wm_base" and "wm" not in state:
            state["wm"] = reg.bind(name, XdgWmBase, min(ver, 6))
            state["wm"].dispatcher["ping"] = lambda p, serial: p.pong(serial)
        elif ifc == "wl_seat" and "seat" not in state:
            state["seat"] = reg.bind(name, WlSeat, min(ver, 9))
        elif ifc == "wl_shm" and "shm" not in state:
            state["shm"] = reg.bind(name, WlShm, min(ver, 1))

    reg = d.get_registry()
    reg.dispatcher["global"] = on_global
    reg.dispatcher["global_remove"] = lambda *a: None
    d.roundtrip()
    assert "comp" in state and "wm" in state and "seat" in state, state.keys()

    surf = state["comp"].create_surface()
    xdg = state["wm"].get_xdg_surface(surf)
    toplevel = xdg.get_toplevel()
    toplevel.set_title("omadesk-kb-spy")
    toplevel.set_app_id("omadesk-kb-spy")
    xdg.dispatcher["configure"] = lambda p, serial: (xdg.ack_configure(serial))

    kb = state["seat"].get_keyboard()
    kb.dispatcher["enter"] = lambda p, serial, *a: print("KB ENTER", flush=True)
    kb.dispatcher["leave"] = lambda p, *a: print("KB LEAVE", flush=True)
    kb.dispatcher["modifiers"] = lambda p, *a: print("KB MODS", a, flush=True)
    kb.dispatcher["key"] = lambda p, serial, time, key, state_: print(f"KB KEY serial={serial} key={key} state={state_}", flush=True)
    kb.dispatcher["keymap"] = lambda p, fmt, fd, size: (
        print("KB KEYMAP", fmt, "size", size, flush=True),
        _dump_keymap(fd, size),
    )

    def _dump_keymap(fd, size):
        import os as _os
        data = _os.pread(fd, min(size, 200000), 0)
        # show alias lines for unicode slots and the keysym names used
        tails = [ln for ln in data.decode(errors="replace").splitlines()
                 if "U+" not in ln and ("U0" in ln or "U1" in ln)]
        print("KB KEYMAP unicode aliases:", tails[-4:], flush=True)

    # 1x2 blue buffer
    w, h = 64, 48
    data = b"".join([bytes([0x8B, 0x2A, 0x1C, 0xFF])] * (w * h))
    buf, fd = make_shm_buffer(state["shm"], w, h, data)
    surf.attach(buf, 0, 0)
    surf.damage(0, 0, w, h)
    surf.commit()
    d.flush()

    rounds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    t0 = time.time()
    while time.time() - t0 < rounds:
        r, _, _ = select.select([d.get_fd()], [], [], 0.2)
        if r:
            d.read()
            d.dispatch(block=False)
    d.disconnect()


main()
