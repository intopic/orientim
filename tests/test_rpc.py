# -*- coding: utf-8 -*-
"""JSON-RPC evidence reader v1: eight acceptance groups, and one integration.

Read-side only. These tests exist to pin the things the contract in
`lab/JSONRPC.md` refuses to do, so the list below is mostly a list of
non-links:

    a confirmed link is CORROBORATED or BY_ID and nothing else
    `answered` comes out of the correspondence and out of nothing else
    the scope is one HTTP exchange, and "no response" says so
    the evidence is about the **stored representation**, never the wire

The last one has teeth. Capture re-serialises every JSON body it can parse
and rewrites a value that carries `scheme://user:pass@host`, so two ids can
agree on disk and not be shown to have agreed on the wire. That case is
`REPRESENTATION_ONLY`, and it is not an answer.
"""
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import chain, evaluate, integrity, rpc, session, store

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/rpc"
CONFIRMED = (rpc.CORROBORATED, rpc.BY_ID)


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _step(req, body, b64=False, i=1):
    """One recorded step, as the recorder would have left it."""
    return {"t": "http", "i": i, "b64": b64,
            "req": None if req is None else json.dumps(req),
            "body": None if body is None
            else (body if isinstance(body, str) else json.dumps(body))}


def _links(step):
    return rpc.read_exchange(step)


def _states(ev):
    return [ln["link"] for ln in ev["links"]]


def _req(rid=7, method="tools/call", **kw):
    out = {"jsonrpc": "2.0", "id": rid, "method": method}
    out.update(kw)
    return out


def _res(rid=7, **kw):
    out = {"jsonrpc": "2.0", "id": rid}
    out.update(kw or {"result": {}})
    return out


# --- 1. structure -------------------------------------------------------------

def t_a_malformed_structure_never_confirms_a_link():
    """Each of these breaks one rule of the spec, and none of them may come
    out of the reader as a request that was answered."""
    bad = []
    for label, step in (
        ("an id that is an object",
         _step(_req(rid={"a": 1}), _res(rid={"a": 1}))),
        ("an id that is an array", _step(_req(rid=[1]), _res(rid=[1]))),
        ("an id that is a boolean", _step(_req(rid=True), _res(rid=True))),
        ("params as a scalar",
         _step(_req(params=5), _res())),
        ("params as a string",
         _step(_req(params="x"), _res())),
        ("an error with no message",
         _step(_req(), _res(error={"code": -32603}))),
        ("an error code that is a boolean",
         _step(_req(), _res(error={"code": True, "message": "x"}))),
        ("an error that is not an object",
         _step(_req(), _res(error="boom"))),
        ("a method that is not a string",
         _step(_req(method=7), _res())),
    ):
        ev = _links(step)
        if any(s in CONFIRMED for s in _states(ev)) or ev["answered"]:
            bad.append("%s -> %r" % (label, _states(ev)))
    return not bad, "; ".join(bad) or "nine broken shapes, no confirmed link"


# --- 2. parsing ---------------------------------------------------------------

def t_every_way_a_body_can_fail_to_parse_stays_its_own_case():
    """Missing, null, damaged, repeated keys and an unsupported version are
    five different facts, and collapsing any two of them loses the reason."""
    seen = {}
    for label, body, want in (
        ("absent", None, rpc.ABSENT),
        ("empty", "", rpc.ABSENT),
        ("json null", "null", rpc.JSON_NULL),
        ("damaged", '{"jsonrpc": "2.0"', rpc.MALFORMED),
        ("repeated key", '{"jsonrpc": "2.0", "id": 7, "id": 8}',
         rpc.DUPLICATE_KEYS),
        ("a bare string", '"hello"', rpc.NOT_A_MESSAGE),
    ):
        ev = _links(_step(_req(), body))
        seen[label] = ev["response_envelope"]["parse"]
        if seen[label] != want:
            return False, "%s read as %r, wanted %r" % (label, seen[label],
                                                        want)
    # an unsupported version is a fact about the *message*, not the envelope
    ev = _links(_step(_req(), {"jsonrpc": "1.0", "id": 7, "result": {}}))
    kinds = [m["kind"] for m in ev["messages"] if m["ref"]["side"] == "received"]
    binary = _links({"t": "http", "i": 1, "b64": True, "req": None,
                     "body": "AAAA"})
    return (kinds == [rpc.UNSUPPORTED]
            and binary["response_envelope"]["parse"] == rpc.BINARY), \
        "version=%r binary=%r" % (kinds,
                                  binary["response_envelope"]["parse"])


