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

def t_no_equality_is_created_by_conversion():
    """Every pair here is two ids. A reader that converts either side finds
    one, and attaches a response to a request nobody can show it answered."""
    pairs = [(7, "7"), (0, ""), (0, False), (1, True), (1, 1.0),
             (10 ** 20, float(10 ** 20)), ("", None), (None, None),
             (1.5, "1.5")]
    bad = [p for p in pairs if rpc.ids_equal(p[0], p[1])]
    same = [(7, 7), ("a", "a"), (0, 0), (1.5, 1.5), (10 ** 20, 10 ** 20)]
    missed = [p for p in same if not rpc.ids_equal(p[0], p[1])]
    # and the same thing end to end, through the links
    ev = _links(_step(_req(rid=10 ** 20), _res(rid=float(10 ** 20))))
    return (not bad and not missed
            and not any(s in CONFIRMED for s in _states(ev))), \
        "equal when they should not be: %r; not equal when they should: %r; " \
        "links %r" % (bad, missed, _states(ev))


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
            and summary["links"].get(rpc.CORROBORATED) == 2
            and summary["answered"] == 2), \
        "before==after: %s | summary %r" % (before == after, summary)
