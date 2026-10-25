"""Virtual input injection over Wayland (pointer + keyboard).

Uses zwp_virtual_keyboard_manager_v1 + zwlr_virtual_pointer_manager_v1,
both implemented by Hyprland. Keyboard text input follows the wtype
approach: a synthetic xkb keymap ("complete" types/compat, explicit key
aliases) that is rebuilt whenever a new character needs a key slot.

All Wayland I/O happens on a dedicated thread with its own display
connection so encoder/network code never blocks compositor traffic.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time

log = logging.getLogger("omadesk.input")

# wl_keyboard keymap format
FORMAT_XKB_V1 = 1

# evdev keycodes for browser KeyboardEvent.code values
BROWSER_CODE_TO_EVDEV = {
    "Escape": 1, "Digit1": 2, "Digit2": 3, "Digit3": 4, "Digit4": 5, "Digit5": 6,
    "Digit6": 7, "Digit7": 8, "Digit8": 9, "Digit9": 10, "Digit0": 11,
    "Minus": 12, "Equal": 13, "Backspace": 14, "Tab": 15,
    "KeyQ": 16, "KeyW": 17, "KeyE": 18, "KeyR": 19, "KeyT": 20, "KeyY": 21,
    "KeyU": 22, "KeyI": 23, "KeyO": 24, "KeyP": 25, "BracketLeft": 26,
    "BracketRight": 27, "Enter": 28, "ControlLeft": 29,
    "KeyA": 30, "KeyS": 31, "KeyD": 32, "KeyF": 33, "KeyG": 34, "KeyH": 35,
    "KeyJ": 36, "KeyK": 37, "KeyL": 38, "Semicolon": 39, "Quote": 40,
    "Backquote": 41, "ShiftLeft": 42, "Backslash": 43, "KeyZ": 44, "KeyX": 45,
    "KeyC": 46, "KeyV": 47, "KeyB": 48, "KeyN": 49, "KeyM": 50, "Comma": 51,
    "Period": 52, "Slash": 53, "ShiftRight": 54, "KeypadMultiply": 55,
    "AltLeft": 56, "Space": 57, "CapsLock": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65,
    "F8": 66, "F9": 67, "F10": 68, "NumLock": 69, "ScrollLock": 70,
    "Numpad7": 71, "Numpad8": 72, "Numpad9": 73, "NumpadSubtract": 74,
    "Numpad4": 75, "Numpad5": 76, "Numpad6": 77, "NumpadAdd": 78,
    "Numpad1": 79, "Numpad2": 80, "Numpad3": 81, "Numpad0": 82,
    "NumpadDecimal": 83, "F11": 87, "F12": 88, "NumpadEnter": 96,
    "ControlRight": 97, "NumpadDivide": 98, "AltRight": 100,
    "Home": 102, "ArrowUp": 103, "PageUp": 104, "ArrowLeft": 105,
    "ArrowRight": 106, "End": 107, "ArrowDown": 108, "PageDown": 109,
    "Insert": 110, "Delete": 119,
    "MetaLeft": 125, "MetaRight": 126,
}

# xkb keysym names for ASCII punctuation / special keys
_PUNCT = {
    16: ("q", "Q"), 17: ("w", "W"), 18: ("e", "E"), 19: ("r", "R"), 20: ("t", "T"),
    21: ("y", "Y"), 22: ("u", "U"), 23: ("i", "I"), 24: ("o", "O"), 25: ("p", "P"),
    26: ("bracketleft", "braceleft"), 27: ("bracketright", "braceright"),
    30: ("a", "A"), 31: ("s", "S"), 32: ("d", "D"), 33: ("f", "F"), 34: ("g", "G"),
    35: ("h", "H"), 36: ("j", "J"), 37: ("k", "K"), 38: ("l", "L"),
    44: ("z", "Z"), 45: ("x", "X"), 46: ("c", "C"), 47: ("v", "V"), 48: ("b", "B"),
    49: ("n", "N"), 50: ("m", "M"),
    12: ("minus", "underscore"), 13: ("equal", "plus"),
    39: ("semicolon", "colon"), 40: ("apostrophe", "quotedbl"),
    41: ("grave", "asciitilde"), 43: ("backslash", "bar"),
    51: ("comma", "less"), 52: ("period", "greater"), 53: ("slash", "question"),
    2: ("1", "exclam"), 3: ("2", "at"), 4: ("3", "numbersign"), 5: ("4", "dollar"),
    6: ("5", "percent"), 7: ("6", "asciicircum"), 8: ("7", "ampersand"),
    9: ("8", "asterisk"), 10: ("9", "parenleft"), 11: ("0", "parenright"),
    14: ("BackSpace",), 15: ("Tab", "ISO_Left_Tab"), 1: ("Escape",),
    28: ("Return",), 57: ("space",),
    102: ("Home",), 103: ("Up",), 104: ("Prior",), 105: ("Left",),
    106: ("Right",), 107: ("End",), 108: ("Down",), 109: ("Next",),
    110: ("Insert",), 119: ("Delete",),
    71: ("KP_7", "Home"), 72: ("KP_8", "Up"), 73: ("KP_9", "Prior"),
    74: ("KP_Subtract",), 75: ("KP_4", "Left"), 76: ("KP_5",),
    77: ("KP_6", "Right"), 78: ("KP_Add",), 79: ("KP_1", "End"),
    80: ("KP_2", "Down"), 81: ("KP_3", "Next"),
    82: ("KP_0",), 83: ("KP_Decimal",), 96: ("Return",),
    55: ("KP_Multiply",), 98: ("KP_Divide",),
    42: ("Shift_L",), 54: ("Shift_R",), 29: ("Control_L",), 97: ("Control_R",),
    56: ("Alt_L",), 100: ("Alt_R",), 125: ("Super_L",), 126: ("Super_R",),
    58: ("Caps_Lock",), 69: ("Num_Lock",),
}
for _c in range(59, 69):
    _PUNCT[_c] = (f"F{_c - 58}",)
_PUNCT[87] = ("F11",)
_PUNCT[88] = ("F12",)

# ASCII printable -> (evdev, shifted) typing path
CHAR_KEY: dict[str, tuple[int, int]] = {}
for _ev, _syms in _PUNCT.items():
    if len(_syms) == 2:
        a, b = _syms
        if len(a) == 1 and a.isalnum():
            CHAR_KEY[a] = (_ev, 0)
            CHAR_KEY[b] = (_ev, 1)

# punctuation / space / special chars: char -> (evdev, shift)
CHAR_KEY.update({
    "-": (12, 0), "_": (12, 1),
    "=": (13, 0), "+": (13, 1),
    "[": (26, 0), "{": (26, 1),
    "]": (27, 0), "}": (27, 1),
    ";": (39, 0), ":": (39, 1),
    "'": (40, 0), '"': (40, 1),
    "`": (41, 0), "~": (41, 1),
    "\\": (43, 0), "|": (43, 1),
    ",": (51, 0), "<": (51, 1),
    ".": (52, 0), ">": (52, 1),
    "/": (53, 0), "?": (53, 1),
    "!": (2, 1), "@": (3, 1), "#": (4, 1), "$": (5, 1),
    "%": (6, 1), "^": (7, 1), "&": (8, 1),
    "*": (9, 1), "(": (10, 1), ")": (11, 1),
    " ": (57, 0),
    "\t": (15, 0),
    "\n": (28, 0),
})

# xkb modifier bit masks (Shift, Lock, Control, Mod1=Alt, Mod2=Num, Mod5=AltGr)
MOD_SHIFT = 1
MOD_LOCK = 2
MOD_CTRL = 4
MOD_ALT = 8
MOD_NUM = 16
MOD_SUPER = 64
MOD_ALTGR = 128

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

# wheel axis enums (wl_pointer.axis)
AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1


class VirtualInput:
    """Owns a dedicated Wayland connection with virtual pointer + keyboard."""

    def __init__(self, display: str | None = None):
        self._display_name = display or os.environ.get("WAYLAND_DISPLAY", "wayland-1")
        self._q: "queue.Queue[dict | None]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        # layout extents (global coordinates of the output bounding box)
        self.layout_x = 0
        self.layout_y = 0
        self.layout_w = 1
        self.layout_h = 1
        self._keymap_lock = threading.Lock()
        self._char_slots: dict[str, int] = {}   # char -> xkb keycode

    # ---- thread plumbing -------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="omadesk-input", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=8)
        if self._error:
            raise RuntimeError(self._error)

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)

    def _run(self) -> None:  # runs on the dedicated thread
        try:
            from pywayland.client import Display
            from pywayland.protocol.wayland import WlOutput, WlSeat
            from omadesk.protocol_gen.wlr_virtual_pointer_unstable_v1 import (
                ZwlrVirtualPointerManagerV1,
            )
            from omadesk.protocol_gen.virtual_keyboard_unstable_v1 import (
                ZwpVirtualKeyboardManagerV1,
            )

            def on_global(reg, name, interface, version):
                if interface == "wl_seat":
                    g["seat"] = reg.bind(name, WlSeat, min(version, 9))
                elif interface == "zwlr_virtual_pointer_manager_v1":
                    g["vpm"] = reg.bind(name, ZwlrVirtualPointerManagerV1, min(version, 2))
                elif interface == "zwp_virtual_keyboard_manager_v1":
                    g["vkm"] = reg.bind(name, ZwpVirtualKeyboardManagerV1, 1)
                elif interface == "wl_output":
                    out = reg.bind(name, WlOutput, min(version, 4))
                    info = {"proxy": out, "x": 0, "y": 0, "w": 0, "h": 0}
                    g.setdefault("outs", []).append(info)

                    def geometry(proxy, x, y, *_a, info=info):
                        info["x"], info["y"] = x, y
                    def mode(proxy, flags, w, h, *_a, info=info):
                        info["w"], info["h"] = w, h

                    out.dispatcher["geometry"] = geometry
                    out.dispatcher["mode"] = mode

            d = Display(self._display_name)
            d.connect()
            self._display = d
            g: dict = {}

            reg = d.get_registry()
            reg.dispatcher["global"] = on_global
            reg.dispatcher["global_remove"] = lambda *a: None
            d.roundtrip()
            d.roundtrip()

            self._update_layout(g.get("outs", []))

            if "vpm" not in g:
                raise RuntimeError("zwlr_virtual_pointer_manager_v1 not available")
            if "vkm" not in g:
                raise RuntimeError("zwp_virtual_keyboard_manager_v1 not available")

            self.vp = g["vpm"].create_virtual_pointer(g.get("seat"))
            self.kb = g["vkm"].create_virtual_keyboard(g["seat"])
            d.flush()

            self._gen_keymap({})

            # hygiene: stale seat-level pressed keys from crashed virtual
            # keyboards make Hyprland silently drop presses of those codes.
            # Send a release sweep once to clear them (harmless if clean).
            for ev in range(1, 121):
                self.kb.key(0, ev, 0)
            self.kb.key(0, 125, 0)
            self.kb.key(0, 126, 0)
            d.flush()
            d.roundtrip()

            self._ready.set()
            log.info("virtual input ready (pointer+keyboard modules bound)")

            import select as _select
            fd = d.get_fd()
            while not self._stop.is_set():
                # service queued requests
                try:
                    item = self._q.get(timeout=0.15)
                except queue.Empty:
                    item = None
                if item is None:
                    if self._stop.is_set():
                        break
                    continue
                self._apply(item, d)
                # drain compositor events so we don't fill its send buffer
                rdy, _, _ = _select.select([fd], [], [], 0)
                if rdy:
                    d.read()
                    d.dispatch(block=False)
                d.flush()
            try:
                self.vp.destroy()
                self.kb.destroy()
                d.flush()
                d.disconnect()
            except Exception:
                pass
        except Exception as exc:  # surface to start()
            self._error = f"input thread failed: {exc}"
            log.exception("input thread failed")
            self._ready.set()

    def _update_layout(self, outs) -> None:
        if not outs:
            return
        x0 = min(o["x"] for o in outs)
        y0 = min(o["y"] for o in outs)
        x1 = max(o["x"] + o["w"] for o in outs if o["w"])
        y1 = max(o["y"] + o["h"] for o in outs if o["h"])
        if x1 > x0 and y1 > y0:
            self.layout_x, self.layout_y, self.layout_w, self.layout_h = x0, y0, x1 - x0, y1 - y0
            log.info("layout: %dx%d at %d,%d", self.layout_w, self.layout_h, self.layout_x, self.layout_y)

    def refresh_layout(self, outs) -> None:
        self._q.put({"op": "layout", "outs": outs})

    # ---- public API (thread-safe) ----------------------------------------
    def pointer_move(self, x: float, y: float) -> None:
        """x,y normalized [0,1] within the full layout."""
        self._q.put({"op": "move", "x": x, "y": y})

    def pointer_button(self, button: str, down: bool) -> None:
        code = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}[button]
        self._q.put({"op": "button", "button": code, "down": down})

    def pointer_scroll(self, dx: float, dy: float) -> None:
        self._q.put({"op": "scroll", "dx": dx, "dy": dy})

    def key(self, code: str, down: bool, mods: dict | None = None) -> None:
        """Send a physical key event by browser KeyboardEvent.code."""
        self._q.put({"op": "key", "code": code, "down": down, "mods": mods or {}})

    def combo_up(self, mods: dict | None = None) -> None:
        """Clear modifiers (touch modifier toggles)."""
        self._q.put({"op": "mods", "mods": mods or {}})

    def type_text(self, text: str) -> None:
        self._q.put({"op": "text", "text": text})

    # ---- request application --------------------------------------------
    def _apply(self, item: dict, d) -> None:
        t = int((time.time() * 1000) % 1000000)
        op = item["op"]
        vp, kb = self.vp, self.kb
        if op == "layout":
            self._update_layout(item["outs"])
        elif op == "move":
            gx = self.layout_x + item["x"] * self.layout_w
            gy = self.layout_y + item["y"] * self.layout_h
            vp.motion_absolute(t, int(gx), int(gy), self.layout_w, self.layout_h)
            vp.frame()
        elif op == "button":
            vp.button(t, item["button"], 1 if item["down"] else 0)
            vp.frame()
        elif op == "scroll":
            if item["dy"]:
                vp.axis(t, AXIS_VERTICAL, item["dy"])
            if item["dx"]:
                vp.axis(t, AXIS_HORIZONTAL, item["dx"])
            vp.frame()
        elif op == "mods":
            mask = self._mod_mask(item["mods"])
            kb.modifiers(mask, 0, 0, 0)
        elif op == "key":
            ev = BROWSER_CODE_TO_EVDEV.get(item["code"])
            if ev is None:
                log.debug("unmapped browser code %r", item["code"])
                return
            mask = self._mod_mask(item["mods"])
            down = item["down"]
            if down:
                kb.modifiers(mask, 0, 0, 0)
            kb.key(t, ev, 1 if down else 0)
            if not down:
                kb.modifiers(0, 0, 0, 0)
        elif op == "text":
            self._type_text(item["text"])

    @staticmethod
    def _mod_mask(mods: dict) -> int:
        mask = 0
        if mods.get("shift"):
            mask |= MOD_SHIFT
        if mods.get("ctrl"):
            mask |= MOD_CTRL
        if mods.get("alt"):
            mask |= MOD_ALT
        if mods.get("meta"):
            mask |= MOD_SUPER
        return mask

    # ---- keyboard internals ----------------------------------------------
    def _gen_keymap(self, extra: dict[str, int]) -> None:
        """Build + upload the synthetic keymap. extra: char -> evdev keycode.

        Aliases live at evdev+8 (the xkb keycode), matching the wire-space
        key() events (evdev codes).
        """
        lines = [
            "xkb_keymap {",
            'xkb_keycodes "(unnamed)" {', "minimum = 8;", "maximum = 255;",
        ]
        for ev, syms in _PUNCT.items():
            lines.append(f'\t<K{ev + 8:03d}> = {ev + 8};')
        for char, ev in extra.items():
            lines.append(f'\t<K{ev + 8:03d}> = {ev + 8};')
        lines.append("};")
        lines.append('xkb_types "(unnamed)" { include "complete" };')
        lines.append('xkb_compatibility "(unnamed)" { include "complete" };')
        lines.append('xkb_symbols "(unnamed)" {')
        for ev, syms in _PUNCT.items():
            if len(syms) == 1:
                lines.append(f'\tkey <K{ev + 8:03d}> {{[{syms[0]}]}};')
            else:
                lines.append(f'\tkey <K{ev + 8:03d}> {{[{syms[0]}, {syms[1]}]}};')
        for char, ev in extra.items():
            cp = ord(char)
            lines.append(f'\tkey <K{ev + 8:03d}> {{[U{cp:04X}]}};')
        lines.append("};")
        lines.append("};")
        text = ("\n".join(lines) + "\n").encode() + b"\x00"
        size = len(text)
        fd = os.memfd_create("omadesk-keymap")
        try:
            os.ftruncate(fd, size)
            os.write(fd, text)
            os.lseek(fd, 0, os.SEEK_SET)
            self.kb.keymap(FORMAT_XKB_V1, fd, size)
            self._display.flush()
        finally:
            os.close(fd)
        self._display.roundtrip()

    def _slot_for_char(self, ch: str) -> int:
        """Allocate an evdev keycode for an arbitrary char, rebuilding the keymap.

        Pool: evdev codes 127..181 (xkb keycodes 135..189 — the Kana/HP
        zone, unused by the static aliases and not in Hyprland press
        tracking) — never colliding with static aliases.
        """
        if ch in self._char_slots:
            return self._char_slots[ch]
        used = set(self._char_slots.values())
        ev = next((c for c in range(127, 182) if c not in used), None)
        if ev is None and self._char_slots:  # pool full: FIFO-evict one
            oldest = next(iter(self._char_slots))
            ev = self._char_slots.pop(oldest)
        if ev is None:
            raise RuntimeError("keymap slot pool exhausted")
        self._char_slots[ch] = ev
        self._gen_keymap(self._char_slots)
        return ev

    def _type_text(self, text: str) -> None:
        for ch in text:
            try:
                if ch in CHAR_KEY:
                    ev, shift = CHAR_KEY[ch]
                    if shift:
                        self.kb.modifiers(MOD_SHIFT, 0, 0, 0)
                    self.kb.key(0, ev, 1)
                    self.kb.key(0, ev, 0)
                    if shift:
                        self.kb.modifiers(0, 0, 0, 0)
                    self._display.flush()
                else:
                    kc = self._slot_for_char(ch)
                    self.kb.key(0, kc, 1)
                    self.kb.key(0, kc, 0)
                    self._display.flush()
                time.sleep(0.004)
            except Exception:
                log.exception("failed typing char %r", ch)
                break
