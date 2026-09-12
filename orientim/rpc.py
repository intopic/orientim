# -*- coding: utf-8 -*-
"""JSON-RPC 2.0, read out of what a recording already stores.

Read-side only. Nothing here captures, redacts, matches, replays or judges:
the lookup key, `chain.DIGEST_FIELDS`, the cursor, integrity, the evaluators
and the gates are untouched, and this module adds no field to a recording. It
answers one question — *what does the stored exchange let anyone say about the
messages in it* — and refuses the rest.

**What this evidence is about.** The stored representation, and not the wire.
Orientim stores a request body as `redact_body(bytes)` and a response body as
`redact_response(text)`, both of which re-serialise any JSON they can parse; a
binary body is stored as base64. So every statement below is a statement about
the representation on disk.

**Four things kept apart, because they fail separately.**

    validation          is this a well-formed JSON-RPC 2.0 message
    message origin      which side sent it: the request body, or the response
    correspondence scope within what a link holds — here, one HTTP exchange
    observation coverage what the recording lets anyone see at all

The third is the one that is easy to lose. A link here holds inside **one HTTP
exchange** and nowhere else, so "no response" always means *no response in
this exchange*, never "no response anywhere".

The fourth has teeth of its own. This reader stops after `MAX_MESSAGES` of a
batch, and a candidate it never read is a candidate it cannot rule out — so a
side that was not fully enumerated cannot produce a confirmed link. An
analysis limit must not manufacture uniqueness.

**The asymmetry that shapes the states.** Over HTTP the transport has already
paired a request body with a response body before any `id` is read. So in the
ordinary case the id is a *second, independent* claim about a link that
already exists, and the two can disagree. In a batch the transport pairs
nothing and the id is the only link there is.

Out of v1, deliberately: a run-level linker, session or stream resumption, MCP
semantics — `tools/call` is a method name here and nothing more — and any new
SSE parsing.
"""
import decimal
import json
import re

SCHEMA = 2
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
REPRESENTATION_ONLY = "REPRESENTATION_ONLY"   # equal, at a field a marker sits in
UNENUMERATED = "UNENUMERATED"       # a candidate we never read cannot be ruled out
CONFLICT = "CONFLICT"               # transport-paired, and the ids disagree
AMBIGUOUS = "AMBIGUOUS"             # more than one candidate, on either side
UNLINKED = "UNLINKED"               # nothing in scope to attach it to
NOT_CONFIRMABLE = "NOT_CONFIRMABLE"  # a notification, by definition

#: The only states in which a response was shown to answer a request.
CONFIRMED = (CORROBORATED, BY_ID)

SENT = "sent"                       # the request body: client to server
RECEIVED = "received"               # the response body: server to client
SCOPE = "http_exchange"

# What capture leaves behind when it rewrites a value. Seeing this in an id
# says a marker is *present* — never that capture put it there: a server may
# send the literal text, and from the stored representation the two are
# indistinguishable. `REPRESENTATION_ONLY` means "not shown to correspond".
MARK = "<redacted>"

MAX_MESSAGES = 200                  # a batch longer than this is not enumerated

# Id classes. `number` is one class: `1`, `1.0` and `1e0` are one id.
NUMBER, STRING, NULL, INVALID_ID = "number", "string", "null", "invalid"

_TOKEN = re.compile(r"^[0-9A-Za-z._+-]{1,16}$")


class _NonJSON(object):
    """`NaN`, `Infinity`, `-Infinity`: Python reads them, JSON does not have
    them, and neither does JSON-RPC. They are not values here."""

    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "<non-json %s>" % self.text


def _safe_token(value):
    """A short, boring string is safe to quote back. Nothing else is.

    A diagnostic is worth more when it names the value, and a value out of a
    stored body can be anything — a prompt, a key, a megabyte. So only a
    version-shaped token survives into a finding.
    """
    if isinstance(value, str) and _TOKEN.match(value):
        return value
    return None


