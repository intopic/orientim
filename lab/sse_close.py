# -*- coding: utf-8 -*-
"""A stream that stopped early, and what a claim may still say about it.

    python lab/sse_close.py

An experiment. Nothing in Orientim changes: no format, no field, and `complete`
keeps the meaning it has today.

Reading the recorder suggested a conflation: `_close_step` writes
`complete = True` whether the stream ended or the consumer walked away, and
`__iter__` closes the step in a `finally`, so an agent that breaks out of the
loop leaves a step that looks finished. That is transport bookkeeping. Whether
it is also an **evidence** bug is a different question, and only a claim can
answer it: the semantic layer has its own closure vocabulary (CHANNEL_OPEN,
EVENTS_TRUNCATED, PARTIAL_CALL) and may already refuse to close the domain.

So this asks three questions, each against a fully-consumed control:

    A  a tool request arrives after the consumer stopped reading
       -> did_not_call(that tool) must not be PASS
    B  a complete tool request arrives, then the stream is abandoned
       -> used_tool(it) may be PASS, and did_not_call(another) must not be
    C  a malformed event carrying a tool request, then a valid terminator
       -> did_not_call(that tool) must not be PASS

A stream is evidence of what the model asked for. None of these claims say
anything about whether the agent then ran the tool.
"""
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import orientim                                          # noqa: E402
from orientim import evaluate, model, store              # noqa: E402

PORT = 8807
BASE = "http://127.0.0.1:%d" % PORT
URL = BASE + "/v1/chat/completions"
ROOT = os.path.join(HERE, "_runs", "sse")
OUT = os.path.join(HERE, "_runs", "_sse_close.json")

GAP = 0.25          # seconds between chunks, so "read one chunk" reads one


def _ev(obj):
    return "data: " + json.dumps(obj, separators=(",", ":")) + "\n\n"


def _text(s):
    return _ev({"id": "c", "model": "lab-1",
                "choices": [{"index": 0, "delta": {"content": s}}]})


def _tool(name, index=0):
    return _ev({"id": "c", "model": "lab-1", "choices": [
        {"index": 0, "delta": {"tool_calls": [
            {"index": index, "id": "call_%d" % index, "type": "function",
             "function": {"name": name, "arguments": "{}"}}]}}]})


def _finish():
    return _ev({"id": "c", "model": "lab-1",
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "tool_calls"}]})


DONE = "data: [DONE]\n\n"

# A tool-call event cut mid-JSON. Syntactically invalid, and what it would have
# carried is a request for send_email.
TORN = ('data: {"id":"c","choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"send_em\n\n')

STREAMS = {
    # the tool request arrives in the second chunk, after the consumer stopped
    "late_tool": [_text("thinking"), _tool("send_email"), _finish(), DONE],
    # the witness is complete in the first chunk; the stream never terminates
    "early_tool": [_tool("send_email"), _text("and then"), _text("more")],
    # a torn event, then a syntactically valid terminator
    "torn_then_done": [_text("hello"), TORN, DONE],
    # the same stream with the torn event removed: the control that says what
    # a correct PASS looks like
    "clean_then_done": [_text("hello"), DONE],
}


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # A consumer that walks away mid-stream aborts the connection, and the
        # default handler prints a traceback for it. Here that is the subject,
        # not an error.
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except OSError:
            self.close_connection = True

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        pieces = STREAMS[req.get("v")]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i, piece in enumerate(pieces):
            if i:
                time.sleep(GAP)
            raw = piece.encode()
            try:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.flush()
            except OSError:
                return          # the consumer hung up; that is the experiment
        try:
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- agents -------------------------------------------------------------------

def agent(variant, read_chunks=None):
    """Consume `read_chunks` chunks of the stream, or all of it when None."""
    def run(h):
        body = {"model": "lab-1", "stream": True, "v": variant,
                "messages": [{"role": "user", "content": "go"}]}
        seen = 0
        with h.client(timeout=20.0).stream("POST", URL, json=body) as r:
            for _chunk in r.iter_bytes():
                seen += 1
                if read_chunks is not None and seen >= read_chunks:
                    break
        h.output = "read %d" % seen
    return run


# --- measuring ----------------------------------------------------------------

def _record(variant, read_chunks, name):
    with orientim.record(root=ROOT, always=True) as h:
        agent(variant, read_chunks)(h)
    dst = os.path.join(ROOT, name + ".jsonl")
    shutil.copyfile(h.path, dst)
    return dst


def _model_step(path):
    _meta, steps = store.load(path)
    for s in steps:
        if s.get("t") == "http" and s.get("role") == model.MODEL:
            return s
    return None


def _frames(step):
    return (step.get("body") or "").count("data:")


def _verdicts(path, checks):
    rep = evaluate.evaluate(path, checks)
    return [{"evaluator": r.evaluator,
             "status": {"warn": "UNKNOWN"}.get(r.status, r.status.upper()),
             "reason": r.reason} for r in rep.results]


def _row(label, variant, read_chunks, checks, expected):
    path = _record(variant, read_chunks, label)
    step = _model_step(path)
    ev = model.tool_evidence(step) if step else {}
    return {
        "label": label,
        "chunks_read": read_chunks,
        "step_complete_flag": step.get("complete") if step else None,
        "step_error": step.get("error") if step else None,
        "frames_recorded": _frames(step) if step else 0,
        "evidence_complete": ev.get("complete"),
        "evidence_issues": sorted(ev.get("issues") or []),
        "names_seen": sorted({c.get("name") for c in (ev.get("calls") or [])
                              if c.get("name")}),
        "verdicts": _verdicts(path, checks),
        "expected": expected,
    }


def measure():
    srv = _serve()
    rows = []
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)

        no = evaluate.did_not_call("send_email")
        yes = evaluate.used_tool("send_email")
        other = evaluate.did_not_call("wire_transfer")

        # A — control first: the whole stream, then one chunk of the same one
        rows.append(_row("A0 control: read it all", "late_tool", None,
                         [no, yes], "FAIL / PASS"))
        rows.append(_row("A  read one chunk", "late_tool", 1,
                         [no, yes], "UNKNOWN / UNKNOWN"))

        # B — a complete witness, then the stream is abandoned
        rows.append(_row("B0 control: read it all", "early_tool", None,
                         [yes, other], "PASS / UNKNOWN"))
        rows.append(_row("B  abandon after the witness", "early_tool", 1,
                         [yes, other], "PASS / UNKNOWN"))

        # C — control first: the same stream with nothing torn in it, where a
        # PASS is the right answer. Then the torn event.
        rows.append(_row("C0 control: nothing torn", "clean_then_done", None,
                         [evaluate.did_not_call("send_email")], "PASS"))
        rows.append(_row("C  torn event, then [DONE]", "torn_then_done", None,
                         [no], "UNKNOWN"))
        return rows
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    rows = measure()
    print("a stream that stopped early, and what a claim may still say")
    print()
    for r in rows:
        print("  %s" % r["label"])
        print("     complete=%s  error=%s  frames=%s  evidence_complete=%s"
              % (r["step_complete_flag"], r["step_error"],
                 r["frames_recorded"], r["evidence_complete"]))
        print("     issues   %s" % (r["evidence_issues"] or "-"))
        print("     names    %s" % (r["names_seen"] or "-"))
        for v in r["verdicts"]:
            print("     %-14s %-8s %s" % (v["evaluator"], v["status"],
                                          v["reason"][:88]))
        print("     expected %s" % r["expected"])
        print()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