# --- 3. identity --------------------------------------------------------------

def _text_step(id_a, id_b):
    """An exchange built from JSON **text**. An id is a JSON value, and a
    test that hands the reader pre-converted Python objects is testing its own
    conversion rather than the reader's."""
    return {"t": "http", "i": 1, "b64": False,
            "req": '{"jsonrpc":"2.0","id":%s,"method":"m"}' % id_a,
            "body": '{"jsonrpc":"2.0","id":%s,"result":{}}' % id_b}


def t_numeric_ids_are_compared_as_exact_decimals():
    """A JSON number is one kind of id, and two of them are the same id when
    their exact decimal values are equal.

    Both directions are counterexamples, which is why an int/float split was
    the wrong definition. `1` and `1.0` are one id, and the split called them
    two. `0.1` and `0.1000000000000000055511151231257827` are two ids and the
    *same binary float*, so a comparison that goes through a float calls them
    one.
    """
    one_id = ["1 vs 1.0", "1 vs 1e0", "100 vs 1e2", "1e2 vs 100",
              "0.50 vs 0.5", "-0 vs 0"]
    two_ids = ["0.1 vs 0.1000000000000000055511151231257827",
               "100000000000000000001 vs 1e20",
               "9007199254740993 vs 9007199254740992",
               '7 vs "7"', '0 vs ""', "1 vs true"]
    bad = []
    for pair in one_id:
        a, b = pair.split(" vs ")
        if not any(s in CONFIRMED for s in _states(_links(_text_step(a, b)))):
            bad.append("%s should be one id" % pair)
    for pair in two_ids:
        a, b = pair.split(" vs ")
        if any(s in CONFIRMED for s in _states(_links(_text_step(a, b)))):
            bad.append("%s should be two ids" % pair)
    return not bad, "; ".join(bad) or "six equal writings, six distinct ids"


def t_a_non_json_constant_is_not_an_id():
    """`NaN` and `Infinity` are things Python reads and JSON does not define.
    They are refused rather than compared — and `NaN != NaN` is not the
    reason, because `Infinity == Infinity` would otherwise have linked."""
    bad = []
    for const in ("NaN", "Infinity", "-Infinity"):
        ev = _links(_text_step(const, const))
        states = _states(ev)
        classes = [m["id"].get("class") for m in ev["messages"]]
        if (any(s in CONFIRMED for s in states) or ev["answered"]
                or classes != [rpc.INVALID_ID, rpc.INVALID_ID]):
            bad.append("%s -> %r %r" % (const, states, classes))
    whole = rpc.read_exchange({"t": "http", "i": 1, "req": "NaN",
                               "body": "Infinity"})
    if whole["request_envelope"]["parse"] != rpc.NOT_A_MESSAGE:
        bad.append("a body of NaN parsed as a message")
    return not bad, "; ".join(bad) or "three constants, none of them an id"


def t_a_null_id_links_nothing_from_either_side():
    for step in (_step(_req(rid=None), _res(rid=None)),
                 _step(_req(rid=7), _res(rid=None))):
        ev = _links(step)
        if any(s in CONFIRMED for s in _states(ev)) or ev["answered"]:
            return False, "%r" % (_states(ev),)
    return True, "null links nothing, in either direction"


# --- 4. multiplicity ----------------------------------------------------------

