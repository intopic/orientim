# -*- coding: utf-8 -*-
"""What an MCP profile can read out of a recording, and what it cannot.

    python lab/mcp_profile.py

An experiment, and a contract's evidence. Nothing in Orientim changes here: no
recording format, no chain, no field, no matcher, no cursor, no lookup key, no
replay semantics, no evaluator and no gate. The reader is `orientim.rpc` at
`SCHEMA 2`, unmodified, and the framing is `lab/sse_framing.py`, which knows
about line endings and nothing about payloads.

**Three layers, and the seams between them are the point.**

    framing      text in, records out. No JSON, no `[DONE]`, no model
                 conventions. A record that arrives and cannot be read is
                 still a record that arrived
    parsing      each record's payload goes to `rpc.parse_envelope` **as
                 text**, so a repeated key is refused instead of silently
                 collapsed, and a number keeps the decimal it was written with
    profile      MCP vocabulary over what those two produced, and nothing they
                 did not produce

**What a fact here is about.** One HTTP exchange, and the stored
representation of it. Not the session, not the run, not the wire. So there is
no fact here called *the session is initialized*, only *an initialize request
and a result were exchanged in this exchange*; no *effective protocol
version*, only *the revision this InitializeResult stated*; and no
correspondence across exchanges at all, because the evidence for one is not in
the recording.
"""
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx                                            # noqa: E402
import orientim                                         # noqa: E402
import sse_framing                                      # noqa: E402
from orientim import rpc, store                         # noqa: E402

#: The revision this profile is written against, pinned **explicitly**. Taking
#: it from the installed SDK would make the experiment mean something
#: different after an upgrade, silently, which is the one thing a profile may
#: not do about a version. What the SDK says is measured beside it and
#: reported, never substituted for it.
SPEC = "2025-11-25"

#: What a peer that declares no revision is treated as speaking. A different
#: string, and so a different reading: "no version was declared" and "version
#: 2025-11-25" are two facts, and a profile may not merge them.
NO_VERSION_DECLARED = "2025-03-26"


def _sdk():
    try:
        import importlib.metadata as md

        import mcp.types as mt
        return {"version": md.version("mcp"),
                "latest": mt.LATEST_PROTOCOL_VERSION,
                "default": mt.DEFAULT_NEGOTIATED_VERSION}
    except Exception:                                   # pragma: no cover
        return {"version": "absent", "latest": None, "default": None}


PORT = 8809
BASE = "http://127.0.0.1:%d" % PORT
URL = BASE + "/mcp"
ROOT = "lab/_runs/mcp_profile"
OUT = "lab/_runs/mcp_profile.json"

SESSION_A = "mcp-sess-0f1e2d3c4b5a6978"
SESSION_B = "mcp-sess-99887766554433ff"
SERVER_REQUEST_ID = "srv-1"

#: A decimal that a float cannot hold apart from 0.1. Two different ids, and a
#: reader that goes through float says they are one.
NEAR_TENTH = "0.1000000000000000055511151231257827"


# --- a server in MCP's wire form ---------------------------------------------

def _result_for(method, params):
    if method == "initialize":
        return {"protocolVersion": SPEC,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "lab-mcp", "version": "0"}}
    if method == "tools/list":
        return {"tools": [{"name": "lookup_order", "description": "d",
                           "inputSchema": {"type": "object"}}]}
    if method == "tools/call":
        args = (params or {}).get("arguments") or {}
        return {"content": [{"type": "text",
                             "text": "order %s" % args.get("order_id")}],
                "isError": (params or {}).get("name") == "always_fails"}
    return {}


def _event(payload_text, event_id=None):
    """One record, built from payload **text**, so the server can send bytes
    this lab's own json.dumps would never produce."""
    head = "" if event_id is None else "id: %s\n" % event_id
    return head + "data: %s\n\n" % payload_text


