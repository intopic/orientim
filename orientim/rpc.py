# -*- coding: utf-8 -*-
"""JSON-RPC 2.0, read out of what a recording already stores.

Read-side only. Nothing here captures, redacts, matches, replays or judges:
the lookup key, `chain.DIGEST_FIELDS`, the cursor, the evaluators and the
gates are untouched, and this module adds no field to a recording. It answers
one question — *what does the stored exchange let anyone say about the
messages in it* — and refuses the rest.

**What this evidence is about.** The stored representation, and not the wire.
Orientim stores a request body as `redact_body(bytes)` and a response body as
`redact_response(text)`, both of which re-serialise any JSON they can parse;
a binary body is stored as base64. So every statement below is a statement
about the representation on disk. Where the representation was transformed at
the field a link depends on, the link says so rather than claiming the
original correspondence — `REPRESENTATION_ONLY` exists for exactly that.

**Four things kept apart, because they fail separately.**

    validation          is this a well-formed JSON-RPC 2.0 message
    message origin      which side sent it: the request body, or the response
    correspondence scope within what a link holds — here, one HTTP exchange
    observation coverage what the recording lets anyone see at all

The third is the one that is easy to lose. A link in this module holds inside
**one HTTP transaction** and nowhere else. "No response" always means *no
response in this exchange*, never "no response anywhere": a response delivered
on a stream another transaction opened is out of scope, and joining the two
needs a run-level contract that does not exist yet.

**The asymmetry that shapes the states.** Over HTTP the transport has already
paired a request body with a response body before any `id` is read. So in the
ordinary case the id is a *second, independent* claim about a link that
already exists, and the two can disagree. In a batch, or for a message that
arrived on the other side from where its partner would be, the transport pairs
nothing and the id is the only link there is.

Out of v1, deliberately: a run-level linker, session or stream resumption, MCP
semantics — `tools/call` is a method name here and nothing more — and any new
SSE parsing.
"""
import json

SCHEMA = 1
VERSION = "2.0"

# What one message is.
REQUEST = "request"                 # a method, and an id member
NOTIFICATION = "notification"       # a method, and no id member
RESULT = "response_result"          # exactly result
ERROR = "response_error"            # exactly error
INVALID = "invalid"                 # not a well-formed 2.0 message
UNSUPPORTED = "unsupported"         # a version this reader does not read

# What happened to the envelope before any message was looked at.
OK = "ok"
ABSENT = "absent"                   # no body stored at all
JSON_NULL = "null"                  # a body of literal `null`
MALFORMED = "malformed"             # bytes that are not JSON
DUPLICATE_KEYS = "duplicate_keys"   # JSON with a repeated key: two readings
NOT_A_MESSAGE = "not_a_message"     # valid JSON, and neither object nor array
BINARY = "binary"                   # stored as base64; not read here

# How a response came to be attached to a request, and what that is worth.
CORROBORATED = "CORROBORATED"       # transport-paired, and the ids agree
BY_ID = "BY_ID"                     # the id is the only link
REPRESENTATION_ONLY = "REPRESENTATION_ONLY"   # equal only after a transform
CONFLICT = "CONFLICT"               # transport-paired, and the ids disagree
AMBIGUOUS = "AMBIGUOUS"             # more than one candidate on either side
UNLINKED = "UNLINKED"               # nothing in scope to attach it to
NOT_CONFIRMABLE = "NOT_CONFIRMABLE"  # a notification, by definition

SENT = "sent"                       # the request body: client to server
RECEIVED = "received"               # the response body: server to client
SCOPE = "http_exchange"

# What capture leaves behind when it rewrites a value. Equality of two of
# these is equality of the marker, not of what it replaced.
MARK = "<redacted>"

MAX_MESSAGES = 200                  # a batch longer than this is not enumerated


# --- identity -----------------------------------------------------------------

