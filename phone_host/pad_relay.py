"""Laptop controller -> phone games. Reads an Xbox (XInput) controller connected to THIS laptop
(USB, Xbox Wireless adapter or Bluetooth) and streams it to phones over the /ws/pad WebSocket, so a
game running ON the phone can be played with the controller that stays paired to the laptop.

Message to phones (text, ~60 Hz while anything changes, else every 0.5 s as a heartbeat):
    "pad,<buttons>,<lx>,<ly>,<rx>,<ry>,<lt>,<rt>"
buttons = bitmask in GODOT JoyButton order (bit 0 = A, 1 = B, 2 = X, 3 = Y, 4 = Back, 5 = Guide,
6 = Start, 7 = L3, 8 = R3, 9 = LB, 10 = RB, 11..14 = d-pad up/down/left/right); axes -1..1 in Godot
convention (stick down = +Y), triggers 0..1.  "pad,none" when no controller is connected.
"""
from __future__ import annotations

import ctypes
import threading
import time

# XInput bit -> Godot JoyButton index
_XI_TO_GODOT = {
    0x1000: 0, 0x2000: 1, 0x4000: 2, 0x8000: 3,       # A B X Y
    0x0020: 4, 0x0010: 6,                             # Back, Start
    0x0040: 7, 0x0080: 8,                             # L3, R3
    0x0100: 9, 0x0200: 10,                            # LB, RB
    0x0001: 11, 0x0002: 12, 0x0004: 13, 0x0008: 14,   # d-pad up/down/left/right
}


class _Gamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _Gamepad)]


def _load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, name)
        except (OSError, AttributeError):
            continue
    return None


class PadRelay:
    def __init__(self) -> None:
        self.latest = "pad,none"
        self.version = 0              # bumps on every change
        self.connected_index: int | None = None
        self._xi = _load_xinput()
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self._xi is not None

    def _read(self) -> str:
        st = _State()
        indices = [self.connected_index] if self.connected_index is not None else []
        indices += [i for i in range(4) if i != self.connected_index]
        for i in indices:
            if self._xi.XInputGetState(i, ctypes.byref(st)) == 0:
                if self.connected_index != i:
                    self.connected_index = i
                    print(f"[pad] laptop controller found (XInput slot {i}); streaming it to phone games")
                g = st.Gamepad
                bits = 0
                for xi_bit, godot_index in _XI_TO_GODOT.items():
                    if g.wButtons & xi_bit:
                        bits |= 1 << godot_index
                f = lambda v: max(-1.0, min(1.0, v / 32767.0))
                return "pad,%d,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f" % (
                    bits, f(g.sThumbLX), -f(g.sThumbLY), f(g.sThumbRX), -f(g.sThumbRY),
                    g.bLeftTrigger / 255.0, g.bRightTrigger / 255.0)
        if self.connected_index is not None:
            print("[pad] laptop controller disconnected")
            self.connected_index = None
        return "pad,none"

    def run(self) -> None:
        """Poll at ~120 Hz in a thread (XInput calls are cheap)."""
        if not self._xi:
            print("[pad] XInput not available: no laptop controller relay")
            return
        idle = 0.0
        while True:
            msg = self._read()
            with self._lock:
                if msg != self.latest:
                    self.latest = msg
                    self.version += 1
            # an absent controller is re-checked slowly (XInputGetState on empty slots is slowish)
            if msg == "pad,none":
                idle += 1
                time.sleep(1.0 if idle > 3 else 0.25)
            else:
                idle = 0
                time.sleep(1 / 120)

    def snapshot(self) -> tuple[int, str]:
        with self._lock:
            return self.version, self.latest


relay = PadRelay()


def start() -> None:
    threading.Thread(target=relay.run, daemon=True, name="pad-relay").start()