class _MCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body=b"", ctype=None, session=None):
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("MCP-Protocol-Version", SPEC)
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        """The stream a client opens to hear from the server.

        A notification, a record with an id and no data, and a request *from*
        the server, which is the direction nothing in this exchange answers.
        """
        note = json.dumps({"jsonrpc": "2.0",
                           "method": "notifications/tools/list_changed"})
        ask = json.dumps({"jsonrpc": "2.0", "id": SERVER_REQUEST_ID,
                          "method": "sampling/createMessage",
                          "params": {"messages": [], "maxTokens": 16}})
        body = (_event(note, "e1")
                + "id: e2\n\n"          # an id, and no data: not an event
                + _event(ask, "e3"))
        self._send(200, body.encode(), "text/event-stream")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        method, rid = sent.get("method"), sent.get("id")
        mode = self.path.rsplit("/", 1)[-1]

        if method is None and rid is not None:
            # The client answering a request the server made on another
            # exchange. There is nothing to return.
            self._send(202)
            return
        if rid is None:                     # a notification: no response
            self._send(202)
            return

        session = SESSION_A if method == "initialize" else None
        if mode == "badinit":
            # An InitializeResult that is not one: no protocolVersion, and a
            # serverInfo of the wrong shape. Well-formed JSON-RPC, and not a
            # result any revision may be read out of.
            body = json.dumps({"jsonrpc": "2.0", "id": rid,
                               "result": {"capabilities": {},
                                          "serverInfo": "lab-mcp"}}).encode()
            self._send(200, body, "application/json", session)
            return

        result = _result_for(method, sent.get("params"))
        answer = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})

        if mode == "sse":
            self._send(200, _event(answer, "r1").encode(),
                       "text/event-stream", session)
            return
        if mode == "cut":
            # The stream stops inside a record: no blank line, so the record
            # never ends and was never dispatched.
            whole = _event(answer, "r1")
            self._send(200, whole[:len(whole) - 24].encode(),
                       "text/event-stream")
            return
        if mode == "boundary":
            # One complete record, then the stream closes. The response to
            # this request never came, and the closing is clean.
            note = json.dumps({"jsonrpc": "2.0",
                               "method": "notifications/progress",
                               "params": {"progressToken": "p1",
                                          "progress": 1}})
            self._send(200, _event(note, "b1").encode(), "text/event-stream")
            return
        if mode == "dupe":
            # Two `result` members in one payload: two readings, and neither
            # is chosen. json.dumps cannot produce this, so it is written out.
            payload = ('{"jsonrpc":"2.0","id":%s,"result":{"a":1},'
                       '"result":{"a":2}}' % json.dumps(rid))
            self._send(200, _event(payload, "d1").encode(),
                       "text/event-stream")
            return
        if mode == "decimal":
            # The same id written another way, and a near neighbour a float
            # would merge with it.
            written = {"100": "1E+2", "0.1": NEAR_TENTH}.get(
                json.dumps(rid), json.dumps(rid))
            payload = ('{"jsonrpc":"2.0","id":%s,"result":{"ok":true}}'
                       % written)
            self._send(200, _event(payload, "n1").encode(),
                       "text/event-stream")
            return
        if mode == "emptyid":
            body = ("id: q1\n\n"            # an id, and no data field
                    "id: q2\ndata:\n\n"     # data fields coming to nothing
                    + _event(answer, "q3")
                    + "id: q4\n\n")         # the last id is not an event
            self._send(200, body.encode(), "text/event-stream")
            return

        self._send(200, answer.encode(), "application/json", session)


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
    """initialize, initialized, tools/list, two tools/call, JSON responses."""
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


def _one_call(path, rid=7, order="B-1"):
    def flow(_h):
        with httpx.Client() as c:
            _post(c, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                      "params": {"name": "lookup_order",
                                 "arguments": {"order_id": order}}},
                  SESSION_A, path=path)
    return flow


def decimal_flow(_h):
    """Two ids: one the server rewrites exactly, one it answers next to."""
    with httpx.Client() as c:
        _post(c, {"jsonrpc": "2.0", "id": 100, "method": "tools/list"},
              SESSION_A, path="/mcp/decimal")
        _post(c, {"jsonrpc": "2.0", "id": 0.1, "method": "tools/list"},
              SESSION_A, path="/mcp/decimal")


def badinit_flow(_h):
    with httpx.Client() as c:
        _post(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": SPEC, "capabilities": {},
                             "clientInfo": {"name": "lab", "version": "0"}}},
              path="/mcp/badinit")


def reverse_flow(_h):
    """The server asks, on the stream; the client answers, in another POST."""
    with httpx.Client() as c:
        c.get(URL, headers=_headers(SESSION_A))
        _post(c, {"jsonrpc": "2.0", "id": SERVER_REQUEST_ID,
                  "result": {"model": "m", "role": "assistant",
                             "content": {"type": "text", "text": "ok"}}},
              SESSION_A)


def two_clients_flow(_h):
    """Two clients, two sessions, and both of them mint id 1."""
    with httpx.Client() as a, httpx.Client() as b:
        _post(a, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "lookup_order",
                             "arguments": {"order_id": "from-A"}}}, SESSION_A)
        _post(b, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "lookup_order",
                             "arguments": {"order_id": "from-B"}}}, SESSION_B)


