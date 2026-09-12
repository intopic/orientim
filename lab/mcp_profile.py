# -*- coding: utf-8 -*-
"""What an MCP profile can read out of a recording, and what it cannot.

    python lab/mcp_profile.py

An experiment, and a contract's evidence. Nothing in Orientim changes here:
no format, no chain, no field, no matcher, no cursor, no lookup key, no
evaluator and no gate. The reader used is `orientim.rpc`, unmodified.

**The spec revision is pinned, and measured rather than assumed.** The profile
is written against the revision the installed SDK names as latest, and that
string is printed with the run: a profile that does not say which revision it
reads is a profile that will silently read the wrong one.

Three things this keeps apart, because they fail separately and the failures
look alike from a distance:

    a JSON response      one exchange, one body, paired by the transport
    messages in SSE      one exchange, many messages, paired by nothing
    links beyond one     a server request and the client's answer to it, in
    exchange             two exchanges, joined only by an id

The third is where a profile is tempted to guess. When the stream identity,
the session identity or the protocol version is missing from the recording,
this states the limit and links nothing.
"""
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx                                            # noqa: E402
import orientim                                         # noqa: E402
from orientim import model, rpc, store                  # noqa: E402

try:
    import importlib.metadata as _md

    import mcp.types as mt
    SPEC = mt.LATEST_PROTOCOL_VERSION
    #: What a peer that declares no revision is treated as speaking. It is not
    #: the same string as the latest, and a profile that reads one where the
    #: other applies reads the wrong spec.
    FALLBACK = mt.DEFAULT_NEGOTIATED_VERSION
    SDK = _md.version("mcp")
except Exception:                                       # pragma: no cover
    SPEC, FALLBACK, SDK = "unknown", "unknown", "absent"

PORT = 8809
BASE = "http://127.0.0.1:%d" % PORT
URL = BASE + "/mcp"
ROOT = "lab/_runs/mcp_profile"
OUT = "lab/_runs/mcp_profile.json"

SESSION = "mcp-sess-0f1e2d3c4b5a6978"
SERVER_REQUEST_ID = "srv-1"


# --- a server in MCP's wire form ---------------------------------------------

def _tool_result(params, error=False):
    args = (params or {}).get("arguments") or {}
    return {"content": [{"type": "text",
                         "text": "order %s" % args.get("order_id")}],
            "isError": error}


def _result_for(method, params):
    if method == "initialize":
        return {"protocolVersion": SPEC,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "lab-mcp", "version": "0"}}
    if method == "tools/list":
        return {"tools": [{"name": "lookup_order", "description": "d",
                           "inputSchema": {"type": "object"}}]}
    if method == "tools/call":
        name = (params or {}).get("name")
        return _tool_result(params, error=(name == "always_fails"))
    return {}


def _event(payload, event_id=None):
    head = "" if event_id is None else "id: %s\n" % event_id
    return head + "data: %s\n\n" % json.dumps(payload)


class _MCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body=b"", ctype=None, session=False):
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("MCP-Protocol-Version", SPEC)
        if session:
            self.send_header("Mcp-Session-Id", SESSION)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        """The stream a client opens to hear from the server.

        It carries a request *from* the server, which is the direction that
        has no answer inside this exchange.
        """
        ask = {"jsonrpc": "2.0", "id": SERVER_REQUEST_ID,
               "method": "sampling/createMessage",
               "params": {"messages": [], "maxTokens": 16}}
        note = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        body = (_event(note, "e1") + _event(ask, "e2")).encode()
        self._send(200, body, "text/event-stream")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        method, rid = sent.get("method"), sent.get("id")
        mode = self.path.rsplit("/", 1)[-1]

        if method is None and rid is not None:
            # The client answering a request the server made earlier, on
            # another exchange. There is nothing to return.
            self._send(202)
            return
        if rid is None:                     # a notification: no response
            self._send(202)
            return

        result = _result_for(method, sent.get("params"))
        answer = {"jsonrpc": "2.0", "id": rid, "result": result}
        if mode == "sse":
            self._send(200, _event(answer, "r1").encode(),
                       "text/event-stream",
                       session=(method == "initialize"))
            return
        if mode == "cut":
            # A record the stream stopped in the middle of: no blank line, so
            # the event never ends and was never dispatched.
            whole = _event(answer, "r1")
            self._send(200, whole[:len(whole) - 24].encode(),
                       "text/event-stream")
            return
        self._send(200, json.dumps(answer).encode(), "application/json",
                   session=(method == "initialize"))


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _MCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- the client ---------------------------------------------------------------

