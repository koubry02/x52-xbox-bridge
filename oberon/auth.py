"""
auth.py — PIN pairing and session tokens for the LAN web app.

THE TRUST ANCHOR is physical access. A PIN is shown only on the X52's own
throttle screen and in root's journal, so reading one proves you are either
standing at the stick or already on the box. Everything else is derived from
that one moment of proof.

TIERS
  open        the page shell, and asking for a PIN
  session     a valid bearer token — layouts, menu mode, capture
  privileged  a valid token AND a PIN redeemed within STEPUP_S — anything that
              runs code

The privileged tier is the point. This is plaintext HTTP on a LAN: on WPA2-PSK
anyone with the WiFi password who caught your handshake can read a bearer
token off the wire, and bearer tokens are replayable by design. So a token can
never be sufficient for an operation that fetches and executes code as root —
that needs a secret which only exists on hardware in the room, produced fresh
at the moment of the request. Same shape as sudo asking again.

Tokens are stored HASHED, like passwords: if sessions.json leaks, the tokens
in it cannot be replayed.

What is deliberately NOT gated: the controller itself. The stick keeps working
with every session revoked and the web app locked. Auth guards the web app,
never your input.
"""

import hashlib
import json
import os
import secrets
import threading
import time

PIN_DIGITS   = 6
PIN_TTL_S    = 60.0      # how long a PIN works, and stays on the throttle screen
PIN_TRIES    = 5         # wrong guesses before the PIN is burned
PIN_MIN_GAP  = 10.0      # a fresh PIN can't be minted more often than this
SESSION_TTL_S    = 12 * 3600      # idle timeout, slides on use
SESSION_MAX_S    = 7 * 24 * 3600  # hard cap regardless of use
STEPUP_S     = 120.0     # how recently a PIN must have been entered for tier 3


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def state_dir():
    """Somewhere root-only to keep sessions. Falls back next to the layouts if
    /var/lib isn't writable (running from a checkout, tests)."""
    for d in ("/var/lib/hotas-bridge",
              os.path.join(os.path.dirname(__file__), "..", "layouts")):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            if os.access(d, os.W_OK):
                return os.path.abspath(d)
        except OSError:
            continue
    return None