def t_a_repeated_id_has_no_unique_answer():
    """Three shapes, and the third is the one a one-sided reader gets wrong:
    one request with two responses reads as answered if you stop at the first
    candidate."""
    two_requests = _step([_req(rid=1, method="a"), _req(rid=1, method="b")],
                         [_res(rid=1)])
    two_responses = _step([_req(rid=1)], [_res(rid=1), _res(rid=1)])
    both = _step([_req(rid=1), _req(rid=1)], [_res(rid=1), _res(rid=1)])
    out = {}
    for label, step in (("two requests", two_requests),
                        ("two responses", two_responses),
                        ("both", both)):
        ev = _links(step)
        out[label] = (_states(ev), len(ev["answered"]))
        if rpc.AMBIGUOUS not in _states(ev) or ev["answered"]:
            return False, "%s -> %r" % (label, out[label])
    return True, "%r" % (out,)


# --- 5. batch -----------------------------------------------------------------

def t_the_order_of_a_batch_response_changes_nothing():
    sent = [_req(rid=1, method="a"), _req(rid=2, method="b"),
            _req(rid=3, method="c")]
    first = _links(_step(sent, [_res(rid=1), _res(rid=2), _res(rid=3)]))
    second = _links(_step(sent, [_res(rid=3), _res(rid=1), _res(rid=2)]))

    def pairs(ev):
        return sorted((ln["link"], repr(ln.get("id")))
                      for ln in ev["links"])

    return (pairs(first) == pairs(second)
            and len(first["answered"]) == 3
            and all(s == rpc.BY_ID for s in _states(first))), \
        "in order %r, permuted %r" % (pairs(first), pairs(second))


def t_the_envelope_is_validated_apart_from_its_messages():
    empty = _links(_step([], None))
    mixed = _links(_step([_req(rid=1), {"jsonrpc": "2.0", "method": "note"},
                          {"jsonrpc": "2.0", "id": 2}],
                         [_res(rid=1)]))
    notes_only = _links(_step([{"jsonrpc": "2.0", "method": "a"},
                               {"jsonrpc": "2.0", "method": "b"}], None))
    return (any("empty_batch" in f for f in empty["findings"])
            and empty["request_envelope"]["batch"]
            and len(mixed["answered"]) == 1
            and rpc.NOT_CONFIRMABLE in _states(mixed)
            and any("not_a_request_or_response" in f
                    for m in mixed["messages"] for f in m["findings"])
            and _states(notes_only) == [rpc.NOT_CONFIRMABLE,
                                        rpc.NOT_CONFIRMABLE]
            and not notes_only["answered"]), \
        "empty=%r mixed=%r notes=%r" % (empty["findings"],
                                        _states(mixed), _states(notes_only))


# --- 6. partial evidence ------------------------------------------------------

def t_an_unreadable_response_does_not_erase_the_request():
    ev = _links(_step(_req(rid=7, method="tools/call"), "{not json"))
    sent = [m for m in ev["messages"] if m["ref"]["side"] == "sent"]
    reason = " ".join(f for ln in ev["links"] for f in ln["findings"])
    return (len(sent) == 1 and sent[0]["kind"] == rpc.REQUEST
            and sent[0]["method"] == "tools/call"
            and ev["response_envelope"]["parse"] == rpc.MALFORMED
            and "in this exchange" in reason
            and "anywhere" in reason
            and not ev["answered"]), \
        "sent=%r parse=%r reason=%r" % (
            [m["kind"] for m in sent],
            ev["response_envelope"]["parse"], reason[:120])


# --- 7. direction -------------------------------------------------------------

def t_a_message_from_the_other_side_is_kept_as_what_it_is():
    """A request in a response body is a request from the server; a response
    in a request body is the client answering something asked earlier. Both
    are kept, and neither is joined to another exchange by guess."""
    server_asked = _links(_step(_req(rid=7),
                                {"jsonrpc": "2.0", "id": 9,
                                 "method": "sampling/createMessage"}))
    client_answered = _links(_step({"jsonrpc": "2.0", "id": 9, "result": {}},
                                   None))
    kinds = [(m["ref"]["side"], m["kind"]) for m in server_asked["messages"]]
    out_of_scope = [f for ln in server_asked["links"] for f in ln["findings"]
                    if "out_of_scope_direction" in f]
    client_kinds = [m["kind"] for m in client_answered["messages"]]
    return (("received", rpc.REQUEST) in kinds and out_of_scope
            and client_kinds == [rpc.RESULT]
            and not client_answered["answered"]
            and not server_asked["answered"]), \
        "kinds=%r out_of_scope=%d client=%r" % (kinds, len(out_of_scope),
                                                client_kinds)


