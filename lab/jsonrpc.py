# -*- coding: utf-8 -*-
"""What a captured JSON-RPC exchange proves, and where the link runs out.

    python lab/jsonrpc.py

An experiment. Nothing in Orientim changes: no format, no chain, no field, no
matcher, no cursor, no replay semantics, and no id is normalised anywhere. The
reader below is **lab code**, written so the contract can be attacked before a
line of it reaches production.

Everything here is read out of recordings Orientim already writes: `req` is the
request body it stored, `body` is the response body. The question is what those
two let anyone say.

**The asymmetry that decides the design.** Over HTTP, a POST and its response
are already paired *by the transport*, before any id is read. So for the
ordinary one-request-one-response case the id is not what links them — it is a
second, independent claim about a link the transport already established, and
the two can disagree. Where the transport pairs nothing — a batch, or a
response delivered on a stream opened by a different request — the id is the
only link there is, and then its weaknesses are the link's weaknesses.

The spec (jsonrpc.org/specification) is used as written, and in particular:

    a Notification is a Request with no `id` member, and is not confirmable
    by definition, since it has no Response object

    a Response `id` MUST be the same as the Request's; where the id could not
    be determined it MUST be Null

    a Response has exactly one of `result` or `error`

    a Batch response array MAY be in any order, and the client SHOULD match
    by id — so order proves nothing
"""
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
from orientim import store                                # noqa: E402

PORT = 8811
URL = "http://127.0.0.1:%d/rpc" % PORT
ROOT = os.path.join(HERE, "_runs", "jsonrpc")
OUT = os.path.join(HERE, "_runs", "_jsonrpc.json")

# --- the vocabulary -----------------------------------------------------------

REQUEST = "request"             # method, and an id member
NOTIFICATION = "notification"   # method, and no id member
RESULT = "response_result"      # exactly result
ERROR = "response_error"        # exactly error
INVALID = "invalid"             # not a JSON-RPC 2.0 message
UNSUPPORTED = "unsupported"     # shaped like one, in a way v1 does not read

# How a response came to be attached to a request, and how much that is worth.
CORROBORATED = "CORROBORATED"   # the transport paired them and the ids agree
TRANSPORT = "TRANSPORT_ONLY"    # paired by the transaction; no id to check
BY_ID = "BY_ID"                 # the only link is an equal id
CONFLICT = "CONFLICT"           # paired by the transport, and the ids disagree
AMBIGUOUS = "AMBIGUOUS"         # more than one candidate, or a repeated id
UNLINKED = "UNLINKED"           # nothing to attach it to
NOT_CONFIRMABLE = "NOT_CONFIRMABLE"   # a notification, by definition


def _kind(msg):
    """What one message is, from its own structure. Never from its position.

    Order in an array, or arrival order on a connection, says nothing about
    what a message is — so nothing here reads either.
    """
    if not isinstance(msg, dict):
        return INVALID, "not an object"
    if msg.get("jsonrpc") != "2.0":
        return UNSUPPORTED, "jsonrpc=%r" % (msg.get("jsonrpc"),)
    has_method = "method" in msg
    has_result, has_error = "result" in msg, "error" in msg
    if has_method and not (has_result or has_error):
        if not isinstance(msg.get("method"), str):
            return INVALID, "method is not a string"
        # The whole difference, and it is the *member* rather than its value:
        # `{"id": null}` is a request whose id is null, not a notification.
        if "id" not in msg:
            return NOTIFICATION, None
        return REQUEST, None
    if has_result and has_error:
        return INVALID, "both result and error"
    if not (has_result or has_error):
        return INVALID, "neither method, result nor error"
    if "id" not in msg:
        return INVALID, "a response with no id member"
    if has_error:
        err = msg.get("error")
        if not isinstance(err, dict) or not isinstance(err.get("code"), int):
            return INVALID, "an error with no integer code"
        return ERROR, None
    return RESULT, None


