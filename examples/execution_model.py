# -*- coding: utf-8 -*-
"""What format 4 records, and the one thing it catches that format 3 could not.

Run it:

    python examples/execution_model.py

No API key, no network: it starts a tiny OpenAI-shaped server on localhost so
the example is about the execution model rather than about your credentials.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")
import orientim
from orientim import store

PORT = 8791
BASE = "http://127.0.0.1:%d" % PORT


class Provider(BaseHTTPRequestHandler):
    """An inference endpoint and a tool endpoint. Both perfectly deterministic."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v1/chat/completions":
            asked = json.loads(body or b"{}").get("model", "?")
            payload = {
                "model": asked + "-2024-07-18",
                "choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "order 4471 shipped monday"}}],
                "usage": {"prompt_tokens": 24, "completion_tokens": 9},
            }
        else:
            payload = {"status": "shipped", "day": "monday"}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def agent(run, v2=False):
    """A two-step agent: look the order up, then ask the model to phrase it."""
    c = run.client()
    c.post(BASE + "/tools/order-status", content=b'{"order": 4471}')
    answer = c.post(BASE + "/v1/chat/completions", content=json.dumps({
        "model": "gpt-4o-mini",
        "temperature": 0.2,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "phrase the order status"}],
        "tools": [{"type": "function", "function": {"name": "order_status"}}],
    }).encode()).json()["choices"][0]["message"]["content"]

    # Version 2 formats the answer differently. Ordinary post-processing — a
    # template, a parse, a truncation — the kind of change that lands in a
    # normal pull request. It sends no request and reads no clock, so neither
    # the HTTP boundary nor the time/random shims can see it.
    if v2:
        answer = answer.upper()
    return answer


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # --- record ---------------------------------------------------------------
    with orientim.record(root="examples/_runs", always=True,
                         agent={"name": "order-support", "version": "1.0.0"}) as run:
        run.output = agent(run)

    meta, steps = store.load(run.path)
    http = [s for s in steps if s.get("t") == "http"]

    print("recorded %s" % run.path)
    print("  agent    %s" % (meta["agent"],))
    print("  runtime  python %s, httpx %s"
          % (meta["runtime"]["python"],
             meta["runtime"]["libraries"].get("httpx", "?")))
    print("  output   %r" % (meta["outcome"]["value"],))
    print()
    print("  steps")
    for s in http:
        line = "    %d  %-5s  %s" % (s["i"], s["role"], s["url"])
        if s.get("model"):
            line += "\n           asked  %s" % (s["model"],)
        if s.get("served"):
            line += "\n           got    %s" % (s["served"],)
        print(line)

    # --- replay the same code -------------------------------------------------
    print()
    print("replay, unchanged code:")
    print(orientim.replay(run.path, lambda r: agent(r)).report())

    # --- replay a version whose answer changed without its traffic changing ---
    print()
    print("replay, after a post-processing change (version 2):")
    d = orientim.replay(run.path, lambda r: agent(r, v2=True))
    print(d.report())
    print()
    print("  every call matched: %d of %d" % (d.n_matched, d.n_recorded))
    print("  step it points at : %r" % (d.index,))
    print("  recorded answer   : %r" % (d.recorded_output["value"],))
    print("  this time         : %r" % (d.replay_output["value"],))
    print()
    print("Format 3 called this IDENTICAL: the HTTP was identical.")

    srv.shutdown()


if __name__ == "__main__":
    main()