def resume_flow(_h):
    with httpx.Client() as c:
        c.get(URL, headers=_headers(SESSION_A, last_event="e1"))


# --- reading it back ----------------------------------------------------------

def _record(agent, name):
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    dst = os.path.join(ROOT, name + ".jsonl")
    shutil.copyfile(h.path, dst)
    _meta, steps = store.load(dst)
    return [s for s in steps if s.get("t") == "http"]


def _header(step, name):
    for k, v in (step.get("headers") or {}).items():
        if k.lower() == name:
            return v
    return None


def read_stream(text):
    """A stored event-stream body, read through the two layers in order.

    Framing first, and it stops at framing: each record's payload goes to the
    strict parser **as text**, so a repeated key is refused rather than
    silently collapsed and a number keeps the decimal it was written with.
    """
    framed = sse_framing.frames(text or "")
    out = {"records": len(framed["records"]), "losses": framed["losses"],
           "last_id": framed["last_id"], "parses": [], "findings": [],
           "messages": []}
    for rec in framed["records"]:
        env = rpc.parse_envelope(rec["data"])
        out["parses"].append(env["parse"])
        out["findings"].extend(env["findings"])
        for i, raw in enumerate(env["raw"]):
            out["messages"].append(rpc.read_message(raw, rpc.RECEIVED, i))
    return out


def read_exchange(step):
    """One exchange, JSON or stream, with the difference kept visible.

    A JSON body is one message the transport already paired with the request,
    so its id corroborates a link that exists. A stream is a sequence of
    messages the transport paired with *nothing*, so `transport_paired` is
    false there and an id is the only link on offer.
    """
    ctype = (_header(step, "content-type") or "").split(";")[0].strip()
    sent_env = rpc.parse_envelope(step.get("req"))
    sent = [rpc.read_message(raw, rpc.SENT, i)
            for i, raw in enumerate(sent_env["raw"])]

    if ctype == "text/event-stream":
        detail = read_stream(step.get("body"))
        received, paired = detail["messages"], False
        # A record that arrived and was not read is a candidate nobody ruled
        # out, exactly as an unread batch element is. An unterminated record
        # may have held the response, and a record past the framing bound
        # certainly was not looked at, so neither leaves the stream fully
        # enumerated. Without this the cut stream reads as UNLINKED, which
        # claims no response carried this id when one may well have.
        lost = detail["losses"]
        enumerated = bool(sent_env["enumerated"]
                          and not lost["unterminated"] and not lost["unread"])
    else:
        env = rpc.parse_envelope(step.get("body"),
                                 binary=bool(step.get("b64")))
        received = [rpc.read_message(raw, rpc.RECEIVED, i)
                    for i, raw in enumerate(env["raw"])]
        paired = not (sent_env["batch"] or env["batch"])
        enumerated = bool(sent_env["enumerated"] and env["enumerated"])
        detail = {"records": None, "losses": None, "last_id": None,
                  "parses": [env["parse"]], "findings": env["findings"],
                  "messages": received}

    messages = sent + received
    links = rpc.correspond(messages, paired, enumerated)
    return {
        "content_type": ctype, "status": step.get("status"),
        "transport_paired": paired,
        "sent": len(sent), "received": len(received),
        "kinds": [m["kind"] for m in messages],
        "rows": [{"kind": m["kind"], "method": m["method"],
                  "side": m["ref"]["side"]} for m in messages],
        "ids": [m["id"].get("text") for m in messages],
        "methods": [m["method"] for m in messages if m["method"]],
        "links": [ln["link"] for ln in links],
        "answered": len(rpc.answered(links)),
        "link_rows": [{"link": ln["link"], "method": ln.get("method"),
                       "id": (ln.get("id") or {}).get("text")}
                      for ln in links],
        "findings": (sent_env["findings"] + detail["findings"]
                     + [f for ln in links for f in ln["findings"]]),
        "stream": {k: detail[k]
                   for k in ("records", "losses", "last_id", "parses")},
    }


