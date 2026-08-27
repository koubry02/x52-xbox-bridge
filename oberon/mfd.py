"""
X52 Pro MFD (multi-function display) status output.

Uses libx52's `x52cli` (https://github.com/nirenjan/libx52) to write the three
16-character text lines on the throttle's display. Everything here is optional
and best-effort: if x52cli isn't installed, all calls become silent no-ops so
the bridge runs exactly as before.

Install libx52 on the Pi:
    sudo apt-add-repository ppa:nirenjan/libx52
    sudo apt update
    sudo apt install x52pro-linux      # provides the x52cli binary
    # (or build from source per the repo's INSTALL.md)

Display layout (3 lines x 16 chars):
    line 0:  <Pi IP address>          e.g. 192.168.1.69
    line 1:  XBOX:<ON/--> <ping>ms    e.g. XBOX:ON   45ms
    line 2:  MENU:<ON/OFF> <game>     e.g. MENU:ON  AC7
"""
import shutil
import subprocess
import threading
import time

# Resolve the libx52 CLI once. Different packages/versions name it differently:
#   x52cli     - nirenjan/libx52 (PPA, source)
#   x52output  - some distro packages
# None means "not installed" -> no-op everywhere.
_X52CLI = shutil.which("x52cli") or shutil.which("x52output")

# libx52's CLI addresses the three MFD lines as 0, 1, 2.
_LINE_LEN = 16


def available():
    return _X52CLI is not None


def _set_line(line_no, text):
    """Write one MFD line. Best-effort; never raises into the caller.

    libx52 CLI syntax is:  x52cli mfd <line> "<text>"
    (line is 0, 1, or 2; text is max 16 chars, extra is discarded).
    """
    if _X52CLI is None:
        return
    text = (text or "")[:_LINE_LEN]
    try:
        subprocess.run(
            [_X52CLI, "mfd", str(line_no), text],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
    except Exception:
        pass  # display is cosmetic; input must never be affected


def _cli(*args):
    """Run an arbitrary x52cli subcommand, best-effort."""
    if _X52CLI is None:
        return
    try:
        subprocess.run([_X52CLI, *[str(a) for a in args]],
                       check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=1.0)
    except Exception:
        pass


# X52 Pro color LED groups (each supports red/amber/green/off).
_COLOR_LEDS = ("a", "b", "d", "e", "t1", "t2", "t3", "pov", "clutch")
# On/off-only LEDs.
_ONOFF_LEDS = ("fire", "throttle")


def set_brightness_full():
    """Turn MFD and LED backlight to full. The X52 Pro MFD is green by
    hardware; this just makes sure it's lit brightly."""
    _cli("bri", "mfd", "128")
    _cli("bri", "led", "128")


def set_mfd_brightness(level_0_128):
    """Set MFD backlight brightness, 0..128. Used by the throttle brightness
    knob (ABS_RY) for live control."""
    lvl = max(0, min(128, int(level_0_128)))
    _cli("bri", "mfd", str(lvl))


def set_led_brightness(level_0_128):
    """Set button-LED brightness, 0..128. Used by the throttle knob (ABS_RX)."""
    lvl = max(0, min(128, int(level_0_128)))
    _cli("bri", "led", str(lvl))


def set_all_leds(color):
    """Set every color LED to `color` (green/amber/red/off) and the on/off
    LEDs to on (for green/amber) or off (for off)."""
    for led in _COLOR_LEDS:
        _cli("led", led, color)
    onoff = "off" if color == "off" else "on"
    for led in _ONOFF_LEDS:
        _cli("led", led, onoff)


class MFDStatus:
    """
    Tracks bridge status and pushes it to the MFD. Thread-safe. A background
    thread refreshes the display ~2x/sec so the ping and connection state stay
    current without the caller having to redraw on every packet.
    """

    def __init__(self, ip="", refresh_hz=2.0):
        self._lock = threading.Lock()
        self._ip = ip
        self._connected = False
        self._ping_ms = None
        self._menu = False
        self._game = ""      # active layout short_name, e.g. "AC7"
        self._last = None  # last rendered tuple, to skip redundant writes
        self._last_led = None
        self._stop = False
        self._enabled = available()
        if self._enabled:
            # MFD is green by hardware — bring the backlight up and light the
            # button LEDs green as a "ready" state.
            set_brightness_full()
            set_all_leds("green")
            self._last_led = "green"
            t = threading.Thread(target=self._loop, args=(refresh_hz,), daemon=True)
            t.start()

    # ---- state setters (called from the server) ----
    def set_ip(self, ip):
        with self._lock:
            self._ip = ip

    def set_connected(self, connected):
        with self._lock:
            self._connected = connected
            if not connected:
                self._ping_ms = None

    def set_ping(self, ping_ms):
        with self._lock:
            self._ping_ms = ping_ms

    def set_menu(self, menu_on):
        with self._lock:
            self._menu = menu_on

    def set_game(self, short_name):
        """Show which game layout is active (right side of the MENU line)."""
        with self._lock:
            self._game = (short_name or "")[:8]

    def set_mfd_brightness(self, level_0_128):
        """Live MFD brightness from the throttle knob (0..128)."""
        set_mfd_brightness(level_0_128)

    def set_led_brightness(self, level_0_128):
        """Live button-LED brightness from the throttle knob (0..128)."""
        set_led_brightness(level_0_128)

    def stop(self):
        self._stop = True

    # ---- rendering ----
    def _render(self):
        with self._lock:
            ip = self._ip or "no IP"
            if self._connected:
                p = f"{int(self._ping_ms)}ms" if self._ping_ms is not None else "--ms"
                l1 = f"XBOX:ON {p:>7}"[:_LINE_LEN]
            else:
                l1 = "XBOX:--  waiting"
            menu = f"MENU:{'ON' if self._menu else 'OFF'}"
            l2 = f"{menu:<8}{self._game:>8}" if self._game else menu
        return (ip[:_LINE_LEN], l1, l2)

    def _loop(self, hz):
        # Give the device a moment to settle, then refresh on a cadence.
        period = 1.0 / max(0.5, hz)
        while not self._stop:
            lines = self._render()
            if lines != self._last:
                for i, txt in enumerate(lines):
                    _set_line(i, txt)
                self._last = lines
            # LED color reflects state: amber while in menu mode (throttle
            # frozen), green while flying/live.
            with self._lock:
                want_led = "amber" if self._menu else "green"
            if want_led != self._last_led:
                set_all_leds(want_led)
                self._last_led = want_led
            time.sleep(period)
