# -*- coding: utf-8 -*-
"""What the hashes in a recording cover, and what they leave to trust.

    python lab/artifact_integrity.py

An experiment. Nothing in Orientim changes: no format, no chain, no field, no
replay semantics, and no existing hash is given a new meaning.

`lab/integrity.py` measured six mutations and found that none of them is
caught, because nothing verifies a recording against anything. This asks the
next question, which is narrower and decides a contract: **of the things a
recording contains, which ones does a hash actually cover, and which ones
decide something without being covered?**

Three measurements:

    A  a coverage matrix, field by field: does the chain root move, does the
       replay notice, and what does the field decide
    B  the stored body against `body_sha`, which are two different objects
       and are routinely assumed to be one
    C  corruption against tampering, which need different mechanisms and are
       usually sold as the same word
"""
import copy
import hashlib
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import orientim                                          # noqa: E402
from orientim import chain, evaluate, session, store      # noqa: E402

PORT = 8809
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(HERE, "_runs", "integrity2")
OUT = os.path.join(HERE, "_runs", "_artifact_integrity.json")

TOOL_BODY = {"model": "m", "choices": [{"index": 0,
                                        "finish_reason": "tool_calls",
                                        "message": {"role": "assistant",
                                                    "tool_calls": [
                                                        {"id": "c0",
                                                         "type": "function",
                                                         "function": {
                                                             "name": "send_email",
                                                             "arguments": "{}"}}]}}]}


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        kind = req.get("kind")
        if kind == "secret":
            body = json.dumps({"access_token": "sk-LIVE-SECRET",
                               "ok": True}).encode()
            ctype = "application/json"
        elif kind == "text":
            body = b"plain text, no json here"
            ctype = "text/plain"
        elif kind == "binary":
            body = bytes(range(256))
            ctype = "application/octet-stream"
        else:
            body = json.dumps(TOOL_BODY, separators=(",", ":")).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Served-By", "lab-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _agent(kinds=("tool",), got=None):
    def run(h):
        c = h.client()
        for kind in kinds:
            r = c.post(BASE + "/v1/chat/completions",
                       content=json.dumps({"model": "m", "kind": kind,
                                           "messages": [{"role": "user",
                                                         "content": "go"}]}
                                          ).encode())
            if got is not None:
                got.append(r.text[:60])
        h.output = "done"
    return run


# --- helpers ------------------------------------------------------------------

def _rows(path):
    return [json.loads(x) for x in
            open(path, encoding="utf-8").read().splitlines()]


