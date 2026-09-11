# -*- coding: utf-8 -*-
"""What a session identifier costs to store, and what it costs to remove.

    python lab/session_identity.py

An experiment for the session-identity contract. Nothing in Orientim changes
on disk: no format, no chain, no field, no matcher. The cases that need the
proposed recorder behaviour *simulate* it by wrapping two functions inside
this process only, which is the point of running it before writing any of it.

An MCP server mints a session identifier and returns it in a response header.
The client reads it **there** and sends it back on every later request, so the
identifier is not a constant the agent was configured with — it is a value the
run derived from a response. That is what makes it awkward:

    store it              a bearer-ish identifier sits in a shared file
    drop it               the replayed client has nothing to echo
    replace them all      two different sessions become the same session

A --- D measure the first three and the fourth option: one distinct token per
value, applied on both sides at record time.

E --- I are the boundary. A session the run did *not* derive from a response,
two recordings of one session, two sessions in one recording, and what the
transform does not cover.
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
from orientim import diff, session, store, transport      # noqa: E402

try:
    from mcp import types as mt
    PROTOCOL = mt.LATEST_PROTOCOL_VERSION
except Exception:                                        # pragma: no cover
    mt, PROTOCOL = None, "2025-11-25"

PORT = 8808
URL = "http://127.0.0.1:%d/mcp" % PORT
ROOT = os.path.join(HERE, "_runs", "session")
OUT = os.path.join(HERE, "_runs", "_session_identity.json")
HEADER = "mcp-session-id"

P1 = {"name": "lookup_order", "arguments": {"order_id": 4471}}

# What a real server mints: unguessable, and meaningful only inside its
# session. `configured` is the other kind — a value the client was started
# with, which no response ever emitted.
STATE = {
    "minted": [],
    "sticky": False,        # initialize returns the same session every time
    "echo": False,          # send the session header on every response
    "in_body": False,       # and put it in the result body too
    "configured": "sess-configured-" + secrets.token_hex(10),
}


def _mint():
    if STATE["sticky"] and STATE["minted"]:
        return STATE["minted"][-1]
    STATE["minted"].append("sess-" + secrets.token_hex(16))
    return STATE["minted"][-1]


def _known(value):
    return value and (value in STATE["minted"] or value == STATE["configured"])


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
        issued = None
        if method == "initialize":
            issued = _mint()
            result = {"protocolVersion": PROTOCOL, "capabilities": {},
                      "serverInfo": {"name": "lab", "version": "0"}}
        elif not _known(given):
            # The server enforces it, so "the client echoed it" is a fact of
            # the run rather than an assumption of the experiment.
            return self._send(400, {"jsonrpc": "2.0", "id": rid,
                                    "error": {"code": -32600,
                                              "message": "bad session"}}, None)
        elif method == "tools/list":
            result = {"tools": [{"name": "lookup_order", "description": "d",
                                 "inputSchema": {"type": "object"}}]}
        else:
            text = "order 4471"
            if STATE["in_body"]:
                # Some servers say it again where nobody is looking.
                text += " (session %s)" % given
            result = {"content": [{"type": "text", "text": text}],
                      "isError": False}
        header = issued or (given if STATE["echo"] else None)
        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result}, header)

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


# --- agents -------------------------------------------------------------------

def _headers(session_id=None):
    h = {"Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL}
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def derived(seen=None, force=None):
    """initialize, read Mcp-Session-Id from the response, then use it."""
    def run(h):
        c = h.client()
        r = c.post(URL, headers=_headers(), content=_rpc(1, "initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "lab", "version": "0"}}))
        got = force or r.headers.get(HEADER) or ""
        if seen is not None:
            seen.append(got)
        c.post(URL, headers=_headers(got), content=_rpc(2, "tools/list"))
        last = c.post(URL, headers=_headers(got),
                      content=_rpc(3, "tools/call", P1))
        h.output = last.text
    return run


def configured():
    """A session the client already had. No initialize, nothing derived."""
    def run(h):
        c = h.client()
        s = STATE["configured"]
        c.post(URL, headers=_headers(s), content=_rpc(2, "tools/list"))
        last = c.post(URL, headers=_headers(s),
                      content=_rpc(3, "tools/call", P1))
        h.output = last.text
    return run


def two_sessions(seen=None):
    """One run, two initializes, two servers' worth of session identity."""
    def run(h):
        c = h.client()
        got = []
        for rid in (1, 11):
            r = c.post(URL, headers=_headers(),
                       content=_rpc(rid, "initialize", {
                           "protocolVersion": PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "lab", "version": "0"}}))
            got.append(r.headers.get(HEADER) or "")
        for s, rid in zip(got, (3, 13)):
            c.post(URL, headers=_headers(s), content=_rpc(rid, "tools/call", P1))
        if seen is not None:
            seen.extend(got)
        h.output = "two"
    return run


