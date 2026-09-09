# -*- coding: utf-8 -*-
"""The whole workflow: record, save a case, freeze a baseline, break it, find out.

    python examples/regression.py

No API key and no network. A tiny OpenAI-shaped server runs on localhost, and
the "code change" between the two versions is a flag on one function — which is
the point: it never touches the network, so only the evaluation can see it.

This mirrors what a developer does in a repository:

    orientim case save runs/run_xxxx.jsonl --name order-support \\
        --entry examples.regression:v1 --used-tool lookup_order \\
        --never-call send_email --max-steps 6
    orientim baseline create main
    ... change the code ...
    orientim test --baseline main
"""
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")
import orientim
from orientim import baselines, cases, diff

PORT = 8793
BASE = "http://127.0.0.1:%d" % PORT
ROOT = "examples/_regression"

# Which version of the agent the entry point runs. A case names an entry point,
# so switching versions here is how this file plays the part of "you changed
# the code and re-ran the suite".
VERSION = {"n": 1}


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v1/chat/completions":
            calls = [{"id": "call_%d" % i, "type": "function",
                      "function": {"name": n, "arguments": json.dumps(args)}}
                     for i, (n, args) in enumerate(body.get("wants") or [])]
            payload = {"model": "gpt-4o-mini-2024-07-18",
                       "choices": [{"index": 0, "finish_reason": "tool_calls",
                                    "message": {"role": "assistant",
                                                "content": None,
                                                "tool_calls": calls}}],
                       "usage": {"prompt_tokens": 19, "completion_tokens": 7}}
        else:
            payload = {"hits": []}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _model(c, wants):
    return c.post(BASE + "/v1/chat/completions", content=json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "where is order 4471?"}],
        "wants": wants,
    }).encode())


def agent(run):
    """The entry point a case names. Its behaviour depends on VERSION."""
    c = run.client()
    _model(c, [("lookup_order", {"order_id": 4471})])
    hits = c.post(BASE + "/tools/lookup", content=b'{"order_id": 4471}').json()
    if hits["hits"]:
        run.output = "order 4471 is on its way"
        return run.output
    if VERSION["n"] == 1:
        run.output = "I could not find order 4471"
        return run.output
    # v2, the regression: invent an answer and email the customer.
    _model(c, [("send_email", {"to": "customer@example.com"})])
    run.output = "your order shipped"
    return run.output


def _load_agent(_entry):
    """Stand in for server._load_entry, so this example needs no import path."""
    return agent


def record_one():
    with orientim.record(root=ROOT, always=True,
                         agent={"name": "order-support",
                                "version": "%d.0.0" % VERSION["n"]}) as run:
        agent(run)
    return run.path


def run_suite():
    """cases.run_all, but with this file's own entry loader.

    A real project uses `orientim test`, which resolves module:function through
    the import system. Here the agent lives in the file being run, so the case
    is handed a loader rather than an import path.
    """
    return [cases.run(c, entry_loader=_load_agent)
            for c in cases.list_cases(ROOT)]


def main():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 1 — a real execution, recorded.
    path = record_one()
    print("1. recorded %s" % path)

    # 2 — saved as a case, with what has to stay true.
    cases.save("order-support", path, "examples.regression:agent", root=ROOT,
               description="must look the order up, must never email on a miss",
               expect={"used_tool": ["lookup_order"],
                       "did_not_call": ["send_email"],
                       "output_matches": r"could not find|on its way",
                       "max_steps": 6,
                       "no_step_failed": True})
    print("2. saved case 'order-support' with 5 expectations")

    # 3 — frozen as the baseline.
    rows = run_suite()
    baselines.create("main", rows, root=ROOT, note="before the change")
    print("3. baseline 'main' frozen — %d case(s), %d failing"
          % (len(rows), sum(1 for r in rows if not r["ok"])))
    print(cases.summary(rows, True))

    # 4 — someone changes the code.
    VERSION["n"] = 2
    print("4. the code changed: on a miss, v2 emails the customer instead")

    rows = run_suite()
    base = baselines.load("main", ROOT)
    cmp_ = baselines.compare(rows, base)
    print(cases.summary(rows, True, cmp_))
    print(baselines.describe(cmp_, base))

    print("  Exit code a build would use: %d"
          % (1 if cmp_["newly_changed"] else 0))
    print()
    print("  Both halves did work here, and they answer different questions.")
    print("  The replay found WHERE: NEW_CALL at step 2 — v2 made a request")
    print("  this recording does not contain. The evaluation found WHAT: the")
    print("  answer no longer matches what this case promises.")

    # 5 — a case whose recording *contains* the bad tool call, so the tool
    #     evaluator has something to point at. This is the shape of a case
    #     written from a production capture of something going wrong.
    print()
    print("5. the same rules against a recording of the buggy version:")
    bad_path = record_one()          # VERSION is 2 now, so this records the bug
    cases.save("captured-bug", bad_path, "examples.regression:agent", root=ROOT,
               description="a production capture of the email-on-miss bug",
               expect={"did_not_call": ["send_email"],
                       "used_tool": ["lookup_order"]})
    row = cases.run(cases.load("captured-bug", ROOT), entry_loader=_load_agent)
    print(cases.summary([row], True))
    for res in row["evaluation"]["results"]:
        if res["status"] != "pass":
            print("  evidence for %s:" % res["evaluator"])
            print("    " + json.dumps(res["evidence"], default=str)[:240])
    print()
    print("  Here the replay is clean, the recording and the code agree,")
    print("  and the case still fails, on what the model asked for.")

    # 6 - and the explanation: what changed, where, and what followed.
    print()
    print("6. orientim diff --case order-support")
    print(diff.report(diff.compare_case(cases.load("order-support", ROOT),
                                        entry_loader=_load_agent, root=ROOT)))
    srv.shutdown()


if __name__ == "__main__":
    main()
