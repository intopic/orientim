# -*- coding: utf-8 -*-
"""A deterministic model endpoint, OpenAI-shaped.

Not a stub that returns a fixed string: it drives a real agent loop. Given a
conversation it decides whether to ask for tools or to answer, exactly as a
provider would, and the agents below cannot tell the difference at the wire.

Deterministic on purpose. Every answer is a function of the messages, so two
runs of the same scenario produce the same bytes and a divergence means the
*agent* changed. A provider with a temperature would make every experiment in
this lab a coin toss.
"""
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9101


def _last_user(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _tool_results(messages):
    return [m for m in messages if m.get("role") == "tool"]


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def decide(req):
    """What the model does with this conversation.

    The agent tells us which tools it has by declaring them, and passes a
    `plan` — the tool sequence it wants for this task. A real model chooses;
    this one chooses the same way every time, which is what makes the lab a
    laboratory.
    """
    messages = req.get("messages") or []
    plan = req.get("plan") or []
    results = _tool_results(messages)
    asked = _last_user(messages)

    # Turn 1: no tool results yet, so ask for the first batch.
    if not results and plan:
        calls = plan[0] if isinstance(plan[0], list) else [plan[0]]
        return {"tool_calls": [
            {"id": "call_%d" % i, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args,
                                                                sort_keys=True)}}
            for i, (name, args) in enumerate(calls)]}

    # Turn 2+: another batch if the plan has one, otherwise answer.
    step = len(set(m.get("name") for m in results)) if results else 0
    if step < len(plan):
        calls = plan[step] if isinstance(plan[step], list) else [plan[step]]
        return {"tool_calls": [
            {"id": "call_%d_%d" % (step, i), "type": "function",
             "function": {"name": name, "arguments": json.dumps(args,
                                                                sort_keys=True)}}
            for i, (name, args) in enumerate(calls)]}

    facts = []
    for m in results:
        try:
            facts.append(json.loads(m.get("content") or "{}"))
        except ValueError:
            facts.append({"raw": m.get("content")})
    if not facts:
        # A summarising turn: no tools, the facts are in the question. A real
        # model reads its context; so does this one, or the supervisor's final
        # answer would be a constant and a changed child would be invisible in
        # it.
        start = asked.find("[")
        if start >= 0:
            try:
                facts = json.loads(asked[start:])
            except ValueError:
                facts = []
    return {"content": _answer(asked, facts, req)}


def _answer(asked, facts, req):
    """The final text. A template, so the answer is a function of the facts."""
    style = req.get("style", "plain")
    bits = []
    for f in facts:
        if "status" in f:
            bits.append("order %s is %s" % (f.get("order_id", "?"), f["status"]))
        if "risk" in f:
            bits.append("risk %s" % f["risk"])
        if "articles" in f:
            bits.append("%d article(s) found" % len(f["articles"]))
        if "refunded" in f:
            bits.append("refund %s" % ("issued" if f["refunded"] else "refused"))
    if not bits:
        bits.append("nothing to report")
    text = "; ".join(bits)
    if style == "formal":
        return "Dear customer, %s. Reference %s." % (text, _digest(asked))
    return text


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        if self.path.split("?")[0] != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})

        out = decide(req)
        message = {"role": "assistant", "content": out.get("content")}
        if out.get("tool_calls"):
            message["tool_calls"] = out["tool_calls"]
        self._send(200, {
            "id": "chatcmpl-lab",
            "object": "chat.completion",
            "model": str(req.get("model", "unknown")) + "-2024-07",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if out.get("tool_calls")
                         else "stop"}],
            # Deterministic and a function of the input, so a changed prompt
            # shows up as changed usage the way it would with a real provider.
            "usage": {"prompt_tokens": sum(len(json.dumps(m)) for m in
                                           (req.get("messages") or [])) // 4,
                      "completion_tokens": 12},
        })

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    print("model on %d" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