# --- 8. capture and privacy ---------------------------------------------------

def t_a_transformed_id_is_not_a_demonstrated_correspondence():
    """Measured, not assumed: capture rewrites a value carrying
    `scheme://user:pass@host`, so both sides of this exchange arrive on disk
    as the same marker. They agree in the stored representation, and that is
    all the evidence says."""
    _fresh()
    rid = "http://user:pass@example.test/x"

    def agent(h):
        h.client().post(B + "/echo",
                        content=json.dumps(_req(rid=rid)).encode())
        h.output = "done"

    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    _meta, steps = store.load(h.path)
    step = [s for s in steps if s.get("t") == "http"][0]
    stored_id = json.loads(step["req"])["id"]
    # the same marker on the response side, so the two "agree" on disk
    step = dict(step, body=json.dumps({"jsonrpc": "2.0", "id": stored_id,
                                       "result": {}}))
    ev = rpc.read_exchange(step)
    states = _states(ev)
    return ("<redacted>" in stored_id and rid not in json.dumps(step)
            and rpc.REPRESENTATION_ONLY in states
            and not ev["answered"]), \
        "stored id=%r states=%r answered=%r" % (stored_id, states,
                                                ev["answered"])


def t_the_evidence_carries_no_payload():
    """params, result and error data are never read out of a message, so
    nothing here can put them in front of anybody."""
    ev = _links(_step(_req(params={"secret": "sk-IN-PARAMS"}),
                      _res(result={"text": "sk-IN-RESULT"})))
    text = json.dumps(ev)
    return ("sk-IN-PARAMS" not in text and "sk-IN-RESULT" not in text
            and "tools/call" in text), "evidence was %s" % text[:200]


# --- Phase C: the reader changes nothing --------------------------------------

def t_reading_a_recording_changes_nothing_about_it():
    """Storage, integrity and the reader over one real recording.

    The bytes, the chain root, the integrity verdict, the replay verdict and
    the evaluation are taken before and after. Analysis that moves any of them
    is not analysis.
    """
    _fresh()

    def agent(h):
        c = h.client()
        c.post(B + "/rpc", content=json.dumps(_req(rid=1)).encode())
        c.post(B + "/rpc", content=json.dumps(_req(rid=2)).encode())
        h.output = "done"

    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    path = h.path

    def snapshot():
        raw = open(path, "rb").read()
        meta, steps = store.load(path)
        http = [s for s in steps if s.get("t") == "http"]
        snap, _parsed = store.read_snapshot(path)
        d = session.replay(path, agent, strict=True)
        report = evaluate.evaluate(
            evaluate.Execution.load(path),
            [evaluate.did_not_call("send_email"), evaluate.max_steps(9)])
        return {
            "sha": hashlib.sha256(raw).hexdigest(),
            "root": chain.build_steps(http)[1],
            "integrity": snap.self_state,
            "digest": integrity.digest(snap.meta, snap.step_lines),
            "verdict": d.diagnosis[0],
            "roots": (d.recorded_root, d.replay_root),
            "results": [(r.evaluator, r.status) for r in report.results],
        }

    before = snapshot()
    evidence = rpc.read_steps(store.load(path)[1])
    summary = rpc.summarise(evidence)
    after = snapshot()
    return (before == after and summary["exchanges"] == 2
            and summary["link_states"].get(rpc.CORROBORATED) == 2
            and summary["answered"] == 2
            and summary["unenumerated_exchanges"] == 0), \
        "before==after: %s | summary %r" % (before == after, summary)


# --- 2 (again). the analysis limit, and uniqueness ----------------------------

