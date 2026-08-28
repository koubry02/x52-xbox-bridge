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
    line 1:  XBOX:<ON/--> <ping>      e.g. XBOX:ON   45ms
                                           XBOX:ON   1.4s   (laggy)
                                           XBOX:ON  STALL   (nothing arriving)
    line 2:  MENU:<ON/OFF> <game>     e.g. MENU:ON  AC7

Every line is written padded to the full 16 characters: the MFD does not
clear a line first, so a shorter string would leave the tail of the previous
one on screen.
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

# No poll for this long while connected = the link has stalled, and the last
# ping figure is no longer telling the truth.
STALL_AFTER_S = 1.5


def available():
    return _X52CLI is not None


def _set_line(line_no, text):
    """Write one MFD line. Best-effort; never raises into the caller.
    Returns True only if the write actually went through.

    libx52 CLI syntax is:  x52cli mfd <line> "<text>"
    (line is 0, 1, or 2; text is max 16 chars, extra is discarded).

    The text is padded to the full 16 characters. The MFD does NOT clear a
    line before writing it, so a short string leaves the tail of whatever was
    there before — write "45ms" over "1234ms" and the display keeps the stray
    trailing characters. Padding overwrites the whole line every time.
    """
    if _X52CLI is None:
        return False
    text = (text or "")[:_LINE_LEN].ljust(_LINE_LEN)
    try:
        r = subprocess.run(
            [_X52CLI, "mfd", str(line_no), text],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
        return r.returncode == 0
    except Exception:
        return False   # display is cosmetic; input must never be affected


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
        self._ping_at = None   # when the last poll landed, for stall detection
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
                self._ping_at = None

    def set_ping(self, ping_ms):
        with self._lock:
            self._ping_ms = ping_ms
            self._ping_at = time.monotonic()

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
    @staticmethod
    def _fmt_ping(ping_ms, age_s):
        """The poll cadence, in a form that fits and never lies.

        A stalled link is the important case: if the Oberon app stops polling,
        the last figure would otherwise sit there reading a healthy 45ms while
        nothing is arriving at all.
        """
        if ping_ms is None:
            return "--ms"
        if age_s is not None and age_s > STALL_AFTER_S:
            return "STALL"
        if ping_ms >= 1000:
            return f"{ping_ms / 1000:.1f}s"      # 1.4s reads better than 1400ms
        return f"{int(ping_ms)}ms"

    def _render(self):
        with self._lock:
            ip = self._ip or "no IP"
            if self._connected:
                age = None if self._ping_at is None else (time.monotonic() - self._ping_at)
                l1 = f"XBOX:ON {self._fmt_ping(self._ping_ms, age):>7}"
            else:
                l1 = "XBOX:--  waiting"
            menu = f"MENU:{'ON' if self._menu else 'OFF'}"
            l2 = f"{menu:<8}{self._game:>8}" if self._game else menu
        # Pad here too: every line handed to _set_line is a full-width overwrite.
        return tuple(s[:_LINE_LEN].ljust(_LINE_LEN) for s in (ip, l1, l2))

    def _loop(self, hz):
        # Give the device a moment to settle, then refresh on a cadence.
        period = 1.0 / max(0.5, hz)
        while not self._stop:
            lines = self._render()
            if lines != self._last:
                # Only remember what we drew if it actually got there. A write
                # that times out (which is likeliest exactly when the board is
                # busy) must be retried, or the display keeps a mix of old and
                # new lines until the text happens to change again.
                ok = all([_set_line(i, txt) for i, txt in enumerate(lines)])
                self._last = lines if ok else None
            # LED color reflects state: amber while in menu mode (throttle
            # frozen), green while flying/live.
            with self._lock:
                want_led = "amber" if self._menu else "green"
            if want_led != self._last_led:
                set_all_leds(want_led)
                self._last_led = want_led
            time.sleep(period)
