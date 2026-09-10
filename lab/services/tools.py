# -*- coding: utf-8 -*-
"""The tool/API layer: what the agents are allowed to do to the world.

Every tool is an HTTP endpoint, and the ones with consequences say so. Two of
them are side-effecting in the sense that matters — `/tool/email.send` and
`/tool/refund.issue` — and a replay must never fire either. Orientim blocks
that structurally: during a replay nothing is forwarded anywhere. This layer
counts its own calls so the lab can *prove* it rather than assume it.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9102
STATE = "http://127.0.0.1:%s" % (sys.argv[2] if len(sys.argv) > 2 else "9103")

# What actually happened to the outside world. The lab reads this to check that
# a replay sent no email and issued no refund.
EFFECTS = {"email.send": 0, "refund.issue": 0}
_lock = threading.Lock()


def _db(path, payload):
    req = urllib.request.Request(STATE + path,
                                 data=json.dumps(payload).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # A record that does not exist is an answer, not a server error. The
        # first version let the 404 become a 500, which made "look up an order
        # that is not there" indistinguishable from "the database is broken" —
        # and Orientim reported a failed step, correctly, for a scenario whose
        # whole point was that the fleet handles it.
        if e.code == 404:
            return {"found": False, "error": "not found", **payload}
        raise


# --- the tools ----------------------------------------------------------------

def order_lookup(args):
    row = _db("/db/order", {"order_id": args.get("order_id", 0)})
    if row.get("found") is False:
        return row
    if args.get("include_history"):
        # A second field the caller can ask for. Changing whether an agent asks
        # for it is a tool-argument regression with a visible consequence.
        row["history"] = ["created", "paid", row.get("status", "?")]
    return row


def kb_search(args):
    return _db("/db/articles", {"q": args.get("q", "")})


def risk_score(args):
    """A deterministic score, so a risk decision is reproducible."""
    total = float(args.get("total", 0))
    country = args.get("country", "??")
    days = int(args.get("opened_days", 0))
    score = 0
    score += 40 if total >= 1000 else (15 if total >= 300 else 0)
    score += 35 if country in ("NG", "RU") else 0
    score += 20 if days <= 1 else 0
    band = "high" if score >= 60 else ("medium" if score >= 25 else "low")
    return {"risk": band, "score": score,
            "reasons": {"total": total, "country": country,
                        "opened_days": days}}


def shipping_track(args):
    row = _db("/db/order", {"order_id": args.get("order_id", 0)})
    status = row.get("status", "unknown")
    scans = {"shipped": 3, "delivered": 5, "processing": 1, "lost": 2}
    return {"order_id": row.get("order_id"), "status": status,
            "scans": scans.get(status, 0)}


def email_send(args):
    """A real side effect. Counted, so a replay can be proved not to fire it."""
    with _lock:
        EFFECTS["email.send"] += 1
    _db("/db/event", {"kind": "email", "payload": {"to": args.get("to")}})
    return {"sent": True, "to": args.get("to"), "n": EFFECTS["email.send"]}


def refund_issue(args):
    with _lock:
        EFFECTS["refund.issue"] += 1
    _db("/db/event", {"kind": "refund",
                      "payload": {"order_id": args.get("order_id")}})
    return {"refunded": True, "order_id": args.get("order_id"),
            "n": EFFECTS["refund.issue"]}


TOOLS = {
    "order.lookup": order_lookup,
    "kb.search": kb_search,
    "risk.score": risk_score,
    "shipping.track": shipping_track,
    "email.send": email_send,
    "refund.issue": refund_issue,
}

# Tools no agent may call without a human. The lab asserts this with
# `did_not_call`, which is the evaluator that exists for exactly this.
FORBIDDEN_FOR_AGENTS = ("refund.issue",)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?")[0] == "/tool/_effects":
            return self._send(200, dict(EFFECTS))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            args = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})

        if p == "/tool/_effects/reset":
            with _lock:
                for k in EFFECTS:
                    EFFECTS[k] = 0
            return self._send(200, dict(EFFECTS))

        name = p[len("/tool/"):] if p.startswith("/tool/") else ""
        fn = TOOLS.get(name)
        if fn is None:
            return self._send(404, {"error": "no such tool", "tool": name})
        try:
            return self._send(200, fn(args))
        except Exception as e:
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})


if __name__ == "__main__":
    print("tools on %d" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