def _batch_of(n, dup_at=None, dup_id=1):
    msgs = [{"jsonrpc": "2.0", "id": i, "method": "m"}
            for i in range(1, n + 1)]
    if dup_at is not None:
        msgs[dup_at]["id"] = dup_id
    return msgs


def t_the_analysis_limit_never_creates_uniqueness():
    """199, 200, 201 — and the last one hides a duplicate past the limit.

    A candidate this reader never read is a candidate it cannot rule out, so
    uniqueness is unprovable there. Below the limit the links are confirmed as
    before; at the limit too; over it, every would-be link becomes
    `UNENUMERATED`, nothing is answered, and the messages that *were* read are
    still in the evidence — the limit costs the link, not the evidence.
    """
    out = {}
    for n in (199, 200, 201):
        ev = _links(_step(_batch_of(n, dup_at=n - 1), [_res(rid=1)]))
        read = len([m for m in ev["messages"] if m["ref"]["side"] == "sent"])
        out[n] = (ev["request_envelope"]["enumerated"], read,
                  len(ev["answered"]),
                  sorted(set(_states(ev))))
    ok_199 = out[199][0] and out[199][1] == 199 and out[199][2] == 0
    ok_200 = out[200][0] and out[200][1] == 200
    over = out[201]
    return (ok_199 and ok_200
            and over[0] is False and over[1] == 200 and over[2] == 0
            and over[3] == [rpc.UNENUMERATED]
            and rpc.CORROBORATED not in over[3]
            and rpc.BY_ID not in over[3]), "%r" % (out,)


def t_a_duplicate_past_the_limit_is_not_ruled_out_on_either_side():
    """The same rule for the response side: 201 responses, one of them a
    duplicate the reader never reaches."""
    req = [_req(rid=1, method="a")]
    responses = [_res(rid=i) for i in range(1, 202)]
    responses[-1] = _res(rid=1)
    ev = _links(_step(req, responses))
    return (ev["response_envelope"]["enumerated"] is False
            and ev["enumerated"] is False
            and not ev["answered"]
            and rpc.CORROBORATED not in _states(ev)
            and rpc.BY_ID not in _states(ev)), \
        "enumerated=%r answered=%d states=%r" % (
            ev["response_envelope"]["enumerated"], len(ev["answered"]),
            sorted(set(_states(ev))))


def t_a_batch_permutation_is_stable_at_the_limit():
    """Order is never read, at any size: the link multiset is identical."""
    sent = _batch_of(200)
    forward = [_res(rid=i) for i in range(1, 201)]
    reverse = list(reversed(forward))

    def multiset(ev):
        return sorted((ln["link"], (ln["id"] or {}).get("text"))
                      for ln in ev["links"])

    a, b = _links(_step(sent, forward)), _links(_step(sent, reverse))
    return (multiset(a) == multiset(b) and len(a["answered"]) == 200
            and set(_states(a)) == {rpc.BY_ID}), \
        "answered=%d states=%r stable=%s" % (
            len(a["answered"]), sorted(set(_states(a))),
            multiset(a) == multiset(b))


# --- 3 (again). a marker is possible, a transformation is measured -----------

def t_a_marker_is_possible_and_never_proven():
    """The negative control. A server may send the literal marker text, and
    from the stored representation that is indistinguishable from capture
    having put it there — so the reader says a marker is *present* and never
    that capture rewrote anything."""
    rid = '"http://<redacted>@h/x"'
    ev = _links(_text_step(rid, rid))
    text = " ".join(f for ln in ev["links"] for f in ln["findings"])
    msg_text = " ".join(f for m in ev["messages"] for f in m["findings"])
    return (_states(ev) == [rpc.REPRESENTATION_ONLY]
            and not ev["answered"]
            and "not shown" in text
            and "rewrote" not in text and "rewrote" not in msg_text
            and "marker" in msg_text
            and ev["messages"][0]["id"].get("marker") is True), \
        "states=%r link=%r msg=%r" % (_states(ev), text[:90], msg_text[:90])


