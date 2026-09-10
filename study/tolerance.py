# -*- coding: utf-8 -*-
"""How far does an agent get past a divergence, and what does that buy?

    python study/tolerance.py

Nine of ten lab regressions produced a single step and lost every evaluator
downstream. That was read as a property of replay. It is not: an unmatched
request is answered with a real `httpx.Response(599)` carrying a JSON body, and
control returns to the agent. The lab agents then do

    r.json()["choices"][0]["message"]

and raise KeyError on the divergence body. **The agent stops itself.**

So the collapse is at least partly a fact about three agents I wrote, and the
honest thing is to measure it rather than design around it. Two variables:

    posture   what the agent does with an error - crash, check, retry, tolerate
    shape     whether the next request depends on the model's answer

    model-driven   the agent reads tool_calls out of the response and calls
                   what it was told to. Without a response there is nothing to
                   do, and tolerance may not help at all.
    plan-driven    the sequence of calls is fixed in code and the model fills
                   in content. The steps after a failed model call are the
                   same bytes either way, so they can still match.

Recorded on v1, replayed on v2, where v2 changes only the first model request -
the same shape as every lab regression. What is measured is how many recorded
steps the replay reaches, whether an answer is produced at the end, and how
many evaluators can be answered rather than degraded.

Nothing here touches Orientim.
"""
import json
import os
import shutil
import sys
import threading
import time as _rt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orientim                                        # noqa: E402
from orientim import diff, evaluate, session, store     # noqa: E402

PORT = 9312
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_runs", "tol")

# The fixed pipeline a plan-driven agent walks whatever the model says.
PLAN = [("order.lookup", {"order_id": 4471}),
        ("shipping.track", {"order_id": 4471})]


