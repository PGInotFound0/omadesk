#!/usr/bin/env python3
"""PoC: damage-driven capture loop + virtual pointer/keyboard injection."""
from __future__ import annotations
import mmap, os, select, time
from pywayland.client import Display
from pywayland.protocol.wayland import WlOutput, WlShm, WlSeat
from pywayland.protocol.ext_image_copy_capture_v1 import ExtImageCopyCaptureManagerV1
from pywayland.protocol.ext_image_capture_source_v1 import ExtOutputImageCaptureSourceManagerV1

def main():
    d = Display("wayland-1"); d.connect()
    g = {}
    def on_global(reg, idn, ifc, ver):
        if ifc == "wl_output":
            out = reg.bind(idn, WlOutput, 4); g.setdefault("outs", []).append(out)
        elif ifc == "wl_shm": g["shm"] = reg.bind(idn, WlShm, 1)
        elif ifc == "ext_image_copy_capture_manager_v1": g["cap"] = reg.bind(idn, ExtImageCopyCaptureManagerV1, 1)
        elif ifc == "ext_output_image_capture_source_manager_v1": g["src"] = reg.bind(idn, ExtOutputImageCaptureSourceManagerV1, 1)
    r = d.get_registry(); r.dispatcher["global"] = on_global; r.dispatcher["global_remove"] = lambda *a: None
    d.roundtrip(); d.roundtrip()
    print("globals ok:", "cap" in g and "src" in g, flush=True)

    out = g["outs"][0]
    fd = os.memfd_create("omadesk")
    size = 1680 * 1050 * 4
    os.ftruncate(fd, size)

    src = g["src"].create_source(out); d.flush()
    sess = g["cap"].create_session(src, 0); d.flush()
    got = {"n": 0}
    sess.dispatcher["done"] = lambda *a: None
    d.roundtrip()
    pool = g["shm"].create_pool(fd, size); d.flush()
    buf = pool.create_buffer(0, 1680, 1050, 1680*4, 0); d.flush()

    # frame loop
    frames = {"n": 0, "last": time.time(), "deltas": []}
    frame_state = {"fr": None}

    def drain(tmo=0.05):
        rdy, _, _ = select.select([d.get_fd()], [], [], tmo)
        if rdy:
            d.read(); d.dispatch(block=False)

    def make_frame():
        fr = sess.create_frame(); d.flush()
        fr.dispatcher["ready"] = lambda p: (frames.__setitem__("n", frames["n"]+1),
                                            frames["deltas"].append(time.time()-frames["last"] if frames["n"] else 0),
                                            frames.__setitem__("last", time.time())) and None
        fr.dispatcher["failed"] = lambda p, reason: print("frame failed", reason, flush=True)
        fr.damage_buffer(0, 0, 2147483647, 2147483647)
        fr.attach_buffer(buf); d.flush()
        fr.capture(); d.flush()
        frame_state["fr"] = fr

    make_frame()
    t0 = time.time()
    while time.time() - t0 < 4.0:
        drain()
        if frames["n"] > frames.get("consumed", -1) and frame_state["fr"] is not None:
            fr = frame_state["fr"]; fr.destroy(); frame_state["fr"] = None
            frames["consumed"] = frames["n"]
            make_frame()
    ds = [x for x in frames["deltas"] if x > 0]
    if ds:
        print(f"damage-driven frames in 4s: {frames['n']}, avg delta {1000*sum(ds)/len(ds):.1f}ms"
              f" (~{1/ (sum(ds)/len(ds)):.1f} fps)", flush=True)
    else:
        print("no repeat frames (idle => no damage)", flush=True)

    d.disconnect()

main()