# --- identity -----------------------------------------------------------------

def id_class(value):
    """Which kind of id this is. `bool` before `int`: in Python it is one."""
    if value is None:
        return NULL
    if isinstance(value, bool) or isinstance(value, _NonJSON):
        return INVALID_ID
    if isinstance(value, str):
        return STRING
    if isinstance(value, (int, decimal.Decimal)):
        return NUMBER
    if isinstance(value, float):
        # Only reachable if a caller parsed the text itself, without
        # `parse_float`. Kept as a number, and the text below is the float's.
        return NUMBER
    return INVALID_ID


def _decimal_of(value):
    if isinstance(value, decimal.Decimal):
        return value
    return decimal.Decimal(str(value)) if isinstance(value, float) \
        else decimal.Decimal(value)


def ids_equal(a, b):
    """Same id: the same kind, and — for numbers — the same exact value.

    Numbers are compared as exact decimals taken from the stored text, never
    through a binary float. Both directions of that matter, and both are
    measured:

        1, 1.0 and 1e0 are one id; 100 and 1e2 are one id
        0.1 and 0.1000000000000000055511151231257827 are two, and a float
        of either is the same float

    A string id is compared as text, so `7` and `"7"` stay two ids. Null
    identifies nothing and is never equal to anything, including itself —
    the spec uses it for an id that could not be determined.
    """
    ca, cb = id_class(a), id_class(b)
    if ca != cb or ca in (NULL, INVALID_ID):
        return False
    if ca == STRING:
        return a == b
    try:
        return _decimal_of(a) == _decimal_of(b)
    except (ArithmeticError, ValueError, TypeError):
        return False


def _number_text(value):
    """The exact decimal the id was written as, normalised only in form."""
    try:
        d = _decimal_of(value)
    except (ArithmeticError, ValueError, TypeError):
        return None
    return format(d.normalize(), "f") if d == d.to_integral_value() \
        else str(d.normalize())


def id_view(present, value):
    """The id, in a form that is safe to hand on.

    An id that is an object or an array is **not** exported: its class is the
    fact, and copying the structure out of a stored body would put a payload
    somewhere a payload has no business being.
    """
    if not present:
        return {"present": False}
    cls = id_class(value)
    out = {"present": True, "class": cls}
    if cls == STRING:
        out["text"] = value
        if MARK in value:
            out["marker"] = True
    elif cls == NUMBER:
        out["text"] = _number_text(value)
    return out


def _id_findings(present, value):
    """What is worth saying about an id, without deciding anything with it."""
    out = []
    if not present:
        return out
    cls = id_class(value)
    if cls == NULL:
        out.append("id_null: the spec uses null for an id that could not be "
                   "determined, so it identifies nothing")
    elif cls == INVALID_ID:
        out.append("id_not_scalar: an id MUST be a String, a Number or Null; "
                   "this one is %s" % _kind_name(value))
    elif cls == STRING and MARK in value:
        out.append("id_marker_present: this id carries the text capture "
                   "leaves behind when it rewrites a value. A marker is not "
                   "proof that capture put it there — a server may send the "
                   "same text — so an equality here is not shown to be an "
                   "equality of the originals")
    return out


def _kind_name(value):
    """A name with its article, so a finding reads as a sentence."""
    if isinstance(value, _NonJSON):
        return "a non-JSON constant"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "an array"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, (int, decimal.Decimal, float)):
        return "a number"
    return "a %s" % type(value).__name__


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


def _loads(text):
    """`json.loads`, minus three ways it is too generous.

    A repeated key raises rather than picking the last one. A number arrives
    as an exact decimal rather than a binary float, so two ids that differ in
    the twentieth digit stay two ids. And `NaN`/`Infinity`, which Python reads
    and JSON does not define, arrive as a value that is not a value.
    """
    return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                      parse_float=decimal.Decimal,
                      parse_constant=_NonJSON)