def _record(agent):
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    _meta, steps = store.load(h.path)
    return [s for s in steps if s.get("t") == "http"]


def _ask(rid, answer_id):
    """Send `rid`, and have the real server answer with `answer_id`."""
    def agent(h):
        h.client().post(B + "/rpc", content=json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
             "answer_id": answer_id}).encode())
        h.output = "done"
    return agent


def t_a_server_that_answers_another_id_is_recorded_as_a_conflict():
    """The integrated proof, with no editing after the fact: a real server
    receives id 7 and answers id 8, and the recording is read as it was
    written."""
    steps = _record(_ask(7, 8))
    ev = rpc.read_exchange(steps[0])
    sent = json.loads(steps[0]["req"])
    got = json.loads(steps[0]["body"])
    return (sent["id"] == 7 and got["id"] == 8
            and _states(ev) == [rpc.CONFLICT, rpc.UNLINKED]
            and not ev["answered"]),         "sent=%r got=%r states=%r" % (sent["id"], got.get("id"),
                                      _states(ev))


def t_two_different_ids_that_redaction_merges_are_not_a_correspondence():
    """The measurement, and nothing is touched after recording either. Two
    *different* original ids, both carrying credentials in a URL: the server
    answers with the second, capture rewrites both into one stored value, and
    they are equal on disk while never having been equal on the wire."""
    a = "http://alice:pw1@example.test/x"
    b = "http://bob:pw2@example.test/x"
    steps = _record(_ask(a, b))
    step = steps[0]
    stored_request_id = json.loads(step["req"])["id"]
    stored_response_id = json.loads(step["body"])["id"]
    ev = rpc.read_exchange(step)
    return (a != b
            and stored_request_id == stored_response_id
            and "<redacted>" in stored_request_id
            and _states(ev) == [rpc.REPRESENTATION_ONLY]
            and not ev["answered"]),         "stored request=%r response=%r states=%r" % (
            stored_request_id, stored_response_id, _states(ev))


def t_a_long_numeric_id_is_exported_exactly_or_not_at_all():
    """`normalize()` rounded a fifty-digit id through the decimal context and
    the `"f"` form expanded an exponent into a hundred thousand characters.
    Exactly as written, bounded — and past the bound the digit count instead
    of a number nobody should trust."""
    fifty = "1" + "234567890" * 5 + "9"
    ev = _links(_text_step(fifty, fifty))
    exact = ev["messages"][0]["id"].get("text") == fifty
    big = _links(_text_step("1e100000", "1e100000"))["messages"][0]["id"]
    huge = "9" * (rpc.MAX_ID_TEXT + 10)
    dropped = _links(_text_step(huge, huge))["messages"][0]["id"]
    return (exact and any(s in CONFIRMED for s in _states(ev))
            and big.get("text") == "1E+100000"
            and "text" not in dropped
            and dropped.get("digits") == rpc.MAX_ID_TEXT + 10),         "exact=%s big=%r dropped=%r" % (exact, big, dropped)


def t_a_non_finite_number_is_never_an_id():
    """Through the core API, which is the door a caller's own parse comes in
    by. `Infinity == Infinity` is true, so without this a non-finite id
    linked a request to a response."""
    import decimal
    bad = []
    for value in (float("inf"), float("-inf"), float("nan"),
                  decimal.Decimal("Infinity"), decimal.Decimal("NaN")):
        if rpc.id_class(value) != rpc.INVALID_ID or rpc.ids_equal(value, value):
            bad.append("%r -> %s" % (value, rpc.id_class(value)))
    finite = rpc.id_class(0.1) == rpc.NUMBER and rpc.ids_equal(0.1, 0.1)
    return (not bad and finite),         "%s; a finite float is still a number: %s" % (bad, finite)


