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

No auth: everyone on your LAN can edit layouts and poke menu mode. That is
the point of the tool — don't expose the port to the internet.
"""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import layouts as layouts_mod

try:
    from evdev import ecodes as _ecodes
except ImportError:
    _ecodes = None

_INDEX = os.path.join(os.path.dirname(__file__), "web", "index.html")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")


def _known_codes():
    """evdev names worth offering in the editor's dropdowns."""
    if _ecodes is None:
        return {"axes": [], "buttons": []}
    axes = sorted(n for n in _ecodes.ecodes if n.startswith("ABS_"))
    btns = sorted(n for n in _ecodes.ecodes if n.startswith("BTN_"))
    return {"axes": axes, "buttons": btns}


def start(port, hub, mgr, state, telemetry, capture=None):
    """Start the web server in a daemon thread. Returns the thread."""

    codes = _known_codes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "HOTASBridge/1.0"

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
