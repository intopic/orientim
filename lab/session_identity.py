# -*- coding: utf-8 -*-
"""What a session identifier costs to store, and what it costs to remove.

    python lab/session_identity.py

An experiment for the session-identity contract. Nothing in Orientim changes
on disk: no format, no chain, no field, no matcher. Case C *simulates* a
proposed recorder change by wrapping two functions inside this process only,
which is the point of running it before writing any of it.

An MCP server mints a session identifier and returns it in a response header.
The client reads it there and sends it back on every later request, so the
identifier is not a constant the agent was configured with — it is a value the
run *derived from a response*. That is what makes it awkward:

    store it              a bearer-ish identifier sits in a shared file
    drop it               the replayed client has nothing to echo
    replace them all      two different sessions become the same session

The question this measures is whether there is a fourth option: replace it
with a distinct token, consistently on both sides of the recording, so the
replayed client re-derives the token instead of the identifier and every
comparison behaves exactly as it does today.
"""
import json
import os
import secrets
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import orientim                                          # noqa: E402
from orientim import session, store, transport           # noqa: E402

try:
    from mcp import types as mt
    PROTOCOL = mt.LATEST_PROTOCOL_VERSION
except Exception:                                        # pragma: no cover
    mt, PROTOCOL = None, "2025-11-25"

PORT = 8808
URL = "http://127.0.0.1:%d/mcp" % PORT
ROOT = os.path.join(HERE, "_runs", "session")
OUT = os.path.join(HERE, "_runs", "_session_identity.json")

# What a real server mints: unguessable, and meaningful only inside its session.
REAL = {"id": "sess-" + secrets.token_hex(16)}
P1 = {"name": "lookup_order", "arguments": {"order_id": 4471}}


def _rpc(rid, method, params=None):
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    return json.dumps(body, separators=(",", ":")).encode()


class _MCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        method, rid = sent.get("method"), sent.get("id")
        given = self.headers.get("Mcp-Session-Id")
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {},
                      "serverInfo": {"name": "lab", "version": "0"}}
        elif given != REAL["id"]:
            # The server enforces it, so "the client echoed it" is a fact of
            # the run rather than an assumption of the experiment.
            return self._send(400, {"jsonrpc": "2.0", "id": rid,
                                    "error": {"code": -32600,
                                              "message": "bad session"}}, None)
        elif method == "tools/list":
            result = {"tools": [{"name": "lookup_order", "description": "d",
                                 "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "order 4471"}],
                      "isError": False}
        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result},
                   REAL["id"] if method == "initialize" else None)

    def _send(self, code, obj, session_id):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("MCP-Protocol-Version", PROTOCOL)
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _MCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- the agent: it derives the session from the response ----------------------

