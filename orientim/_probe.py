# -*- coding: utf-8 -*-
"""Ephemeral local server used by the conformance check.

Binds to port 0 so it never collides with anything the user is running.
Nothing leaves the machine; this only exists so the patterns exercise a real
socket rather than a mock.
"""
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CRLF = bytes((13, 10))    # chunked framing, written by hand
STATE = {"calls": 0, "flaky": set(), "drift": 0, "effects": 0}


def reset():
    STATE.update(calls=0, drift=0, effects=0)
    STATE["flaky"] = set()


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._route(b"")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._route(self.rfile.read(n))

    def _route(self, body):
        p = self.path.split("?")[0]
        STATE["calls"] += 1

        if p == "/stable":
            self._send(200, {"text": "stable reply"})
        elif p == "/sampled":
            self._send(200, {"text": "reply-%d" % random.randint(1, 10 ** 9)})
        elif p == "/drift":
            STATE["drift"] += 1
            self._send(200, {"text": "v%d" % (1 if STATE["drift"] < 3 else 2)})
        elif p == "/search":
            time.sleep(random.uniform(0.004, 0.07))
            try:
                q = json.loads(body or b"{}").get("q")
            except Exception:
                q = None
            self._send(200, {"hits": q})
        elif p == "/flaky":
            k = body.decode("utf-8", "replace")[:24]
            if k not in STATE["flaky"]:
                STATE["flaky"].add(k)
                self._send(503, {"error": "transient"})
            else:
                self._send(200, {"ok": True})
        elif p == "/ratelimit":
            self._send(429 if STATE["calls"] % 3 == 1 else 200, {"rl": True})
        elif p == "/send-email":
            time.sleep(0.02)
            STATE["effects"] += 1
            self._send(200, {"sent": STATE["effects"]})
        elif p == "/stream":
            # Server-sent events with real gaps. A recorder that buffers turns
            # four arrivals into one, which is a behaviour change the agent can
            # see — so the probe measures arrivals, not just the final text.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(4):
                piece = ("data: tok%d" + chr(10) * 2) % i
                raw = piece.encode()
                self.wfile.write(b"%x" % len(raw) + CRLF + raw + CRLF)
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0" + CRLF + CRLF)
        elif p == "/slow":
            time.sleep(0.4)
            self._send(200, {"slow": True})
        else:
            self._send(404, {"error": "not found"})


class Probe:
    """Context manager: `with Probe() as base_url:`"""

    def __init__(self):
        self.srv = None
        self.base = None

    def __enter__(self):
        reset()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        port = self.srv.server_address[1]
        self.base = "http://127.0.0.1:%d" % port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        time.sleep(0.15)
        return self.base

    def __exit__(self, *exc):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        return False