def by_method(exchanges):
    """Counts keyed by the method a request named, and never by position.

    A response carries no method of its own, so it is counted under the method
    of the request it was linked to. A response linked to nothing has no
    method to be counted under and lands in `(no request in scope)`, rather
    than being attributed to whichever request happened to be nearby.
    """
    rows = {}

    def row(name):
        return rows.setdefault(name, {"requests": 0, "notifications": 0,
                                      "answered": 0, "links": {}})

    for ex in exchanges:
        for m in ex["rows"]:
            if m["kind"] == rpc.REQUEST:
                row(m["method"] or "(no method)")["requests"] += 1
            elif m["kind"] == rpc.NOTIFICATION:
                row(m["method"] or "(no method)")["notifications"] += 1
        for ln in ex["link_rows"]:
            r = row(ln["method"] or "(no request in scope)")
            r["links"][ln["link"]] = r["links"].get(ln["link"], 0) + 1
            if ln["link"] in rpc.CONFIRMED:
                r["answered"] += 1
    return rows


def measure():
    sdk = _sdk()
    out = {"spec": SPEC, "no_version_declared": NO_VERSION_DECLARED,
           "sdk": sdk, "sdk_agrees": sdk["latest"] == SPEC,
           "schema": rpc.SCHEMA, "max_records": sse_framing.MAX_RECORDS}
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)

    runs = {
        "normal": normal_flow,
        "sse": _one_call("/mcp/sse"),
        "cut": _one_call("/mcp/cut"),
        "boundary": _one_call("/mcp/boundary"),
        "dupe": _one_call("/mcp/dupe"),
        "emptyid": _one_call("/mcp/emptyid"),
        "decimal": decimal_flow,
        "badinit": badinit_flow,
        "reverse": reverse_flow,
        "two_clients": two_clients_flow,
        "resume": resume_flow,
    }
    steps = {name: _record(flow, name) for name, flow in runs.items()}
    read = {name: [read_exchange(s) for s in ss] for name, ss in steps.items()}
    for name in runs:
        out[name] = {"exchanges": read[name], "by_method": by_method(read[name])}

    normal = steps["normal"]
    init = json.loads(normal[0].get("body") or "{}")
    bad = json.loads(steps["badinit"][0].get("body") or "{}")
    fails = json.loads(normal[4].get("body") or "{}")
    out["normal"].update({
        "stated_version": (init.get("result") or {}).get("protocolVersion"),
        "client_asked": ((json.loads(normal[0].get("req") or "{}")
                          .get("params")) or {}).get("protocolVersion"),
        "session_header": _header(normal[0], "mcp-session-id"),
        "protocol_header": _header(normal[0], "mcp-protocol-version"),
        "tool_is_error": (fails.get("result") or {}).get("isError"),
        "rpc_error": "error" in fails,
    })
    out["badinit"].update({
        "result_keys": sorted((bad.get("result") or {}).keys()),
        "stated_version": (bad.get("result") or {}).get("protocolVersion"),
        "server_info_is_object": isinstance(
            (bad.get("result") or {}).get("serverInfo"), dict),
    })

    ids = [set(e["ids"]) - {None} for e in read["two_clients"]]
    out["two_clients"]["shared_ids"] = sorted(
        ids[0] & ids[1]) if len(ids) > 1 else []
    out["two_clients"]["session_recoverable"] = any(
        SESSION_A in json.dumps(s) or SESSION_B in json.dumps(s)
        for s in steps["two_clients"])
    out["resume"]["last_event_id_readable"] = (
        "Last-Event-ID" in json.dumps(steps["resume"][0]))
    out["resume"]["hdr_fp_differs"] = (
        steps["resume"][0].get("hdr_fp") != steps["reverse"][0].get("hdr_fp"))
    out["request_side"] = {"hdr_fp_len": len(normal[0].get("hdr_fp") or "")}
    return out


# --- the matrix ---------------------------------------------------------------

def _row(fact, needs, has, limit, measured):
    return {"fact": fact, "needs": needs, "has": has, "limit": limit,
            "measured": measured}


def _live(counts):
    return {k: v for k, v in (counts or {}).items() if v}