def _headers(session=None, last_event=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": SPEC}
    if session:
        h["Mcp-Session-Id"] = session
    if last_event:
        h["Last-Event-ID"] = last_event
    return h


def _post(client, body, session=None, path="/mcp"):
    return client.post(BASE + path, content=json.dumps(body).encode(),
                       headers=_headers(session))


def normal_flow(_h):
    """initialize, initialized, tools/list, two tools/call — JSON responses."""
    with httpx.Client() as c:
        r = _post(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": SPEC, "capabilities": {},
                                 "clientInfo": {"name": "lab",
                                                "version": "0"}}})
        session = r.headers.get("Mcp-Session-Id")
        _post(c, {"jsonrpc": "2.0", "method": "notifications/initialized"},
              session)
        _post(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        _post(c, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "lookup_order",
                             "arguments": {"order_id": "A-1"}}}, session)
        _post(c, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "always_fails",
                             "arguments": {"order_id": "A-2"}}}, session)


def sse_flow(_h):
    """The same call, answered on an event stream instead of a JSON body."""
    with httpx.Client() as c:
        _post(c, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "lookup_order",
                             "arguments": {"order_id": "B-1"}}},
              path="/mcp/sse")


def cut_flow(_h):
    """An event stream that stops inside a record."""
    with httpx.Client() as c:
        _post(c, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                  "params": {"name": "lookup_order",
                             "arguments": {"order_id": "C-1"}}},
              path="/mcp/cut")


def reverse_flow(_h):
    """The server asks, on the stream; the client answers, in another POST."""
    with httpx.Client() as c:
        c.get(URL, headers=_headers(SESSION))
        _post(c, {"jsonrpc": "2.0", "id": SERVER_REQUEST_ID,
                  "result": {"model": "m", "role": "assistant",
                             "content": {"type": "text", "text": "ok"}}},
              SESSION)


def resume_flow(_h):
    """The same stream, asked for again with a Last-Event-ID."""
    with httpx.Client() as c:
        c.get(URL, headers=_headers(SESSION, last_event="e1"))


# --- reading it back ----------------------------------------------------------

def _record(agent, name):
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    dst = os.path.join(ROOT, name + ".jsonl")
    shutil.copyfile(h.path, dst)
    _meta, steps = store.load(dst)
    return [s for s in steps if s.get("t") == "http"]


def _sent(step):
    try:
        return json.loads(step.get("req") or "")
    except Exception:
        return None


def _header(step, name):
    for k, v in (step.get("headers") or {}).items():
        if k.lower() == name:
            return v
    return None


def _framed(step):
    """What the profile would have to do: frame first, then read each data
    payload as a message. `rpc` does not do this, and measuring it here says
    how far the pieces that already exist reach."""
    events, terminated, truncated, losses = model._sse_frames(
        step.get("body") or "")
    read = [rpc.read_message(e, rpc.RECEIVED, i)
            for i, e in enumerate(events) if isinstance(e, dict)]
    return {"events": len(events), "terminated": terminated,
            "truncated": truncated, "losses": losses,
            "kinds": [m["kind"] for m in read],
            "methods": [m["method"] for m in read if m["method"]],
            "ids": [m["id"].get("text") for m in read]}