def id_class(value):
    """Which JSON type an id is. `bool` first: in Python it is an int."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "other"


def ids_equal(a, b):
    """Same id: the same JSON type *and* the same value. No conversion.

    Every trap here is one a conversion would have walked into. `7` and `"7"`
    are two ids. `0` and `""` are two ids and both are falsy. `1` and `1.0`
    are two ids, because an int and a float are two JSON forms and comparing
    them means choosing one — and a 20-digit integer put through a float
    stops being the id anybody sent. `True` and `1` are two ids.
    """
    ca, cb = id_class(a), id_class(b)
    if ca != cb or ca in ("other",):
        return False
    if ca == "null":
        return False        # null identifies nothing; see `_id_findings`
    return a == b


def _id_findings(present, value):
    """What is worth saying about an id, without deciding anything with it."""
    out = []
    if not present:
        return out
    cls = id_class(value)
    if cls == "null":
        out.append("id_null: the spec uses null for an id that could not be "
                   "determined, so it identifies nothing")
    elif cls == "float":
        out.append("id_fractional: the spec says a Number id SHOULD NOT "
                   "contain a fractional part")
    elif cls in ("bool", "other"):
        out.append("id_not_scalar: an id MUST be a String, a Number or Null")
    elif cls == "string" and MARK in value:
        out.append("id_transformed: capture rewrote this value, so an "
                   "equality here is between transformed representations")
    return out


def _transformed(value):
    return id_class(value) == "string" and MARK in value


# --- parsing that refuses to choose ------------------------------------------

class _Duplicate(ValueError):
    pass


def _no_duplicate_keys(pairs):
    seen = set()
    for k, _v in pairs:
        if k in seen:
            raise _Duplicate(k)
        seen.add(k)
    return dict(pairs)


def parse_envelope(text, binary=False):
    """One stored body, into a parse state and a list of raw messages.

    The envelope is validated apart from the messages in it, because the two
    fail separately: a batch can be well-formed and hold an invalid message,
    and a body can be unreadable while the other side of the exchange is
    perfectly readable.
    """
    out = {"parse": OK, "batch": False, "raw": [], "count": 0,
           "findings": []}
    if binary:
        out["parse"] = BINARY
        out["findings"].append(
            "binary_body: stored as base64 and not read as JSON here")
        return out
    if text is None or text == "":
        out["parse"] = ABSENT
        return out
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except _Duplicate as e:
        out["parse"] = DUPLICATE_KEYS
        out["findings"].append(
            "duplicate_key: %s appears twice, so this body has two readings "
            "and neither is chosen" % (e,))
        return out
    except Exception as e:
        out["parse"] = MALFORMED
        out["findings"].append("malformed_json: %s" % type(e).__name__)
        return out
    if obj is None:
        out["parse"] = JSON_NULL
        out["findings"].append("json_null: a body of literal null carries no "
                               "message")
        return out
    if isinstance(obj, list):
        out["batch"] = True
        out["count"] = len(obj)
        if not obj:
            out["findings"].append(
                "empty_batch: an empty Array is an Invalid Request")
        out["raw"] = obj[:MAX_MESSAGES]
        if len(obj) > MAX_MESSAGES:
            out["findings"].append(
                "batch_not_enumerated: %d messages, %d read"
                % (len(obj), MAX_MESSAGES))
        return out
    if isinstance(obj, dict):
        out["raw"], out["count"] = [obj], 1
        return out
    out["parse"] = NOT_A_MESSAGE
    out["findings"].append(
        "not_a_message: the body is valid JSON and is neither an object nor "
        "an array")
    return out


# --- one message --------------------------------------------------------------

def read_message(obj, side, index):
    """What one message is, from its own structure and never its position.

    Order in an array says nothing about what a message is, and arrival order
    says nothing about what it answers, so neither is read.
    """
    ref = {"side": side, "index": index}
    msg = {"ref": ref, "kind": INVALID, "method": None,
           "id_present": False, "id": None, "id_class": None,
           "findings": []}
    if not isinstance(obj, dict):
        msg["findings"].append("not_an_object: a message must be an object")
        return msg

    if obj.get("jsonrpc") != VERSION:
        msg["kind"] = UNSUPPORTED
        msg["findings"].append(
            "unsupported_version: jsonrpc=%r, not read as 2.0"
            % (obj.get("jsonrpc"),))
        return msg

    has_method, has_result = "method" in obj, "result" in obj
    has_error = "error" in obj
    msg["id_present"] = "id" in obj
    msg["id"] = obj.get("id")
    msg["id_class"] = id_class(obj.get("id")) if msg["id_present"] else None
    msg["findings"].extend(_id_findings(msg["id_present"], obj.get("id")))

    if has_method and not (has_result or has_error):
        method = obj.get("method")
        if not isinstance(method, str):
            msg["findings"].append("method_not_a_string")
            return msg
        msg["method"] = method
        params = obj.get("params")
        if "params" in obj and not isinstance(params, (list, dict)):
            # A scalar params is not a Request, and a message that is not a
            # Request cannot be the thing a response answers.
            msg["findings"].append(
                "params_not_structured: params MUST be an Array or an Object")
            return msg
        if msg["id_present"] and id_class(obj.get("id")) in ("bool", "other"):
            return msg          # the id finding is already recorded
        # The whole difference, and it is the member rather than its value:
        # {"id": null} is a request whose id is null, not a notification.
        msg["kind"] = REQUEST if msg["id_present"] else NOTIFICATION
        return msg

    if has_result and has_error:
        msg["findings"].append("result_and_error: a Response has exactly one")
        return msg
    if not (has_result or has_error):
        msg["findings"].append(
            "not_a_request_or_response: no method, no result, no error")
        return msg
    if not msg["id_present"]:
        msg["findings"].append(
            "response_without_id: a Response MUST carry an id member")
        return msg
    if id_class(obj.get("id")) in ("bool", "other"):
        return msg
    if has_error:
        err = obj.get("error")
        if not isinstance(err, dict):
            msg["findings"].append("error_not_an_object")
            return msg
        code = err.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            # A boolean is not an Integer, however Python compares it.
            msg["findings"].append(
                "error_code_not_an_integer: code=%r" % (code,))
            return msg
        if not isinstance(err.get("message"), str):
            msg["findings"].append(
                "error_without_message: message is REQUIRED and is a String")
            return msg
        msg["kind"] = ERROR
        return msg
    msg["kind"] = RESULT
    return msg


# --- correspondence -----------------------------------------------------------

def _candidates(msg, others):
    return [o for o in others if ids_equal(o["id"], msg["id"])]


def correspond(messages, transport_paired):
    """Link responses to requests inside one scope, from both sides.

    Both sides, deliberately. Asking only "which request does this response
    match" hides the case that matters most in a batch: one request with two
    responses has no unique answer, and a reader that stops at the first hit
    reports it as answered.

    Only a request on the **sent** side can be answered by a response on the
    **received** side. A request that arrived in a response body is a request
    from the server, and nothing in this exchange can answer it; a response
    sitting in a request body is the client answering something the server
    asked earlier. Both are kept as the messages they are, and neither is
    guessed into a link with another transaction.
    """
    links = []
    requests = [m for m in messages
                if m["kind"] == REQUEST and m["ref"]["side"] == SENT]
    responses = [m for m in messages
                 if m["kind"] in (RESULT, ERROR)
                 and m["ref"]["side"] == RECEIVED]
    stray = [m for m in messages
             if m not in requests and m not in responses
             and m["kind"] in (REQUEST, RESULT, ERROR)]

    for m in messages:
        if m["kind"] == NOTIFICATION:
            links.append({"request": m["ref"], "response": None,
                          "link": NOT_CONFIRMABLE, "id": None,
                          "method": m["method"],
                          "findings": ["a Notification has no Response "
                                       "object, by definition"]})

    for m in stray:
        links.append({
            "request": m["ref"] if m["kind"] == REQUEST else None,
            "response": m["ref"] if m["kind"] != REQUEST else None,
            "link": UNLINKED, "id": m["id"], "method": m["method"],
            "findings": ["out_of_scope_direction: a %s on the %s side cannot "
                         "be paired inside one HTTP exchange, and is not "
                         "joined to another one by guess"
                         % (m["kind"], m["ref"]["side"])]})

    one_to_one = (transport_paired and len(requests) == 1
                  and len(responses) == 1)

    for r in responses:
        hits = _candidates(r, requests)
        if id_class(r["id"]) == "null":
            links.append({"request": None, "response": r["ref"],
                          "link": UNLINKED, "id": None,
                          "findings": ["a null response id links nothing: it "
                                       "is the spec's value for an id that "
                                       "could not be determined"]})
            continue
        if len(hits) > 1:
            links.append({"request": None, "response": r["ref"],
                          "link": AMBIGUOUS, "id": r["id"],
                          "findings": ["%d requests in this exchange carry "
                                       "this id" % len(hits)]})
            continue
        if len(hits) == 1:
            req = hits[0]
            mine = [x for x in responses if ids_equal(x["id"], req["id"])]
            if len(mine) > 1:
                links.append({"request": req["ref"], "response": r["ref"],
                              "link": AMBIGUOUS, "id": r["id"],
                              "findings": ["%d responses in this exchange "
                                           "carry this id, so the request has "
                                           "no unique answer" % len(mine)]})
                continue
            state = CORROBORATED if one_to_one else BY_ID
            findings = []
            if _transformed(r["id"]) or _transformed(req["id"]):
                state = REPRESENTATION_ONLY
                findings.append(
                    "these ids agree in the stored representation, which "
                    "capture rewrote at this field: the original "
                    "correspondence is not shown")
            links.append({"request": req["ref"], "response": r["ref"],
                          "link": state, "id": r["id"],
                          "method": req["method"], "findings": findings})
            continue
        if one_to_one:
            links.append({
                "request": requests[0]["ref"], "response": r["ref"],
                "link": CONFLICT, "id": r["id"],
                "findings": ["the transport paired these two and the ids "
                             "disagree: this exchange carried the request id "
                             "%r" % (requests[0]["id"],)]})
        else:
            links.append({"request": None, "response": r["ref"],
                          "link": UNLINKED, "id": r["id"],
                          "findings": ["no request in this exchange carries "
                                       "this id"]})

    linked_requests = {id(x) for x in requests
                       for r in responses if ids_equal(r["id"], x["id"])}
    for req in requests:
        if id(req) in linked_requests:
            continue
        links.append({"request": req["ref"], "response": None,
                      "link": UNLINKED, "id": req["id"],
                      "method": req["method"],
                      "findings": ["no response in this exchange carries this "
                                   "id — which is not the same as no response "
                                   "anywhere"]})
    return links


ANSWERED = (CORROBORATED, BY_ID)


def answered(links):
    """The requests a response was shown to answer. Only from the links.

    Not from "a response came back", not from a status, and not from a count.
    `REPRESENTATION_ONLY` is not here: an agreement between two transformed
    values is not a demonstration about the originals.
    """
    return [ln["request"] for ln in links
            if ln["link"] in ANSWERED and ln.get("request")]


# --- the HTTP adapter ---------------------------------------------------------

def read_exchange(step):
    """One recorded HTTP step, as JSON-RPC evidence.

    The adapter is this small on purpose: the core above knows about messages
    and the scope, and this knows where a body is stored. Nothing else about
    the step is read — not the url, not the status, not the chain fields — and
    nothing about it is written.
    """
    step = step or {}
    ev = {"schema": SCHEMA, "scope": SCOPE, "step": step.get("i"),
          "representation": "stored", "messages": [], "links": [],
          "answered": [], "findings": []}
    if step.get("t") != "http":
        ev["findings"].append("not_an_http_step")
        return ev

    sent = parse_envelope(step.get("req"))
    got = parse_envelope(step.get("body"), binary=bool(step.get("b64")))
    ev["request_envelope"] = {k: sent[k] for k in ("parse", "batch", "count")}
    ev["response_envelope"] = {k: got[k] for k in ("parse", "batch", "count")}
    ev["findings"].extend("request_envelope: " + f for f in sent["findings"])
    ev["findings"].extend("response_envelope: " + f for f in got["findings"])

    for i, raw in enumerate(sent["raw"]):
        ev["messages"].append(read_message(raw, SENT, i))
    for i, raw in enumerate(got["raw"]):
        ev["messages"].append(read_message(raw, RECEIVED, i))

    # An unreadable response does not erase a readable request. The messages
    # that could be read are kept, the envelope says why the other side is
    # missing, and the link is absent rather than invented.
    transport_paired = not (sent["batch"] or got["batch"])
    ev["links"] = correspond(ev["messages"], transport_paired)
    ev["answered"] = answered(ev["links"])
    return ev


def read_steps(steps):
    """Every http step of a recording, each in its own scope.

    A list, and not a joined graph: linking across exchanges is a different
    contract, and this one does not have the evidence for it.
    """
    return [read_exchange(s) for s in (steps or [])
            if isinstance(s, dict) and s.get("t") == "http"]


def summarise(evidence):
    """Counts, for a caller that wants the shape without the detail.

    No payloads: `params`, `result` and `error.data` are never read out of a
    message by this module, so nothing here can carry them.
    """
    kinds, links = {}, {}
    for ev in evidence:
        for m in ev["messages"]:
            kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
        for ln in ev["links"]:
            links[ln["link"]] = links.get(ln["link"], 0) + 1
    return {"schema": SCHEMA, "exchanges": len(evidence), "kinds": kinds,
            "links": links,
            "answered": sum(len(ev["answered"]) for ev in evidence),
            "findings": sum(len(ev["findings"]) for ev in evidence)}
