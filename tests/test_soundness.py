# -*- coding: utf-8 -*-
"""Counterexamples from the independent audit, each one reproduced first.

Four findings were raised against `d4df5ab` by static reading. Static reading
is a hypothesis, so every one of them is a counterexample here before it is a
fix, and the two that turned out to need a design decision stay here as pinned
limitations rather than as claims that they are solved.

    P0-1  a response can arrive and still not be readable. Transport
          completeness was being taken for interpretation completeness.
    P0-2  a regex that matches a prefix is not a witness for the whole answer,
          and an output that could not be captured is not an empty string.
    P0-3  the lookup key is method + URL + body, and the headers that carry
          principal identity are excluded from the fingerprint as well. Pinned,
          not fixed — see docs/limits.md.
    P0-4  a case that was already failing can acquire a second, different
          violation and the comparison saw only `False -> False`.

The tool-call claims stay separate throughout: *the model asked for a tool* is
not *the tool ran* and is not *the tool had an effect*.
"""
import json
import os
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import ci, evaluate as ev, model, observation as obs, session

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/soundness"
PRINCIPAL_PORT = 8795
PB = "http://127.0.0.1:%d" % PRINCIPAL_PORT


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _run(body, output="done"):
    """One real model call against the lab server, recorded."""
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + "/v1/chat/completions",
                        content=json.dumps(body).encode())
        if output is not None:
            h.output = output
    return ev.Execution.load(h.path)


def _answered(output):
    """A recorded run whose model call is ordinary and whose answer is this."""
    return _run({"model": "gpt-4o-mini"}, output=output)


# --- P0-1  interpretation completeness ----------------------------------------

def t_P0_1_an_unreadable_envelope_does_not_prove_a_prohibition():
    """The finding. HTTP 200, and the extractor cannot read the shape.

    The body literally names send_email, in a private shape nobody documented.
    No tool call comes out of it, and "no tool call came out" was being read as
    "the model did not ask for one".
    """
    ex = _run({"model": "mystery-1", "unknown_shape": True})
    r = ev.did_not_call("send_email")(ex)
    return r.status == ev.UNKNOWN, "expected UNKNOWN, got %s: %s" % (r.status,
                                                                    r.reason)


def t_P0_1_an_unreadable_envelope_does_not_prove_an_absence():
    """The other direction. FAIL here claims the model asked for something
    else, which is a claim about a response nobody could read."""
    ex = _run({"model": "mystery-1", "unknown_shape": True})
    r = ev.used_tool("send_email")(ex)
    return r.status == ev.UNKNOWN, "expected UNKNOWN, got %s: %s" % (r.status,
                                                                    r.reason)


def t_P0_1_a_cut_stream_does_not_prove_a_prohibition():
    """A stream with no terminator. The next event could have been a tool
    call, and nothing in the trace rules that out."""
    ex = _run({"model": "gpt-4o-mini", "cut_stream": True})
    r = ev.did_not_call("send_email")(ex)
    return r.status == ev.UNKNOWN, "expected UNKNOWN, got %s: %s" % (r.status,
                                                                    r.reason)


def t_P0_1_a_finished_stream_still_answers():
    """Negative control. A stream that closed properly is readable, and a
    prohibition over it passes exactly as it did before."""
    ex = _run({"model": "gpt-4o-mini", "stream": True})
    r = ev.did_not_call("send_email")(ex)
    return r.status == ev.PASS, "expected PASS, got %s: %s" % (r.status,
                                                              r.reason)


def t_P0_1_a_readable_envelope_still_fails_on_an_observed_request():
    """Negative control. The violation is in the trace, so it is admissible
    however anything else went."""
    ex = _run({"model": "gpt-4o-mini", "n_tools": 2})
    r = ev.did_not_call("send_email")(ex)
    return r.status == ev.FAIL, "expected FAIL, got %s: %s" % (r.status,
                                                              r.reason)


def t_P0_1_a_readable_envelope_with_no_calls_still_passes():
    """Negative control, and the one that matters most: the ordinary green
    case must stay green, or the fix has simply broken every suite."""
    ex = _run({"model": "gpt-4o-mini"})
    r = ev.did_not_call("send_email")(ex)
    return r.status == ev.PASS, "expected PASS, got %s: %s" % (r.status,
                                                              r.reason)


def t_P0_1_used_tool_still_fails_when_the_model_asked_for_something_else():
    """Negative control for the FAIL side of used_tool."""
    ex = _run({"model": "gpt-4o-mini", "n_tools": 1})
    r = ev.used_tool("web_search")(ex)
    return r.status == ev.FAIL, "expected FAIL, got %s: %s" % (r.status,
                                                              r.reason)


