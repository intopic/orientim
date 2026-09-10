# -*- coding: utf-8 -*-
"""Second-pass audit: verify each counterexample against the repo, or refute it.

Asserts nothing. Prints what Orientim does, so each claim can be judged on
behaviour instead of on a reading of the source.
"""
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")
import orientim
from orientim import (ci, evaluate as ev, explain, model,
                      observation as obs, store)

PORT = 8796
B = "http://127.0.0.1:%d" % PORT
ROOT = "tests/_runs/audit2"
CRLF = bytes((13, 10))


def fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, lines):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ln in lines:
            raw = ("data: " + ln + chr(10) * 2).encode()
            self.wfile.write(b"%x" % len(raw) + CRLF + raw + CRLF)
            self.wfile.flush()
        self.wfile.write(b"0" + CRLF + CRLF)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        v = req.get("v")

        if v == "known_key_object":
            # `output` is a container tool_view recognises. The Responses API
            # puts a LIST there; this vendor put an object.
            return self._json({"model": "vendor-x",
                               "output": {"actions": [{"invoke": "send_email"}]}})

        if v == "malformed_choices":
            # 200, `choices` present, and not iterable as the extractor expects.
            return self._json({"model": "vendor-x", "choices": 7})

        if v == "sse_over_limit":
            # 400 content events, the tool call at 401, then a real terminator.
            evs = ['{"id":"c","choices":[{"delta":{"content":"%d"}}]}' % i
                   for i in range(400)]
            evs.append('{"id":"c","choices":[{"delta":{"tool_calls":[{"index":0,'
                       '"id":"call_x","function":{"name":"send_email",'
                       '"arguments":"{}"}}]}}]}')
            evs.append("[DONE]")
            return self._sse(evs)

        if v == "fifty_one_tools":
            calls = [{"id": "call_%d" % i, "type": "function",
                      "function": {"name": "lookup_order",
                                   "arguments": "{}"}} for i in range(50)]
            calls.append({"id": "call_50", "type": "function",
                          "function": {"name": "send_email",
                                       "arguments": "{}"}})
            return self._json({"model": "vendor-x", "choices": [
                {"index": 0, "finish_reason": "tool_calls",
                 "message": {"role": "assistant", "tool_calls": calls}}]})

        if v == "fake_done":
            # No terminator. The model wrote the marker into its own text.
            return self._sse([
                '{"id":"c","model":"m","choices":[{"delta":'
                '{"content":"the marker is [DONE]"}}]}'])

        if v == "local_closure":
            # choice 0 finished; choice 1 never did; no [DONE].
            return self._sse([
                '{"id":"c","model":"m","choices":[{"index":0,"delta":{},'
                '"finish_reason":"stop"},{"index":1,"delta":'
                '{"content":"still going"}}]}'])

        if v == "partial_tool":
            # A tool call whose arguments were cut mid-JSON.
            return self._sse([
                '{"id":"c","model":"m","choices":[{"delta":{"tool_calls":'
                '[{"index":0,"id":"call_p","function":{"name":"send_email",'
                '"arguments":"{\\"to\\":"}}]}}]}'])

        if v == "one_tool":
            return self._json({"model": "vendor-x", "choices": [
                {"index": 0, "finish_reason": "tool_calls",
                 "message": {"role": "assistant", "tool_calls": [
                     {"id": "c0", "type": "function",
                      "function": {"name": "send_email",
                                   "arguments": "{}"}}]}}]})

        return self._json({"model": "vendor-x", "choices": [
            {"index": 0, "finish_reason": "stop",
             "message": {"role": "assistant", "content": "plain"}}]})


def record(variant, path="/v1/chat/completions", output="done"):
    fresh()
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + path,
                        content=json.dumps({"model": "m", "v": variant}).encode())
        h.output = output
    return h.path


def show(label, variant, tool="send_email", path="/v1/chat/completions"):
    p = record(variant, path=path)
    ex = ev.Execution.load(p)
    steps = [s for s in ex.steps if s.get("t") == "http"]
    step = steps[0]
    ev_ = model.tool_evidence(step)
    view = ("complete" if ev_["complete"]
            else "INCOMPLETE " + ",".join(ev_["issues"]))
    names = model.tool_names_in(ex.steps)
    dnc = ev.did_not_call(tool)(ex)
    ut = ev.used_tool(tool)(ex)
    print("\n%s" % label)
    print("    role=%-6s tool_view=%-28s extracted=%s"
          % (step.get("role"), view, names[:3] + (["..."] if len(names) > 3 else [])))
    print("    TOOL_VIEW complete = %s" % ex.observation.complete(obs.TOOL_VIEW))
    print("    did_not_call(%s) -> %-5s %s" % (tool, dnc.status, dnc.reason[:88]))
    print("    used_tool(%s)    -> %-5s %s" % (tool, ut.status, ut.reason[:88]))
    return ex


