# -*- coding: utf-8 -*-
"""Asking a recorded run a question, and getting evidence back.

    python examples/evaluation.py

No API key and no network: a tiny OpenAI-shaped server runs on localhost, so
this is about what a recording can answer rather than about your credentials.

The agent below has a bug on purpose. It is told to look an order up and answer
from what it finds; when the lookup comes back empty it invents an answer and
emails the customer instead. Both are things a replay alone cannot tell you,
because the HTTP is identical in either case — it is *which tools the model
asked for* that differs.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")
import orientim
from orientim import evaluate as ev

PORT = 8792
BASE = "http://127.0.0.1:%d" % PORT


class Provider(BaseHTTPRequestHandler):
    """An inference endpoint that answers with tool calls, and the tools."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v1/chat/completions":
            wants = body.get("wants") or []
            payload = {
                "model": "gpt-4o-mini-2024-07-18",
                "choices": [{"index": 0, "finish_reason": "tool_calls",
                             "message": {"role": "assistant", "content": None,
                                         "tool_calls": [
                                             {"id": "call_%d" % i,
                                              "type": "function",
                                              "function": {
                                                  "name": name,
                                                  "arguments": json.dumps(args)}}
                                             for i, (name, args) in enumerate(wants)]}}],
                "usage": {"prompt_tokens": 21, "completion_tokens": 11},
            }
        else:
            payload = {"hits": []}          # the lookup finds nothing
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def ask_model(client, wants):
    return client.post(BASE + "/v1/chat/completions", content=json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "where is order 4471?"}],
        "tools": [{"type": "function", "function": {"name": n}}
                  for n in ("lookup_order", "send_email")],
        "wants": wants,
    }).encode())


def agent(run, honest):
    c = run.client()
    ask_model(c, [("lookup_order", {"order_id": 4471})])
    hits = c.post(BASE + "/tools/lookup", content=b'{"order_id": 4471}').json()

    if hits["hits"]:
        return "order 4471 is on its way"
    if honest:
        return "I could not find order 4471"
    # The bug: invent an answer, and email it to the customer.
    ask_model(c, [("send_email", {"to": "customer@example.com",
                                  "body": "your order shipped"})])
    return "your order shipped"


def run_once(honest):
    with orientim.record(root="examples/_runs", always=True,
                         agent={"name": "order-support",
                                "version": "2.0.0" if honest else "1.0.0"}) as run:
        run.output = agent(run, honest)
    return run.path


# The properties this agent is supposed to have, written once and applied to
# every version of it.
RULES = [
    ev.used_tool("lookup_order"),                    # it must actually look
    ev.did_not_call("send_email"),                   # it must not email on a miss
    ev.output_matches(r"could not find|on its way"),  # it must not invent
    ev.max_steps(6),
    ev.no_step_failed(),
    ev.check(lambda x: all((c.get("arguments") or {}).get("order_id") == 4471
                           for c in x.calls_named("lookup_order")),
             name="looked_up_the_right_order"),
]


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    for label, honest in (("v1.0.0, the buggy one", False),
                          ("v2.0.0, after the fix", True)):
        path = run_once(honest)
        report = ev.evaluate(path, RULES)
        print()
        print(label)
        print(report.report())

    print()
    print("The evidence, for the rule that fails:")
    report = ev.evaluate(run_once(False), RULES)
    for r in report.failed:
        print("  %s: %s" % (r.evaluator, r.reason))
        print("    %s" % json.dumps(r.evidence, default=str)[:300])

    print()
    print("Both runs replay IDENTICAL against their own recording — the HTTP")
    print("did not lie. What differs is which tool the model asked for.")
    srv.shutdown()


if __name__ == "__main__":
    main()