def parse_envelope(text, binary=False):
    """One stored body, into a parse state and a list of raw messages.

    `enumerated` is the coverage fact: false when a batch is longer than this
    reader reads. Everything downstream has to consume it, because a candidate
    nobody read is a candidate nobody ruled out.
    """
    out = {"parse": OK, "batch": False, "raw": [], "count": 0, "read": 0,
           "enumerated": True, "findings": []}
    if binary:
        out["parse"] = BINARY
        out["findings"].append(
            "binary_body: stored as base64 and not read as JSON here")
        return out
    if text is None or text == "":
        out["parse"] = ABSENT
        return out
    try:
        obj = _loads(text)
    except _Duplicate as e:
        out["parse"] = DUPLICATE_KEYS
        out["findings"].append(
            "duplicate_key: a key appears twice (%s), so this body has two "
            "readings and neither is chosen" % (_safe_token(str(e)) or "name "
                                                "withheld"))
        return out
    except Exception as e:
        out["parse"] = MALFORMED
        out["findings"].append("malformed_json: %s" % type(e).__name__)
        return out
    if obj is None:
        out["parse"] = JSON_NULL
        out["findings"].append(
            "json_null: a body of literal null carries no message")
        return out
    if isinstance(obj, _NonJSON):
        out["parse"] = NOT_A_MESSAGE
        out["findings"].append(
            "non_json_constant: the body is %s, which JSON does not define"
            % obj.text)
        return out
    if isinstance(obj, list):
        out["batch"] = True
        out["count"] = len(obj)
        if not obj:
            out["findings"].append(
                "empty_batch: an empty Array is an Invalid Request")
        out["raw"] = obj[:MAX_MESSAGES]
        out["read"] = len(out["raw"])
        if len(obj) > MAX_MESSAGES:
            out["enumerated"] = False
            out["findings"].append(
                "batch_not_enumerated: %d messages, %d read — a candidate "
                "that was not read cannot be ruled out"
                % (len(obj), MAX_MESSAGES))
        return out
    if isinstance(obj, dict):
        out["raw"], out["count"], out["read"] = [obj], 1, 1
        return out
    out["parse"] = NOT_A_MESSAGE
    out["findings"].append(
        "not_a_message: the body is valid JSON and is neither an object nor "
        "an array")
    return out


# --- one message --------------------------------------------------------------

def read_message(obj, side, index):
    """What one message is, from its own structure and never its position.

    `_id` is the parsed id, for the correspondence below. `id` is the view,
    which is what anybody else gets: an id that is an object or an array is
    reported by class and is not copied out.
    """
    ref = {"side": side, "index": index}
    msg = {"ref": ref, "kind": INVALID, "method": None,
           "id": {"present": False}, "_id": None, "id_present": False,
           "findings": []}
    if not isinstance(obj, dict):
        msg["findings"].append(
            "not_an_object: a message must be an object, this is %s"
            % _kind_name(obj))
        return msg

    version = obj.get("jsonrpc")
    if version != VERSION:
        msg["kind"] = UNSUPPORTED
        said = _safe_token(version)
        msg["findings"].append(
            "unsupported_version: the jsonrpc member is %s, and this reader "
            "reads 2.0" % ("%r" % said if said else _kind_name(version)))
        return msg

    has_method, has_result = "method" in obj, "result" in obj
    has_error = "error" in obj
    msg["id_present"] = "id" in obj
    msg["_id"] = obj.get("id")
    msg["id"] = id_view(msg["id_present"], obj.get("id"))
    msg["findings"].extend(_id_findings(msg["id_present"], obj.get("id")))

    if has_method and not (has_result or has_error):
        method = obj.get("method")
        if not isinstance(method, str):
            msg["findings"].append(
                "method_not_a_string: it is %s" % _kind_name(method))
            return msg
        msg["method"] = method
        if "params" in obj and not isinstance(obj["params"], (list, dict)):
            # A scalar params is not a Request, and a message that is not a
            # Request cannot be the thing a response answers.
            msg["findings"].append(
                "params_not_structured: params MUST be an Array or an "
                "Object, this is %s" % _kind_name(obj["params"]))
            return msg
        if msg["id_present"] and id_class(obj.get("id")) == INVALID_ID:
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
    if id_class(obj.get("id")) == INVALID_ID:
        return msg
    if has_error:
        err = obj.get("error")
        if not isinstance(err, dict):
            msg["findings"].append(
                "error_not_an_object: it is %s" % _kind_name(err))
            return msg
        code = err.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            # A boolean is not an Integer, however Python compares it. The
            # class is the finding; the value is not copied out.
            msg["findings"].append(
                "error_code_not_an_integer: it is %s" % _kind_name(code))
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