def p0_1_counterexamples():
    print("=" * 78)
    print("  P0-1  is READABLE a promise the extractor actually kept?")
    print("=" * 78)
    show("1. unknown envelope carrying a KNOWN container key ('output' as object)",
         "known_key_object")
    show("2. malformed but 200: choices = 7", "malformed_choices")
    show("3. SSE: tool call at event 401, parser stops at 400, [DONE] at the end",
         "sse_over_limit")
    show("4. the 51st tool call, with MAX_TOOL_CALLS = 50", "fifty_one_tools")
    show("5. the model wrote '[DONE]' into its own content; stream never closed",
         "fake_done")
    show("6. choice 0 finished, choice 1 did not; no [DONE]", "local_closure")
    show("7. a tool call whose arguments were cut mid-JSON (partial witness)",
         "partial_tool")
    show("8. a model endpoint `classify` does not recognise", "one_tool",
         path="/infer")
    print("\n   (8 is the control for role misclassification: the same response"
          "\n    that yields a FAIL at /v1/chat/completions.)")
    show("8b. the same body at a recognised path", "one_tool")


def p0_4_counterexamples():
    print("\n" + "=" * 78)
    print("  P0-4  is an evaluator name an obligation identity?")
    print("=" * 78)

    def row(results, ok=False):
        return {"case": "support", "run_id": "r", "ok": ok,
                "verdict": "IDENTICAL", "steps": 1,
                "evaluation": {"results": [{"evaluator": k, "status": v}
                                           for k, v in results]}}

    def frozen(results, ok=False):
        return {"case": "support", "run_id": "r", "ok": ok,
                "verdict": "IDENTICAL", "steps": 1,
                "failed_evaluators": [k for k, v in results if v == "fail"],
                "evaluators": {k: v for k, v in results}}

    base = {"runs": [frozen([("max_steps", "fail"),
                             ("did_not_call", "pass"),     # refund
                             ("did_not_call", "pass")])]}  # send_email
    cur = [row([("max_steps", "fail"),
                ("did_not_call", "fail"),      # refund, now violated
                ("did_not_call", "pass")])]    # send_email, still fine
    cmp_ = ci.compare(cur, base, key="case")
    print("\n  two did_not_call rules, different subjects, one newly violated")
    print("    baseline evaluators map -> %s" % base["runs"][0]["evaluators"])
    print("    current  evaluators map -> %s"
          % {r["evaluator"]: r["status"]
             for r in cur[0]["evaluation"]["results"]})
    print("    comparison              -> %s"
          % {k: v for k, v in cmp_.items() if v})
    print("    new violation surfaced? -> %s" % ("did_not_call" in json.dumps(
        cmp_.get("new_failures") or {})))

    cur2 = [row([("max_steps", "fail"),
                 ("did_not_call", "pass"),     # send_email first now
                 ("did_not_call", "fail")])]   # refund last
    print("    same run, rules listed in the other order -> %s"
          % {k: v for k, v in ci.compare(cur2, base, key="case").items() if v})


def p0_4_gate():
    print("\n  the exit code with a baseline")
    src = open("orientim/cli.py", encoding="utf-8").read()
    i = src.find('bad = cmp_["newly_changed"]')
    print("    cli.py: %s" % src[i:i + 66].split(chr(10))[0])
    print("    -> new_failures / weakened / dropped_obligations do not reach it")


def diff_disagreement():
    print("\n" + "=" * 78)
    print("  DIFF  does the tool comparison know about the new domain?")
    print("=" * 78)
    fresh()
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + "/v1/chat/completions",
                        content=json.dumps({"model": "m", "v": "one_tool"}).encode())
        h.output = "done"
    a = h.path
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + "/v1/chat/completions",
                        content=json.dumps({"model": "m",
                                            "v": "known_key_object"}).encode())
        h.output = "done"
    b = h.path

    meta_a, steps_a = store.load(a)
    meta_b, steps_b = store.load(b)
    blind = explain.unreadable_tool_view(steps_a, steps_b)
    changes = explain.run_tool_changes(steps_a, steps_b)
    print("\n  A: a readable response requesting send_email")
    print("  B: the same request, answered in an envelope nobody can read")
    print("    A extracted -> %s" % model.tool_names_in(steps_a))
    print("    B extracted -> %s" % model.tool_names_in(steps_b))
    print("    B evidence  -> %s"
          % model.tool_evidence(
              [s for s in steps_b if s.get("t") == "http"][0])["issues"])
    print("    unreadable_tool_view() -> %r" % (blind,))
    print("    tool_changes()         -> %s" % (changes,))
    exb = ev.Execution.of(meta_b, steps_b)
    print("    evaluation on B        -> %s"
          % ev.did_not_call("send_email")(exb).status)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        p0_1_counterexamples()
        p0_4_counterexamples()
        p0_4_gate()
        diff_disagreement()
    finally:
        srv.shutdown()
    print("\ndone")