def t_a_non_json_constant_anywhere_invalidates_the_message():
    """Not only in the id. A body holding NaN is not a JSON document wherever
    the constant sits, so the message cannot be a request, a response, or half
    of a confirmed link."""
    cases = [
        ('{"jsonrpc":"2.0","id":1,"method":"m","params":{"x":NaN}}',
         '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'),
        ('{"jsonrpc":"2.0","id":1,"method":"m"}',
         '{"jsonrpc":"2.0","id":1,"result":{"y":Infinity}}'),
        ('{"jsonrpc":"2.0","id":1,"method":"m","params":[[[{"d":-Infinity}]]]}',
         '{"jsonrpc":"2.0","id":1,"result":{}}'),
    ]
    bad = []
    for req, body in cases:
        ev = rpc.read_exchange({"t": "http", "i": 1, "req": req, "body": body})
        named = any("non_json_value" in f
                    for m in ev["messages"] for f in m["findings"])
        if (any(s in CONFIRMED for s in _states(ev)) or ev["answered"]
                or not named):
            bad.append("%r -> %r" % (req[:40], _states(ev)))
    return not bad, "; ".join(bad) or "three nestings, none of them a message"


def t_a_finding_never_quotes_a_value():
    """The assumption that a short string is a safe string is gone: a secret
    can be nine characters, and a key name is a value out of a stored body
    like any other."""
    version = _links({"t": "http", "i": 1,
                      "req": '{"jsonrpc":"sk-ABC123","id":1,"method":"m"}',
                      "body": None})
    repeated = _links({"t": "http", "i": 1,
                       "req": '{"sk-SECRET-KEY":1,"sk-SECRET-KEY":2}',
                       "body": None})
    return ("sk-ABC123" not in json.dumps(version)
            and "sk-SECRET-KEY" not in json.dumps(repeated)
            and any("unsupported_version" in f
                    for m in version["messages"] for f in m["findings"])
            and any("duplicate_key" in f
                    for f in repeated["findings"])),         "version=%s repeated=%s" % (json.dumps(version)[:120],
                                    json.dumps(repeated)[:120])


def t_no_invalid_structure_leaves_the_reader():
    """An id that is an object or an array is reported by class, and its
    contents are not copied out; a finding names a class rather than quoting
    a value it cannot vouch for."""
    step = _step({"jsonrpc": "2.0", "id": {"leak": "sk-IN-THE-ID"},
                  "method": "m", "params": "sk-IN-PARAMS"},
                 {"jsonrpc": "2.0", "id": ["sk-IN-THE-ARRAY"],
                  "error": {"code": {"leak": "sk-IN-THE-CODE"},
                            "message": "x"}})
    ev = rpc.read_exchange(step)
    text = json.dumps(ev)
    classes = [m["id"].get("class") for m in ev["messages"]]
    version = _links(_step({"jsonrpc": {"leak": "sk-IN-THE-VERSION"},
                            "id": 1, "method": "m"}, None))
    return (all(s not in text for s in ("sk-IN-THE-ID", "sk-IN-PARAMS",
                                        "sk-IN-THE-ARRAY", "sk-IN-THE-CODE"))
            and classes == [rpc.INVALID_ID, rpc.INVALID_ID]
            and all("text" not in m["id"] for m in ev["messages"])
            and "sk-IN-THE-VERSION" not in json.dumps(version)), \
        "classes=%r evidence=%s" % (classes, text[:160])


def t_the_summary_counts_by_a_stated_rule():
    """One exchange: two requests (one a duplicate id), one notification, two
    responses. The counts follow the rule in `summarise`, and a link record
    is not a message."""
    ev = _links(_step([_req(rid=1, method="a"), _req(rid=1, method="b"),
                       {"jsonrpc": "2.0", "method": "note"}],
                      [_res(rid=1), _res(rid=9)]))
    s = rpc.summarise([ev])
    return (s["exchanges"] == 1 and s["unenumerated_exchanges"] == 0
            and s["messages"] == 5
            and s["kinds"].get(rpc.REQUEST) == 2
            and s["kinds"].get(rpc.NOTIFICATION) == 1
            and s["kinds"].get(rpc.RESULT) == 2
            and s["links"] == sum(s["link_states"].values())
            and s["answered"] == 0
            and s["findings"] > 0
            and s["schema"] == rpc.SCHEMA), "%r" % (s,)
