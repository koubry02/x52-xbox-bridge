"""
webapp.py — built-in LAN web app for HOTAS Bridge: live status + layout editor.

Served by oberon_server.py in a daemon thread (default port 8088). Pure
Python stdlib — no extra packages on the Pi. Open  http://<pi-ip>:8088
from any browser on the same network.

API:
  GET  /api/status                 live bridge status + layout list
  GET  /api/layouts                layout summaries
  GET  /api/layouts/<name>         {"raw", "resolved", "inherited"}
  PUT  /api/layouts/<name>         save (validated); re-applies live if active
  DELETE /api/layouts/<name>       delete (refused for default / inherited-from)
  POST /api/activate  {"name"}     switch the active layout now
  POST /api/menu      {"on"}       toggle menu mode (throttle freeze)
  GET  /api/codes                  known evdev axis/button names for pickers
  POST /api/capture   {"kind"}     listen for the next button press / axis move
  GET  /api/capture                what it heard (idle/listening/detected)
  DELETE /api/capture              stop listening
  POST /api/auth/pin               show a PIN on the throttle screen + journal
  POST /api/auth/login {"pin"}     trade it for a token, or refresh step-up
  GET  /api/auth                   who am I, and are my sessions
  POST /api/auth/logout            drop this session
  POST /api/auth/revoke-all        drop every session   [privileged]
  GET  /api/update                 deployed version + this run's progress log
  POST /api/update/source          point the board at another repo/branch
                                   [privileged: needs a fresh PIN]
  POST /api/update/check           ask the repo what is available
  POST /api/update/apply           install it, restart, roll back if broken
                                   [privileged: needs a fresh PIN]

AUTH. Everything but the page shell and the PIN request needs a bearer token,
obtained by reading a PIN off the X52's screen (or root's journal) — proof you
are at the stick or on the box. Operations that run code additionally need a
PIN redeemed in the last two minutes, because this is plaintext HTTP and a
bearer token can be sniffed and replayed. See auth.py.

Tokens travel in the Authorization header, never a cookie: a cookie would make
every endpoint CSRF-able by any site you happen to visit, since your browser
can reach this port. We also refuse cross-origin requests outright, and refuse
a Host header that is a name rather than an IP, which is what DNS rebinding
needs.
"""

import ipaddress
import json
import subprocess
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth as auth_mod
import layouts as layouts_mod

try:
    from evdev import ecodes as _ecodes
except ImportError:
    _ecodes = None

_INDEX = os.path.join(os.path.dirname(__file__), "web", "index.html")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")

# Where updates come from unless the owner points the board somewhere else.
DEFAULT_REPO = "https://github.com/koubry02/x52-xbox-bridge"
DEFAULT_BRANCH = "main"

# Deliberately narrow. This string is handed to `git clone`, so it decides what
# code the board will run: only plain https, only a host and an owner/repo path,
# and never something starting with "-", which git would read as an option
# rather than a URL.
_REPO_RE = re.compile(
    r"^https://[A-Za-z0-9][A-Za-z0-9.\-]*(:[0-9]{1,5})?"
    r"/[A-Za-z0-9][A-Za-z0-9._\-]*/[A-Za-z0-9][A-Za-z0-9._\-]*(\.git)?/?$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,80}$")


def _repo_ok(u):
    return bool(isinstance(u, str) and len(u) <= 300 and _REPO_RE.match(u)
                and ".." not in u)


def _branch_ok(b):
    return bool(isinstance(b, str) and _BRANCH_RE.match(b)
                and ".." not in b and not b.endswith(("/", ".lock")))


def _known_codes():
    """evdev names worth offering in the editor's dropdowns."""
    if _ecodes is None:
        return {"axes": [], "buttons": []}
    axes = sorted(n for n in _ecodes.ecodes if n.startswith("ABS_"))
    btns = sorted(n for n in _ecodes.ecodes if n.startswith("BTN_"))
    return {"axes": axes, "buttons": btns}


