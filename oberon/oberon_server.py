#!/usr/bin/env python3
"""
oberon_server.py — single-Pi Oberon Remote server for the Saitek X52.

Reads the X52 via evdev and serves controller state on port 26401 in the
Oberon Remote protocol. The official Oberon Remote app on the Xbox connects
here and injects the input as a virtual controller. No USB proxy, no second
board, no auth handshake.

Install Oberon Remote on your Xbox first:
  Microsoft Store -> search "Oberon Remote Input" (developer: SamsidParty)
  Direct link: https://apps.microsoft.com/detail/9pk5stjzff3s

GAME LAYOUTS
  Mappings live as JSON files in the layouts/ directory (with inheritance —
  see layouts.py). The layout_switch_button (default: T2 rocker) cycles
  through them live; the active game shows on the X52 Pro's throttle screen
  and in the web UI. The built-in web app (default port 8088) lets you view
  status and create/edit layouts from any browser on the LAN.

  Legacy mode: pass --config <file> to run one fixed config the old way
  (no layout switching), e.g. a calibrated sender_config.json.

Protocol (reverse-engineered from OberonRemote open-source):
  On connect  server -> client: [0x0A] + hostname bytes   (handshake)
  Poll        client -> server: [0xFA] (optionally + 16 rumble bytes, ignored)
  Response    server -> client: 100-byte controller state buffer

Buffer layout (4 controller slots x 25 bytes each):
  Slot 0 (our X52), slots 1-3 zeroed (no controllers):
    byte  0:    0xFF = connected
    bytes 1-2:  LX  int16 LE  floor(lx  *  32767)
    bytes 3-4:  LY  int16 LE  floor(ly  * -32767)   <- Oberon inverts Y
    bytes 5-6:  RX  int16 LE  floor(rx  *  32767)
    bytes 7-8:  RY  int16 LE  floor(ry  * -32767)   <- Oberon inverts Y
    bytes 9-10: LT  uint16 LE floor((lt+1)/2 * 32767)
    bytes 11-12:RT  uint16 LE floor((rt+1)/2 * 32767)
    byte 13:    button group 1: A B X Y LB RB Menu View   (bit7..bit0)
    byte 14:    button group 2: - LS RS Up Dn Lt Rt Guide  (bit7..bit0)
    bytes 15-24: zero

Usage:
    sudo python3 oberon_server.py                    # layouts mode (default)
    sudo python3 oberon_server.py --layout ac7       # start on a given layout
    sudo python3 oberon_server.py --config /path/to/sender_config.json
    sudo python3 oberon_server.py --list             # show input devices
    sudo python3 oberon_server.py --probe            # live event names
    sudo python3 oberon_server.py --verbose          # print state on each poll
"""

import argparse
import asyncio
import json
import math
import os
import socket
import sys
import threading
import time

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    sys.exit("python3-evdev missing.  Run: sudo apt install python3-evdev")

try:
    import websockets
    try:
        from websockets.asyncio.server import serve as ws_serve   # websockets >= 13
    except ImportError:
        from websockets.server import serve as ws_serve           # older versions
except ImportError:
    sys.exit("websockets missing.  Run: pip3 install --break-system-packages websockets")

import layouts as layouts_mod

# Optional X52 Pro MFD status display (libx52). Silent no-op if not installed.
try:
    import mfd as mfd_mod
except ImportError:
    mfd_mod = None

PORT          = 26401
WEB_PORT      = 8088
AXIS_TARGETS  = ("lx", "ly", "rx", "ry", "lt", "rt")
SPLIT_TARGET  = "split_lt_rt"

def neutral_axes():
    """Resting values: sticks centre at 0.0, TRIGGERS release at -1.0.
    A trigger at 0.0 encodes to a HALF-press (16383), which fires/scrolls —
    so triggers must rest at -1.0 (encodes to 0)."""
    return {t: (-1.0 if t in ("lt", "rt") else 0.0) for t in AXIS_TARGETS}