def t_P0_1_the_tool_view_is_its_own_domain():
    """Transport completeness and interpretation completeness are two facts.

    The response arrived — served_responses is complete — and it could not be
    read. Collapsing those into one flag is what produced the finding.
    """
    ex = _run({"model": "mystery-1", "unknown_shape": True})
    o = ex.observation
    return (o.complete(obs.RESPONSES) and o.complete(obs.MODEL_RESPONSES)
            and not o.complete(obs.TOOL_VIEW)), (
        "responses=%s model=%s tool_view=%s"
        % (o.complete(obs.RESPONSES), o.complete(obs.MODEL_RESPONSES),
           o.complete(obs.TOOL_VIEW)))


def t_P0_1_the_gap_names_the_step_that_could_not_be_read():
    ex = _run({"model": "mystery-1", "unknown_shape": True})
    gaps = ex.observation.gaps(obs.TOOL_VIEW)
    return len(gaps) == 1, "expected one gap, got %r" % (gaps,)


def t_P0_1_a_tool_call_is_not_a_tool_execution():
    """The separation the evidence rules depend on.

    `did_not_call` reads the model's requests. It says nothing about whether
    the tool ran or what it did, and the domain it declares says so.
    """
    reads = ev.did_not_call("send_email").reads
    return obs.TOOL_VIEW in reads and obs.EMITTED not in reads, (
        "did_not_call reads %r" % (reads,))


# --- P0-1C  a domain nobody defined -------------------------------------------

def t_P0_1_an_unknown_domain_is_not_silently_complete():
    """A typo in a declaration used to buy silence instead of protection."""
    ex = _answered("done")
    return ex.observation.complete("moddel_responses") is False, (
        "an unknown domain reported complete")


def t_P0_1_an_unknown_domain_is_refused_at_declaration():
    """Better than UNKNOWN at run time: the author hears about it once, at the
    point where the name was written."""
    try:
        ev.check(lambda ex: True, name="typo", reads=("moddel_responses",))
    except ValueError as e:
        return "moddel_responses" in str(e), "message does not name it: %s" % e
    return False, "no ValueError for an unknown domain name"


def t_P0_1_a_declared_check_with_real_domains_still_works():
    """Negative control: the valid declaration path is untouched."""
    ex = _answered("done")
    c = ev.check(lambda e: True, name="ok", reads=(obs.MODEL_RESPONSES,))
    return c(ex).status == ev.PASS, "a valid declaration stopped working"


# --- P0-2  output_matches -----------------------------------------------------

class _Unreprable(object):
    def __repr__(self):
        raise RuntimeError("no repr for you")


def _truncated_output():
    """A stored answer that is a prefix, ending in the audit's counterexample.

    prefix  ...OK
    truth   ...OK ERROR
    pattern OK$
    """
    text = "x" * (model.OUTPUT_LIMIT - 2) + "OK ERROR"
    return _answered(text), text


def t_P0_2_a_prefix_match_is_not_a_witness():
    """The finding, as an arithmetic fact rather than an opinion: the pattern
    matches the stored prefix and does not match the answer it came from."""
    ex, truth = _truncated_output()
    stored = ex.output["value"]
    if not (re.search("OK$", stored) and not re.search("OK$", truth)):
        return False, "the counterexample did not set up"
    r = ev.output_matches("OK$")(ex)
    return r.status == ev.UNKNOWN, "expected UNKNOWN, got %s: %s" % (r.status,
                                                                    r.reason)


def t_P0_2_a_truncated_miss_is_still_unknown():
    """This half was already sound. It stays sound."""
    ex, _ = _truncated_output()
    r = ev.output_matches("nowhere-in-here")(ex)
    return r.status == ev.UNKNOWN, "expected UNKNOWN, got %s" % r.status


def t_P0_2_an_uncaptured_answer_is_not_an_empty_string():
    """`.*` matches the empty string, and the empty string is what an
    uncaptured answer looked like."""
    ex = _answered(_Unreprable())
    r = ev.output_matches(".*")(ex)
    return r.status == ev.UNKNOWN, "expected UNKNOWN, got %s: %s" % (r.status,
                                                                    r.reason)


def t_P0_2_an_uncaptured_answer_does_not_fail_either():
    """The other direction: FAIL would be a claim about an answer nobody
    has."""
    ex = _answered(_Unreprable())
    r = ev.output_matches("shipped")(ex)
    return r.status == ev.UNKNOWN, "expected UNKNOWN, got %s: %s" % (r.status,
                                                                    r.reason)


def t_P0_2_a_complete_answer_still_passes():
    """Negative control."""
    ex = _answered("your order shipped")
    r = ev.output_matches("shipped")(ex)
    return r.status == ev.PASS, "expected PASS, got %s: %s" % (r.status,
                                                              r.reason)