class Auth:
    """PIN pairing + bearer sessions. Thread-safe; every method may be called
    from the web threads while the reader and MFD threads run."""

    def __init__(self, store_dir=None, on_pin=None, pin_ttl=PIN_TTL_S):
        self._lock = threading.Lock()
        self._store = os.path.join(store_dir, "sessions.json") if store_dir else None
        self._pin_ttl = pin_ttl
        # one pending PIN at a time — see request_pin()
        self._pin = None            # {"pin", "expires", "tries"}
        self._last_mint = 0.0
        self._sessions = {}         # sha256(token) -> session dict
        self._on_pin = on_pin or (lambda pin, expires: None)
        self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self):
        if not self._store:
            return
        try:
            with open(self._store) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        self._sessions = {
            k: v for k, v in data.get("sessions", {}).items()
            if isinstance(v, dict) and v.get("expires", 0) > now
        }

    def _save(self):
        if not self._store:
            return
        tmp = self._store + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"sessions": self._sessions}, f)
            os.replace(tmp, self._store)
        except OSError:
            pass    # losing sessions is survivable; failing a request is not

    # ── PIN ──────────────────────────────────────────────────────────────
    def request_pin(self):
        """Ask for a PIN. Returns (pin, seconds_left, fresh).

        A pending, unexpired PIN is returned AS-IS rather than replaced. That
        is what stops a hostile client from parking the throttle screen on an
        endless series of new PINs: hammering this endpoint is idempotent.
        """
        with self._lock:
            now = time.monotonic()
            p = self._pin
            if p and p["expires"] > now:
                return p["pin"], p["expires"] - now, False
            if now - self._last_mint < PIN_MIN_GAP:
                return None, PIN_MIN_GAP - (now - self._last_mint), False
            pin = f"{secrets.randbelow(10 ** PIN_DIGITS):0{PIN_DIGITS}d}"
            self._pin = {"pin": pin, "expires": now + self._pin_ttl, "tries": 0}
            self._last_mint = now
            ttl = self._pin_ttl
        # Outside the lock: this writes to the throttle screen and the journal.
        self._on_pin(pin, ttl)
        return pin, ttl, True

    def _clear_pin_locked(self):
        self._pin = None

    def cancel_pin(self):
        with self._lock:
            had = self._pin is not None
            self._clear_pin_locked()
        if had:
            self._on_pin(None, 0)

    def pin_pending(self):
        with self._lock:
            p = self._pin
            return bool(p and p["expires"] > time.monotonic())

    # ── login / step-up ──────────────────────────────────────────────────
    def redeem(self, pin, token=None, ip="", agent=""):
        """Trade a PIN for a session, or refresh step-up on the token you have.

        Returns (token, error). One endpoint covers both flows: present a valid
        token and the PIN re-stamps it for the privileged tier; present none
        and you get a new session.
        """
        clear_display = False
        try:
            with self._lock:
                p = self._pin
                now = time.monotonic()
                if not p or p["expires"] <= now:
                    clear_display = self._pin is not None
                    self._clear_pin_locked()
                    return None, "no PIN is waiting — ask for one"

                ok = (isinstance(pin, str) and len(pin) == PIN_DIGITS
                      and pin.isdigit() and secrets.compare_digest(pin, p["pin"]))
                if not ok:
                    p["tries"] += 1
                    if p["tries"] >= PIN_TRIES:
                        # Burned. Take it off the screen too — a dead PIN left
                        # on the glass is confusing and pointlessly on show.
                        self._clear_pin_locked()
                        clear_display = True
                        return None, "too many wrong tries — ask for a new PIN"
                    return None, f"wrong PIN ({PIN_TRIES - p['tries']} left)"

                self._clear_pin_locked()
                clear_display = True
            wall = time.time()
            key = _hash(token) if token else None
            if key and key in self._sessions:          # step-up an existing one
                s = self._sessions[key]
                s["stepup"] = wall
                s["last_seen"] = wall
                s["expires"] = min(wall + SESSION_TTL_S, s["created"] + SESSION_MAX_S)
                out = token
            else:                                       # brand new session
                out = secrets.token_urlsafe(32)
                self._sessions[_hash(out)] = {
                    "created": wall, "last_seen": wall, "stepup": wall,
                    "expires": wall + SESSION_TTL_S, "ip": ip, "agent": agent[:80],
                }
            self._save()
            return out, None
        finally:
            # Outside the lock, and on every exit path that ended the PIN.
            if clear_display:
                self._on_pin(None, 0)

    # ── checking ─────────────────────────────────────────────────────────
    def check(self, token):
        """Valid session for this token, or None. Slides the idle timeout."""
        if not token:
            return None
        key = _hash(token)
        with self._lock:
            s = self._sessions.get(key)
            now = time.time()
            if not s or s["expires"] <= now:
                if s:
                    del self._sessions[key]
                    self._save()
                return None
            s["last_seen"] = now
            s["expires"] = min(now + SESSION_TTL_S, s["created"] + SESSION_MAX_S)
            return dict(s)

    def stepped_up(self, token):
        """True only if this session redeemed a PIN within STEPUP_S. Guards
        anything that runs code — a token alone must never be enough."""
        s = self.check(token)
        return bool(s and time.time() - s.get("stepup", 0) <= STEPUP_S)

    # ── management ───────────────────────────────────────────────────────
    def sessions(self):
        with self._lock:
            now = time.time()
            return [{"created": s["created"], "last_seen": s["last_seen"],
                     "expires": s["expires"], "ip": s.get("ip", ""),
                     "agent": s.get("agent", ""),
                     "stepped_up": now - s.get("stepup", 0) <= STEPUP_S}
                    for s in self._sessions.values()]

    def revoke(self, token):
        with self._lock:
            gone = self._sessions.pop(_hash(token), None) is not None
            if gone:
                self._save()
            return gone

    def revoke_all(self):
        with self._lock:
            n = len(self._sessions)
            self._sessions = {}
            self._save()
        return n