def _ids_equal(a, b):
    """Same id, as the spec means it: same value **and** same type.

    `7` and `"7"` are two ids. Treating them as one is normalisation, and
    normalisation is how a response gets attached to a request nobody can show
    it answered. `True == 1` in Python, so the type check is explicit.
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, str) != isinstance(b, str):
        return False
    if isinstance(a, (int, float)) != isinstance(b, (int, float)):
        return False
    return a == b


def _link(sent, got):
    """What the captured exchange proves about one HTTP transaction.

    `sent` and `got` are the parsed request and response bodies of one step —
    which is to say the transport has already paired them. This decides what
    the ids do to that pairing, and what a batch leaves unresolved.
    """
    out = {"links": [], "issues": []}

    sent_msgs = sent if isinstance(sent, list) else [sent]
    got_msgs = got if isinstance(got, list) else ([] if got is None else [got])
    out["batch"] = isinstance(sent, list)

    if isinstance(sent, list) and not sent:
        out["issues"].append("an empty batch is an Invalid Request")

    requests, notifications = [], []
    for m in sent_msgs:
        kind, why = _kind(m)
        if kind == REQUEST:
            requests.append(m)
        elif kind == NOTIFICATION:
            notifications.append(m)
        else:
            out["issues"].append("request side: %s%s" % (
                kind, " (%s)" % why if why else ""))

    responses = []
    for m in got_msgs:
        kind, why = _kind(m)
        if kind in (RESULT, ERROR):
            responses.append(m)
        else:
            out["issues"].append("response side: %s%s" % (
                kind, " (%s)" % why if why else ""))

    out["counts"] = {"requests": len(requests),
                     "notifications": len(notifications),
                     "responses": len(responses)}

    # A repeated id on the request side is unresolvable by id, and there is
    # nothing else to resolve it with inside one transaction.
    for r in requests:
        if "id" in r and r.get("id") is None:
            out["issues"].append(
                "a request id of null is discouraged by the spec and cannot "
                "be linked: a null response id means *undetermined*")
    for i, a in enumerate(requests):
        for b in requests[i + 1:]:
            if _ids_equal(a.get("id"), b.get("id")):
                out["issues"].append("two requests carry the id %r"
                                     % (a.get("id"),))

    for msg in notifications:
        out["links"].append({"id": None, "link": NOT_CONFIRMABLE,
                             "method": msg.get("method")})

    for msg in responses:
        rid = msg.get("id")
        if rid is None:
            # Null is the spec's signal for an id that could not be
            # determined, so it identifies nothing — not even a request whose
            # own id was null, because the two are indistinguishable.
            out["links"].append({"id": None, "link": UNLINKED,
                                 "why": "a null response id links nothing: "
                                        "the spec uses null for an id it "
                                        "could not determine"})
            continue
        hits = [r for r in requests if _ids_equal(r.get("id"), rid)]
        if len(hits) > 1:
            out["links"].append({"id": rid, "link": AMBIGUOUS,
                                 "why": "%d requests carry this id" % len(hits)})
        elif len(hits) == 1:
            if out["batch"]:
                out["links"].append({"id": rid, "link": BY_ID,
                                     "method": hits[0].get("method")})
            else:
                out["links"].append({"id": rid, "link": CORROBORATED,
                                     "method": hits[0].get("method")})
        elif len(requests) == 1 and not out["batch"]:
            # The transport paired these two and the ids do not agree. Not a
            # link by id, and not nothing: a disagreement worth reporting.
            out["links"].append({
                "id": rid, "link": CONFLICT,
                "why": "the transaction carried the request id %r"
                       % (requests[0].get("id"),)})
        else:
            out["links"].append({"id": rid, "link": UNLINKED,
                                 "why": "no request in this transaction "
                                        "carries this id"})

    answered = {id(r) for r in requests
                for m in responses if _ids_equal(r.get("id"), m.get("id"))}
    for r in requests:
        if id(r) not in answered:
            out["links"].append({"id": r.get("id"), "link": UNLINKED,
                                 "method": r.get("method"),
                                 "why": "no response in this transaction "
                                        "carries this id"})
    return out


# --- a server that produces each shape on purpose -----------------------------

SHAPES = {}


def shape(name):
    def add(fn):
        SHAPES[name] = fn
        return fn
    return add


@shape("control")
def _control(sent):
    return 200, {"jsonrpc": "2.0", "id": sent.get("id"), "result": {"ok": True}}


@shape("notification")
def _notification(_sent):
    return 202, None                      # nothing to return, by definition


@shape("id_type_changed")
def _id_type(sent):
    return 200, {"jsonrpc": "2.0", "id": str(sent.get("id")),
                 "result": {"ok": True}}


@shape("id_null")
def _id_null(_sent):
    return 200, {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "Invalid Request"}}


@shape("id_missing")
def _id_missing(_sent):
    return 200, {"jsonrpc": "2.0", "result": {"ok": True}}


@shape("result_and_error")
def _both(sent):
    return 200, {"jsonrpc": "2.0", "id": sent.get("id"),
                 "result": {"ok": True},
                 "error": {"code": -32603, "message": "Internal error"}}


@shape("neither")
def _neither(sent):
    return 200, {"jsonrpc": "2.0", "id": sent.get("id")}


@shape("error_without_code")
def _bad_error(sent):
    return 200, {"jsonrpc": "2.0", "id": sent.get("id"),
                 "error": {"message": "something"}}


@shape("wrong_version")
def _wrong_version(sent):
    return 200, {"jsonrpc": "1.0", "id": sent.get("id"), "result": {}}


@shape("batch_out_of_order")
def _batch(sent):
    ids = [m.get("id") for m in sent if "id" in m]
    out = [{"jsonrpc": "2.0", "id": i, "result": {"n": i}}
           for i in reversed(ids)]
    return 200, out


@shape("batch_duplicate_ids")
def _batch_dupes(sent):
    return 200, [{"jsonrpc": "2.0", "id": m.get("id"), "result": {}}
                 for m in sent if "id" in m]


@shape("batch_unasked_id")
def _batch_unasked(sent):
    out = [{"jsonrpc": "2.0", "id": m.get("id"), "result": {}}
           for m in sent if "id" in m]
    out.append({"jsonrpc": "2.0", "id": 999, "result": {"ok": True}})
    return 200, out


@shape("batch_empty")
def _batch_empty(_sent):
    # What the spec says to answer an empty array with.
    return 200, {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "Invalid Request"}}


@shape("truncated")
def _truncated(sent):
    return 200, "TRUNCATE"                # written as a cut-off byte string


PENDING = []


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        # The other delivery shape: the POST was accepted with no body, and
        # the answer comes back on a stream this GET opened. Nothing in the
        # transaction that carries it ever sent a request.
        body = (b'data: ' + json.dumps(
            {"jsonrpc": "2.0", "id": PENDING[-1] if PENDING else None,
             "result": {"ok": True}}).encode() + b"\n\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        name = self.headers.get("X-Shape") or "control"
        try:
            sent = json.loads(raw or b"null")
        except Exception:
            sent = None
        code, payload = SHAPES[name](sent)
        if payload is None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if payload == "TRUNCATE":
            body = json.dumps({"jsonrpc": "2.0", "id": 7,
                               "result": {"text": "x" * 40}}).encode()[:48]
        else:
            body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- the matrix ---------------------------------------------------------------

CASES = [
    ("1  one request, one response", "control",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}),
    ("2  a notification", "notification",
     {"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ("3  the response id changed type", "id_type_changed",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
    ("4  the response id is null", "id_null",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
    ("5  the response has no id member", "id_missing",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
    ("6  result and error together", "result_and_error",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
    ("7  neither result nor error", "neither",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
    ("8  an error with no code", "error_without_code",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
    ("9  the response says jsonrpc 1.0", "wrong_version",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
    ("10 a request with a null id", "control",
     {"jsonrpc": "2.0", "id": None, "method": "tools/call"}),
    ("11 batch, answered out of order", "batch_out_of_order",
     [{"jsonrpc": "2.0", "id": 1, "method": "a"},
      {"jsonrpc": "2.0", "id": 2, "method": "b"},
      {"jsonrpc": "2.0", "id": 3, "method": "c"},
      {"jsonrpc": "2.0", "method": "note"}]),
    ("12 batch with a repeated id", "batch_duplicate_ids",
     [{"jsonrpc": "2.0", "id": 1, "method": "a"},
      {"jsonrpc": "2.0", "id": 1, "method": "b"}]),
    ("13 batch, a response nobody asked for", "batch_unasked_id",
     [{"jsonrpc": "2.0", "id": 1, "method": "a"}]),
    ("14 an empty batch", "batch_empty", []),
    ("15 a truncated response", "truncated",
     {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}),
]


def _agent(case):
    _label, shape_name, payload = case

    def run(h):
        h.client().post(URL, content=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json",
                                 "X-Shape": shape_name})
        h.output = "done"
    return run


def _step_of(path):
    _meta, steps = store.load(path)
    http = [s for s in steps if s.get("t") == "http"]
    return http[0] if http else None


def _as_json(text):
    try:
        return json.loads(text or ""), None
    except Exception as e:
        return None, type(e).__name__


def measure_cross():
    """The case a per-step reader cannot answer: the request and its response
    are in two different HTTP transactions."""
    del PENDING[:]
    PENDING.append(7)

    def agent(h):
        c = h.client()
        c.post(URL, content=json.dumps(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Shape": "notification"})      # 202, no body
        c.get(URL)
        h.output = "done"

    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    _meta, steps = store.load(h.path)
    rows = []
    for s in [x for x in steps if x.get("t") == "http"]:
        sent, _e1 = _as_json(s.get("req"))
        text = s.get("body") or ""
        # One SSE frame, read only far enough to find the message.
        if text.startswith("data:"):
            payload = text.split("data:", 1)[1].strip()
            got, _e2 = _as_json(payload)
        else:
            got, _e2 = _as_json(text)
        out = _link(sent, got)
        rows.append({"step": s.get("i"), "method": s.get("method"),
                     "links": out["links"], "issues": out["issues"]})
    return rows


def measure():
    srv = _serve()
    rows = []
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)
        for case in CASES:
            label = case[0]
            with orientim.record(root=ROOT, always=True) as h:
                _agent(case)(h)
            step = _step_of(h.path)
            sent, sent_err = _as_json(step.get("req"))
            got, got_err = _as_json(step.get("body"))
            row = {"case": label, "status": step.get("status"),
                   "request_parsed": sent_err is None,
                   "response_parsed": got_err is None or not step.get("body")}
            if sent_err:
                row["request_issue"] = "the stored request is not JSON (%s)" % sent_err
            if got_err and not (step.get("body") or "").strip():
                # No body at all. For a notification that is exactly right —
                # it has no Response object by definition — and for anything
                # else it is a response that never arrived. Two facts, and the
                # request side decides which.
                out = _link(sent, None)
                row["links"] = out["links"]
                row["issues"] = out["issues"]
                row["counts"] = out["counts"]
                row["batch"] = out["batch"]
                row["response_parsed"] = True
            elif got_err:
                row["response_issue"] = (
                    "the stored response is not JSON (%s)" % got_err)
                row["links"], row["issues"] = [], [
                    "no message could be read from the response, so nothing "
                    "is linked and nothing is claimed"]
            else:
                out = _link(sent, got)
                row["links"] = out["links"]
                row["issues"] = out["issues"]
                row["counts"] = out["counts"]
                row["batch"] = out["batch"]
            kinds = []
            for m in (sent if isinstance(sent, list) else [sent]):
                kinds.append(_kind(m)[0])
            row["request_kinds"] = kinds
            rows.append(row)
        return {"matrix": rows, "cross": measure_cross()}
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    m = measure()
    rows = m["matrix"]
    print("what a captured JSON-RPC exchange proves")
    print()
    print("  %-38s %-22s %s" % ("case", "request side", "links"))
    print("  " + "-" * 112)
    for r in rows:
        links = ", ".join(
            "%s%s" % (x["link"], "" if x.get("id") is None
                      else "(%r)" % (x["id"],)) for x in r["links"]) or "-"
        print("  %-38s %-22s %s"
              % (r["case"], ",".join(r["request_kinds"]), links))
    print()
    for r in rows:
        notes = list(r.get("issues") or [])
        notes += [x["why"] for x in r["links"] if x.get("why")]
        for n in notes:
            print("     %-38s %s" % (r["case"], n))
    print()
    print("  the request and its response in two transactions")
    for r in m["cross"]:
        links = ", ".join(
            "%s%s" % (x["link"], "" if x.get("id") is None
                      else "(%r)" % (x["id"],)) for x in r["links"]) or "-"
        print("     step %-3s %-6s %s" % (r["step"], r["method"], links))
        for x in r["links"]:
            if x.get("why"):
                print("              %s" % x["why"])
    print()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