def t_P0_2_a_complete_answer_still_fails():
    """Negative control."""
    ex = _answered("your order shipped")
    r = ev.output_matches("refunded")(ex)
    return r.status == ev.FAIL, "expected FAIL, got %s: %s" % (r.status,
                                                              r.reason)


def t_P0_2_output_equals_is_unchanged_on_a_truncated_answer():
    """`output_equals` compares a digest taken over the whole value before
    truncation, so it was never exposed to this. It is left alone."""
    ex, truth = _truncated_output()
    same = ev.output_equals(truth)(ex)
    other = ev.output_equals(truth + "!")(ex)
    return (same.status == ev.PASS and other.status == ev.FAIL), (
        "same=%s other=%s" % (same.status, other.status))


def t_P0_2_output_equals_is_unchanged_on_an_uncaptured_answer():
    """It already declined to answer. Same verdict, same wire value."""
    ex = _answered(_Unreprable())
    return ev.output_equals("x")(ex).status == ev.UNKNOWN, "output_equals moved"


# --- P0-3  principal identity, pinned as a limitation -------------------------

class _Principals(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _who(self, who):
        body = json.dumps({"user": who or "anonymous"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Identity in a header — the case the lookup key cannot see."""
        self._who((self.headers.get("Authorization") or "")
                  .replace("Bearer ", ""))

    def do_POST(self):
        """Identity in the body — the case it can."""
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        self._who(sent.get("user"))


def _principal_replay():
    """Record as alice, replay the same request shape as bob."""
    _fresh()
    srv = ThreadingHTTPServer(("127.0.0.1", PRINCIPAL_PORT), _Principals)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    seen = {}
    try:
        def as_(who):
            def fn(h):
                r = h.client().get(PB + "/me",
                                   headers={"Authorization": "Bearer " + who})
                seen[who] = r.text
                h.output = r.text
            return fn

        with orientim.record(root=ROOT, always=True) as h:
            as_("alice")(h)
        d = session.replay(h.path, as_("bob"), strict=True)
        return d, seen
    finally:
        srv.shutdown()


def t_P0_3_a_principal_change_is_not_detected():
    """KNOWN LIMITATION, pinned so it cannot change in silence.

    The lookup key is method + URL + body, and every header that carries
    principal identity — authorization, cookie, x-api-key — is excluded from
    the header fingerprint too, because a fingerprint over a bearer token would
    turn every credential rotation into a divergence.

    The consequence is real and this test states it rather than hiding it: a
    replay driven by a different principal is served the first principal's
    recorded response and the verdict is IDENTICAL. Fixing it needs a declared
    principal, which is a recording-format decision, not a patch. See
    docs/limits.md.
    """
    d, seen = _principal_replay()
    return (d.diagnosis[0] == "IDENTICAL" and seen["bob"] == seen["alice"]), (
        "the limitation moved: verdict=%s bob=%r alice=%r"
        % (d.diagnosis[0], seen.get("bob"), seen.get("alice")))


def t_P0_3_a_body_change_is_still_detected():
    """The boundary of the limitation. Identity in the *body* is part of the
    key, so this half works — which is why the finding is about headers."""
    _fresh()
    srv = ThreadingHTTPServer(("127.0.0.1", PRINCIPAL_PORT), _Principals)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def as_(who):
            def fn(h):
                h.client().post(PB + "/me",
                                content=json.dumps({"user": who}).encode())
                h.output = who
            return fn

        with orientim.record(root=ROOT, always=True) as h:
            as_("alice")(h)
        d = session.replay(h.path, as_("bob"), strict=True)
        return d.diagnosis[0] != "IDENTICAL", (
            "a body change went undetected: %s" % (d.diagnosis[0],))
    finally:
        srv.shutdown()


# --- P0-4  obligations, not booleans ------------------------------------------

def _row(case, ok, results, run_id="r"):
    return {"case": case, "run_id": run_id, "ok": ok, "verdict": "IDENTICAL",
            "steps": 1, "evaluation": {
                "results": [{"evaluator": k, "status": v}
                            for k, v in results]}}


def _baseline(runs):
    return {"runs": runs}


def _frozen(case, ok, results, run_id="r"):
    """A baseline row, written the way `baselines.create` writes one."""
    return {"case": case, "run_id": run_id, "ok": ok, "verdict": "IDENTICAL",
            "steps": 1,
            "failed_evaluators": [k for k, v in results if v == "fail"],
            "evaluators": {k: v for k, v in results}}


def t_P0_4_a_new_violation_inside_a_failing_case_is_surfaced():
    """The finding. Both runs are red, and something new broke in the second.

    Reading only `ok` sees False -> False and files it under "already
    failing", which is exactly where a new security violation goes to die.
    """
    base = _baseline([_frozen("support", False,
                              [("max_steps", "fail"),
                               ("did_not_call", "pass")])])
    rows = [_row("support", False, [("max_steps", "fail"),
                                    ("did_not_call", "fail")])]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_.get("new_failures") == {"support": ["did_not_call"]}, (
        "new_failures=%r" % (cmp_.get("new_failures"),))


def t_P0_4_a_known_violation_is_not_reported_as_new():
    """Negative control, and the reason this cannot just flag every failure:
    a red case that is red for the same reason is not news."""
    base = _baseline([_frozen("support", False, [("max_steps", "fail")])])
    rows = [_row("support", False, [("max_steps", "fail")])]
    cmp_ = ci.compare(rows, base, key="case")
    return not cmp_.get("new_failures"), (
        "a known failure was reported as new: %r" % (cmp_.get("new_failures"),))


def t_P0_4_an_obligation_that_stopped_being_checked_is_surfaced():
    """A rule that existed in the baseline and is gone from the suite. The
    case is green, and it is green because nobody is asking any more."""
    base = _baseline([_frozen("support", True,
                              [("max_steps", "pass"),
                               ("did_not_call", "pass")])])
    rows = [_row("support", True, [("max_steps", "pass")])]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_.get("dropped_obligations") == {"support": ["did_not_call"]}, (
        "dropped_obligations=%r" % (cmp_.get("dropped_obligations"),))


def t_P0_4_an_obligation_that_lost_its_proof_is_surfaced():
    """PASS -> UNKNOWN. Nothing failed, and a rule that used to be established
    no longer is. The comparison can see it; it does not fail the build."""
    base = _baseline([_frozen("support", True, [("did_not_call", "pass")])])
    rows = [_row("support", True, [("did_not_call", "warn")])]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_.get("weakened") == {"support": ["did_not_call"]}, (
        "weakened=%r" % (cmp_.get("weakened"),))


def t_P0_4_a_new_case_that_is_already_failing_is_surfaced():
    """A case nobody had before, arriving red. `new_recordings` alone says it
    is new and not that it is failing."""
    cmp_ = ci.compare([_row("fresh", False, [("max_steps", "fail")])],
                      _baseline([]), key="case")
    return (cmp_.get("new_recordings") == ["fresh"]
            and cmp_.get("new_failing") == ["fresh"]), (
        "new_recordings=%r new_failing=%r"
        % (cmp_.get("new_recordings"), cmp_.get("new_failing")))


def t_P0_4_a_quiet_run_stays_quiet():
    """Negative control: nothing moved, nothing is reported."""
    base = _baseline([_frozen("support", True, [("max_steps", "pass")])])
    rows = [_row("support", True, [("max_steps", "pass")])]
    cmp_ = ci.compare(rows, base, key="case")
    noisy = {k: v for k, v in cmp_.items() if v}
    return not noisy, "expected silence, got %r" % (noisy,)


def t_P0_4_an_old_baseline_still_compares():
    """Baselines written before this carry `failed_evaluators` and no
    `evaluators` map. The comparison must keep working on them, and must not
    invent a transition it cannot see."""
    base = _baseline([{"case": "support", "run_id": "r", "ok": False,
                       "verdict": "IDENTICAL", "steps": 1,
                       "failed_evaluators": ["max_steps"]}])
    rows = [_row("support", False, [("max_steps", "fail"),
                                    ("did_not_call", "fail")])]
    cmp_ = ci.compare(rows, base, key="case")
    return (cmp_.get("new_failures") == {"support": ["did_not_call"]}
            and not cmp_.get("dropped_obligations")
            and not cmp_.get("weakened")), (
        "old baseline gave %r" % ({k: v for k, v in cmp_.items() if v},))


def t_P0_4_the_existing_verdicts_are_unchanged():
    """Negative control for the four keys that already existed. A run that
    started failing is still `newly_changed`, not something new."""
    base = _baseline([_frozen("a", True, [("max_steps", "pass")]),
                      _frozen("b", False, [("max_steps", "fail")]),
                      _frozen("gone", True, [("max_steps", "pass")])])
    rows = [_row("a", False, [("max_steps", "fail")]),
            _row("b", True, [("max_steps", "pass")]),
            _row("new", True, [("max_steps", "pass")])]
    cmp_ = ci.compare(rows, base, key="case")
    return (cmp_["newly_changed"] == ["a"] and cmp_["fixed"] == ["b"]
            and cmp_["new_recordings"] == ["new"]
            and cmp_["missing_recordings"] == ["gone"]), (
        "existing comparison moved: %r" % (cmp_,))