class Provider(BaseHTTPRequestHandler):
    """Answers model calls with a tool request, and tool calls with data."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if "/v1/chat/completions" in self.path:
            try:
                asked = json.loads(raw or b"{}")
            except Exception:
                asked = {}
            turn = asked.get("turn", 0)
            calls = ([{"id": "c1", "type": "function",
                       "function": {"name": PLAN[0][0],
                                    "arguments": json.dumps(PLAN[0][1])}}]
                     if turn == 0 else
                     [{"id": "c2", "type": "function",
                       "function": {"name": PLAN[1][0],
                                    "arguments": json.dumps(PLAN[1][1])}}]
                     if turn == 1 else [])
            msg = {"role": "assistant", "content": "ok"}
            if calls:
                msg["tool_calls"] = calls
            payload = {"id": "chatcmpl-fixed", "model": "gpt-4o-mini",
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": msg}],
                       "usage": {"prompt_tokens": 8, "completion_tokens": 2,
                                 "total_tokens": 10}}
        else:
            payload = {"ok": True, "path": self.path, "value": "delivered"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# --- postures -----------------------------------------------------------------
#
# Each returns the assistant message, or a sentinel. The difference between them
# is only what happens when the response is not the shape they expected, which
# is the entire subject of this file.

DEGRADED = {"role": "assistant", "content": "", "tool_calls": []}


def p_naive(post, payload):
    """What the lab agents do. Reads the shape it expects and nothing else."""
    return post(payload).json()["choices"][0]["message"]


def p_checked(post, payload):
    """Looks at the status and gives up cleanly, which is still giving up."""
    r = post(payload)
    if r.status_code >= 400:
        raise RuntimeError("model call failed with %d" % r.status_code)
    return r.json()["choices"][0]["message"]


def p_retry(post, payload):
    """Two more attempts, then the same clean surrender."""
    for _ in range(3):
        r = post(payload)
        if r.status_code < 400:
            return r.json()["choices"][0]["message"]
    raise RuntimeError("model call failed after 3 attempts")


def p_tolerant(post, payload):
    """Carries on without an answer. The posture under test."""
    r = post(payload)
    if r.status_code >= 400:
        return dict(DEGRADED)
    try:
        return r.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError):
        return dict(DEGRADED)


POSTURES = [("naive", p_naive), ("status check", p_checked),
            ("retry x3", p_retry), ("tolerant", p_tolerant)]


# --- shapes -------------------------------------------------------------------

def make_agent(shape, posture, variant):
    """v1 and v2 differ in one field of the first model request, and nowhere
    else - the same shape as every regression the lab injects."""

    def agent(run):
        c = run.client()

        def post(payload):
            return c.post(BASE + "/v1/chat/completions",
                          content=json.dumps(payload).encode())

        def tool(name, args):
            return c.post(BASE + "/" + name,
                          content=json.dumps(args).encode())

        first = {"model": "gpt-4o-mini", "turn": 0,
                 "messages": [{"role": "user", "content": "where is 4471"}]}
        if variant == "v2":
            first["include_history"] = True          # the whole change

        msg = posture(post, first)

        if shape == "plan-driven":
            # The model is consulted; the pipeline does not depend on it.
            for name, args in PLAN:
                tool(name, args)
            posture(post, {"model": "gpt-4o-mini", "turn": 2,
                           "messages": [{"role": "user",
                                         "content": "summarise"}]})
            run.output = "done: %d step(s) planned" % len(PLAN)
            return

        # model-driven: everything after the first call is read out of it.
        turn = 1
        for call in (msg or {}).get("tool_calls") or []:
            fn = call.get("function") or {}
            tool(fn.get("name", "?"), json.loads(fn.get("arguments") or "{}"))
            nxt = posture(post, {"model": "gpt-4o-mini", "turn": turn,
                                 "messages": [{"role": "user",
                                               "content": "next"}]})
            turn += 1
            for c2 in (nxt or {}).get("tool_calls") or []:
                f2 = c2.get("function") or {}
                tool(f2.get("name", "?"),
                     json.loads(f2.get("arguments") or "{}"))
        run.output = "done: %d tool(s) run" % (turn - 1)

    return agent


# --- measurement ----------------------------------------------------------------

EXPECT = [evaluate.used_tool(PLAN[0][0]),
          evaluate.did_not_call("refund.issue"),
          evaluate.output_matches("done"),
          evaluate.no_step_failed()]


def answerable(d):
    """How many of four evaluators the replay can actually answer."""
    steps = diff.merge_unmatched(d.replay_steps, d.unmatched_requests)
    ex = evaluate.Execution.of({"outcome": d.replay_output}, steps)
    rep = evaluate.evaluate(ex, EXPECT)
    return (len(rep.results) - len(rep.warnings), len(rep.results),
            [r.evaluator for r in rep.warnings])


def measure(shape, posture_name, posture):
    root = os.path.join(ROOT, "%s-%s" % (shape, posture_name.replace(" ", "-")))
    if os.path.isdir(root):
        shutil.rmtree(root)
    row = {"shape": shape, "posture": posture_name}

    try:
        with orientim.record(root=root, always=True) as run:
            make_agent(shape, posture, "v1")(run)
        path = run.path
    except Exception as e:
        row["verdict"] = "RECORD_ERROR: %s" % type(e).__name__
        return row

    _meta, recorded = store.load(path)
    row["recorded"] = len([s for s in recorded if s.get("t") == "http"])

    try:
        d = session.replay(path, make_agent(shape, posture, "v2"), strict=True)
    except Exception as e:
        row["verdict"] = "REPLAY_RAISED: %s" % type(e).__name__
        row["reached"] = 0
        row["answered"] = 0
        return row

    steps = diff.merge_unmatched(d.replay_steps, d.unmatched_requests)
    matched = [s for s in steps if not s.get("unmatched")]
    ans, total, warned = answerable(d)
    row.update({
        "verdict": d.diagnosis[0] if not d.ok else "IDENTICAL",
        "reached": len(matched),
        "unmatched": len(steps) - len(matched),
        "answer": bool(d.replay_output),
        "answered": ans, "of": total, "degraded": warned,
    })
    return row


# --- where the divergence lands -------------------------------------------------
#
# The first table showed a tolerant plan-driven agent making four requests and
# matching none of them, although those four were byte-identical to what was
# recorded. That points at the matcher rather than the agent.
#
# `_take_ordered` claims only the *head* of the recorded queue. When a request
# does not match the head, the cursor does not advance - so every later request
# is compared against a step that has already been passed by, and can never
# match. If that is the mechanism, then where the divergence lands decides
# exactly how much of the run remains observable, and a tolerant agent buys
# nothing after the first miss.
#
# A straight-line agent of six steps, with one step's body changed, tests it.

def straight(n_steps, change_at=None):
    def agent(run):
        c = run.client()
        for i in range(n_steps):
            payload = {"step": i, "q": "4471"}
            if change_at == i:
                payload["changed"] = True
            c.post(BASE + "/tool/step%d" % i,
                   content=json.dumps(payload).encode())
        run.output = "done"
    return agent


def where_it_lands(n_steps=6):
    rows = []
    for k in range(n_steps):
        root = os.path.join(ROOT, "cursor-%d" % k)
        if os.path.isdir(root):
            shutil.rmtree(root)
        with orientim.record(root=root, always=True) as run:
            straight(n_steps)(run)
        t0 = _rt.monotonic()
        d = session.replay(run.path, straight(n_steps, change_at=k),
                           strict=True)
        secs = _rt.monotonic() - t0
        steps = diff.merge_unmatched(d.replay_steps, d.unmatched_requests)
        matched = len([s for s in steps if not s.get("unmatched")])
        rows.append({"changed_at": k, "of": n_steps, "matched": matched,
                     "unmatched": len(steps) - matched,
                     "seconds": round(secs, 1),
                     "verdict": d.diagnosis[0] if not d.ok else "IDENTICAL"})
    return rows


def main():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    rows = []
    print()
    print("  HOW FAR PAST A DIVERGENCE?   recorded on v1, replayed on v2")
    print("  " + "-" * 88)
    print("  %-13s %-14s %-8s %-9s %-8s %-8s %s"
          % ("shape", "posture", "recorded", "reached", "unmatch", "answer",
             "evaluators answered"))
    print("  " + "-" * 88)
    for shape in ("model-driven", "plan-driven"):
        for name, posture in POSTURES:
            r = measure(shape, name, posture)
            rows.append(r)
            print("  %-13s %-14s %-8s %-9s %-8s %-8s %s"
                  % (shape, name, r.get("recorded", "-"),
                     r.get("reached", "-"), r.get("unmatched", "-"),
                     "yes" if r.get("answer") else "no",
                     "%s of %s%s" % (r.get("answered", "-"), r.get("of", "-"),
                                     (" (degraded: %s)"
                                      % ", ".join(r.get("degraded") or []))
                                     if r.get("degraded") else "")))
        print()

    print("  WHERE THE DIVERGENCE LANDS   six identical steps, one body changed")
    print("  " + "-" * 88)
    print("  %-13s %-10s %-11s %-9s %s"
          % ("changed at", "matched", "unmatched", "seconds", "verdict"))
    print("  " + "-" * 88)
    landed = where_it_lands()
    for r in landed:
        print("  step %-8d %-10s %-11s %-9s %s"
              % (r["changed_at"], "%d of %d" % (r["matched"], r["of"]),
                 r["unmatched"], r["seconds"], r["verdict"]))
    print()
    consistent = all(r["matched"] == r["changed_at"] for r in landed)
    print("  matched == steps before the change, every time: %s" % consistent)
    print("  -> a request that does not match the head leaves the cursor where")
    print("     it was, so nothing recorded after it can ever be claimed.")
    print()

    srv.shutdown()

    best = max((r for r in rows if "reached" in r),
               key=lambda r: (r["reached"], r.get("answered", 0)), default=None)
    naive = [r for r in rows if r["posture"] == "naive"]
    print("  naive agents reached: %s of their recorded steps"
          % ", ".join("%s/%s" % (r.get("reached", "-"), r.get("recorded", "-"))
                      for r in naive))
    if best:
        print("  furthest any posture got: %s, %s - %d of %d steps, %d of %d "
              "evaluators answered"
              % (best["shape"], best["posture"], best["reached"],
                 best["recorded"], best.get("answered", 0), best.get("of", 0)))
    print()
    with open(os.path.join(ROOT, "_tolerance.json"), "w",
              encoding="utf-8") as f:
        json.dump({"postures": rows, "where_it_lands": landed}, f, indent=2)
    print("  written to %s" % os.path.join(ROOT, "_tolerance.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