def correspond(messages, transport_paired, enumerated=True):
    """Link responses to requests inside one scope, from both sides.

    Both sides, deliberately. Asking only "which request does this response
    match" hides the case that matters most: one request with two responses
    has no unique answer, and a reader that stops at the first hit reports it
    as answered.

    `enumerated` is the coverage fact from the envelopes, and it is consumed
    here rather than reported beside the links: uniqueness is a claim about a
    whole candidate set, so a side this reader did not finish reading cannot
    produce `CORROBORATED`, `BY_ID` or an answer. The messages that *were*
    read are kept — the limit costs the link, not the evidence.

    Only a request on the **sent** side can be answered by a response on the
    **received** side. A request that arrived in a response body is a request
    from the server, and nothing in this exchange can answer it; a response in
    a request body is the client answering something asked earlier. Both are
    kept as the messages they are, and neither is guessed into a link with
    another transaction.
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
                          "link": NOT_CONFIRMABLE, "id": m["id"],
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
    unread = ["the candidate set was not fully read, so this id cannot be "
              "shown to be unique"] if not enumerated else []

    for r in responses:
        hits = [q for q in requests if ids_equal(q["_id"], r["_id"])]
        if id_class(r["_id"]) == NULL:
            links.append({"request": None, "response": r["ref"],
                          "link": UNLINKED, "id": r["id"],
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
            mine = [x for x in responses if ids_equal(x["_id"], req["_id"])]
            if len(mine) > 1:
                links.append({"request": req["ref"], "response": r["ref"],
                              "link": AMBIGUOUS, "id": r["id"],
                              "findings": ["%d responses in this exchange "
                                           "carry this id, so the request has "
                                           "no unique answer" % len(mine)]})
                continue
            if not enumerated:
                links.append({"request": req["ref"], "response": r["ref"],
                              "link": UNENUMERATED, "id": r["id"],
                              "method": req["method"], "findings": unread})
                continue
            state, findings = (CORROBORATED if one_to_one else BY_ID), []
            if r["id"].get("marker") or req["id"].get("marker"):
                state = REPRESENTATION_ONLY
                findings.append(
                    "these ids are equal in the stored representation, and a "
                    "capture marker sits in the field the equality rests on: "
                    "the correspondence of the originals is not shown")
            links.append({"request": req["ref"], "response": r["ref"],
                          "link": state, "id": r["id"],
                          "method": req["method"], "findings": findings})
            continue
        if one_to_one:
            links.append({
                "request": requests[0]["ref"], "response": r["ref"],
                "link": CONFLICT, "id": r["id"],
                "findings": ["the transport paired these two and the ids "
                             "disagree"]})
        else:
            links.append({"request": None, "response": r["ref"],
                          "link": UNLINKED, "id": r["id"],
                          "findings": ["no request in this exchange carries "
                                       "this id"] + unread})

    linked = {id(q) for q in requests
              for r in responses if ids_equal(r["_id"], q["_id"])}
    for req in requests:
        if id(req) in linked:
            continue
        links.append({"request": req["ref"], "response": None,
                      "link": UNENUMERATED if not enumerated else UNLINKED,
                      "id": req["id"], "method": req["method"],
                      "findings": (unread if not enumerated else
                                   ["no response in this exchange carries "
                                    "this id — which is not the same as no "
                                    "response anywhere"])})
    return links


def answered(links):
    """The requests a response was shown to answer. Only from the links.

    Not from "a response came back", not from a status, not from a count, and
    not from a candidate set this reader did not finish reading.
    `REPRESENTATION_ONLY` is not here either: an equality at a field a marker
    sits in is not a demonstration about the originals.
    """
    return [ln["request"] for ln in links
            if ln["link"] in CONFIRMED and ln.get("request")]


# --- the HTTP adapter ---------------------------------------------------------

def read_exchange(step):
    """One recorded HTTP step, as JSON-RPC evidence.

    The adapter is this small on purpose: the core knows about messages and
    the scope, and this knows where a body is stored. Nothing else about the
    step is read — not the url, not the status, not the chain fields — and
    nothing about it is written.
    """
    step = step or {}
    ev = {"schema": SCHEMA, "scope": SCOPE, "step": step.get("i"),
          "representation": "stored", "messages": [], "links": [],
          "answered": [], "enumerated": True, "findings": []}
    if step.get("t") != "http":
        ev["findings"].append("not_an_http_step")
        return ev

    sent = parse_envelope(step.get("req"))
    got = parse_envelope(step.get("body"), binary=bool(step.get("b64")))
    keys = ("parse", "batch", "count", "read", "enumerated")
    ev["request_envelope"] = {k: sent[k] for k in keys}
    ev["response_envelope"] = {k: got[k] for k in keys}
    ev["enumerated"] = bool(sent["enumerated"] and got["enumerated"])
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
    ev["links"] = correspond(ev["messages"], transport_paired, ev["enumerated"])
    ev["answered"] = answered(ev["links"])
    # `_id` is the parsed value the correspondence needed — an exact decimal,
    # or a structure that has no business leaving this module. The view stays;
    # the raw value goes, so what comes back is JSON-safe and carries no
    # payload by accident.
    for m in ev["messages"]:
        m.pop("_id", None)
    return ev


def read_steps(steps):
    """Every http step of a recording, each in its own scope.

    A list, and not a joined graph: linking across exchanges is a different
    contract, and this one does not have the evidence for it.
    """
    return [read_exchange(s) for s in (steps or [])
            if isinstance(s, dict) and s.get("t") == "http"]


def summarise(evidence):
    """Counts and diagnostics, under one stated rule.

    The rule, because a count nobody can reproduce is worse than no count:

        messages   one per message **read**, by kind. A batch longer than
                   MAX_MESSAGES contributes what was read and says so
        links      one per link *record*, by state. A request and a response
                   that did not pair each produce their own record, so the
                   totals do not match the message count and are not meant to
        exchanges  one per http step examined, and `unenumerated` counts the
                   exchanges where a side was not read to the end
        answered   requests in a CONFIRMED link — never more than the requests
        findings   one per finding text, wherever it sits: on an envelope, on
                   a message, or on a link

    No payloads: `params`, `result` and `error.data` are never read out of a
    message by this module, so nothing here can carry them.
    """
    kinds, links, findings = {}, {}, 0
    messages = unenumerated = 0
    for ev in evidence:
        findings += len(ev["findings"])
        if not ev.get("enumerated", True):
            unenumerated += 1
        for m in ev["messages"]:
            messages += 1
            kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
            findings += len(m["findings"])
        for ln in ev["links"]:
            links[ln["link"]] = links.get(ln["link"], 0) + 1
            findings += len(ln["findings"])
    return {"schema": SCHEMA, "exchanges": len(evidence),
            "unenumerated_exchanges": unenumerated,
            "messages": messages, "kinds": kinds,
            "links": sum(links.values()), "link_states": links,
            "answered": sum(len(ev["answered"]) for ev in evidence),
            "findings": findings}