def agent(seen=None, force=None):
    """initialize, read Mcp-Session-Id from the response, then use it."""
    def run(h):
        c = h.client()
        r = c.post(URL, content=_rpc(1, "initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "lab", "version": "0"}}),
            headers={"Content-Type": "application/json",
                     "MCP-Protocol-Version": PROTOCOL})
        got = force or r.headers.get("mcp-session-id") or ""
        if seen is not None:
            seen.append(got)
        hdrs = {"Content-Type": "application/json",
                "MCP-Protocol-Version": PROTOCOL}
        if got:
            hdrs["Mcp-Session-Id"] = got
        c.post(URL, content=_rpc(2, "tools/list"), headers=hdrs)
        last = c.post(URL, content=_rpc(3, "tools/call", P1), headers=hdrs)
        h.output = last.text
    return run


# --- helpers ------------------------------------------------------------------

def _record(fn, name, patched=False):
    if patched:
        with _pseudonymised():
            with orientim.record(root=ROOT, always=True) as h:
                fn(h)
    else:
        with orientim.record(root=ROOT, always=True) as h:
            fn(h)
    dst = os.path.join(ROOT, name + ".jsonl")
    shutil.copyfile(h.path, dst)
    return dst


def _verdict(d):
    return {"diagnosis": d.diagnosis[0] if d.diagnosis else "?",
            "matched": d.n_matched, "recorded": d.n_recorded,
            "headers_changed": bool(d.headers_changed),
            "raised": str(d.raised) if d.raised else None}


def _rewrite(path, name, fn):
    rows = [json.loads(x) for x in
            open(path, encoding="utf-8").read().splitlines()]
    for row in rows:
        if row.get("t") == "http":
            fn(row)
    dst = os.path.join(ROOT, name + ".jsonl")
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")
    return dst


def _session_in(path):
    """Every session value the recording exposes, and whether the real one is
    among them."""
    raw = open(path, encoding="utf-8").read()
    _meta, steps = store.load(path)
    values = sorted({(s.get("headers") or {}).get("mcp-session-id")
                     for s in steps if s.get("t") == "http"} - {None})
    return {"stored": values, "real_id_in_file": REAL["id"] in raw}


# --- the proposal, simulated in this process only -----------------------------

class _pseudonymised(object):
    """Two wrappers, for the length of one recording.

    What a recorder would do: mint a distinct token the first time a session
    identifier is seen, store the token instead of the identifier, and compute
    the request fingerprint over the token as well — so the file is consistent
    with itself and the identifier never lands in it.

    The map lives here, in memory, and is never written. Nothing in Orientim
    is modified; the functions are restored when the block ends.
    """
    HEADER = "mcp-session-id"

    def __init__(self):
        self.map = {}

    def token(self, value):
        return self.map.setdefault(
            value, "sess-" + secrets.token_hex(8))

    def __enter__(self):
        self._resp, self._fp = transport._resp_headers, transport._hdr_fp

        def resp_headers(headers):
            out = self._resp(headers)
            for k in list(out):
                if k.lower() == self.HEADER and out[k]:
                    out[k] = self.token(out[k])
            return out

        def hdr_fp(headers):
            items = {}
            for k, v in headers.items():
                if k.lower() == self.HEADER and v in self.map:
                    v = self.map[v]
                items[k] = v
            return self._fp(items)

        transport._resp_headers, transport._hdr_fp = resp_headers, hdr_fp
        return self

    def __exit__(self, *exc):
        transport._resp_headers, transport._hdr_fp = self._resp, self._fp
        return False


# --- the measurements ---------------------------------------------------------

def measure():
    srv = _serve()
    rows = {}
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)

        # A — the control. Today's recorder, a session derived from a response.
        seen = []
        path = _record(agent(seen), "a")
        replayed = []
        rows["A_control"] = {
            "client_derived_at_record": seen[0],
            "client_derived_at_replay": replayed[0] if replayed else None,
            "replay": _verdict(session.replay(path, agent(replayed),
                                              strict=True)),
            "exposure": _session_in(path),
        }
        rows["A_control"]["client_derived_at_replay"] = (
            replayed[0] if replayed else None)
        rows["A_control"]["replay_echoed_the_real_identifier"] = bool(
            replayed and replayed[0] == REAL["id"])

        # B — what removing it costs. The header is dropped from the stored
        # response, which is all a deny-list can do.
        dropped = _rewrite(path, "b_dropped", lambda s: (s.get("headers") or {})
                           .pop("mcp-session-id", None))
        rows["B_dropped"] = {"replay": _verdict(
            session.replay(dropped, agent(), strict=True))}

        # B2 — and what one shared placeholder costs: the client echoes a value
        # the recorded fingerprint was never taken over.
        def placeholder(s):
            h = s.get("headers") or {}
            if "mcp-session-id" in h:
                h["mcp-session-id"] = "<redacted>"

        shared = _rewrite(path, "b_placeholder", placeholder)
        rows["B_placeholder"] = {"replay": _verdict(
            session.replay(shared, agent(), strict=True))}

        # C — the proposal. A distinct token, on both sides, at record time.
        seen_c = []
        path_c = _record(agent(seen_c), "c", patched=True)
        replayed_c = []
        rows["C_proposal"] = {
            "client_derived_at_record": seen_c[0],
            "replay": _verdict(session.replay(path_c, agent(replayed_c),
                                              strict=True)),
            "client_derived_at_replay": replayed_c[0] if replayed_c else None,
            "exposure": _session_in(path_c),
        }
        token_c = _session_in(path_c)["stored"][0]
        rows["C_proposal"]["replay_echoed_the_stored_token"] = bool(
            replayed_c and replayed_c[0] == token_c)

        # D — two sessions stay two sessions. A second run, a second real
        # identifier, a second token; and one recording replayed by a client
        # holding the other's token.
        REAL["id"] = "sess-" + secrets.token_hex(16)
        seen_d = []
        path_d = _record(agent(seen_d), "d", patched=True)
        token_d = _session_in(path_d)["stored"][0]
        cross = session.replay(path_c, agent(force=token_d), strict=True)
        rows["D_distinct"] = {
            "stored_token_1": token_c, "stored_token_2": token_d,
            "tokens_differ": token_c != token_d,
            "cross_replay": _verdict(cross),
            "exposure": _session_in(path_d),
        }
        return rows
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    m = measure()
    print("what a session identifier costs to store, and to remove")
    print()
    for name in ("A_control", "B_dropped", "B_placeholder", "C_proposal",
                 "D_distinct"):
        row = m[name]
        print("  %s" % name)
        for k, v in row.items():
            if k == "replay" or k == "cross_replay":
                print("     %-30s %-18s matched %s/%s headers_changed %s%s"
                      % (k, v["diagnosis"], v["matched"], v["recorded"],
                         v["headers_changed"],
                         "  raised %s" % v["raised"] if v["raised"] else ""))
            else:
                print("     %-30s %s" % (k, v))
        print()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
