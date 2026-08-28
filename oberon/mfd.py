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
import os
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


_last_onoff = None


def set_all_leds(color):
    """Set every color LED to `color` (green/amber/red/off) and the on/off
    LEDs to on (for green/amber) or off (for off).

    Every one of these is a process spawn plus a USB control transfer, so the
    on/off pair is only touched when it actually changes — green and amber
    both mean "on", and re-sending that on every menu toggle was two wasted
    round-trips out of eleven."""
    global _last_onoff
    for led in _COLOR_LEDS:
        _cli("led", led, color)
    onoff = "off" if color == "off" else "on"
    if onoff != _last_onoff:
        _last_onoff = onoff
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
        self._shown_ping = None  # the figure currently on the glass
        # Wanted brightness, applied by the refresh thread. These used to be
        # written straight from the evdev reader, which meant turning a knob
        # forked a process and waited on USB *in the thread reading the stick*.
        self._want_bri = None
        self._want_led_bri = None
        # Set by anything the user is waiting to see, so the refresh loop
        # redraws now instead of on its next tick.
        self._wake = threading.Event()
        self._menu = False
        self._game = ""      # active layout short_name, e.g. "AC7"
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
                self._shown_ping = None

    def set_latency(self, total_ms, input_ms=None, link_ms=None):
        """total = stick moved -> Xbox has it. The breakdown (input_ms /
        link_ms) is for the web page; the throttle screen shows one figure."""
        with self._lock:
            self._ping_ms = total_ms
            self._ping_at = time.monotonic()

    def set_ping(self, ping_ms):            # callers that only have one figure
        self.set_latency(ping_ms)

    def set_menu(self, menu_on):
        with self._lock:
            changed = self._menu != menu_on
            self._menu = menu_on
        if changed:
            self._wake.set()   # you pressed a button; don't wait for a tick

    def set_game(self, short_name):
        """Show which game layout is active (right side of the MENU line)."""
        with self._lock:
            name = (short_name or "")[:8]
            changed = self._game != name
            self._game = name
        if changed:
            self._wake.set()

    def set_mfd_brightness(self, level_0_128):
        """Ask for a new MFD brightness. Records it and wakes the refresh
        thread — the x52cli call happens there, never in the caller, because
        the caller is the thread reading your stick."""
        with self._lock:
            self._want_bri = max(0, min(128, int(level_0_128)))
        self._wake.set()

    def set_led_brightness(self, level_0_128):
        """Same, for the button-LED brightness knob."""
        with self._lock:
            self._want_led_bri = max(0, min(128, int(level_0_128)))
        self._wake.set()

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
        if ping_ms < 10:
            # A good wired-ish link lands under a millisecond. Rounding that to
            # "0ms" looks broken rather than excellent.
            return f"{ping_ms:.1f}ms"
        return f"{int(ping_ms)}ms"

    def _settled_ping(self, ping_ms):
        """The figure to actually print, held steady against jitter.

        Every change costs a process spawn and a USB round-trip, and a live
        latency reading wobbles by a fraction of a millisecond constantly —
        which would have the screen rewriting itself twice a second forever.
        This only moves when it moves meaningfully. The web page carries the
        live unfiltered version, and the full breakdown with it."""
        if ping_ms is None:
            self._shown_ping = None
        else:
            cur = self._shown_ping
            if cur is None or abs(ping_ms - cur) >= max(1.0, cur * 0.25):
                self._shown_ping = ping_ms
        return self._shown_ping

    def _render(self):
        with self._lock:
            # Line 0 is the IP, always — it's what you type into the Oberon
            # app, and a line that never changes never has to be redrawn.
            # Where the latency goes lives on the web page, not here.
            l0 = self._ip or "no IP"
            if self._connected:
                age = None if self._ping_at is None else (time.monotonic() - self._ping_at)
                l1 = f"XBOX:ON {self._fmt_ping(self._settled_ping(self._ping_ms), age):>7}"
            else:
                self._shown_ping = None
                l1 = "XBOX:--  waiting"
            menu = f"MENU:{'ON' if self._menu else 'OFF'}"
            l2 = f"{menu:<8}{self._game:>8}" if self._game else menu
            ip = l0
        # Pad here too: every line handed to _set_line is a full-width overwrite.
        return tuple(s[:_LINE_LEN].ljust(_LINE_LEN) for s in (ip, l1, l2))

    def _loop(self, hz):
        # The display must never outrank the stick. The service runs at
        # Nice=-10 so the input path wins under load, but that applies to every
        # thread — including this one, whose job is a burst of fork/exec plus
        # USB round-trips. On Linux nice is per-thread, so drop just this one.
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (OSError, AttributeError, ValueError):
            pass

        period = 1.0 / max(0.5, hz)
        drawn = [None, None, None]      # what's actually on each line
        done_bri = done_led_bri = None  # brightness actually pushed to the stick
        while not self._stop:
            lines = self._render()
            # Write only the lines that changed. Every _set_line is a process
            # spawn plus a USB round-trip — by far the most expensive thing
            # this program does — and in practice only the ping line moves.
            # A line that fails to write stays None so the next tick retries
            # it, instead of leaving old and new text mixed on the display.
            for i, txt in enumerate(lines):
                if txt != drawn[i]:
                    drawn[i] = txt if _set_line(i, txt) else None
            # LED color reflects state: amber while in menu mode (throttle
            # frozen), green while flying/live.
            with self._lock:
                want_led = "amber" if self._menu else "green"
            if want_led != self._last_led:
                set_all_leds(want_led)
                self._last_led = want_led
            # Brightness the knobs asked for, applied here where a blocked
            # USB round-trip costs nothing but a late redraw.
            with self._lock:
                bri, led_bri = self._want_bri, self._want_led_bri
                self._want_bri = self._want_led_bri = None
            if bri is not None and bri != done_bri:
                set_mfd_brightness(bri); done_bri = bri
            if led_bri is not None and led_bri != done_led_bri:
                set_led_brightness(led_bri); done_led_bri = led_bri
            # Wait for the next tick OR for something to actually change.
            # Toggling menu mode used to sit here for up to half a second
            # before the screen and the LEDs caught up, which read as the
            # toggle itself being slow.
            self._wake.wait(period)
            self._wake.clear()
