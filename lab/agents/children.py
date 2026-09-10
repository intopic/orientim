# -*- coding: utf-8 -*-
"""The three child agents, each an HTTP service that records every request.

Research, Support and Risk. One file because they share the serving shape and
differ only in what they do; the differences are where the regressions live.

Each wraps its request handler in `orientim.record()` — the pattern the docs
prescribe for a long-lived service — so every delegation the supervisor makes
produces a recording on the child's side as well as a step on the supervisor's.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.environ.get("LAB_REPO", ".")))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orientim
from agents import common as C


# --- research -----------------------------------------------------------------

def research(run, task):
    """Looks things up in the knowledge base, then in shipping.

    v1 asks the knowledge base and then tracks the parcel. The `call_removed`
    variant drops the tracking call; `order_change` swaps the two.
    """
    c = C.client(run)
    order_id = task.get("order_id", 0)
    plan = [[("kb.search", {"q": task.get("topic", "refund")})],
            [("shipping.track", {"order_id": order_id})]]

    if C.on("call_removed"):
        plan = plan[:1]                                   # regression 8
    elif C.on("order_change"):
        plan = [plan[1], plan[0]]                         # regression 9
    elif C.on("new_call"):
        plan = plan + [[("order.lookup", {"order_id": order_id})]]   # 7

    answer, used = C.loop(c, "research order %s" % order_id, plan,
                          tools=["kb.search", "shipping.track", "order.lookup"])
    return {"agent": "research", "answer": answer,
            "tools": [u["tool"] for u in used]}


# --- support ------------------------------------------------------------------

def support(run, task):
    """Answers the customer.

    v1 looks the order up and answers plainly. Four regressions live here:
    a different tool, different arguments, a forbidden tool, and a changed
    answer format.
    """
    c = C.client(run)
    order_id = task.get("order_id", 0)

    lookup = ("kb.search", {"q": "refund policy"}) if C.on("tool_change") else \
             ("order.lookup", {"order_id": order_id})                  # 2
    if C.on("arg_change") and lookup[0] == "order.lookup":
        lookup = ("order.lookup", {"order_id": order_id,
                                   "include_history": True})           # 3

    plan = [[lookup]]
    if C.on("forbidden_tool"):
        # regression 5: an agent must never issue a refund on its own.
        plan.append([("refund.issue", {"order_id": order_id})])

    style = "formal" if C.on("output_change") else "plain"             # 6
    answer, used = C.loop(c, "help the customer with order %s" % order_id,
                          plan, style=style,
                          tools=["order.lookup", "kb.search", "refund.issue"])
    return {"agent": "support", "answer": answer,
            "tools": [u["tool"] for u in used]}


# --- risk ---------------------------------------------------------------------

def risk(run, task):
    """Scores the order for fraud.

    v1 looks the order up and scores it. `tool_unused` drops the scoring call
    entirely, which is the regression where an agent silently stops doing the
    safety check it exists for.
    """
    c = C.client(run)
    order_id = task.get("order_id", 0)
    row = C.call_tool(c, "order.lookup", {"order_id": order_id})

    if C.on("tool_unused"):                                            # 4
        return {"agent": "risk", "answer": "skipped", "tools": ["order.lookup"]}

    plan = [[("risk.score", {"total": row.get("total", 0),
                             "country": row.get("country", "??"),
                             "opened_days": row.get("opened_days", 0)})]]
    answer, used = C.loop(c, "assess order %s" % order_id, plan,
                          tools=["risk.score"])
    return {"agent": "risk", "answer": answer,
            "tools": ["order.lookup"] + [u["tool"] for u in used]}


AGENTS = {"research": research, "support": support, "risk": risk}


# --- serving ------------------------------------------------------------------

def make_handler(name, fn, root):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                return self._send(200, {"ok": True, "agent": name,
                                        "variant": C.variant()})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if self.path.split("?")[0] != "/run":
                return self._send(404, {"error": "not found"})
            try:
                task = json.loads(raw or b"{}")
            except ValueError:
                return self._send(400, {"error": "body is not JSON"})

            # The documented pattern: wrap the handler, not the server.
            with orientim.record(root=root, always=True,
                                 tags={"agent": name,
                                       "scenario": task.get("scenario", "?")},
                                 agent={"name": name,
                                        "version": C.variant()}) as run:
                out = fn(run, task)
                run.output = json.dumps(out, sort_keys=True)

            # Deliberately NOT returning our run id. A downstream agent that
            # puts its own correlation id in the payload makes every upstream
            # recording differ on every run — a trap this lab documents rather
            # than walks into. See lab/README.md.
            return self._send(200, out)

        def _send(self, code, obj):
            body = json.dumps(obj, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


if __name__ == "__main__":
    name = sys.argv[1]
    port = int(sys.argv[2])
    root = os.environ.get("LAB_RUNS", "lab/_runs")
    root = os.path.join(root, name)
    fn = AGENTS[name]
    print("%s agent on %d, recording to %s, variant %s"
          % (name, port, root, C.variant()), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port),
                        make_handler(name, fn, root)).serve_forever()
