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

    def do_POST(self):
        if self.path.split("?")[0] != "/api/replay":
            return self._json({"error": "not found"}, 404)
        if not self._authorised():
            return self._json({"error": "forbidden"}, 403)
        with LOCK:
            if STATE["running"]:
                return self._json({"error": "a replay is already running"}, 409)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        strict = bool(body.get("strict", True))
        t = threading.Thread(
            target=_run_replay,
            args=(self.ctx["path"], self.ctx["fn"], strict),
            daemon=True)
        t.start()
        self._json({"started": True, "strict": strict})


def serve(path, entry, port=8740, open_browser=True, strict=True):
    fn = _load_entry(entry)
    token = secrets.token_urlsafe(24)
    html_path = viewer.build(path, out=os.path.splitext(path)[0] + ".live.html",
                             open_browser=False, live=True, token=token)
    Handler.ctx = {"path": path, "fn": fn, "token": token,
                   "origin": "http://127.0.0.1:%d" % port,
                   "html": open(html_path, encoding="utf-8").read()}
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
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