# --- the proposal, simulated in this process only -----------------------------

class _pseudonymised(object):
    """Two wrappers, for the length of one recording.

    What a recorder would do: mint a distinct token the first time a session
    identifier is seen in a response, store the token instead of the
    identifier, and take the request fingerprint over the token as well — so
    the file is consistent with itself and the identifier never lands in it.

    `rule` is the difference E and F exist to measure:

        "derived"  tokenise only a value no earlier request already carried,
                   which is what "the run derived it from a response" means
                   in terms a recorder can actually check
        "naive"    tokenise every session header in every response

    The map lives here, in memory, and is never written. Nothing in Orientim
    is modified; the functions are restored when the block ends.
    """

    def __init__(self, rule="derived"):
        self.rule = rule
        self.map = {}
        self.from_requests = set()

    def token(self, value):
        # Same length and character class as the value it replaces. Whether a
        # strict client accepts it is not measured here and is not claimed.
        return self.map.setdefault(
            value, ("sess-" + secrets.token_hex(32))[:len(value)])

    def __enter__(self):
        self._resp, self._fp = transport._resp_headers, transport._hdr_fp

        def resp_headers(headers):
            out = self._resp(headers)
            for k in list(out):
                if k.lower() != HEADER or not out[k]:
                    continue
                v = out[k]
                if self.rule == "derived" and v in self.from_requests \
                        and v not in self.map:
                    continue        # the client already had it: not ours
                out[k] = self.token(v)
            return out

        def hdr_fp(headers):
            items = {}
            for k, v in headers.items():
                if k.lower() == HEADER and v:
                    self.from_requests.add(v)
                    v = self.map.get(v, v)
                items[k] = v
            return self._fp(items)

        # The request fingerprint is taken before the response headers are
        # stored, in the same step, so `from_requests` is already populated
        # when a response is examined.
        transport._resp_headers, transport._hdr_fp = resp_headers, hdr_fp
        return self

    def __exit__(self, *exc):
        transport._resp_headers, transport._hdr_fp = self._resp, self._fp
        return False


# --- helpers ------------------------------------------------------------------

def _record(fn, name, rule=None):
    if rule:
        with _pseudonymised(rule):
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


def _exposure(path, values):
    """What the file shows, and whether any real identifier survived in it."""
    raw = open(path, encoding="utf-8").read()
    _meta, steps = store.load(path)
    stored = sorted({(s.get("headers") or {}).get(HEADER)
                     for s in steps if s.get("t") == "http"} - {None})
    return {"stored_headers": stored,
            "a_real_id_is_in_the_file": any(v and v in raw for v in values)}


def _fps(path):
    _meta, steps = store.load(path)
    return [s.get("hdr_fp") for s in steps if s.get("t") == "http"]


def _diff(a, b):
    cmp = diff.compare(a, b)
    whys = [r.get("why") for r in cmp["steps"] if r.get("op") != "same"]
    return {"identical": cmp["identical"],
            "changed_steps": len([w for w in whys if w]),
            "why": sorted({w for w in whys if w})}


# --- the measurements ---------------------------------------------------------

