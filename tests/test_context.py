# -*- coding: utf-8 -*-
"""The pre-serve gate: a fixture is released, or it is not.

The counterexample, measured before any of this existed: an agent replayed as
tenant `globex` was handed tenant `acme`'s recorded response, parsed it, and
branched on it — "released 100 to globex" — and `HEADERS_CHANGED` appeared
only after the run had finished. Detection after the fact is not mediation,
and the difference is whether the agent already acted on somebody else's data.

Four things this file pins as hard as the fix itself:

    legacy is untouched, byte for byte, and never yields ELIGIBLE
    a refusal does not consume the recorded step
    a refusal is not a behaviour change, and does not borrow a verdict from one
    what the answer rests on is named, including where it is weak
"""
import hashlib
import json
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import orientim
from orientim import chain, ci, context as ctx, session, store

PORT = 8802
BASE = "http://127.0.0.1:%d" % PORT
ROOT = "tests/_runs/context"
_SRV = {}


class _Tenants(BaseHTTPRequestHandler):
    """An endpoint whose answer belongs to whoever asked for it."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"tenant": self.headers.get("X-Tenant"),
                           "balance": 100}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _up():
    if not _SRV:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Tenants)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _SRV["srv"] = srv
    return _SRV["srv"]


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)


def _agent(tenant, saw=None, calls=1):
    """Reads, then acts on what it read. The branch is the evidence: a
    response that reaches this line was not merely delivered, it was used."""
    def fn(h):
        for _ in range(calls):
            r = h.client().post(BASE + "/accounts/4471",
                                headers={"X-Tenant": tenant},
                                content=json.dumps({"q": 1}).encode())
            if saw is not None:
                saw["status"] = r.status_code
                saw["consumed"] = (r.json().get("tenant")
                                   if r.status_code == 200 else None)
                saw["acted"] = ("released to " + tenant
                                if r.status_code == 200 else None)
            h.output = r.text
    return fn


def _recorded(context=None, tenant="acme"):
    _up()
    _fresh()
    with orientim.record(root=ROOT, always=True, context=context) as h:
        _agent(tenant)(h)
    return h.path


# --- the counterexample -------------------------------------------------------

def t_a_fixture_is_not_released_to_another_tenant():
    """The finding, closed. Nothing out of the file reaches the agent."""
    path = _recorded({"tenant": "acme"})
    saw = {}
    d = session.replay(path, _agent("globex", saw),
                       context={"tenant": "globex"}, contract=("tenant",))
    return (saw.get("status") == 599 and saw.get("consumed") is None
            and saw.get("acted") is None
            and d.diagnosis[0] == "FIXTURE_REFUSED"), (
        "status=%s consumed=%r acted=%r verdict=%s"
        % (saw.get("status"), saw.get("consumed"), saw.get("acted"),
           d.diagnosis[0]))


def t_the_same_tenant_still_gets_its_own_fixture():
    """The control that decides whether this is usable: a replay that is who
    it says it is must be unaffected."""
    path = _recorded({"tenant": "acme"})
    saw = {}
    d = session.replay(path, _agent("acme", saw), context={"tenant": "acme"},
                       contract=("tenant",))
    return (d.diagnosis[0] == "IDENTICAL" and saw.get("consumed") == "acme"), (
        "verdict=%s consumed=%r" % (d.diagnosis[0], saw.get("consumed")))


def t_a_refusal_does_not_consume_the_recorded_step():
    """The cursor never moves. A refused replay leaves the queue exactly as it
    was, which is why refusing is a decision and not a kind of miss."""
    path = _recorded({"tenant": "acme"})
    d = session.replay(path, _agent("globex", calls=2),
                       context={"tenant": "globex"}, contract=("tenant",))
    matched = getattr(d, "n_matched", None)
    return (matched in (0, None) and d.index in (0, None)
            and d.diagnosis[0] == "FIXTURE_REFUSED"), (
        "n_matched=%r index=%r verdict=%s" % (matched, d.index, d.diagnosis[0]))


def t_a_refusal_is_not_a_behaviour_change():
    """It must not borrow a verdict that says the code moved. `NEW_CALL` and
    `UNCAPTURED_SOURCE` both mean the agent called something else; nothing
    about the agent changed here."""
    path = _recorded({"tenant": "acme"})
    d = session.replay(path, _agent("globex"), context={"tenant": "globex"},
                       contract=("tenant",))
    code, _title, message, _action = d.diagnosis
    return (code == "FIXTURE_REFUSED"
            and code not in ("NEW_CALL", "UNCAPTURED_SOURCE",
                             "HEADERS_CHANGED", "BODY_CHANGED")
            and "not a behaviour change" in message), (
        "code=%s message=%r" % (code, message[:120]))


# --- legacy, pinned -----------------------------------------------------------

def t_legacy_behaviour_is_byte_for_byte_what_it_was():
    """No contract, no context, no gate. The replay that ran before any of
    this existed: the fixture is served, the agent consumes it, and the
    mismatch is reported afterwards — which is the defect, kept working
    under a name rather than changed underneath anybody."""
    path = _recorded()
    saw = {}
    d = session.replay(path, _agent("globex", saw))
    return (saw.get("status") == 200 and saw.get("consumed") == "acme"
            and d.diagnosis[0] == "HEADERS_CHANGED"), (
        "status=%s consumed=%r verdict=%s"
        % (saw.get("status"), saw.get("consumed"), d.diagnosis[0]))


def t_legacy_is_unmediated_not_eligible():
    """Zero obligations evaluated is not an eligibility guarantee, and the
    vocabulary must not let a report say otherwise."""
    verdict, reasons = ctx.mediate(ctx.LEGACY, None, None)
    return (verdict == ctx.LEGACY_UNMEDIATED and verdict != ctx.ELIGIBLE), (
        "%s %r" % (verdict, reasons))


def _root(path):
    _meta, steps = store.load(path)
    return chain.build_steps([s for s in steps if s.get("t") == "http"])[1]


def t_a_context_does_not_move_the_hash_chain():
    """It is metadata. A recording made with one and a recording made without
    must digest identically, or a declared tenant would read as a behaviour
    change in every comparison that exists."""
    _up()
    _fresh()
    with orientim.record(root=ROOT, always=True) as a:
        _agent("acme")(a)
    bare = _root(a.path)
    _fresh()
    with orientim.record(root=ROOT, always=True,
                         context={"tenant": "acme"}) as b:
        _agent("acme")(b)
    with_ctx = _root(b.path)
    return bool(bare) and bare == with_ctx, (
        "chain moved: %r vs %r" % (bare, with_ctx))


# --- the four ways not to release ---------------------------------------------

def t_a_contradiction_is_ineligible():
    path = _recorded({"tenant": "acme"})
    d = session.replay(path, _agent("globex"), context={"tenant": "globex"},
                       contract=("tenant",))
    said = " ".join(u.get("detail", "") for u in (d.uncaptured or []))
    return ("INELIGIBLE" in said and "acme" in said and "globex" in said), said


def t_a_recording_that_predates_contracts_is_unknown():
    """No context in the file, and nothing invented for it. The obligation is
    unanswerable, so the fixture is not released and the reason says which
    side is silent."""
    path = _recorded()                       # recorded with no context at all
    d = session.replay(path, _agent("acme"), context={"tenant": "acme"},
                       contract=("tenant",))
    said = " ".join(u.get("detail", "") for u in (d.uncaptured or []))
    return (d.diagnosis[0] == "FIXTURE_REFUSED"
            and "UNKNOWN" in said
            and "the recording carries no tenant" in said), (
        "verdict=%s said=%r" % (d.diagnosis[0], said))


def t_a_replay_that_declares_nothing_is_unknown():
    """The other side of the same question."""
    path = _recorded({"tenant": "acme"})
    d = session.replay(path, _agent("acme"), contract=("tenant",))
    said = " ".join(u.get("detail", "") for u in (d.uncaptured or []))
    return (d.diagnosis[0] == "FIXTURE_REFUSED"
            and "this replay declares no tenant" in said), said


def t_an_unusable_contract_is_an_analysis_error():
    """A misconfigured contract is a fact about the configuration. Answering
    it with INELIGIBLE would blame the caller for a typo."""
    verdict, reasons = ctx.mediate(("nonesuch",), {"tenant": {"value": "a"}},
                                   {"tenant": {"value": "a"}})
    return (verdict == ctx.ANALYSIS_ERROR
            and "no such replay relation" in reasons[0]), "%s %r" % (verdict,
                                                                     reasons)


def t_equality_is_exact():
    """No normalisation. Deciding that ACME is acme is a policy, and nobody
    declared one."""
    verdict, _ = ctx.mediate(("tenant",), {"tenant": {"value": "acme"}},
                             {"tenant": {"value": "ACME"}})
    return verdict == ctx.INELIGIBLE, verdict


# --- what a context may not hold ----------------------------------------------

def t_a_credential_relation_is_refused_with_its_reason():
    """Not "no such relation", which is true and useless. The relation is
    known, unsupported, and the message is the design: a redacted credential
    is worse than none, because two secrets can redact to one string and then
    compare equal."""
    try:
        ctx.normalize({"credential": "same_credential_bytes"})
    except ctx.ContextError as e:
        return ("not supported yet" in str(e)
                and "compare equal" in str(e)), str(e)[:120]
    return False, "a credential relation was accepted"


def t_a_credential_shaped_value_is_refused():
    """Refused, not redacted. Redaction here would collapse two different
    secrets into one stored value that compares equal — a false ELIGIBLE
    manufactured by the privacy measure."""
    for value in ("Bearer sk-AAAA", "ghp_deadbeefdeadbeefdead",
                  "my-api-key-1234"):
        try:
            ctx.normalize({"tenant": value})
            return False, "accepted a credential-shaped value: %r" % value
        except ctx.ContextError:
            pass
    return True, "three credential shapes refused"


def t_two_different_values_never_compare_equal():
    """The property the refusal exists to protect."""
    a = ctx.normalize({"tenant": "acme"})["tenant"]["value"]
    b = ctx.normalize({"tenant": "globex"})["tenant"]["value"]
    return a != b and "redact" not in (a + b), "%r vs %r" % (a, b)


def t_the_evidence_kind_is_named_in_the_file():
    """`declared_unchained` is the whole disclosure: stated by the
    application rather than observed, and stored outside the chain."""
    path = _recorded({"tenant": "acme"})
    meta = json.loads(open(path, encoding="utf-8").readline())["_meta"]
    kind = meta["context"]["tenant"]["evidence"]
    verdict, reasons = ctx.mediate(("tenant",), meta["context"],
                                   ctx.normalize({"tenant": "acme"}))
    return (kind == ctx.DECLARED_UNCHAINED and verdict == ctx.ELIGIBLE
            and ctx.DECLARED_UNCHAINED in reasons[0]), (
        "kind=%r reasons=%r" % (kind, reasons))


# --- declared limitations, pinned so they cannot move in silence --------------

def t_KNOWN_LIMIT_an_edited_context_flips_the_decision():
    """KNOWN LIMITATION. The chain covers steps, not metadata.

    Editing `_meta.context` in a recording changes what the gate decides, and
    nothing detects it. Integrity-binding the context means touching the hash
    chain, which is frozen, and the protection this buys is real without it:
    it stops a replay that is *wrong* about itself — a suite re-run for
    another tenant that quietly receives the first tenant's fixtures — and it
    does not stop someone who can edit the file, who could equally delete the
    context or replay under the legacy contract.

    Stated here rather than discovered later. See docs/limits.md.
    """
    path = _recorded({"tenant": "acme"})
    lines = open(path, encoding="utf-8").read().splitlines()
    meta = json.loads(lines[0])
    meta["_meta"]["context"]["tenant"]["value"] = "globex"
    edited = os.path.join(ROOT, "edited.jsonl")
    with open(edited, "w", encoding="utf-8") as f:
        f.write(json.dumps(meta) + "\n" + "\n".join(lines[1:]) + "\n")
    saw = {}
    d = session.replay(edited, _agent("globex", saw),
                       context={"tenant": "globex"}, contract=("tenant",))
    return (saw.get("status") == 200 and d.diagnosis[0] != "FIXTURE_REFUSED"), (
        "the limitation moved: status=%s verdict=%s"
        % (saw.get("status"), d.diagnosis[0]))


def t_KNOWN_LIMIT_a_recording_is_not_tamper_evident_at_rest():
    """The scope of the limitation above, measured rather than assumed.

    A recording is not tamper-evident at rest, and not only where the context
    is. The chain is a comparison device between a recording and a replay: it
    digests the fields in `chain.DIGEST_FIELDS`, so editing a response body
    without its `body_sha` does not move the root at all, editing both moves
    it consistently, and there is nothing the file is ever checked against.

    That is why the declared context is not integrity-bound here. Binding it
    alone would make it the one tamper-evident field of a file that is not
    tamper-evident, which protects nobody and reads as though it did.
    """
    path = _recorded({"tenant": "acme"})
    before = _root(path)

    def _edit(name, change):
        objs = [json.loads(x) for x in
                open(path, encoding="utf-8").read().splitlines()]
        change(objs)
        out = os.path.join(ROOT, name)
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(o) for o in objs) + "\n")
        return out

    def body_only(objs):
        for o in objs:
            if o.get("t") == "http":
                o["body"] = o["body"].replace("100", "999")

    def body_and_sha(objs):
        body_only(objs)
        for o in objs:
            if o.get("t") == "http":
                o["body_sha"] = hashlib.sha256(o["body"].encode()).hexdigest()

    def context_only(objs):
        objs[0]["_meta"]["context"]["tenant"]["value"] = "globex"

    moved = {name: _root(_edit(name + ".jsonl", fn)) != before
             for name, fn in (("body", body_only),
                              ("body_and_sha", body_and_sha),
                              ("context", context_only))}
    return (moved == {"body": False, "body_and_sha": True,
                      "context": False}), "the measurement moved: %r" % (moved,)


def t_a_concurrent_recording_is_not_certified_by_one_context():
    """A context stated once for a whole run cannot speak for requests that
    may have had different callers. Until a per-request context exists, a
    recording that ran on several workers is answered UNKNOWN rather than
    certified."""
    _up()
    _fresh()
    with orientim.record(root=ROOT, always=True,
                         context={"tenant": "acme"}) as h:
        threads = [threading.Thread(target=_agent("acme"), args=(h,))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    verdict, reasons = ctx.mediate(("tenant",), {"tenant": {"value": "acme"}},
                                   {"tenant": {"value": "acme"}}, workers=2)
    d = session.replay(h.path, _agent("acme"), context={"tenant": "acme"},
                       contract=("tenant",))
    return (verdict == ctx.UNKNOWN and "workers" in reasons[0]
            and d.diagnosis[0] == "FIXTURE_REFUSED"), (
        "verdict=%s replay=%s" % (verdict, d.diagnosis[0]))


def t_a_single_worker_recording_is_still_certified():
    """The control for that: an ordinary run must not be refused for being
    ordinary."""
    verdict, _ = ctx.mediate(("tenant",), {"tenant": {"value": "acme"}},
                             {"tenant": {"value": "acme"}}, workers=1)
    return verdict == ctx.ELIGIBLE, verdict


# --- P0-2: the refusal has to survive the layers above replay -----------------
#
# `cases.run_case` sets ok = replayed and nothing-broke, so a refused replay
# made the case red, and `ci.compare` filed it under "cases that started
# failing on this change". That is the harness-vs-behaviour conflation one
# layer up, in the layer that talks to CI.

def _row(case, ok=False, refused=False, verdict="IDENTICAL", statuses=()):
    row = {"case": case, "run_id": "r", "ok": ok, "verdict": verdict,
           "steps": 1, "replayed": not refused,
           "evaluation": {"results": [
               {"evaluator": o.split(":", 1)[0], "obligation": o, "status": s}
               for o, s in statuses]}}
    if refused:
        row["verdict"] = "FIXTURE_REFUSED"
        row["refused"] = {"verdict": "INELIGIBLE",
                          "reasons": ["tenant: recorded acme, replaying as "
                                      "globex"]}
    return row


def _frozen(case, ok=True, statuses=()):
    return {"case": case, "run_id": "r", "ok": ok, "verdict": "IDENTICAL",
            "steps": 1, "refused": False,
            "failed_evaluators": [o.split(":", 1)[0]
                                  for o, s in statuses if s == "fail"],
            "obligations": {o: s for o, s in statuses},
            "evaluators": {o.split(":", 1)[0]: s for o, s in statuses}}


def _base(rows):
    from orientim import evaluate as ev
    return {"runs": rows, "analysis": ev.analysis()}


def t_a_refused_case_is_not_a_case_that_started_failing():
    """The P0-2 counterexample. The agent was handed nothing, so there is no
    behaviour here to have regressed."""
    base = _base([_frozen("support", ok=True)])
    cmp_ = ci.compare([_row("support", refused=True)], base, key="case")
    return (cmp_["newly_changed"] == [] and cmp_.get("refused") == ["support"]
            and cmp_["still_changed"] == []), (
        "newly_changed=%r refused=%r" % (cmp_["newly_changed"],
                                         cmp_.get("refused")))


def t_a_refused_case_makes_no_claim_about_its_rules_either():
    """Its evaluators ran against a replay that was handed nothing, so every
    status they produced is about the refusal. Reading one as a rule that lost
    its proof would be the same mistake one level down."""
    base = _base([_frozen("support", ok=True,
                          statuses=[("did_not_call:refund.issue", "pass")])])
    rows = [_row("support", refused=True,
                 statuses=[("did_not_call:refund.issue", "warn")])]
    cmp_ = ci.compare(rows, base, key="case")
    return (not cmp_.get("weakened") and not cmp_.get("new_failures")
            and cmp_.get("refused") == ["support"]), (
        "weakened=%r new_failures=%r" % (cmp_.get("weakened"),
                                         cmp_.get("new_failures")))


def t_a_refusal_blocks_the_build_under_both_profiles():
    """It must still cost what a case that could not run should cost. A build
    that goes green on a case that never ran is the worst outcome here."""
    base = _base([_frozen("support", ok=True)])
    rows = [_row("support", refused=True)]
    cmp_ = ci.compare(rows, base, key="case")
    legacy = ci.gate(cmp_, rows, profile=ci.LEGACY)
    protected = ci.gate(cmp_, rows, profile=ci.PROTECTED)
    return (legacy[0] == ci.EXIT_CHANGED
            and protected[0] == ci.EXIT_CHANGED), (
        "legacy=%r protected=%r" % (legacy, protected))


def t_the_gate_reason_does_not_blame_the_agent():
    """What CI prints. `cases that started failing` is a sentence about the
    agent, and the agent was handed nothing."""
    base = _base([_frozen("support", ok=True)])
    rows = [_row("support", refused=True)]
    said = " ".join(ci.gate(ci.compare(rows, base, key="case"), rows,
                            profile=ci.PROTECTED)[1]).lower()
    return ("started failing" not in said
            and "regress" not in said
            and "refused these fixtures" in said
            and "nothing about the agent was measured" in said), said


def t_a_new_case_that_is_refused_is_not_new_and_already_failing():
    """A case nobody had before, refused. `new_failing` says it arrived red;
    it did not arrive at all."""
    cmp_ = ci.compare([_row("fresh", refused=True)], _base([]), key="case")
    return (cmp_.get("new_failing") == [] and cmp_.get("refused") == ["fresh"]
            and cmp_["new_recordings"] == ["fresh"]), (
        "new_failing=%r refused=%r" % (cmp_.get("new_failing"),
                                       cmp_.get("refused")))


def t_a_refused_baseline_is_not_a_case_that_got_fixed():
    """The other direction. A baseline row that was refused measured nothing,
    so a green run today is not something this change fixed."""
    base = _base([_frozen("support", ok=False)])
    base["runs"][0]["refused"] = True
    base["runs"][0]["verdict"] = "FIXTURE_REFUSED"
    cmp_ = ci.compare([_row("support", ok=True)], base, key="case")
    return (cmp_["fixed"] == [] and cmp_.get("refused") == ["support"]), (
        "fixed=%r refused=%r" % (cmp_["fixed"], cmp_.get("refused")))


def t_an_ordinary_failing_case_is_untouched_by_any_of_this():
    """The control. A case that really did start failing must still say so,
    in the same words, or the fix has bought safety by going quiet."""
    base = _base([_frozen("support", ok=True)])
    rows = [_row("support", ok=False, verdict="NEW_CALL")]
    cmp_ = ci.compare(rows, base, key="case")
    said = " ".join(ci.gate(cmp_, rows, profile=ci.LEGACY)[1])
    return (cmp_["newly_changed"] == ["support"] and not cmp_.get("refused")
            and "cases that started failing" in said), (
        "newly_changed=%r said=%r" % (cmp_["newly_changed"], said))


def t_the_refusal_reaches_the_case_row_end_to_end():
    """Not a synthetic row: a real recording, a real refusal, and the row the
    case layer hands to everything above it."""
    from orientim import cases
    path = _recorded({"tenant": "acme"})
    case = {"name": "ctx", "recording": path, "run_id": "r",
            "entry": "x:agent", "expect": {}}
    row = cases.run(case, entry_loader=lambda e: _agent("globex"),
                    context={"tenant": "globex"}, contract=("tenant",))
    return (row.get("verdict") == "FIXTURE_REFUSED"
            and isinstance(row.get("refused"), dict)
            and row["refused"]["verdict"] == "INELIGIBLE"), (
        "verdict=%s refused=%r" % (row.get("verdict"), row.get("refused")))