def measure():
    out = {"spec": SPEC, "fallback": FALLBACK, "sdk": SDK,
           "schema": rpc.SCHEMA}
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)

    normal = _record(normal_flow, "normal")
    sse = _record(sse_flow, "sse")
    cut = _record(cut_flow, "cut")
    reverse = _record(reverse_flow, "reverse")
    resume = _record(resume_flow, "resume")

    ev = rpc.read_steps(normal)
    first = rpc.read_exchange(normal[0])
    out["json_path"] = {
        "content_type": _header(normal[0], "content-type"),
        "rpc_sent": len([m for m in first["messages"]
                         if m["ref"]["side"] == rpc.SENT]),
        "rpc_received": len([m for m in first["messages"]
                             if m["ref"]["side"] == rpc.RECEIVED]),
        "rpc_envelope": first["response_envelope"]["parse"],
        "rpc_links": [ln["link"] for ln in first["links"]],
    }
    init = json.loads(normal[0].get("body") or "{}")
    ok_call = json.loads(normal[3].get("body") or "{}")
    bad_call = json.loads(normal[4].get("body") or "{}")
    out["normal"] = {
        "steps": len(normal),
        "methods": [(_sent(s) or {}).get("method") for s in normal],
        "statuses": [s.get("status") for s in normal],
        "links": [[ln["link"] for ln in e["links"]] for e in ev],
        "answered": sum(len(e["answered"]) for e in ev),
        "summary": rpc.summarise(ev),
        "session_header": [_header(s, "mcp-session-id") for s in normal],
        "protocol_header": [_header(s, "mcp-protocol-version")
                            for s in normal],
        "negotiated": (init.get("result") or {}).get("protocolVersion"),
        "client_asked": ((_sent(normal[0]) or {}).get("params")
                         or {}).get("protocolVersion"),
        "tool_is_error": (ok_call.get("result") or {}).get("isError"),
        "failing_is_error": (bad_call.get("result") or {}).get("isError"),
        "failing_rpc_error": "error" in bad_call,
    }

    note = [s for s in normal
            if (_sent(s) or {}).get("method") == "notifications/initialized"][0]
    note_ev = rpc.read_exchange(note)
    out["notification"] = {
        "status": note.get("status"),
        "stored_body": repr(note.get("body")),
        "kinds": [m["kind"] for m in note_ev["messages"]],
        "links": [ln["link"] for ln in note_ev["links"]],
        "answered": len(note_ev["answered"]),
        "response_envelope": note_ev["response_envelope"]["parse"],
    }

    for label, steps in (("sse", sse), ("cut", cut), ("reverse", reverse)):
        step = steps[0]
        e = rpc.read_exchange(step)
        out[label] = {
            "content_type": _header(step, "content-type"),
            "status": step.get("status"),
            "rpc_sent": len([m for m in e["messages"]
                             if m["ref"]["side"] == rpc.SENT]),
            "rpc_received": len([m for m in e["messages"]
                                 if m["ref"]["side"] == rpc.RECEIVED]),
            "rpc_envelope": e["response_envelope"]["parse"],
            "rpc_links": [ln["link"] for ln in e["links"]],
            "rpc_answered": len(e["answered"]),
            "framed": _framed(step),
        }

    answer = reverse[1]
    ans = rpc.read_exchange(answer)
    out["reverse"]["answer_in_another_exchange"] = {
        "status": answer.get("status"),
        "kinds": [m["kind"] for m in ans["messages"]],
        "links": [ln["link"] for ln in ans["links"]],
        "findings": [f for ln in ans["links"] for f in ln["findings"]],
        "answered": len(ans["answered"]),
    }
    out["resume"] = {
        "hdr_fp_differs": resume[0].get("hdr_fp") != reverse[0].get("hdr_fp"),
        "last_event_id_readable": "Last-Event-ID" in json.dumps(resume[0]),
        "stream_ids_in_body": [ln.strip() for ln
                               in (resume[0].get("body") or "").splitlines()
                               if ln.startswith("id:")],
    }
    out["request_side"] = {
        "step_fields": sorted(normal[0].keys()),
        "session_recoverable": SESSION in json.dumps(normal[2]),
        "hdr_fp_len": len(normal[0].get("hdr_fp") or ""),
    }
    return out


def _row(fact, needs, has, limit, measured):
    return {"fact": fact, "needs": needs, "has": has, "limit": limit,
            "measured": measured}


