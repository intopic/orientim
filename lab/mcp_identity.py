# -*- coding: utf-8 -*-
"""What identifies a JSON-RPC call over HTTP, in MCP's wire form.

Labelling, corrected after the JSON-RPC contract was written: what this
measures is **JSON-RPC identity and Orientim's matching**, driven with
MCP-shaped traffic so the bytes are real. It establishes nothing about MCP
semantics — that `tools/call` means an invocation request was sent, and not
that a tool ran, belongs to the profile above JSON-RPC. Read "MCP call"
throughout as "a JSON-RPC call in MCP's wire form".

    python lab/mcp_identity.py

An experiment. Nothing in Orientim changes: no format, no chain, no field, no
matcher.

MCP over HTTP is already recorded — the bytes cross the HTTP boundary like any
other tool call. The question this measures is narrower, and it decides the
design of a JSON-RPC layer: **is a recorded MCP call findable again?**
Orientim's lookup key is `method | url | body`, and a JSON-RPC body carries an
`id` the client mints per session. If that `id` is inside the key, a replay
whose session counted differently asks for a fixture it can never find.

Five measurements, each with its control first:

    A  what a recording of an MCP call already holds
    B  the same call, a different request id
    C  whether a real MCP client can consume a response carrying another id
    D  a different Mcp-Session-Id
    E  two calls with the same method and params, and different results

One thing this deliberately does not claim. `tools/call` on the wire is
evidence that **an invocation request was sent**. It is not evidence that the
server started the tool, that the tool ran, or that anything in the world
changed. The vocabulary here keeps to what the bytes show.
"""
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
from orientim import session, store, transport           # noqa: E402

try:
    from mcp import types as mt
    PROTOCOL = mt.LATEST_PROTOCOL_VERSION
    SDK = True
except Exception:                                        # pragma: no cover
    mt, PROTOCOL, SDK = None, "2025-11-25", False

PORT = 8806
BASE = "http://127.0.0.1:%d" % PORT
URL = BASE + "/mcp"
ROOT = os.path.join(HERE, "_runs", "mcp")
OUT = os.path.join(HERE, "_runs", "_mcp_identity.json")

SESSION_A = "sess-AAAAAAAAAAAAAAAA"
SESSION_B = "sess-BBBBBBBBBBBBBBBB"

P1 = {"name": "lookup_order", "arguments": {"order_id": 4471}}
P2 = {"name": "lookup_order", "arguments": {"order_id": 9002}}


# --- a wire-faithful MCP-over-HTTP server -------------------------------------
#
# Not the SDK's server: that one opens a long-lived GET stream whose lifetime
# would become the experiment rather than its subject. The request and response
# bodies are built by the installed SDK's own types, so the wire shape and the
# pinned protocol version are the SDK's and not mine.

def _result_for(params, n):
    name = (params or {}).get("name")
    args = (params or {}).get("arguments") or {}
    return {"content": [{"type": "text",
                         "text": "%s -> %s #%d" % (name, args.get("order_id"), n)}],
            "isError": False}


def _rpc_request(rid, method, params=None):
    if SDK and rid is not None:
        msg = mt.JSONRPCRequest(jsonrpc="2.0", id=rid, method=method,
                                params=params)
        return mt.JSONRPCMessage(msg).model_dump_json(
            by_alias=True, exclude_none=True).encode()
    if SDK:
        msg = mt.JSONRPCNotification(jsonrpc="2.0", method=method,
                                     params=params)
        return mt.JSONRPCMessage(msg).model_dump_json(
            by_alias=True, exclude_none=True).encode()
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    return json.dumps(body).encode()


def _rpc_response(rid, result):
    if SDK:
        msg = mt.JSONRPCResponse(jsonrpc="2.0", id=rid, result=result)
        return mt.JSONRPCMessage(msg).model_dump_json(
            by_alias=True, exclude_none=True).encode()
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()


class _MCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        method, rid = sent.get("method"), sent.get("id")
        if rid is None:                       # a notification has no id
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {},
                      "serverInfo": {"name": "lab-mcp", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "lookup_order", "description": "d",
                                 "inputSchema": {"type": "object"}}]}
        else:
            type(self).calls += 1
            result = _result_for(sent.get("params"), type(self).calls)
        body = _rpc_response(rid, result)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("MCP-Protocol-Version", PROTOCOL)
        if method == "initialize":
            self.send_header("Mcp-Session-Id", SESSION_A)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _MCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- the agents ---------------------------------------------------------------

