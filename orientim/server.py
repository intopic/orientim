# -*- coding: utf-8 -*-
"""Local server that connects the timeline to a real replay.

Runs on the user's machine. Pressing Replay executes the agent again, fed by
the recording, and the timeline lights up as it happens. No network call
leaves: every response comes from the recorded file.
"""
import importlib
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import session, store, viewer

# The real clock, captured before anything can patch it.
#
# A replay shims time.time() process-wide, and this server is a *different*
# thread in the same process. Every response it sends carries a Date header,
# which BaseHTTPRequestHandler computes with time.time() — so the server's own
# bookkeeping was consuming the agent's recorded clock entries, and a replay
# started from the live view came back UNCAPTURED_CLOCK for any agent that
# reads the clock. Holding the original function bypasses the patch.
_REAL_TIME = time.time

STATE = {
    "running": False,
    "events": [],
    "done": False,
    "result": None,
    "started": 0.0,
}
LOCK = threading.Lock()


def _load_entry(spec):
    """'module:function' -> the callable.

    The documented workflow is `orientim view <run> --entry myapp.agent:run`,
    run from the project root. Installed as a console script, sys.path[0] is the
    script's directory and not the working directory, so without this the
    documented command failed with ModuleNotFoundError on the user's own module.
    """
    mod_name, _, fn_name = spec.partition(":")
    if not fn_name:
        raise ValueError("expected module:function, e.g. myapp.agent:run")
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    mod = importlib.import_module(mod_name)
    return getattr(mod, fn_name)


def _run_replay(path, fn, strict):
    def on_step(ev):
        ev["t"] = (time.monotonic() - STATE["started"]) * 1000.0
        with LOCK:
            STATE["events"].append(ev)

    with LOCK:
        STATE.update(running=True, events=[], done=False, result=None,
                     started=time.monotonic())
    try:
        d = session.replay(path, fn, strict=strict, on_step=on_step)
        code, title, msg, action = d.diagnosis
        res = {
            "code": code, "title": title, "message": msg, "action": action,
            "ok": bool(d.ok),
            "index": d.index,
            "reason": d.reason,
            "root_recorded": d.recorded_root[:16],
            "root_replay": d.replay_root[:16],
            "blocked": len(d.blocked),
            "uncaptured": d.uncaptured,
        }
    except Exception as e:
        res = {"code": "REPLAY_ERROR", "title": "Replay raised an error",
               "message": f"{type(e).__name__}: {e}", "action": "",
               "ok": False, "index": None, "reason": str(e),
               "root_recorded": "", "root_replay": "", "blocked": 0, "uncaptured": []}
    with LOCK:
        STATE.update(running=False, done=True, result=res)


class Handler(BaseHTTPRequestHandler):
    ctx = {}

    def log_message(self, *a):
        pass

    def date_time_string(self, timestamp=None):
        """The Date header, without touching the clock a replay is shimming.

        The base implementation calls time.time() when no timestamp is given.
        Passing one keeps this server out of the agent's recorded sequence.
        """
        if timestamp is None:
            timestamp = _REAL_TIME()
        return BaseHTTPRequestHandler.date_time_string(self, timestamp)

    def _authorised(self):
        """The page holds a token; nothing else does.

        This server executes the user's agent on request. Without a check, any
        website open in the same browser could POST to 127.0.0.1 and start a
        replay. A custom header cannot be sent cross-origin without a preflight
        this server never answers, and the token closes the gap regardless.
        """
        if self.headers.get("X-Orientim-Token") != self.ctx.get("token"):
            return False
        origin = self.headers.get("Origin")
        return origin in (None, self.ctx.get("origin"))

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            html = self.ctx["html"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif p == "/api/progress":
            if not self._authorised():
                return self._json({"error": "forbidden"}, 403)
            with LOCK:
                self._json({"running": STATE["running"], "done": STATE["done"],
                            "events": list(STATE["events"]), "result": STATE["result"]})
        else:
            self._json({"error": "not found"}, 404)

    # A request body nobody asked for is still a body somebody sent. Bounded,
    # because this drains before authorising and an unauthenticated caller must
    # not be able to make the process read as much as it likes.
    MAX_BODY = 64 * 1024

    def _body(self):
        """Read the request body before answering, whatever the answer is.

        Responding without draining leaves unread bytes in the socket, and the
        client sees a connection reset instead of the status that was sent. The
        refusal path is exactly where that matters: a person told "forbidden"
        can act on it, and a person shown a network error cannot.
        """
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(min(n, self.MAX_BODY))

    def do_POST(self):
        raw = self._body()
        if self.path.split("?")[0] != "/api/replay":
            return self._json({"error": "not found"}, 404)
        if not self._authorised():
            return self._json({"error": "forbidden"}, 403)
        with LOCK:
            if STATE["running"]:
                return self._json({"error": "a replay is already running"}, 409)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "body is not JSON"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "body must be an object"}, 400)
        strict = bool(body.get("strict", True))
        t = threading.Thread(
            target=_run_replay,
            args=(self.ctx["path"], self.ctx["fn"], strict),
            daemon=True)
        t.start()
        self._json({"started": True, "strict": strict})


def build_server(path, entry, port=8740, strict=True):
    """Everything `serve` does except blocking, so it can be started and stopped.

    Split out for one reason: `serve` calls serve_forever, so the only way to
    exercise the live-replay endpoint was not to. A feature whose test would
    have to reimplement it is a feature with no test.

    port=0 asks the OS for a free one, and the real port is read back from the
    socket, so the CSRF origin and the printed URL name the port actually bound
    rather than the one that was requested.
    """
    fn = _load_entry(entry)
    token = secrets.token_urlsafe(24)
    html_path = viewer.build(path, out=os.path.splitext(path)[0] + ".live.html",
                             open_browser=False, live=True, token=token)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    bound = srv.server_address[1]
    Handler.ctx = {"path": path, "fn": fn, "token": token,
                   "origin": "http://127.0.0.1:%d" % bound,
                   "html": open(html_path, encoding="utf-8").read(),
                   "strict": strict}
    return srv, "http://127.0.0.1:%d/" % bound, token


def serve(path, entry, port=8740, open_browser=True, strict=True):
    srv, url, _token = build_server(path, entry, port=port, strict=strict)
    meta, _ = store.load(path)
    print(f"  Orientim — {meta['run_id']}")
    print(f"  entry:  {entry}")
    print(f"  open:   {url}")
    print("  Ctrl+C to stop\n")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("  stopped")
    finally:
        srv.shutdown()
