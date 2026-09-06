# -*- coding: utf-8 -*-
"""The real OpenAI and Anthropic SDKs, recorded and replayed.

Every other suite in this repository talks to a lab server through a client we
built. This one uses the vendors' own SDKs — their httpx client, their retry
policy, their headers, their SSE parser — pointed at a local server that speaks
their wire protocol. No API key and no network are needed, and the code path
under test is the one a customer actually runs.

What this still does not prove: that a real api.openai.com endpoint behaves
like this server. That needs somebody's key.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orientim
from orientim import store

CRLF = bytes((13, 10))
FINDINGS = []
STATE = {"calls": 0, "emails": 0}


def check(name, fn):
    try:
        ok, note = fn()
    except Exception as e:
        ok, note = False, "raised %s: %s" % (type(e).__name__, str(e)[:100])
    FINDINGS.append((name, ok, note))
    print("  %s  %-40s %s" % ("ok " if ok else "BUG", name, note))


# --- a server that speaks both wire protocols -------------------------------

class Vendor(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            piece = ("data: " + json.dumps(ev) + chr(10) * 2).encode()
            self.wfile.write(b"%x" % len(piece) + CRLF + piece + CRLF)
            self.wfile.flush()
            time.sleep(0.04)
        done = ("data: [DONE]" + chr(10) * 2).encode()
        self.wfile.write(b"%x" % len(done) + CRLF + done + CRLF)
        self.wfile.write(b"0" + CRLF + CRLF)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        STATE["calls"] += 1
        path = self.path.split("?")[0]

        if path == "/v1/chat/completions":
            if body.get("stream"):
                return self._sse([
                    {"id": "c1", "object": "chat.completion.chunk",
                     "created": 1, "model": "gpt-4o-mini",
                     "choices": [{"index": 0, "delta": {"content": tok},
                                  "finish_reason": None}]}
                    for tok in ("Order ", "4471 ", "has ", "shipped.")
                ])
            # temperature > 0: a different answer every call, which is the
            # point — the recorded one is what a replay must hand back.
            said = "call-%d" % STATE["calls"]
            return self._json(200, {
                "id": "chatcmpl-%d" % STATE["calls"], "object": "chat.completion",
                "created": 1, "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": said}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3,
                          "total_tokens": 12}})

        if path == "/v1/messages":                       # anthropic
            return self._json(200, {
                "id": "msg_%d" % STATE["calls"], "type": "message",
                "role": "assistant", "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "seen-%d" % STATE["calls"]}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 4}})

        if path == "/tools/search":
            return self._json(200, {"hits": []})         # the bug: nothing found

        if path == "/tools/send-email":
            STATE["emails"] += 1
            return self._json(200, {"sent": STATE["emails"]})

        return self._json(404, {"error": "not found"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
R = "tests/_runs/real_sdk"


# --- the agent, written the way anybody would write it ----------------------

def build_openai():
    from openai import OpenAI
    return OpenAI(api_key="sk-test-not-a-real-key", base_url=BASE + "/v1",
                  max_retries=0)


def agent(_run):
    """Plan with the model, call a tool, then act on what came back."""
    import httpx
    client = build_openai()

    plan = client.chat.completions.create(
        model="gpt-4o-mini", temperature=0.9,
        messages=[{"role": "user", "content": "status of order 4471?"}])

    with httpx.Client() as tools:
        hits = tools.post(BASE + "/tools/search",
                          json={"q": plan.choices[0].message.content}).json()

    if not hits.get("hits"):
        # the defect this recording exists to show: it invents an answer
        client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.9,
            messages=[{"role": "user", "content": "make something up"}])
        with httpx.Client() as tools:
            tools.post(BASE + "/tools/send-email", json={"to": "customer"})
        return "invented"
    return "answered"


# --- checks -----------------------------------------------------------------

def t_openai_sdk_captured():
    with orientim.record(root=R, always=True) as h:
        outcome = agent(h)
    _m, steps = store.load(h.path)
    http = [s for s in steps if s.get("t") == "http"]
    leaked = "sk-test-not-a-real-key" in open(h.path, encoding="utf-8").read()
    return (len(http) == 4 and outcome == "invented" and not leaked), (
        "%d steps through the real SDK, outcome=%s, api key in file=%s"
        % (len(http), outcome, leaked))


def t_openai_sdk_replays():
    before_calls = STATE["calls"]
    with orientim.record(root=R, always=True) as h:
        agent(h)
    emails_after_record = STATE["emails"]
    recorded_calls = STATE["calls"] - before_calls

    got = []
    d = orientim.replay(h.path, lambda x: got.append(agent(x)))
    leaked_effect = STATE["emails"] > emails_after_record
    server_untouched = STATE["calls"] == before_calls + recorded_calls
    return (d.ok and not leaked_effect and server_untouched), (
        "replay=%s, email sent twice=%s, server saw the replay=%s"
        % (d.diagnosis[0], leaked_effect, not server_untouched))


def t_openai_streaming():
    arrivals = []

    def streamer(_run):
        client = build_openai()
        t0 = time.monotonic()
        text = []
        stream = client.chat.completions.create(
            model="gpt-4o-mini", stream=True,
            messages=[{"role": "user", "content": "stream it"}])
        for chunk in stream:
            piece = chunk.choices[0].delta.content
            if piece:
                text.append(piece)
                arrivals.append((time.monotonic() - t0) * 1000.0)
        return "".join(text)

    with orientim.record(root=R, always=True) as h:
        said = streamer(h)
    rec = list(arrivals)
    spread = rec[-1] - rec[0] if len(rec) > 1 else 0.0

    arrivals.clear()
    out = []
    d = orientim.replay(h.path, lambda x: out.append(streamer(x)))
    live = len(rec) == 4 and spread > 40          # not delivered as one lump
    return (live and out[0] == said and d.ok), (
        "%d tokens over %.0f ms live, replay text matches=%s, verdict=%s"
        % (len(rec), spread, out and out[0] == said, d.diagnosis[0]))


def t_anthropic_sdk():
    import anthropic

    def ask(_run):
        c = anthropic.Anthropic(api_key="sk-ant-test", base_url=BASE,
                                max_retries=0)
        m = c.messages.create(model="claude-sonnet-4-5", max_tokens=64,
                              messages=[{"role": "user", "content": "hello"}])
        return m.content[0].text

    with orientim.record(root=R, always=True) as h:
        first = ask(h)
    _m, steps = store.load(h.path)
    n = len([s for s in steps if s.get("t") == "http"])
    leaked = "sk-ant-test" in open(h.path, encoding="utf-8").read()
    out = []
    d = orientim.replay(h.path, lambda x: out.append(ask(x)))
    return (n == 1 and d.ok and out[0] == first and not leaked), (
        "%d step, replay returned the recorded answer=%s, key in file=%s"
        % (n, out and out[0] == first, leaked))


def t_counterfactual_on_real_sdk():
    took = []

    def run(_run):
        took.append(agent(_run))

    with orientim.record(root=R, always=True) as h:
        run(h)
    recorded = took[-1]
    emails_before = STATE["emails"]
    # step 1 is the tool call; give it a hit and the agent should not invent
    d = orientim.replay(h.path, run,
                        patch={1: {"body": json.dumps({"hits": ["4471 shipped"]})}})
    return (recorded == "invented" and took[-1] == "answered"
            and STATE["emails"] == emails_before), (
        "recorded=%s, counterfactual=%s, verdict=%s, no email sent"
        % (recorded, took[-1], d.diagnosis[0]))


if __name__ == "__main__":
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print("\n  REAL SDK SUITE — openai %s, anthropic %s\n"
          % (__import__("openai").__version__,
             __import__("anthropic").__version__))
    check("openai SDK traffic is captured", t_openai_sdk_captured)
    check("openai agent replays identically", t_openai_sdk_replays)
    check("openai streaming stays streaming", t_openai_streaming)
    check("anthropic SDK captured and replayed", t_anthropic_sdk)
    check("counterfactual on the real SDK", t_counterfactual_on_real_sdk)
    srv.shutdown()
    srv.server_close()
    bugs = [f for f in FINDINGS if not f[1]]
    print("\n  %d finding(s) to fix\n" % len(bugs))
    sys.exit(1 if bugs else 0)