def _headers(session_id=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": PROTOCOL}
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _call(client, rid, params, session_id=None):
    r = client.post(URL, content=_rpc_request(rid, "tools/call", params),
                    headers=_headers(session_id))
    return r.text


def opening(client, session_id=None):
    """initialize, the initialized notification, tools/list — as a client does."""
    client.post(URL, headers=_headers(),
                content=_rpc_request(1, "initialize",
                                     {"protocolVersion": PROTOCOL,
                                      "capabilities": {},
                                      "clientInfo": {"name": "lab",
                                                     "version": "0"}}))
    client.post(URL, headers=_headers(session_id),
                content=_rpc_request(None, "notifications/initialized"))
    client.post(URL, headers=_headers(session_id),
                content=_rpc_request(2, "tools/list"))


def agent_one(call_id, session_id=None, seen=None):
    def agent(h):
        c = h.client()
        opening(c, session_id)
        got = _call(c, call_id, P1, session_id)
        if seen is not None:
            seen.append(got)
        h.output = got
    return agent


def agent_two(session_id=None, seen=None):
    """Two different calls, so the cursor's progress is observable."""
    def agent(h):
        c = h.client()
        opening(c, session_id)
        for rid, p in ((17, P1), (19, P2)):
            got = _call(c, rid, p, session_id)
            if seen is not None:
                seen.append(got)
        h.output = "two"
    return agent


def agent_duplicates(concurrent=False, seen=None):
    """Two calls with the same method and params, and different results."""
    def agent(h):
        c = h.client()
        opening(c)
        if not concurrent:
            for rid in (17, 18):
                got = _call(c, rid, P1)
                if seen is not None:
                    seen.append(got)
        else:
            out = {}

            def one(rid):
                out[rid] = _call(h.client(), rid, P1)

            ts = [threading.Thread(target=one, args=(r,)) for r in (17, 18)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            if seen is not None:
                seen.extend(out[r] for r in (17, 18))
        h.output = "dup"
    return agent


# --- helpers ------------------------------------------------------------------

def _record(agent, name):
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    dst = os.path.join(ROOT, name + ".jsonl")
    shutil.copyfile(h.path, dst)
    return dst


def _steps(path):
    meta, steps = store.load(path)
    return meta, [s for s in steps if s.get("t") == "http"]


def _rpc_of(step, field):
    try:
        return json.loads(step.get(field) or "")
    except Exception:
        return None


def _call_steps(steps):
    return [s for s in steps
            if (_rpc_of(s, "req") or {}).get("method") == "tools/call"]


def _verdict(d):
    return {"diagnosis": d.diagnosis[0] if d.diagnosis else "?",
            "ok": bool(d.ok), "matched": d.n_matched,
            "attempted": d.n_attempted, "recorded": d.n_recorded,
            "headers_changed": bool(d.headers_changed),
            "incomplete": bool(d.incomplete),
            "blocked": bool(d.blocked),
            "uncaptured": [u.get("kind") for u in (d.uncaptured or [])]}


def _key(step, body, strict):
    # The stored url is the redacted one, which is what the recorder keyed on;
    # for these requests redaction is a no-op.
    return transport._canon(step["method"], step["url"], body, strict)


def _compact(text):
    """The same JSON, serialized the way the SDK serializes it."""
    try:
        return json.dumps(json.loads(text), separators=(",", ":"))
    except Exception:
        return None


def _without_id(raw):
    obj = json.loads(raw)
    obj.pop("id", None)
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _sdk_version():
    try:
        import importlib.metadata as md
        return md.version("mcp")
    except Exception:
        return "?"


# --- A and B ------------------------------------------------------------------

def case_a_and_b():
    seen = []
    path = _record(agent_one(17, seen=seen), "a")
    _meta, steps = _steps(path)
    call = _call_steps(steps)[0]
    sent = _rpc_request(17, "tools/call", P1)

    req, resp = _rpc_of(call, "req"), _rpc_of(call, "body")
    a = {
        "request_semantically_lossless": req == json.loads(sent),
        "request_bytes_preserved": (call.get("req") or "").encode() == sent,
        "response_body_recorded": isinstance(resp, dict),
        "response_semantically_lossless": bool(
            resp and resp.get("id") == 17 and "result" in resp),
        # The server sent the SDK's compact serialization. If what is on disk
        # is not that, the body was re-encoded on the way in.
        "response_bytes_preserved": _compact(call.get("body")) == call.get("body"),
        "body_sha_over_stored_body": (
            call.get("body_sha") == hashlib.sha256(
                (call.get("body") or "").encode()).hexdigest()[:32]),
        "id_in_request": req.get("id") if req else None,
        "id_in_response": resp.get("id") if resp else None,
        "method_readable": req.get("method") if req else None,
        "params_readable": bool(req and req.get("params")),
        "role_assigned": call.get("role"),
        "http_steps_recorded": len(steps),
        "notification_recorded": any(
            (_rpc_of(s, "req") or {}).get("method") == "notifications/initialized"
            for s in steps),
    }

    b = {"control_strict": _verdict(session.replay(path, agent_one(17),
                                                   strict=True)),
         "control_loose": _verdict(session.replay(path, agent_one(17),
                                                  strict=False))}
    for strict in (True, False):
        d = session.replay(path, agent_one(83), strict=strict)
        b["mutated_" + ("strict" if strict else "loose")] = _verdict(d)

    other = _rpc_request(83, "tools/call", P1)
    b["keys"] = {
        "strict_equal": _key(call, sent, True) == _key(call, other, True),
        "loose_equal": _key(call, sent, False) == _key(call, other, False),
        "equal_once_id_removed": (_key(call, _without_id(sent), False)
                                  == _key(call, _without_id(other), False)),
    }
    return a, b


# --- C ------------------------------------------------------------------------

def _drive(shift, out, label):
    """One MCP ClientSession, answered with id + shift. shift=0 is the control."""
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.message import SessionMessage

    async def run():
        c_send, c_recv = anyio.create_memory_object_stream(10)
        s_send, s_recv = anyio.create_memory_object_stream(10)

        async def server():
            async for msg in s_recv:
                root = msg.message.root
                method = getattr(root, "method", None)
                rid = getattr(root, "id", None)
                if method == "initialize":
                    r = mt.JSONRPCResponse(jsonrpc="2.0", id=rid, result={
                        "protocolVersion": PROTOCOL, "capabilities": {},
                        "serverInfo": {"name": "lab", "version": "0"}})
                elif method == "tools/call":
                    out["client_asked_with_id"] = rid
                    r = mt.JSONRPCResponse(jsonrpc="2.0", id=rid + shift,
                                           result=_result_for(P1, 1))
                elif method == "tools/list":
                    # The SDK asks for this *during* call_tool, to validate the
                    # result against the tool's output schema. A server that
                    # does not answer it blocks the call — which is how this
                    # loop found out.
                    r = mt.JSONRPCResponse(jsonrpc="2.0", id=rid, result={
                        "tools": [{"name": "lookup_order", "description": "d",
                                   "inputSchema": {"type": "object"}}]})
                else:
                    continue
                await c_send.send(SessionMessage(mt.JSONRPCMessage(r)))

        async with anyio.create_task_group() as tg:
            tg.start_soon(server)
            async with ClientSession(c_recv, s_send) as sess:
                await sess.initialize()
                try:
                    with anyio.fail_after(2):
                        res = await sess.call_tool("lookup_order",
                                                   {"order_id": 4471})
                    out[label] = "consumed: %s" % res.content[0].text
                except TimeoutError:
                    out[label] = "never delivered (client timed out)"
                except Exception as e:
                    out[label] = "%s: %s" % (type(e).__name__, e)
            tg.cancel_scope.cancel()

    anyio.run(run)


def case_c():
    """Can a real MCP client consume a response carrying a different id?

    Not a stand-in for one: the installed SDK's own ClientSession, over memory
    streams, answered with a well-formed response whose id is not the one it
    asked with.
    """
    if not SDK:
        return {"ran": False, "reason": "mcp SDK not installed"}
    out = {"ran": True, "sdk": "mcp %s" % _sdk_version()}
    _drive(0, out, "control_same_id")
    _drive(66, out, "different_id")
    out["correlates_by"] = "request id (shared/session.py _response_streams)"
    return out


# --- D ------------------------------------------------------------------------

def _session_id_in_fingerprint():
    return "mcp-session-id" not in {h.lower() for h in transport.HEADER_DENY}


def case_d():
    recorded = []
    path = _record(agent_two(SESSION_A, recorded), "d")
    control = _verdict(session.replay(path, agent_two(SESSION_A), strict=True))
    seen = []
    d = session.replay(path, agent_two(SESSION_B, seen), strict=True)
    return {
        "control_same_session": control,
        "replayed_other_session": _verdict(d),
        "served_same_bytes": [s == r for s, r in zip(seen, recorded)],
        "served_same_json": [_compact(s) == _compact(r)
                             for s, r in zip(seen, recorded)],
        "responses_the_agent_got": seen,
        "session_id_in_match_key": False,
        "session_id_in_hdr_fp": _session_id_in_fingerprint(),
    }


# --- E ------------------------------------------------------------------------

def case_e():
    out = {}
    for label, concurrent in (("sequential", False), ("concurrent", True)):
        seen = []
        path = _record(agent_duplicates(concurrent, seen), "e_" + label)
        _meta, steps = _steps(path)
        dup = _call_steps(steps)
        raws = [_rpc_request(int((_rpc_of(s, "req"))["id"]), "tools/call", P1)
                for s in dup]
        texts = [((_rpc_of(s, "body") or {}).get("result", {})
                  .get("content") or [{}])[0].get("text") for s in dup]
        out[label] = {
            "calls": len(dup),
            "results": texts,
            "results_differ": len(set(texts)) == len(texts),
            "keys_differ_with_id": len({_key(dup[0], r, False)
                                        for r in raws}) == len(raws),
            "keys_collide_without_id": len({_key(dup[0], _without_id(r), False)
                                            for r in raws}) == 1,
            "evidence": {
                "occurrence": [s.get("i") for s in dup],
                "actor": [s.get("worker") for s in dup],
                "t0_ms": [round(s.get("t0", 0) * 1000.0, 2) for s in dup],
                "operation_field": any("operation" in s for s in dup),
                "causal_edge_field": any(
                    k in dup[0] for k in ("parent", "caused_by", "depends_on")),
            },
        }
    return out


def measure():
    srv = _serve()
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)
        a, b = case_a_and_b()
        return {"protocol": PROTOCOL, "sdk": _sdk_version(),
                "A": a, "B": b, "C": case_c(), "D": case_d(), "E": case_e()}
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    m = measure()
    print("what identifies an MCP call, and what a recording of one proves")
    print()
    print("  protocol %s   mcp sdk %s" % (m["protocol"], m["sdk"]))
    print()
    print("  A  what the recording already holds")
    for k, v in m["A"].items():
        print("       %-38s %s" % (k, v))
    print()
    print("  B  the same call, a different request id")
    for k in ("control_strict", "control_loose", "mutated_strict",
              "mutated_loose"):
        v = m["B"][k]
        print("       %-16s %-20s matched %s/%s" %
              (k, v["diagnosis"], v["matched"], v["recorded"]))
    for k, v in m["B"]["keys"].items():
        print("       key %-32s %s" % (k, v))
    print()
    print("  C  can a real MCP client consume the other id")
    for k, v in m["C"].items():
        print("       %-22s %s" % (k, v))
    print()
    print("  D  a different Mcp-Session-Id")
    for k in ("control_same_session", "replayed_other_session"):
        v = m["D"][k]
        print("       %-22s %-18s matched %s/%s  headers_changed %s" %
              (k, v["diagnosis"], v["matched"], v["recorded"],
               v["headers_changed"]))
    print("       served same bytes      %s" % m["D"]["served_same_bytes"])
    print("       served same json       %s" % m["D"]["served_same_json"])
    print("       refused                %s"
          % m["D"]["replayed_other_session"]["blocked"])
    print("       session id in hdr_fp   %s" % m["D"]["session_id_in_hdr_fp"])
    print()
    print("  E  two calls, same method and params, different results")
    for label, v in m["E"].items():
        e = v["evidence"]
        print("       %s" % label)
        print("         results              %s" % v["results"])
        print("         differ               %s" % v["results_differ"])
        print("         keys differ with id  %s" % v["keys_differ_with_id"])
        print("         collide without id   %s" % v["keys_collide_without_id"])
        print("         occurrence / actor   %s / %s"
              % (e["occurrence"], e["actor"]))
        print("         operation / causal   %s / %s"
              % (e["operation_field"], e["causal_edge_field"]))
    print()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