# ─── Oberon button bit positions ──────────────────────────────────────────────
# (group, mask)  group 1 = byte 13, group 2 = byte 14 of the slot
OBERON_BTNS = {
    "a":          (1, 0x80),
    "b":          (1, 0x40),
    "x":          (1, 0x20),
    "y":          (1, 0x10),
    "lb":         (1, 0x08),
    "rb":         (1, 0x04),
    "menu":       (1, 0x02),
    "view":       (1, 0x01),
    "ls":         (2, 0x40),
    "rs":         (2, 0x20),
    "dpad_up":    (2, 0x10),
    "dpad_down":  (2, 0x08),
    "dpad_left":  (2, 0x04),
    "dpad_right": (2, 0x02),
}

# Digital buttons that drive a trigger to full deflection
TRIGGER_BTNS = {"lt_button": "lt", "rt_button": "rt"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def code_name(etype, code):
    name = ecodes.bytype.get(etype, {}).get(code)
    if isinstance(name, list):
        name = name[0]
    return name or f"{etype}:{code}"


def find_device(match):
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if match.lower() in dev.name.lower() and ecodes.EV_ABS in dev.capabilities():
            return dev
        dev.close()
    return None


def shape(raw, absinfo, cfg):
    """Normalise a raw evdev value to -1..1 with deadzone + expo."""
    lo, hi = absinfo.min, absinfo.max
    v = (raw - lo) / ((hi - lo) or 1) * 2.0 - 1.0
    if cfg.get("invert"):
        v = -v
    dz = cfg.get("deadzone", 0.0)
    if abs(v) < dz:
        return 0.0
    v = math.copysign((abs(v) - dz) / (1.0 - dz), v)
    expo = cfg.get("expo", 0.0)
    if expo > 0:
        v = (1 - expo) * v + expo * (v ** 3)
    return max(-1.0, min(1.0, v))


def build_packet(axes, g1, g2):
    """Build a 100-byte Oberon state buffer from normalised axis values."""
    buf = bytearray(100)

    def s16(v):
        # floor + two's-complement uint16 LE
        vi = int(math.floor(v)) & 0xFFFF
        buf[pos]     = vi & 0xFF
        buf[pos + 1] = (vi >> 8) & 0xFF

    def u16(v):
        vi = max(0, min(32767, int(math.floor(v))))
        buf[pos]     = vi & 0xFF
        buf[pos + 1] = (vi >> 8) & 0xFF

    # Slot 0: our controller
    buf[0] = 0xFF
    pos = 1;  s16(axes["lx"] * 32767)
    pos = 3;  s16(axes["ly"] * -32767)   # Oberon protocol inverts Y
    pos = 5;  s16(axes["rx"] * 32767)
    pos = 7;  s16(axes["ry"] * -32767)
    pos = 9;  u16((axes["lt"] + 1.0) / 2.0 * 32767)
    pos = 11; u16((axes["rt"] + 1.0) / 2.0 * 32767)
    buf[13] = g1
    buf[14] = g2
    # Slots 1-3 remain zero (not connected)

    return bytes(buf)


# ─── Status hub (shared with the MFD and the web app) ────────────────────────

class StatusHub:
    """Thread-safe live status: what the web UI and MFD show."""

    def __init__(self):
        self._lock = threading.Lock()
        self._d = {
            "ip": "", "device": "", "connected": False, "ping_ms": None,
            "menu": False, "layout": None, "layout_display": None,
            "layout_short": None, "legacy_config": None,
            "started": time.time(),
        }

    def set(self, **kw):
        with self._lock:
            self._d.update(kw)

    def snapshot(self):
        with self._lock:
            d = dict(self._d)
        d["uptime_s"] = int(time.time() - d.pop("started"))
        return d


class Telemetry:
    """Fans status updates out to the StatusHub and (if present) the MFD."""

    def __init__(self, hub, mfd=None):
        self.hub = hub
        self.mfd = mfd

    def set_connected(self, on):
        self.hub.set(connected=on, **({} if on else {"ping_ms": None}))
        if self.mfd: self.mfd.set_connected(on)

    def set_ping(self, ms):
        self.hub.set(ping_ms=round(ms, 1))
        if self.mfd: self.mfd.set_ping(ms)

    def set_menu(self, on):
        self.hub.set(menu=on)
        if self.mfd: self.mfd.set_menu(on)

    def set_game(self, name, short, display):
        self.hub.set(layout=name, layout_short=short, layout_display=display)
        if self.mfd: self.mfd.set_game(short)

    def set_mfd_brightness(self, lvl):
        if self.mfd: self.mfd.set_mfd_brightness(lvl)

    def set_led_brightness(self, lvl):
        if self.mfd: self.mfd.set_led_brightness(lvl)


# ─── Thread-safe HOTAS state ─────────────────────────────────────────────────

class HOTASState:
    def __init__(self, throttle_targets=()):
        self._lock = threading.Lock()
        self._axes = neutral_axes()
        self._g1 = 0
        self._g2 = 0
        self._suspended = False
        self._throttle_targets = tuple(throttle_targets)

    def update(self, axes, g1, g2):
        with self._lock:
            self._axes = dict(axes)
            self._g1   = g1
            self._g2   = g2

    def set_throttle_targets(self, targets):
        with self._lock:
            self._throttle_targets = tuple(targets)

    def set_suspended(self, value):
        with self._lock:
            self._suspended = bool(value)

    def is_suspended(self):
        with self._lock:
            return self._suspended

    def toggle_suspended(self):
        with self._lock:
            self._suspended = not self._suspended
            return self._suspended

    def snapshot(self):
        with self._lock:
            axes = dict(self._axes)
            if self._suspended:
                # Freeze ONLY the throttle axis, computed at POLL time so it
                # holds even when the parked throttle sends no new events.
                # Flight sticks stay live for menu/radial steering.
                for t in self._throttle_targets:
                    axes[t] = -1.0 if t in ("lt", "rt") else 0.0
            return axes, self._g1, self._g2


# ─── Layout compilation ───────────────────────────────────────────────────────

def resolve_button_targets(target):
    """A button target (string or list) -> ([(grp, mask), ...], [lt/rt, ...]).
    Unknown names are reported by the caller; select_mode is handled there."""
    bits, trigs = [], []
    for t in (target if isinstance(target, list) else [target]):
        if t in OBERON_BTNS:
            bits.append(OBERON_BTNS[t])
        elif t in TRIGGER_BTNS:
            trigs.append(TRIGGER_BTNS[t])
        else:
            return None
    return bits, trigs


class Mappings:
    """One layout compiled against the actual device's capabilities."""

    def __init__(self, cfg, absinfo, quiet=False):
        self.cfg  = cfg
        self.name = cfg.get("name", "config")
        self.short_name   = cfg.get("short_name", self.name.upper()[:8])
        self.display_name = cfg.get("display_name", self.name)

        def log(msg):
            if not quiet:
                print(msg)

        # Axes: code -> (target-or-None, acfg, absinfo)
        self.axis_cfg = {}
        # Axis-driven buttons: code -> (low_resolved, high_resolved, threshold)
        self.axis_btns = {}
        self.throttle_targets = ()
        throttle_name = cfg.get("throttle_axis", "ABS_Z")
        for name, acfg in cfg.get("axes", {}).items():
            code = ecodes.ecodes.get(name)
            if code is None or code not in absinfo:
                log(f"  [!] axis {name} not on this device, skipped")
                continue
            tgt = acfg.get("target")
            if tgt is not None and tgt not in AXIS_TARGETS and tgt != SPLIT_TARGET:
                log(f"  [!] axis {name}: unknown target '{tgt}', skipped")
                tgt = None
            low, high = acfg.get("button_low"), acfg.get("button_high")
            if low or high:
                lr = resolve_button_targets(low)  if low  else ([], [])
                hr = resolve_button_targets(high) if high else ([], [])
                if lr is None or hr is None:
                    log(f"  [!] axis {name}: bad button_low/high, skipped")
                else:
                    thr = acfg.get("button_threshold", 0.5)
                    self.axis_btns[code] = (lr, hr, thr)
            if tgt is not None or code in self.axis_btns:
                self.axis_cfg[code] = (tgt, acfg, absinfo[code])
            if name == throttle_name:
                if tgt == SPLIT_TARGET:
                    self.throttle_targets = ("lt", "rt")
                elif tgt in AXIS_TARGETS:
                    self.throttle_targets = (tgt,)
        if not self.throttle_targets:
            self.throttle_targets = ("ly",)

        # Buttons: (mode, code) -> ([(grp,mask), ...], [lt/rt, ...])
        self.mode_sel   = {}
        self.button_cfg = {}
        for mode_name, bmap in cfg.get("buttons", {}).items():
            try:
                mode = int(mode_name.replace("mode", "") or 0)
            except ValueError:
                log(f"  [!] bad mode key '{mode_name}', skipped")
                continue
            for name, target in bmap.items():
                code = ecodes.ecodes.get(name)
                if code is None:
                    log(f"  [!] button code '{name}' unknown, skipped")
                    continue
                if isinstance(target, str) and target.startswith("select_mode"):
                    self.mode_sel[code] = int(target[-1])
                    continue
                resolved = resolve_button_targets(target)
                if resolved is None:
                    log(f"  [!] button '{name}': unknown target '{target}', skipped")
                else:
                    self.button_cfg[(mode, code)] = resolved

        self.hat_dpad = cfg.get("hat_to_dpad", True)

        def keycode(key):
            n = cfg.get(key)
            if not n:
                return None
            c = ecodes.ecodes.get(n)
            if c is None:
                log(f"  [!] {key} '{n}' unknown, ignored")
            return c

        self.suspend_code = keycode("suspend_button")
        self.switch_code  = keycode("layout_switch_button")

        # Brightness rotaries: only active when NOT already mapped as an axis.
        self.brightness = self._rotary(cfg, "brightness_axis", "ABS_RY", absinfo)
        self.led_brightness = self._rotary(cfg, "led_brightness_axis", "ABS_RX", absinfo)

    def _rotary(self, cfg, key, default, absinfo):
        name = cfg.get(key, default)
        if not name:
            return None
        code = ecodes.ecodes.get(name)
        if code is not None and code in absinfo and code not in self.axis_cfg:
            return (code, absinfo[code])
        return None


# ─── Layout manager (live switching) ─────────────────────────────────────────

class LayoutManager:
    """Holds the compiled active layout (.m) and swaps it atomically.
    The evdev reader picks up the new object and resets its local state."""

    def __init__(self, layouts_dir, absinfo, state, telemetry):
        self.layouts_dir = layouts_dir      # None = legacy single-config mode
        self.absinfo     = absinfo
        self.state       = state
        self.telemetry   = telemetry
        self._lock       = threading.Lock()
        self.m           = None             # current Mappings (read atomically)

    def set_legacy(self, cfg):
        m = Mappings(cfg, self.absinfo)
        self._apply(m, persist=False)
        return m

    def activate(self, name, persist=True, quiet=True):
        """Compile and switch to layout `name`. Returns (ok, error_str)."""
        if not self.layouts_dir:
            return False, "running in legacy --config mode; no layouts dir"
        try:
            cfg = layouts_mod.resolve(self.layouts_dir, name)
            m = Mappings(cfg, self.absinfo, quiet=quiet)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return False, f"layout '{name}': {e}"
        self._apply(m, persist=persist)
        print(f"[layout] active: {m.display_name} ({name})", flush=True)
        return True, None

    def cycle(self):
        """Switch to the next layout in order (the HOTAS switch button)."""
        if not self.layouts_dir:
            return
        order = layouts_mod.cycle_order(self.layouts_dir)
        if not order:
            return
        cur = self.m.name if self.m else None
        idx = (order.index(cur) + 1) % len(order) if cur in order else 0
        ok, err = self.activate(order[idx])
        if not ok:
            print(f"[layout] switch failed: {err}", flush=True)

    def _apply(self, m, persist):
        with self._lock:
            self.m = m
        # Publish neutral right away so the old layout's last values can't
        # linger until the next stick event (the reader also resets itself).
        self.state.update(neutral_axes(), 0, 0)
        self.state.set_throttle_targets(m.throttle_targets)
        self.telemetry.set_game(m.name, m.short_name, m.display_name)
        if persist and self.layouts_dir:
            layouts_mod.write_active(self.layouts_dir, m.name)


# ─── evdev reader (runs in daemon thread) ────────────────────────────────────

def evdev_reader(dev, mgr, state, telemetry, verbose=False):
    m = None                    # current Mappings; reset state when it changes
    axes = pressed = hat = axis_st = None
    mode = 1
    _last_bri = -1
    _last_led_bri = -1

    def reset_local():
        nonlocal axes, pressed, hat, axis_st, mode
        axes    = neutral_axes()
        pressed = set()
        hat     = [0, 0]
        axis_st = {}            # axis code -> -1 / 0 / +1 (axis-driven buttons)
        mode    = 1

    reset_local()

    while True:
        try:
            for ev in dev.read_loop():
                if mgr.m is not m:              # layout switched (button or web)
                    m = mgr.m
                    reset_local()
                    state.update(axes, 0, 0)    # publish neutral immediately

                if ev.type == ecodes.EV_ABS:
                    if ev.code in m.axis_cfg:
                        tgt, acfg, info = m.axis_cfg[ev.code]
                        v = shape(ev.value, info, acfg)
                        if tgt == SPLIT_TARGET:
                            # forward half -> RT, back half -> LT (0 at centre)
                            axes["rt"] = (v * 2.0 - 1.0) if v > 0 else -1.0
                            axes["lt"] = (-v * 2.0 - 1.0) if v < 0 else -1.0
                        elif tgt is not None:
                            axes[tgt] = v
                        if ev.code in m.axis_btns:
                            _, _, thr = m.axis_btns[ev.code]
                            axis_st[ev.code] = (-1 if v < -thr else
                                                 1 if v > thr else 0)
                    elif m.brightness and ev.code == m.brightness[0]:
                        # Throttle rotary -> live MFD brightness 0..128.
                        info = m.brightness[1]
                        span = (info.max - info.min) or 1
                        lvl = max(0, min(128, int((ev.value - info.min) / span * 128)))
                        if lvl != _last_bri:
                            _last_bri = lvl
                            telemetry.set_mfd_brightness(lvl)
                    elif m.led_brightness and ev.code == m.led_brightness[0]:
                        # Second rotary -> live button-LED brightness 0..128.
                        info = m.led_brightness[1]
                        span = (info.max - info.min) or 1
                        lvl = max(0, min(128, int((ev.value - info.min) / span * 128)))
                        if lvl != _last_led_bri:
                            _last_led_bri = lvl
                            telemetry.set_led_brightness(lvl)
                    elif m.hat_dpad and ev.code == ecodes.ABS_HAT0X:
                        hat[0] = ev.value
                    elif m.hat_dpad and ev.code == ecodes.ABS_HAT0Y:
                        hat[1] = ev.value

                elif ev.type == ecodes.EV_KEY:
                    if m.suspend_code is not None and ev.code == m.suspend_code and ev.value == 1:
                        suspended = state.toggle_suspended()   # applied at poll time
                        print(f"[menu] {'THROTTLE FROZEN (menu/radial safe)' if suspended else 'LIVE (flying)'}",
                              flush=True)
                        telemetry.set_menu(suspended)
                        if not suspended:
                            # Resuming: drop any buttons currently held (the pinkie
                            # itself, plus anything touched during menu nav) so the
                            # first live packet doesn't flush a burst of phantom
                            # inputs (fire, ping, etc.). They re-register on the
                            # next real press.
                            pressed.clear()
                    elif m.switch_code is not None and ev.code == m.switch_code and ev.value == 1:
                        mgr.cycle()     # picked up at the top of the next event
                        continue
                    elif ev.code in m.mode_sel and ev.value:
                        new_mode = m.mode_sel[ev.code]
                        if new_mode != mode:
                            mode = new_mode
                            print(f"[mode] switched to M{mode}", flush=True)
                    elif ev.value:
                        pressed.add(ev.code)
                    else:
                        pressed.discard(ev.code)

                # Rebuild button bytes every event. Buttons stay live while
                # suspended so you can still select/back in menus; only the axes
                # freeze. The pressed.clear() on resume (above) prevents a held
                # button from flushing as a phantom input when axes come back.
                g1, g2 = 0, 0
                trig_hold = {"lt": False, "rt": False}

                def press(resolved):
                    nonlocal g1, g2
                    bits, trigs = resolved
                    for grp, mask in bits:
                        if grp == 1: g1 |= mask
                        else:        g2 |= mask
                    for t in trigs:
                        trig_hold[t] = True

                for code in pressed:
                    resolved = m.button_cfg.get((mode, code))
                    if resolved:
                        press(resolved)

                # Axis-driven buttons (e.g. twist past the threshold = LB/RB)
                for code, st in axis_st.items():
                    if st:
                        low, high, _ = m.axis_btns[code]
                        press(low if st < 0 else high)

                # POV hats report -1/0/+1. Require a full ±1 before emitting a
                # d-pad direction so a hat resting slightly off-neutral can
                # never hold a direction (which would block menu navigation).
                if hat[0] <= -1: g2 |= OBERON_BTNS["dpad_left"][1]
                if hat[0] >= 1:  g2 |= OBERON_BTNS["dpad_right"][1]
                if hat[1] <= -1: g2 |= OBERON_BTNS["dpad_up"][1]
                if hat[1] >= 1:  g2 |= OBERON_BTNS["dpad_down"][1]

                # The throttle freeze is applied at poll time inside
                # HOTASState.snapshot() (so it holds even when the parked
                # throttle sends no events). Here we just publish live values.
                ax = dict(axes)
                if trig_hold["lt"]: ax["lt"] = 1.0
                if trig_hold["rt"]: ax["rt"] = 1.0

                state.update(ax, g1, g2)

        except OSError:
            time.sleep(1)  # device unplugged; keep trying


# ─── WebSocket server ─────────────────────────────────────────────────────────

def make_handler(state, verbose, telemetry):
    hostname = socket.gethostname()
    handshake = bytes([0x0A]) + hostname.encode("utf-8")

    async def handler(websocket):
        addr = websocket.remote_address
        print(f"[oberon] connected from {addr[0]}")
        await websocket.send(handshake)
        telemetry.set_connected(True)

        # Smoothed poll interval -> shown on the MFD as "ping" (round-trip
        # cadence of the Oberon client's polls, in ms).
        ema = None
        last_poll = time.monotonic()

        try:
            async for msg in websocket:
                if not isinstance(msg, (bytes, bytearray)) or not msg:
                    continue
                if msg[0] != 0xFA:
                    continue

                now = time.monotonic()
                dt = (now - last_poll) * 1000.0  # ms since last poll
                last_poll = now
                if 0 < dt < 1000:
                    ema = dt if ema is None else (0.8 * ema + 0.2 * dt)
                    telemetry.set_ping(ema)

                axes, g1, g2 = state.snapshot()
                pkt = build_packet(axes, g1, g2)
                await websocket.send(pkt)

                if verbose:
                    lt = (axes["lt"] + 1) / 2
                    rt = (axes["rt"] + 1) / 2
                    print(f"  lx={axes['lx']:+.2f} ly={axes['ly']:+.2f} "
                          f"rx={axes['rx']:+.2f} lt={lt:.2f} rt={rt:.2f} "
                          f"g1={g1:08b} g2={g2:08b}")

        except websockets.exceptions.ConnectionClosed:
            pass
        telemetry.set_connected(False)
        print(f"[oberon] disconnected from {addr[0]}")

    return handler


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Oberon Remote WebSocket server for Saitek X52")
    ap.add_argument("--config",  help="legacy single-config mode: run this one "
                                      "JSON config with layout switching off")
    ap.add_argument("--layouts-dir", default=layouts_mod.default_dir(),
                    help="directory of layout JSONs (default: ../layouts)")
    ap.add_argument("--layout",  help="layout to start on (default: last active)")
    ap.add_argument("--device",  help="/dev/input/eventX path")
    ap.add_argument("--port",    type=int, default=PORT)
    ap.add_argument("--web-port", type=int, default=None,
                    help=f"status/editor web app port (default {WEB_PORT}, 0 = off)")
    ap.add_argument("--list",    action="store_true", help="list input devices and exit")
    ap.add_argument("--probe",   action="store_true", help="print live events and exit")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--menu", action="store_true",
                    help="start with axes SUSPENDED (throttle/sticks frozen) so "
                         "the Xbox dashboard doesn't scroll; press the suspend "
                         "button once in-game to start flying")
    args = ap.parse_args()

    if args.list:
        for path in list_devices():
            try:
                d = InputDevice(path); print(f"{path}  {d.name}"); d.close()
            except OSError:
                pass
        return

    # Decide the mode: explicit --config = legacy; otherwise layouts dir.
    legacy_cfg = None
    layouts_dir = None
    if args.config:
        with open(args.config) as f:
            legacy_cfg = json.load(f)
        print(f"[layout] legacy mode: fixed config {args.config}")
    elif os.path.isdir(args.layouts_dir) and layouts_mod.list_layouts(args.layouts_dir):
        layouts_dir = os.path.abspath(args.layouts_dir)
        print(f"[layout] layouts dir: {layouts_dir}")
    else:
        sys.exit(f"No layouts found in {args.layouts_dir} and no --config given.")

    base_cfg = legacy_cfg
    if layouts_dir:
        start = args.layout or layouts_mod.read_active(layouts_dir)
        try:
            base_cfg = layouts_mod.resolve(layouts_dir, start)
        except Exception as e:
            sys.exit(f"Cannot load layout '{start}': {e}")

    dev = InputDevice(args.device) if args.device \
        else find_device(base_cfg.get("device_match", "X52"))
    if dev is None:
        sys.exit(f"No device matching '{base_cfg.get('device_match')}'. "
                 f"Run --list, then re-run with --device /dev/input/eventX")
    print(f"[evdev]  {dev.path}  {dev.name}")

    if args.probe:
        print("Move axes / press buttons. Ctrl-C to stop.\n")
        for ev in dev.read_loop():
            if ev.type in (ecodes.EV_KEY, ecodes.EV_ABS):
                print(f"{code_name(ev.type, ev.code):28s}  value={ev.value}")
        return

    absinfo = dict(dev.capabilities().get(ecodes.EV_ABS, []))
    dev.grab()   # exclusive: nothing else on this board consumes the stick

    if args.menu and not base_cfg.get("suspend_button"):
        print("  [!] --menu set but no working suspend_button — you won't be able\n"
              "      to UNFREEZE. Set suspend_button in the layout first.")
    if args.menu:
        print("  Starting SUSPENDED (menu-safe). Axes are frozen; navigate the\n"
              "  dashboard, launch your game, then press the suspend button once.")

    # Determine this board's IP first (shown on the MFD and printed for the user)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "unknown"

    # Optional X52 Pro MFD status display (libx52). None if not installed.
    mfd = None
    if mfd_mod is not None and mfd_mod.available():
        mfd = mfd_mod.MFDStatus(ip=local_ip)
        mfd.set_menu(args.menu)
        print("[oberon] MFD status display: ON (libx52 detected)")

    hub = StatusHub()
    hub.set(ip=local_ip, device=dev.name, menu=args.menu,
            legacy_config=args.config)
    telemetry = Telemetry(hub, mfd)

    state = HOTASState()
    state.set_suspended(args.menu)   # --menu starts with the throttle frozen

    mgr = LayoutManager(layouts_dir, absinfo, state, telemetry)
    if layouts_dir:
        ok, err = mgr.activate(base_cfg["name"], persist=True, quiet=False)
        if not ok:
            sys.exit(err)
        m = mgr.m
        if m.switch_code is not None:
            print(f"  Layout switch: press {base_cfg.get('layout_switch_button')} "
                  f"to cycle {', '.join(layouts_mod.cycle_order(layouts_dir))}")
    else:
        mgr.set_legacy(legacy_cfg)

    if mgr.m.suspend_code is not None:
        print(f"  Menu-suspend toggle: press {mgr.m.cfg.get('suspend_button')} "
              f"to freeze/unfreeze the throttle")

    threading.Thread(
        target=evdev_reader,
        args=(dev, mgr, state, telemetry, args.verbose),
        daemon=True
    ).start()

    # Web app: live status + layout editor for any browser on the LAN.
    web_port = args.web_port
    if web_port is None:
        web_port = mgr.m.cfg.get("web_port", WEB_PORT)
    if web_port:
        try:
            import webapp
            webapp.start(web_port, hub, mgr, state, telemetry)
            print(f"[web]    layout editor + status: http://{local_ip}:{web_port}")
        except Exception as e:
            print(f"[web]    disabled ({e})")

    async def run():
        handler = make_handler(state, args.verbose, telemetry)
        async with ws_serve(handler, "0.0.0.0", args.port):
            print(f"[oberon] WebSocket server on port {args.port}")
            print(f"[oberon] Board IP : {local_ip}")
            print(f"[oberon] On Xbox  : open Oberon Remote -> enter {local_ip} -> Connect")
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