def table(m):
    n, note = m["normal"], m["notification"]
    sse, cut, rev = m["sse"], m["cut"], m["reverse"]
    answer = rev["answer_in_another_exchange"]
    return [
        _row("a session was initialized",
             "an initialize request linked to its InitializeResult",
             "both bodies, and an rpc link inside one exchange",
             "the link holds inside one HTTP exchange and nowhere else",
             "CLOSED: link %s" % n["links"][0]),
        _row("the protocol revision in force",
             "protocolVersion in the InitializeResult",
             "the response body, and the client's ask in the request body",
             "what the server stated, not what either side then did",
             "CLOSED: asked %s, answered %s"
             % (n["client_asked"], n["negotiated"])),
        _row("the session identifier the server issued",
             "Mcp-Session-Id on the initialize response",
             "stored response headers",
             "plaintext unless session pseudonymisation is on",
             "CLOSED: %r" % (n["session_header"][0],)),
        _row("a later request belonged to that session",
             "Mcp-Session-Id on the request",
             "hdr_fp only: a truncated digest over all request headers",
             "not extractable, and not comparable on its own",
             "NOT DETERMINED: recoverable=%s, hdr_fp is %d chars"
             % (m["request_side"]["session_recoverable"],
                m["request_side"]["hdr_fp_len"])),
        _row("the client declared itself initialized",
             "a notifications/initialized message in a request body",
             "the request body, read as a notification",
             "202 is the POST accepted, not the notification processed",
             "CLOSED: kinds %s, link %s, answered %d"
             % (note["kinds"], note["links"], note["answered"])),
        _row("a tool invocation was requested",
             "a tools/call request carrying params.name",
             "the request body",
             "a request was sent; not that a tool ran",
             "CLOSED: %d answered across the normal flow" % n["answered"]),
        _row("an invocation returned a result",
             "a linked response carrying result",
             "an rpc link in CONFIRMED",
             "the result is the server's text, not the world's state",
             "CLOSED: links %s" % [x[0] for x in n["links"]]),
        _row("a tool error, apart from a protocol error",
             "result.isError true, versus a JSON-RPC error member",
             "both response bodies",
             "two different facts; merging them loses which one failed",
             "CLOSED: isError=%s rpc_error=%s"
             % (n["failing_is_error"], n["failing_rpc_error"])),
        _row("a message carried in an event stream",
             "SSE framing, then each data payload read as a message",
             "the raw body is stored; framing lives in model, not in rpc",
             "rpc reads a whole SSE body as one JSON document",
             "OPEN: rpc reads %d received message(s), envelope %s; after "
             "framing, %d event(s) -> %s"
             % (sse["rpc_received"], sse["rpc_envelope"],
                sse["framed"]["events"], sse["framed"]["kinds"])),
        _row("the stream carried every message it was going to",
             "a framing terminator, or a declared loss",
             "model._sse_frames losses",
             "MCP has no [DONE]; a stream ends when the response ends",
             "CLOSED as a loss: %s" % (cut["framed"]["losses"],)),
        _row("the server sent a request of its own",
             "a request message in the received direction",
             "rpc keeps it, and refuses to pair it",
             "nothing inside that exchange can answer it",
             "CLOSED as unlinked, and only after framing: rpc reads %d, "
             "framing finds %s -> %s"
             % (rev["rpc_received"], rev["framed"]["methods"],
                rev["framed"]["kinds"])),
        _row("the client answered that request",
             "the request and the response joined across two exchanges",
             "both bodies exist; the reader's scope is one exchange",
             "needs a run-level linker, and stream identity to scope it",
             "NOT DETERMINED: %s, link %s, answered %d — %s"
             % (answer["kinds"], answer["links"], answer["answered"],
                (answer["findings"] or ["-"])[0].split(":")[0])),
        _row("the stream was resumed where it stopped",
             "Last-Event-ID on the request, and id: fields in the stream",
             "the stream ids are in the body; the request header is not",
             "hdr_fp changes, and never says which header changed",
             "NOT DETERMINED: ids %s, header readable=%s, hdr_fp differs=%s"
             % (m["resume"]["stream_ids_in_body"],
                m["resume"]["last_event_id_readable"],
                m["resume"]["hdr_fp_differs"])),
        _row("the order of messages within a session",
             "stream identity, and a sequence inside it",
             "the step index i, which is per recording",
             "two streams in one recording interleave by step, not by stream",
             "NOT DETERMINED: no stream identity is stored"),
    ]


def main():
    srv = _serve()
    try:
        m = measure()
    finally:
        srv.shutdown()
        srv.server_close()

    print("MCP profile over HTTP — what a recording already closes")
    print()
    print("  spec revision   %s   (mcp sdk %s)" % (m["spec"], m["sdk"]))
    print("  no revision     %s   is what a peer that declares none gets"
          % m["fallback"])
    print("  rpc reader      SCHEMA %d" % m["schema"])
    print()
    print("  the normal flow")
    print("     %-28s %-8s %s" % ("method", "status", "links"))
    for method, status, links in zip(m["normal"]["methods"],
                                     m["normal"]["statuses"],
                                     m["normal"]["links"]):
        print("     %-28s %-8s %s" % (method, status, ",".join(links) or "-"))
    print("     answered %d of %d requests"
          % (m["normal"]["answered"],
             m["normal"]["summary"]["kinds"].get("request", 0)))
    print()
    print("  three reading paths, and they are not one")
    print("     %-22s %-24s %7s %7s %9s %s"
          % ("path", "content-type", "sent", "recv", "framed", "pairing"))
    j, sse, cut, rev = (m["json_path"], m["sse"], m["cut"], m["reverse"])
    rows = (
        ("a JSON response", j["content_type"], j["rpc_sent"],
         j["rpc_received"], "-", "transport, then the id"),
        ("an SSE response", sse["content_type"], sse["rpc_sent"],
         sse["rpc_received"], sse["framed"]["events"], "the id alone"),
        ("an SSE stream (GET)", rev["content_type"], rev["rpc_sent"],
         rev["rpc_received"], rev["framed"]["events"], "none in scope"),
        ("an SSE stream, cut", cut["content_type"], cut["rpc_sent"],
         cut["rpc_received"], cut["framed"]["events"], "none: a loss"),
    )
    for name, ctype, sent, recv, framed, pairing in rows:
        print("     %-22s %-24s %7s %7s %9s %s"
              % (name, ctype, sent, recv, framed, pairing))
    print()
    print("     `recv` is what rpc reads out of the response body today;")
    print("     `framed` is what SSE framing finds in the same bytes.")
    print()
    for r in table(m):
        print("  %s" % r["fact"])
        print("     needs     %s" % r["needs"])
        print("     has       %s" % r["has"])
        print("     limit     %s" % r["limit"])
        print("     measured  %s" % r["measured"])
        print()
    m["table"] = table(m)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