def _write(rows, name):
    p = os.path.join(ROOT, name + ".jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _root(path):
    _meta, steps = store.load(path)
    return chain.build_steps([s for s in steps if s.get("t") == "http"])[1]


def _http(rows):
    return [r for r in rows if r.get("t") == "http"]


def _verdict(d):
    return "%s%s" % (d.diagnosis[0] if d.diagnosis else "?",
                     " (raised %s)" % type(d.raised).__name__ if d.raised else "")


# --- A: the coverage matrix ---------------------------------------------------

def _mutations():
    """One edit each, on the fields a reader might assume are protected."""

    def step(fn):
        def apply(rows):
            fn(_http(rows)[0])
        return apply

    def meta(fn):
        def apply(rows):
            fn(rows[0]["_meta"])
        return apply

    def _body(s):
        s["body"] = s["body"].replace("send_email", "wire_transfer")

    def _body_and_sha(s):
        _body(s)
        s["body_sha"] = hashlib.sha256(s["body"].encode()).hexdigest()[:32]

    def _headers(s):
        (s.setdefault("headers", {}))["x-served-by"] = "somebody-else"

    def _req(s):
        s["req"] = json.dumps({"model": "m", "messages":
                               [{"role": "user", "content": "something else"}]})

    return [
        ("step.body (what a replay serves)", step(_body), "served bytes"),
        ("step.body + body_sha together", step(_body_and_sha), "served bytes"),
        ("step.headers (served to the agent)", step(_headers), "served headers"),
        ("step.req (the recorded request)", step(_req), "what a reader sees"),
        ("step.status", step(lambda s: s.__setitem__("status", 500)),
         "replay + evaluation"),
        ("step.error", step(lambda s: s.__setitem__("error", "ReadTimeout")),
         "replay + evaluation"),
        ("step.role", step(lambda s: s.__setitem__("role", "tool")),
         "evaluation"),
        ("step.key_strict", step(lambda s: s.__setitem__("key_strict", "0" * 32)),
         "matching"),
        ("step.hdr_fp", step(lambda s: s.__setitem__("hdr_fp", "0" * 16)),
         "divergence"),
        ("step.complete", step(lambda s: s.__setitem__("complete", False)),
         "nothing today"),
        ("meta.context (the R2 gate)",
         meta(lambda m: m.__setitem__(
             "context", {"tenant": {"value": "globex",
                                    "evidence": "declared_unchained"}})),
         "whether a fixture is released"),
        ("meta.transforms (what was done to it)",
         meta(lambda m: m.__setitem__("transforms", [])),
         "how a diff reads the file"),
        ("meta.outcome (what the run returned)",
         meta(lambda m: m.__setitem__("outcome", {"value": "something else"})),
         "evaluation"),
    ]


def case_a(path):
    before = _root(path)
    rows = []
    for label, apply, decides in _mutations():
        edited = copy.deepcopy(_rows(path))
        apply(edited)
        p = _write(edited, "m")
        got = []
        try:
            after = _root(p)
            loads = True
        except Exception as e:
            after, loads = "load failed: %s" % type(e).__name__, False
        try:
            d = session.replay(p, _agent(got=got), strict=True)
            verdict = _verdict(d)
        except Exception as e:
            verdict = "replay raised %s" % type(e).__name__
        rows.append({
            "field": label,
            "decides": decides,
            "loads": loads,
            "chain_root_moved": (after != before) if loads else None,
            "replay": verdict,
        })
    return {"clean_root": before, "rows": rows}


def case_a_consequences(path):
    """Four fields where the mutation changes an answer, not just a byte."""
    out = {}

    # 1. the body a replay serves, and what an evaluator then reads
    edited = copy.deepcopy(_rows(path))
    _http(edited)[0]["body"] = _http(edited)[0]["body"].replace(
        "send_email", "wire_transfer")
    p = _write(edited, "c_body")
    ex = evaluate.Execution.load(p)
    out["body_edited"] = {
        "chain_root_moved": _root(p) != _root(path),
        "did_not_call(send_email) on the recording": (
            evaluate.did_not_call("send_email")(ex).status),
        "on the untouched recording": (
            evaluate.did_not_call("send_email")(
                evaluate.Execution.load(path)).status),
    }

    # 2. the stored response headers, which a replay hands to the agent
    edited = copy.deepcopy(_rows(path))
    _http(edited)[0].setdefault("headers", {})["x-served-by"] = "somebody-else"
    p = _write(edited, "c_headers")
    seen = []

    def reader(h):
        # The same request the recording holds, so the step matches and the
        # stored headers actually reach the agent.
        r = h.client().post(
            BASE + "/v1/chat/completions",
            content=json.dumps({"model": "m", "kind": "tool",
                                "messages": [{"role": "user",
                                              "content": "go"}]}).encode())
        seen.append(r.headers.get("x-served-by"))
        h.output = "done"

    d = session.replay(p, reader, strict=True)
    out["headers_edited"] = {"chain_root_moved": _root(p) != _root(path),
                             "replay": _verdict(d),
                             "agent_was_told": seen[0] if seen else None}
    return out


def case_a_context():
    """The R2 gate reads a meta field no hash covers."""
    with orientim.record(root=ROOT, always=True,
                         context={"tenant": "acme"}) as h:
        _agent()(h)
    path = h.path
    honest = session.replay(path, _agent(), context={"tenant": "acme"},
                            contract=("tenant",))
    rows = copy.deepcopy(_rows(path))
    rows[0]["_meta"]["context"] = {"tenant": {"value": "globex",
                                              "evidence": "declared_unchained"}}
    p = _write(rows, "c_context")
    edited = session.replay(p, _agent(), context={"tenant": "acme"},
                            contract=("tenant",))
    return {"chain_root_moved": _root(p) != _root(path),
            "gate_before": _verdict(honest),
            "gate_after_editing_the_context": _verdict(edited)}


# --- B: the stored body is not what body_sha fingerprints ---------------------

def case_b(path_all):
    _meta, steps = store.load(path_all)
    out = []
    for s in [x for x in steps if x.get("t") == "http"]:
        stored = s.get("body") or ""
        raw = stored.encode("utf-8")
        out.append({
            "kind": (json.loads(s.get("req") or "{}") or {}).get("kind"),
            "b64": bool(s.get("b64")),
            "body_sha": s.get("body_sha"),
            "sha256(stored)[:32]": hashlib.sha256(raw).hexdigest()[:32],
            "equal": hashlib.sha256(raw).hexdigest()[:32] == s.get("body_sha"),
        })
    return out


# --- C: corruption is not tampering -------------------------------------------

def case_c(path):
    out = {}
    raw = open(path, encoding="utf-8").read()

    # a byte flipped inside a step line
    broken = raw.replace('"status": 200', '"status": 2q0', 1)
    p = os.path.join(ROOT, "c_flipped.jsonl")
    open(p, "w", encoding="utf-8").write(broken)
    try:
        _m, steps = store.load(p)
        out["a_flipped_byte"] = "loaded, %d steps" % len(steps)
    except Exception as e:
        out["a_flipped_byte"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    # the file cut in half
    p = os.path.join(ROOT, "c_truncated.jsonl")
    open(p, "w", encoding="utf-8").write(raw[:len(raw) // 2])
    try:
        _m, steps = store.load(p)
        out["a_truncated_file"] = "loaded, %d steps" % len(steps)
    except Exception as e:
        out["a_truncated_file"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    # an editor who changes the content and every hash derived from it
    rows = copy.deepcopy(_rows(path))
    s = _http(rows)[0]
    s["body"] = s["body"].replace("send_email", "wire_transfer")
    s["body_sha"] = hashlib.sha256(s["body"].encode()).hexdigest()[:32]
    if s.get("chunks"):
        s["chunks"] = [[0, len(s["body"].encode())]]
    p = _write(rows, "c_rewritten")
    ex = evaluate.Execution.load(p)
    out["a_rewritten_recording"] = {
        "loads": True,
        "internally_consistent": True,
        "replay": _verdict(session.replay(p, _agent(), strict=True)),
        "evaluator_reads": evaluate.did_not_call("wire_transfer")(ex).status,
        "root_is_stored_in_the_file": _root(p)[:16] in open(
            p, encoding="utf-8").read(),
        "anything_compares_the_root": False,
    }
    return out


def measure():
    srv = _serve()
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)
        with orientim.record(root=ROOT, always=True) as h:
            _agent()(h)
        one = h.path
        with orientim.record(root=ROOT, always=True) as h2:
            _agent(("tool", "secret", "text", "binary"))(h2)
        many = h2.path
        return {"A": case_a(one), "A_consequences": case_a_consequences(one),
                "A_context": case_a_context(), "B": case_b(many),
                "C": case_c(one)}
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    m = measure()
    print("what the hashes cover, and what they leave to trust")
    print()
    print("  A  one edit each, and what it costs")
    print("     %-38s %-7s %-11s %-22s %s"
          % ("field", "loads", "root moved", "replay says", "it decides"))
    print("     " + "-" * 104)
    for r in m["A"]["rows"]:
        print("     %-38s %-7s %-11s %-22s %s"
              % (r["field"], r["loads"], r["chain_root_moved"], r["replay"],
                 r["decides"]))
    print()
    print("  A  what two of them change, beyond a byte")
    for k, v in m["A_consequences"].items():
        print("     %s" % k)
        for kk, vv in v.items():
            print("        %-42s %s" % (kk, vv))
    print("     the R2 gate")
    for k, v in m["A_context"].items():
        print("        %-42s %s" % (k, v))
    print()
    print("  B  sha256(stored body) against body_sha")
    print("     %-10s %-6s %-34s %-34s %s"
          % ("body", "b64", "body_sha", "sha256(stored)[:32]", "equal"))
    print("     " + "-" * 96)
    for r in m["B"]:
        print("     %-10s %-6s %-34s %-34s %s"
              % (r["kind"], r["b64"], r["body_sha"], r["sha256(stored)[:32]"],
                 r["equal"]))
    print()
    print("  C  corruption, and an editor who can rewrite")
    for k, v in m["C"].items():
        if isinstance(v, dict):
            print("     %s" % k)
            for kk, vv in v.items():
                print("        %-42s %s" % (kk, vv))
        else:
            print("     %-42s %s" % (k, v))
    print()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
