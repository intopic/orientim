# -*- coding: utf-8 -*-
"""The supervisor: plans, delegates, and runs two children at once.

This is the agent the lab records. It calls the model to decide what the task
needs, then delegates over HTTP — Research and Risk **in parallel**, Support
after them, because Support needs what the other two found.

The parallel pair is the point of regression 10. Two concurrent HTTP calls
return in whatever order the scheduler allows, so v1 staggers them
deliberately: research is asked first and given a head start, which makes the
recorded order stable. `parallel_order` removes the stagger and starts risk
first, and the recorded order flips. Without the stagger every run would differ for
no reason and the experiment would measure the scheduler rather than the code.
"""
import concurrent.futures as cf
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.environ.get("LAB_REPO", ".")))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orientim
from agents import common as C

# How far ahead the first parallel child starts. Small enough not to slow the
# lab down, large enough that the order is the code's decision and not the
# operating system's.
STAGGER = float(os.environ.get("LAB_STAGGER", "0.25"))


def delegate(client, url, task):
    return C.post_json(client, url + "/run", task)


def run_task(run, task):
    """The supervisor's own execution loop.

    1. ask the model what this task needs
    2. run Research and Risk in parallel
    3. hand both to Support
    4. ask the model to write the answer
    """
    c = C.client(run)
    order_id = task.get("order_id", 4471)
    scenario = task.get("scenario", "?")

    # 1 — plan. The model asks for one cheap tool so the plan itself is a real
    # model call with a real tool result, not a formality.
    plan_answer, _used = C.loop(
        c, "plan the handling of order %s" % order_id,
        [[("order.lookup", {"order_id": order_id})]],
        tools=["order.lookup"])

    # 2 — two children, at the same time.
    first, second = ("research", C.RESEARCH), ("risk", C.RISK)
    if C.on("parallel_order"):                                       # 10
        first, second = second, first

    results = {}
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        futures[pool.submit(delegate, c, first[1],
                            {"order_id": order_id, "scenario": scenario,
                             "topic": task.get("topic", "refund")})] = first[0]
        time.sleep(STAGGER)
        futures[pool.submit(delegate, c, second[1],
                            {"order_id": order_id, "scenario": scenario,
                             "topic": task.get("topic", "refund")})] = second[0]
        for fut in cf.as_completed(futures):
            results[futures[fut]] = fut.result()

    # 3 — support, with what the others found.
    results["support"] = delegate(c, C.SUPPORT, {
        "order_id": order_id, "scenario": scenario,
        "risk": results.get("risk", {}).get("answer"),
        "research": results.get("research", {}).get("answer")})

    # 4 — the model writes the customer-facing answer from the children's work.
    facts = [{"status": results["support"]["answer"]},
             {"risk": results.get("risk", {}).get("answer", "unknown")}]
    final, _ = C.loop(
        c, "summarise for the customer: %s" % json.dumps(facts, sort_keys=True),
        [], tools=[])

    return {
        "order_id": order_id,
        "scenario": scenario,
        "plan": plan_answer,
        "children": {k: results[k]["answer"] for k in sorted(results)},
        "tools_used": sorted({t for r in results.values()
                              for t in r.get("tools", [])}),
        "answer": final or results["support"]["answer"],
    }


# --- the entry point cases point at ------------------------------------------

def handle(run):
    """What `orientim case --entry lab.agents.supervisor:handle` runs.

    The task comes from the environment so the entry point takes no arguments,
    which is what a case needs. LAB_TASK is set by the recorder and by
    `orientim test`.
    """
    task = json.loads(os.environ.get("LAB_TASK") or '{"order_id": 4471}')
    out = run_task(run, task)
    run.output = json.dumps(out, sort_keys=True)
    return run.output


class Handler(BaseHTTPRequestHandler):
    root = "lab/_runs/supervisor"

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
        if self.path.split("?")[0] == "/health":
            return self._send(200, {"ok": True, "agent": "supervisor",
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

        with orientim.record(root=self.root, always=True,
                             tags={"agent": "supervisor",
                                   "scenario": task.get("scenario", "?")},
                             agent={"name": "supervisor",
                                    "version": C.variant()}) as run:
            out = run_task(run, task)
            run.output = json.dumps(out, sort_keys=True)
        out["_recording"] = run.path
        return self._send(200, out)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else C.SUPERVISOR_PORT
    Handler.root = os.path.join(os.environ.get("LAB_RUNS", "lab/_runs"),
                                "supervisor")
    print("supervisor on %d, recording to %s, variant %s"
          % (port, Handler.root, C.variant()), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