def start(port, hub, mgr, state, telemetry, capture=None, auth=None):
    """Start the web server in a daemon thread. Returns the thread."""

    codes = _known_codes()

    # Reachable without a token: the shell you type the PIN into, and asking
    # for a PIN. Nothing else — no status, no layout names, no leak of whether
    # you are mid-game.
    OPEN_PATHS = {"/", "/index.html", "/favicon.ico",
                  "/api/auth/pin", "/api/auth/login"}
    # Runs code or drops everyone's access: token AND a fresh PIN.
    # These run code on the board. A session token is never enough — see auth.py.
    PRIVILEGED_PATHS = {"/api/auth/revoke-all", "/api/update/apply",
                        "/api/update/source"}

    # Same overrides the shell scripts take, so an update can be rehearsed
    # against a throwaway tree. Unset in normal use.
    STATE_DIR = os.environ.get("HOTAS_STATE", "/var/lib/hotas-bridge")
    INSTALL_DIR = os.environ.get("HOTAS_DST") or os.path.abspath(
        os.path.join(os.path.dirname(_INDEX), "..", ".."))

    def _version():
        """What install.sh stamped when it last deployed this tree."""
        try:
            with open(os.path.join(INSTALL_DIR, "VERSION")) as f:
                lines = f.read().split("\n")
            return {"commit": lines[0][:12], "subject": (lines[1] if len(lines) > 1 else "")}
        except OSError:
            return {"commit": None, "subject": ""}

    def _source():
        """Where updates come from. Overridable so you can run your own fork —
        the shell script reads the same file."""
        src = {"repo": DEFAULT_REPO,
               "branch": DEFAULT_BRANCH, "custom": False}
        try:
            with open(os.path.join(STATE_DIR, "update.json")) as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                if _repo_ok(cfg.get("repo", "")):
                    src["repo"] = cfg["repo"]
                if _branch_ok(cfg.get("branch", "")):
                    src["branch"] = cfg["branch"]
        except (OSError, ValueError):
            pass
        src["custom"] = (src["repo"], src["branch"]) != (DEFAULT_REPO, DEFAULT_BRANCH)
        src["default_repo"] = DEFAULT_REPO
        src["default_branch"] = DEFAULT_BRANCH
        return src

    def _update_log():
        """Everything this run has reported, including whatever happened while
        the browser was disconnected for the restart."""
        try:
            with open(os.path.join(STATE_DIR, "update.jsonl")) as f:
                out = []
                for line in f.read().split("\n")[-200:]:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
                return out
        except OSError:
            return []

    def _unit_active(unit):
        try:
            r = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip() in ("active", "activating")
        except Exception:
            return False

    class Handler(BaseHTTPRequestHandler):
        server_version = "HOTASBridge/1.0"

        # ---- auth gate ----
        def _bearer(self):
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h[:7].lower() == "bearer " else None

        def _origin_ok(self):
            """A browser only sends Origin cross-origin. We have no business
            being called that way, so any Origin at all is a refusal."""
            o = self.headers.get("Origin")
            if not o:
                return True
            host = self.headers.get("Host", "")
            return o.split("//")[-1] == host

        def _host_ok(self):
            """DNS rebinding needs a NAME to point at us. A LAN appliance is
            only ever reached by address, so names are refused."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if not host or host in ("localhost",):
                return True
            try:
                ipaddress.ip_address(host)
                return True
            except ValueError:
                return False

        def _authorised(self):
            """None if the request may proceed, else (code, message)."""
            if not self._host_ok():
                return 403, "reach this board by IP address, not by name"
            if not self._origin_ok():
                return 403, "cross-origin requests are refused"
            if auth is None:
                return None
            path = self.path.split("?")[0]
            if path in OPEN_PATHS:
                return None
            tok = self._bearer()
            if not auth.check(tok):
                return 401, "enter the PIN shown on your throttle screen"
            if path in PRIVILEGED_PATHS and not auth.stepped_up(tok):
                return 403, "stepup"
            return None

        # ---- plumbing ----
        def log_message(self, fmt, *a):        # keep journalctl readable
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else \
                json.dumps(body, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, msg):
            self._send(code, {"error": msg})

        def _body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _layout_name(self):
            mm = re.match(r"^/api/layouts/([^/]+)$", self.path)
            if not mm:
                return None
            name = mm.group(1)
            return name if _NAME_RE.match(name) else None

        # ---- GET ----
        def do_GET(self):
            deny = self._authorised()
            if deny:
                return self._err(deny[0], deny[1])
            if self.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if self.path in ("/", "/index.html"):
                try:
                    with open(_INDEX, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    return self._err(500, "web/index.html missing")

            if self.path == "/api/status":
                st = hub.snapshot()
                st["layouts"] = self._summaries()
                st["legacy"] = mgr.layouts_dir is None
                return self._send(200, st)

            if self.path == "/api/layouts":
                return self._send(200, self._summaries())

            if self.path == "/api/capture":
                if capture is None:
                    return self._err(503, "capture unavailable")
                return self._send(200, capture.status())

            if self.path == "/api/update":
                return self._send(200, {
                    "version": _version(),
                    "source": _source(),
                    "running": _unit_active("hotas-update.service")
                               or _unit_active("hotas-update-check.service"),
                    "log": _update_log(),
                })

            if self.path == "/api/auth":
                if auth is None:
                    return self._send(200, {"enabled": False})
                tok = self._bearer()
                return self._send(200, {
                    "enabled": True,
                    "stepped_up": auth.stepped_up(tok),
                    "stepup_seconds": int(auth_mod.STEPUP_S),
                    "sessions": auth.sessions(),
                })

            if self.path == "/api/codes":
                return self._send(200, {
                    **codes,
                    "targets": list(layouts_mod.AXIS_TARGETS) + [layouts_mod.SPLIT_TARGET],
                    "button_targets": list(layouts_mod.BUTTON_TARGETS) +
                                      ["select_mode1", "select_mode2", "select_mode3"],
                })

            name = self._layout_name()
            if name:
                if mgr.layouts_dir is None:
                    return self._err(400, "legacy mode: no layouts dir")
                if name not in layouts_mod.list_layouts(mgr.layouts_dir):
                    return self._err(404, f"no layout '{name}'")
                try:
                    raw = layouts_mod.load_raw(mgr.layouts_dir, name)
                    parent = raw.get("inherits")
                    return self._send(200, {
                        "raw": raw,
                        "resolved": layouts_mod.resolve(mgr.layouts_dir, name),
                        # What this layout would be if its file said nothing —
                        # i.e. purely what it inherits. {} when it has no parent.
                        "inherited": layouts_mod.resolve(mgr.layouts_dir, parent)
                                     if parent else {},
                    })
                except (ValueError, json.JSONDecodeError) as e:
                    return self._err(500, str(e))

            return self._err(404, "not found")

        # ---- PUT (save layout) ----
        def do_PUT(self):
            deny = self._authorised()
            if deny:
                return self._err(deny[0], deny[1])
            name = self._layout_name()
            if not name:
                return self._err(404, "PUT /api/layouts/<name>")
            if mgr.layouts_dir is None:
                return self._err(400, "legacy mode: no layouts dir")
            try:
                raw = self._body_json()
            except ValueError as e:
                return self._err(400, f"bad JSON: {e}")
            problems = layouts_mod.validate(mgr.layouts_dir, name, raw)
            if problems:
                return self._send(422, {"error": "validation failed",
                                        "problems": problems})
            layouts_mod.save(mgr.layouts_dir, name, raw)
            applied = None
            if mgr.m and mgr.m.name == name:      # editing the live layout
                ok, err = mgr.activate(name)
                applied = ok or err
            return self._send(200, {"saved": name, "applied": applied})

        # ---- DELETE ----
        def do_DELETE(self):
            deny = self._authorised()
            if deny:
                return self._err(deny[0], deny[1])
            if self.path == "/api/capture":
                if capture is None:
                    return self._err(503, "capture unavailable")
                capture.cancel()
                return self._send(200, {"state": "idle"})

            name = self._layout_name()
            if not name:
                return self._err(404, "DELETE /api/layouts/<name>")
            if mgr.layouts_dir is None:
                return self._err(400, "legacy mode: no layouts dir")
            if mgr.m and mgr.m.name == name:
                return self._err(409, "layout is active — switch first")
            try:
                layouts_mod.delete(mgr.layouts_dir, name)
            except (ValueError, OSError) as e:
                return self._err(409, str(e))
            return self._send(200, {"deleted": name})

        # ---- POST ----
        def do_POST(self):
            deny = self._authorised()
            if deny:
                return self._err(deny[0], deny[1])

            if self.path == "/api/auth/pin":
                if auth is None:
                    return self._err(503, "auth disabled")
                pin, secs, fresh = auth.request_pin()
                # The PIN itself is never returned over the wire — that would
                # defeat the entire point. Only how long it lasts.
                if pin is None:
                    return self._err(429, f"wait {int(secs) + 1}s for a new PIN")
                return self._send(200, {"seconds": int(secs), "fresh": fresh,
                                        "digits": auth_mod.PIN_DIGITS})

            if self.path == "/api/auth/login":
                if auth is None:
                    return self._err(503, "auth disabled")
                try:
                    pin = str(self._body_json().get("pin", ""))
                except ValueError as e:
                    return self._err(400, f"bad JSON: {e}")
                tok, err = auth.redeem(
                    pin, token=self._bearer(),
                    ip=self.client_address[0],
                    agent=self.headers.get("User-Agent", ""))
                if err:
                    return self._err(401, err)
                return self._send(200, {"token": tok,
                                        "stepup_seconds": int(auth_mod.STEPUP_S)})

            if self.path == "/api/auth/logout":
                if auth is not None:
                    auth.revoke(self._bearer())
                return self._send(200, {"ok": True})

            if self.path == "/api/update/source":
                # Pointing the board at a different repository decides what code
                # it will run, so this sits behind a fresh PIN exactly like
                # installing does. Send {"default": true} to put it back.
                try:
                    body = self._body_json()
                except ValueError as e:
                    return self._err(400, f"bad JSON: {e}")
                if _unit_active("hotas-update.service"):
                    return self._err(409, "an update is running — try again after it finishes")
                if body.get("default"):
                    repo, branch = DEFAULT_REPO, DEFAULT_BRANCH
                else:
                    repo = str(body.get("repo", "")).strip()
                    branch = str(body.get("branch", "")).strip() or DEFAULT_BRANCH
                    if not _repo_ok(repo):
                        return self._err(400, "that isn't a repository address I can use — "
                                              "it must look like https://host/owner/name")
                    if not _branch_ok(branch):
                        return self._err(400, "that isn't a usable branch name")
                try:
                    os.makedirs(STATE_DIR, exist_ok=True)
                    tmp = os.path.join(STATE_DIR, "update.json.tmp")
                    with open(tmp, "w") as f:
                        json.dump({"repo": repo, "branch": branch}, f)
                    os.replace(tmp, os.path.join(STATE_DIR, "update.json"))
                except OSError as e:
                    return self._err(500, f"could not save it: {e}")
                return self._send(200, _source())

            if self.path in ("/api/update/check", "/api/update/apply"):
                unit = ("hotas-update-check.service" if self.path.endswith("check")
                        else "hotas-update.service")
                if _unit_active("hotas-update.service") or \
                   _unit_active("hotas-update-check.service"):
                    return self._err(409, "an update is already running")
                if self.path.endswith("apply") and hub.snapshot().get("connected"):
                    # Restarting mid-match would yank the controls out from
                    # under you. Make it a deliberate act, not a surprise.
                    return self._err(409,
                        "the Xbox is connected — disconnect Oberon Remote first")
                # Open this run's log HERE, not in the script. `systemctl
                # start --no-block` returns before the unit has run a line, so
                # a browser that polls immediately would otherwise read the
                # PREVIOUS run's log — verdict and all — and believe this run
                # had already finished.
                try:
                    os.makedirs(STATE_DIR, exist_ok=True)
                    with open(os.path.join(STATE_DIR, "update.jsonl"), "w") as f:
                        f.write(json.dumps({
                            "t": int(time.time()), "step": "queued", "state": "run",
                            "msg": "Starting the updater"}) + "\n")
                except OSError:
                    pass
                try:
                    # No arguments are constructed from the request. The unit
                    # file decides entirely what runs, so there is nothing here
                    # for a caller to influence.
                    subprocess.run(["systemctl", "start", "--no-block", unit],
                                   capture_output=True, timeout=10, check=True)
                except Exception as e:
                    return self._err(500, f"could not start the updater: {e}")
                return self._send(200, {"started": unit})

            if self.path == "/api/auth/revoke-all":
                if auth is None:
                    return self._err(503, "auth disabled")
                return self._send(200, {"revoked": auth.revoke_all()})
            if self.path == "/api/capture":
                if capture is None:
                    return self._err(503, "capture unavailable")
                try:
                    kind = self._body_json().get("kind")
                except ValueError as e:
                    return self._err(400, f"bad JSON: {e}")
                if kind not in ("button", "axis"):
                    return self._err(400, "kind must be 'button' or 'axis'")
                capture.arm(kind)
                return self._send(200, capture.status())

            if self.path == "/api/activate":
                try:
                    name = self._body_json().get("name", "")
                except ValueError as e:
                    return self._err(400, f"bad JSON: {e}")
                ok, err = mgr.activate(name)
                if not ok:
                    return self._err(400, err)
                return self._send(200, {"active": name})

            if self.path == "/api/menu":
                try:
                    on = bool(self._body_json().get("on"))
                except ValueError as e:
                    return self._err(400, f"bad JSON: {e}")
                state.set_suspended(on)
                telemetry.set_menu(on)
                return self._send(200, {"menu": on})

            return self._err(404, "not found")

        # ---- helpers ----
        def _summaries(self):
            if mgr.layouts_dir is None:
                return []
            active = mgr.m.name if mgr.m else None
            # Cached against file mtimes in layouts.py — this used to reparse
            # every layout twice on every status poll.
            return [dict(e, active=e["name"] == active)
                    for e in layouts_mod.summaries(mgr.layouts_dir)]

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    def serve():
        # The service runs at Nice=-10 so the stick beats everything else, but
        # that applies to every thread — including this one and the per-request
        # threads it spawns, which inherit its nice value. Serving a status
        # page must never compete with the input path.
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (OSError, AttributeError, ValueError):
            pass
        httpd.serve_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t