def measure():
    srv = _serve()
    rows = {}
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)

        # A --- the control. Today's recorder, a session derived from a response.
        seen, replayed = [], []
        path = _record(derived(seen), "a")
        rows["A_today_derived"] = {
            "replay": _verdict(session.replay(path, derived(replayed),
                                              strict=True)),
            "client_derived_at_record": seen[0],
            "client_derived_at_replay": replayed[0] if replayed else None,
            "exposure": _exposure(path, seen),
        }

        # B --- what removing it costs, which is all a deny-list can do.
        dropped = _rewrite(path, "b_dropped",
                           lambda s: (s.get("headers") or {}).pop(HEADER, None))
        rows["B_dropped"] = {"replay": _verdict(
            session.replay(dropped, derived(), strict=True))}

        # B2 --- and what one shared placeholder costs.
        def placeholder(s):
            h = s.get("headers") or {}
            if HEADER in h:
                h[HEADER] = "<redacted>"

        rows["B_placeholder"] = {"replay": _verdict(session.replay(
            _rewrite(path, "b_placeholder", placeholder), derived(),
            strict=True))}

        # C --- the proposal: a distinct token, both sides, at record time.
        seen_c, replayed_c = [], []
        path_c = _record(derived(seen_c), "c", rule="derived")
        rows["C_proposal_derived"] = {
            "replay": _verdict(session.replay(path_c, derived(replayed_c),
                                              strict=True)),
            "client_derived_at_record": seen_c[0],
            "client_derived_at_replay": replayed_c[0] if replayed_c else None,
            "exposure": _exposure(path_c, seen_c),
        }
        token_c = _exposure(path_c, seen_c)["stored_headers"][0]

        # D --- two recordings, two sessions, two tokens.
        seen_d = []
        path_d = _record(derived(seen_d), "d", rule="derived")
        token_d = _exposure(path_d, seen_d)["stored_headers"][0]
        rows["D_two_recordings"] = {
            "token_1": token_c, "token_2": token_d,
            "tokens_differ": token_c != token_d,
            "cross_replay": _verdict(session.replay(
                path_c, derived(force=token_d), strict=True)),
        }

        # E --- a session the run did not derive: no initialize, no response
        # carrying it. Today's recorder, and then the transform.
        STATE["echo"] = False
        cfg = [STATE["configured"]]
        path_e = _record(configured(), "e")
        path_e2 = _record(configured(), "e2", rule="derived")
        rows["E_configured_not_echoed"] = {
            "replay_today": _verdict(session.replay(path_e, configured(),
                                                    strict=True)),
            "exposure_today": _exposure(path_e, cfg),
            "replay_with_transform": _verdict(session.replay(
                path_e2, configured(), strict=True)),
            "exposure_with_transform": _exposure(path_e2, cfg),
        }

        # F --- the same, but the server echoes it on every response, so it
        # does reach the file. This is where the two rules disagree.
        STATE["echo"] = True
        path_f = _record(configured(), "f")
        path_f_naive = _record(configured(), "f_naive", rule="naive")
        path_f_derived = _record(configured(), "f_derived", rule="derived")
        rows["F_configured_echoed"] = {
            "replay_today": _verdict(session.replay(path_f, configured(),
                                                    strict=True)),
            "exposure_today": _exposure(path_f, cfg),
            "naive_rule_replay": _verdict(session.replay(
                path_f_naive, configured(), strict=True)),
            "naive_rule_exposure": _exposure(path_f_naive, cfg),
            "derived_rule_replay": _verdict(session.replay(
                path_f_derived, configured(), strict=True)),
            "derived_rule_exposure": _exposure(path_f_derived, cfg),
        }
        STATE["echo"] = False

        # G --- two recordings of the *same* real session. The fingerprint is
        # stable today because the value is; a per-recording token is not.
        STATE["sticky"] = True
        seen_g1, seen_g2 = [], []
        g1 = _record(derived(seen_g1), "g1")
        g2 = _record(derived(seen_g2), "g2")
        t1, t2 = [], []
        g1t = _record(derived(t1), "g1t", rule="derived")
        g2t = _record(derived(t2), "g2t", rule="derived")
        STATE["sticky"] = False
        rows["G_same_session_twice"] = {
            "same_real_session": seen_g1[0] == seen_g2[0],
            "today_fingerprints_equal": _fps(g1) == _fps(g2),
            "today_diff": _diff(g1, g2),
            "token_fingerprints_equal": _fps(g1t) == _fps(g2t),
            "token_diff": _diff(g1t, g2t),
        }

        # H --- two sessions inside one recording.
        seen_h, replayed_h = [], []
        path_h = _record(two_sessions(seen_h), "h", rule="derived")
        rows["H_two_sessions_one_recording"] = {
            "real_sessions_differ": seen_h[0] != seen_h[1],
            "exposure": _exposure(path_h, seen_h),
            "replay": _verdict(session.replay(path_h, two_sessions(replayed_h),
                                              strict=True)),
        }

        # I --- the limit of the guarantee. The same identifier, in a body.
        STATE["in_body"] = True
        seen_i = []
        path_i = _record(derived(seen_i), "i", rule="derived")
        STATE["in_body"] = False
        _m, steps_i = store.load(path_i)
        rows["I_scope_of_the_guarantee"] = {
            "exposure": _exposure(path_i, seen_i),
            "header_is_a_token": _exposure(path_i, seen_i)["stored_headers"],
            "identifier_still_in_a_response_body": any(
                seen_i[0] in (s.get("body") or "") for s in steps_i
                if s.get("t") == "http"),
        }
        return rows
    finally:
        srv.shutdown()
        srv.server_close()


def _show(name, row, indent="     "):
    print("  %s" % name)
    for k, v in row.items():
        if isinstance(v, dict) and "diagnosis" in v:
            print("%s%-30s %-18s matched %s/%s headers_changed %s%s"
                  % (indent, k, v["diagnosis"], v["matched"], v["recorded"],
                     v["headers_changed"],
                     "  raised %s" % v["raised"] if v["raised"] else ""))
        else:
            print("%s%-30s %s" % (indent, k, v))
    print()


def main():
    m = measure()
    print("what a session identifier costs to store, and to remove")
    print()
    for name in ("A_today_derived", "B_dropped", "B_placeholder",
                 "C_proposal_derived", "D_two_recordings",
                 "E_configured_not_echoed", "F_configured_echoed",
                 "G_same_session_twice", "H_two_sessions_one_recording",
                 "I_scope_of_the_guarantee"):
        _show(name, m[name])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