def matrix(m):
    n = m["normal"]
    first = n["exchanges"][0]
    note = [e for e in n["exchanges"] if rpc.NOTIFICATION in e["kinds"]][0]
    sse = m["sse"]["exchanges"][0]
    cut = m["cut"]["exchanges"][0]
    bound = m["boundary"]["exchanges"][0]
    dupe = m["dupe"]["exchanges"][0]
    empty = m["emptyid"]["exchanges"][0]
    dec = m["decimal"]["exchanges"]
    rev = m["reverse"]["exchanges"]
    two = m["two_clients"]
    return [
        _row("an initialize request and a result were exchanged here",
             "both messages, and a link between them in this exchange",
             "both bodies; the transport paired them and the ids agree",
             "one exchange. Not a claim that a session exists, or that "
             "either side went on to use it",
             "CLOSED: %s" % first["links"]),
        _row("the revision this InitializeResult stated",
             "protocolVersion inside the result of this response",
             "the response body, and the client's ask in the request body",
             "what the server wrote here. Not a revision in force, and not a "
             "revision for any other exchange",
             "CLOSED: asked %s, stated %s"
             % (n["client_asked"], n["stated_version"])),
        _row("an InitializeResult that states no revision",
             "the same field, and the willingness to find it absent",
             "the response body",
             "a well-formed JSON-RPC result is not an InitializeResult, and "
             "no revision may be supplied from elsewhere",
             "CLOSED as undeclared: link %s, result keys %s, version %r, "
             "serverInfo an object=%s"
             % (m["badinit"]["exchanges"][0]["links"],
                m["badinit"]["result_keys"], m["badinit"]["stated_version"],
                m["badinit"]["server_info_is_object"])),
        _row("a session identifier appeared in this response",
             "Mcp-Session-Id among the stored response headers",
             "stored response headers",
             "the stored representation: plaintext, or a token if session "
             "pseudonymisation was on. Not proof the client then used it",
             "CLOSED: %r" % (n["session_header"],)),
        _row("a later request belonged to that session",
             "Mcp-Session-Id on the request",
             "hdr_fp only: a %d-character digest over all request headers"
             % m["request_side"]["hdr_fp_len"],
             "not extractable, and not comparable on its own",
             "NOT DETERMINED: recoverable=%s" % two["session_recoverable"]),
        _row("an initialized notification was sent in this exchange",
             "the message in a request body",
             "the request body, read as a notification",
             "202 is the POST accepted. Not the notification processed, and "
             "not a lifecycle completed",
             "CLOSED: %s, answered %d" % (note["links"], note["answered"])),
        _row("a tools/call request was sent in this exchange",
             "a tools/call request carrying params.name",
             "the request body",
             "a request was sent. Not that a tool ran",
             "CLOSED, by method: %s"
             % {k: v["requests"] for k, v in n["by_method"].items()
                if v["requests"]}),
        _row("a response carrying result was linked to it here",
             "a link in CONFIRMED inside this exchange",
             "rpc.correspond over this exchange's messages",
             "the result is the server's text, not the world's state",
             "CLOSED, answered by method: %s"
             % {k: v["answered"] for k, v in n["by_method"].items()
                if v["answered"]}),
        _row("a tool error, apart from a protocol error",
             "result.isError true, versus a JSON-RPC error member",
             "both response bodies",
             "two different facts; merging them loses which one failed",
             "CLOSED: isError=%s rpc_error=%s"
             % (n["tool_is_error"], n["rpc_error"])),
        _row("a message carried in an event stream",
             "framing, then the payload read as text by the strict parser",
             "lab/sse_framing.py, then rpc.parse_envelope",
             "the transport paired nothing here: the id is the only link",
             "CLOSED: %d record(s) -> %s, links %s, answered %d"
             % (sse["stream"]["records"], sse["stream"]["parses"],
                sse["links"], sse["answered"])),
        _row("a stream payload with a repeated key",
             "a parser that refuses two readings rather than choosing one",
             "rpc.parse_envelope over the record's text",
             "a lenient parser keeps the last value and reports nothing",
             "CLOSED as refused: parses %s, messages %d, links %s, "
             "answered %d"
             % (dupe["stream"]["parses"], dupe["received"], dupe["links"],
                dupe["answered"])),
        _row("a stream payload whose id is written another way",
             "exact decimal equality from the text, never through a float",
             "rpc.ids_equal over values parsed as Decimal",
             "equality is of the stored representation",
             "CLOSED: 100 vs 1E+2 -> %s answered %d; 0.1 vs its float "
             "neighbour -> %s answered %d"
             % (dec[0]["links"], dec[0]["answered"],
                dec[1]["links"], dec[1]["answered"])),
        _row("a record that carried an id and no data",
             "framing that dispatches nothing, and keeps the id",
             "lab/sse_framing.py losses",
             "an empty record is not an empty message; inventing one adds a "
             "message the stream never sent",
             "CLOSED: no_data=%d, records %d, last_id %r, links %s"
             % (empty["stream"]["losses"]["no_data"],
                empty["stream"]["records"], empty["stream"]["last_id"],
                empty["links"])),
        _row("a stream that stopped inside a record",
             "a framing loss, declared, and carried into the linking",
             "lab/sse_framing.py losses, consumed as enumeration",
             "HTTP 200 says the response ended, not that it was complete. A "
             "record that arrived unread is a candidate nobody ruled out, so "
             "the request is unenumerated rather than unanswered",
             "CLOSED as a loss, not as an absence: %s, records %d, links %s, "
             "answered %d"
             % (_live(cut["stream"]["losses"]), cut["stream"]["records"],
                cut["links"], cut["answered"])),
        _row("a stream that closed cleanly before the response",
             "framing losses at zero, and still no response message",
             "the framed records, and the correspondence over them",
             "a clean close is not an answer. Absence here is absence in "
             "this exchange and nowhere wider",
             "CLOSED as unanswered: losses %s, records %d, links %s, "
             "answered %d"
             % (_live(bound["stream"]["losses"]), bound["stream"]["records"],
                bound["links"], bound["answered"])),
        _row("the server sent a request of its own",
             "a request message in the received direction",
             "framing, then rpc keeps it and refuses to pair it",
             "nothing inside that exchange can answer it",
             "CLOSED as unlinked: %s -> %s, answered %d"
             % (rev[0]["methods"], rev[0]["links"], rev[0]["answered"])),
        _row("the client answered that request",
             "the two joined across two exchanges",
             "both bodies exist; the reader's scope is one exchange",
             "needs a run-level linker, and stream identity to scope it",
             "NOT DETERMINED: %s, %s, answered %d"
             % (rev[1]["kinds"], rev[1]["links"], rev[1]["answered"])),
        _row("two clients that both minted id 1",
             "something outside the id to tell the two apart",
             "two exchanges, each internally consistent",
             "an id is unique inside an exchange, and not inside a recording",
             "MEASURED, AND NOT A LINK: shared ids %s, each exchange %s"
             % (two["shared_ids"], [e["links"] for e in two["exchanges"]])),
        _row("the stream was resumed where it stopped",
             "Last-Event-ID on the request, and id: fields in the stream",
             "the ids are in the body; the request header is not",
             "hdr_fp changes, and never says which header changed",
             "NOT DETERMINED: last_id %r, header readable=%s, fp differs=%s"
             % (rev[0]["stream"]["last_id"],
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

    print("MCP profile over HTTP - what one exchange already closes")
    print()
    print("  spec revision    %s   pinned here, explicitly" % m["spec"])
    print("  the sdk says     %s latest, %s default   (mcp %s) agrees=%s"
          % (m["sdk"]["latest"], m["sdk"]["default"], m["sdk"]["version"],
             m["sdk_agrees"]))
    print("  undeclared       %s   is what a peer declaring none gets"
          % m["no_version_declared"])
    print("  rpc reader       SCHEMA %d      framing bound %d records"
          % (m["schema"], m["max_records"]))
    print()

    print("  the normal flow, counted by method")
    print("     %-32s %8s %13s %8s %s"
          % ("method", "requests", "notifications", "answered", "links"))
    for name, r in sorted(m["normal"]["by_method"].items()):
        print("     %-32s %8d %13d %8d %s"
              % (name, r["requests"], r["notifications"], r["answered"],
                 ", ".join("%s x%d" % (k, v)
                           for k, v in sorted(r["links"].items()))))
    print()

    print("  the reading paths, and they are not one")
    print("     %-24s %-20s %5s %5s %8s %7s %s"
          % ("path", "content-type", "sent", "recv", "records", "paired",
             "links"))
    paths = (("a JSON response", m["normal"]["exchanges"][0]),
             ("an SSE response", m["sse"]["exchanges"][0]),
             ("an SSE stream (GET)", m["reverse"]["exchanges"][0]),
             ("an SSE stream, cut", m["cut"]["exchanges"][0]),
             ("an SSE closed early", m["boundary"]["exchanges"][0]),
             ("an SSE with a dupe key", m["dupe"]["exchanges"][0]),
             ("an SSE id, no data", m["emptyid"]["exchanges"][0]))
    for label, e in paths:
        print("     %-24s %-20s %5d %5d %8s %7s %s"
              % (label, e["content_type"] or "-", e["sent"], e["received"],
                 e["stream"]["records"], e["transport_paired"],
                 ",".join(e["links"]) or "-"))
    print()

    for r in matrix(m):
        print("  %s" % r["fact"])
        print("     needs     %s" % r["needs"])
        print("     has       %s" % r["has"])
        print("     limit     %s" % r["limit"])
        print("     measured  %s" % r["measured"])
        print()

    m["matrix"] = matrix(m)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
