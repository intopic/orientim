# -*- coding: utf-8 -*-
"""One extraction, one coverage, one answer — TASK A.

The first pass at observation validity had two sources of semantic truth: an
extractor that produced tool calls, and a `tool_view` that independently
guessed whether the extraction had been exhaustive. Two sources disagree, and a
second independent audit produced eight counterexamples where the certifier was
the more optimistic of the two. Every one of them reproduced.

The fix is not a better guess. It is that coverage comes back from the same
parse that produced the facts, so "no tool call was found" and "the whole set
was enumerated" can never be two different opinions.

Three vocabularies stay apart here and in the docs:

    capture fidelity     the bytes we claim were observed were stored
    extraction validity  the facts derive from a schema we actually support
    claim soundness      the verdict follows from those facts and their coverage

This file is about the second and third. What a witness is worth is decided by
whether the enumeration closed, never by whether the response looked familiar.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import ci, evaluate as ev, explain, model, observation as obs, store

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/evidence"


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _run(path="/v1/chat/completions", **body):
    _fresh()
    payload = {"model": "gpt-4o-mini"}
    payload.update(body)
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + path, content=json.dumps(payload).encode())
        h.output = "done"
    return ev.Execution.load(h.path)


def _step(execution):
    return [s for s in execution.steps if s.get("t") == "http"][0]


def _both(execution, tool="send_email"):
    return (ev.did_not_call(tool)(execution).status,
            ev.used_tool(tool)(execution).status)


def _unknown_prohibition(execution, tool="send_email"):
    """A prohibition must not pass, and existence must not be denied."""
    dnc, used = _both(execution, tool)
    return (dnc == ev.UNKNOWN and used == ev.UNKNOWN), \
        "did_not_call=%s used_tool=%s" % (dnc, used)


# --- the eight reproduced counterexamples -------------------------------------

def t_a_known_container_in_an_unknown_schema_is_not_enumerated():
    """`output` is a container we know. This vendor put an object in it.

    Recognising a field name is not recognising a schema, and the first pass
    certified on the field name.
    """
    return _unknown_prohibition(_run(v="known_key_object"))


def t_a_malformed_container_is_not_enumerated():
    """HTTP 200 with `choices: 7`. The extractor cannot walk it; the first
    pass saw the key and called the response readable."""
    return _unknown_prohibition(_run(v="malformed_choices"))


def t_a_tool_call_past_the_event_bound_is_coverage_loss():
    """The parse stops at a fixed number of events. The tool call is after it,
    and a real terminator follows — so the stream *did* close, and the
    enumeration still did not cover it."""
    return _unknown_prohibition(_run(v="sse_over_limit"))


def t_a_tool_call_past_the_call_bound_is_coverage_loss():
    """The 51st call, with a bound of 50. A bounded list is not an
    exhaustive one, and the bound has to be said out loud."""
    ex = _run(v="over_tool_limit")
    dnc, used = _both(ex)
    # The 50 calls that WERE enumerated stay usable as witnesses.
    kept = ev.used_tool("lookup_order")(ex)
    return (dnc == ev.UNKNOWN and used == ev.UNKNOWN
            and kept.status == ev.PASS), \
        "did_not_call=%s used_tool=%s lookup_order=%s" % (dnc, used,
                                                          kept.status)


def t_the_marker_inside_content_is_not_a_terminator():
    """The model wrote `[DONE]` into its own text and the stream never closed.
    A substring search over the body cannot tell those apart; a frame parse
    can."""
    return _unknown_prohibition(_run(v="fake_done"))


def t_one_closed_channel_does_not_close_the_others():
    """choice 0 finished. choice 1 did not. The tool call could still have
    been coming on channel 1."""
    return _unknown_prohibition(_run(v="local_closure"))


def t_a_response_we_cannot_place_does_not_prove_a_prohibition():
    """The classification counterexample, and the sharpest of the eight: the
    same response body is a violation at a path we recognise and used to be a
    silent PASS at one we do not.

    The fix is not a longer list of paths. A step we cannot rule out as a
    model response makes the enumeration incomplete, so the prohibition is
    withheld rather than granted.
    """
    ex = _run(path="/vendor/generate", v="one_tool")
    dnc, used = _both(ex)
    return (dnc == ev.UNKNOWN and used == ev.UNKNOWN), \
        "did_not_call=%s used_tool=%s (role=%s)" % (dnc, used,
                                                    _step(ex).get("role"))


def t_an_unknown_path_and_an_unknown_envelope_still_withholds():
    """The gap the first fix left, found by asking what it did *not* cover.

    `could this have been a model call` originally wanted two signals to agree
    — a prompt-shaped request and an inference-shaped response — which is the
    discipline `classify` uses and exactly wrong here: when the vendor is
    unknown on both sides, neither signal is available and the prohibition went
    back to passing silently. A request carrying `messages`, `prompt` or
    `contents` is inference-shaped on its own.
    """
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + "/vendor/opaque",
                        content=json.dumps({
                            "model": "m",
                            "messages": [{"role": "user",
                                          "content": "hi"}]}).encode())
        h.output = "done"
    ex = ev.Execution.load(h.path)
    return ev.did_not_call("send_email")(ex).status == ev.UNKNOWN, \
        "expected UNKNOWN, got %s (role=%s)" % (
            ev.did_not_call("send_email")(ex).status, _step(ex).get("role"))


def t_an_ordinary_tool_call_does_not_block_a_prohibition():
    """The control for that, and the one that decides whether this is usable:
    an agent's ordinary HTTP work must not turn every prohibition into a
    question mark."""
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + "/v1/chat/completions",
                        content=json.dumps({"model": "m"}).encode())
        h.client().post(B + "/send-email", content=b'{"to":"customer"}')
        h.client().post(B + "/search", content=b'{"q":"order"}')
        h.output = "done"
    ex = ev.Execution.load(h.path)
    return ev.did_not_call("send_email")(ex).status == ev.PASS, \
        "ordinary tool traffic blocked a prohibition: %s" % (
            ev.did_not_call("send_email")(ex).reason,)


def t_the_same_body_at_a_known_path_is_still_a_violation():
    """The control for the case above. Nothing about the fix may soften a
    violation we can actually see."""
    ex = _run(v="one_tool")
    dnc, used = _both(ex)
    return (dnc == ev.FAIL and used == ev.PASS), \
        "did_not_call=%s used_tool=%s" % (dnc, used)


# --- partial witnesses: the asymmetry is the point ----------------------------

def t_a_partial_call_still_witnesses_a_prohibition():
    """Arguments cut mid-JSON, name complete. The model asked; a prohibition
    on asking is violated whatever happened to the arguments."""
    ex = _run(v="partial_tool")
    return ev.did_not_call("send_email")(ex).status == ev.FAIL, \
        "expected FAIL, got %s" % ev.did_not_call("send_email")(ex).status


def t_a_partial_call_does_not_prove_a_finalized_request():
    """The other half. `used_tool` asks whether the model *made* the request,
    and an unfinished one is a proposal, not a request."""
    ex = _run(v="partial_tool")
    return ev.used_tool("send_email")(ex).status == ev.UNKNOWN, \
        "expected UNKNOWN, got %s" % ev.used_tool("send_email")(ex).status


def t_a_reconstructed_name_that_never_closed_is_not_a_witness():
    """`send_` then `email`, and the stream is cut.

    Reassembling those into `send_email` is a guess about a name we never saw
    whole. It must not decide a prohibition in either direction.
    """
    ex = _run(v="split_name")
    dnc, used = _both(ex)
    return (dnc == ev.UNKNOWN and used == ev.UNKNOWN), \
        "did_not_call=%s used_tool=%s calls=%r" % (
            dnc, used, model.tool_names_in(ex.steps))


def t_a_closed_stream_is_a_full_witness():
    """Negative control: a stream that closed properly enumerates completely,
    and both directions answer."""
    ex = _run(v="closed_stream")
    dnc, used = _both(ex)
    return (dnc == ev.FAIL and used == ev.PASS), \
        "did_not_call=%s used_tool=%s" % (dnc, used)


# --- the extraction result itself ---------------------------------------------

def t_facts_and_coverage_come_from_one_result():
    """The structural claim. One call returns both, so they cannot disagree."""
    ex = _run(v="one_tool")
    e = model.tool_evidence(_step(ex))
    return (isinstance(e.get("calls"), list) and "complete" in e
            and isinstance(e.get("issues"), list)
            and e.get("extractor") is not None), "shape was %r" % (sorted(e),)


def t_a_limit_that_was_hit_is_named():
    ex = _run(v="over_tool_limit")
    e = model.tool_evidence(_step(ex))
    return (model.LIMIT_REACHED in e["issues"] and not e["complete"]), \
        "issues=%r complete=%s" % (e["issues"], e["complete"])


def t_an_unsupported_schema_is_named():
    ex = _run(v="known_key_object")
    e = model.tool_evidence(_step(ex))
    return (not e["complete"] and e["issues"]), \
        "issues=%r complete=%s" % (e["issues"], e["complete"])


def t_an_ordinary_response_is_complete():
    """Negative control: the common case must stay complete, or every suite
    in existence turns into question marks."""
    ex = _run(n_tools=1)
    e = model.tool_evidence(_step(ex))
    return (e["complete"] and not e["issues"]), \
        "issues=%r complete=%s" % (e["issues"], e["complete"])


def t_the_anthropic_shape_is_complete():
    """The other supported envelope, so the container check is not
    accidentally OpenAI-only."""
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + "/v1/messages",
                        content=json.dumps({"model": "claude",
                                            "n_tools": 1}).encode())
        h.output = "done"
    ex = ev.Execution.load(h.path)
    e = model.tool_evidence(_step(ex))
    return e["complete"], "issues=%r" % (e["issues"],)


def t_an_endpoint_with_no_tool_channel_is_complete():
    """`/embeddings` cannot carry a tool call, so nothing is missing."""
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        h.client().post(B + "/v1/embeddings",
                        content=json.dumps({"model": "m",
                                            "input": "hi"}).encode())
        h.output = "done"
    ex = ev.Execution.load(h.path)
    return ev.did_not_call("send_email")(ex).status == ev.PASS, \
        "an embeddings call blocked a prohibition"


def t_the_domain_reads_the_extraction_result():
    """`model_tool_calls` is complete exactly when the extraction says so."""
    bad = _run(v="known_key_object")
    good = _run(v="one_tool")
    return (not bad.observation.complete(obs.TOOL_VIEW)
            and good.observation.complete(obs.TOOL_VIEW)), "domain disagreed"


# --- evaluation and diff cannot contradict each other -------------------------

def _two_runs(v_a, v_b):
    _fresh()
    out = []
    for v in (v_a, v_b):
        with orientim.record(root=ROOT, always=True) as h:
            h.client().post(B + "/v1/chat/completions",
                            content=json.dumps({"model": "m", "v": v}).encode())
            h.output = "done"
        out.append(store.load(h.path))
    return out


def t_diff_does_not_claim_removal_from_an_unreadable_side():
    """The reproduced contradiction: evaluation withheld the question and the
    diff answered it, from the same two runs."""
    (meta_a, steps_a), (meta_b, steps_b) = _two_runs("one_tool",
                                                     "known_key_object")
    changes = explain.run_tool_changes(steps_a, steps_b)
    kinds = sorted({c["change"] for c in changes})
    return "removed" not in kinds, "diff still says %r" % (kinds,)


def t_diff_and_evaluation_agree_about_what_is_unknown():
    """Both read the same coverage, so neither can be certain where the other
    is not."""
    (meta_a, steps_a), (meta_b, steps_b) = _two_runs("one_tool",
                                                     "known_key_object")
    withheld = ev.did_not_call("send_email")(
        ev.Execution.of(meta_b, steps_b)).status == ev.UNKNOWN
    certain = [c for c in explain.run_tool_changes(steps_a, steps_b)
               if c["change"] in ("added", "removed")]
    return (withheld and not certain), \
        "evaluation withheld=%s diff certain=%r" % (withheld, certain)


def t_the_step_level_diff_is_not_certain_either():
    """The same guard one level down.

    `orientim diff` compares tools twice: once over the whole run, and once
    per aligned pair of steps. Fixing only the first would have left the same
    false certainty in the per-step rows, which is where a reader actually
    looks.
    """
    (meta_a, steps_a), (meta_b, steps_b) = _two_runs("one_tool",
                                                     "known_key_object")
    a = [s for s in steps_a if s.get("t") == "http"][0]
    b = [s for s in steps_b if s.get("t") == "http"][0]
    kinds = {c["change"] for c in explain.step_tool_changes(a, b)}
    return "removed" not in kinds, "step-level diff says %r" % (kinds,)


def t_the_step_level_diff_still_reports_a_real_removal():
    """Its control."""
    (meta_a, steps_a), (meta_b, steps_b) = _two_runs("one_tool", "closed_none")
    a = [s for s in steps_a if s.get("t") == "http"][0]
    b = [s for s in steps_b if s.get("t") == "http"][0]
    kinds = {c["change"] for c in explain.step_tool_changes(a, b)}
    return "removed" in kinds, "expected a removal, got %r" % (kinds,)


def t_diff_still_reports_a_real_removal():
    """Negative control. Both sides readable, the tool really is gone."""
    (meta_a, steps_a), (meta_b, steps_b) = _two_runs("one_tool", "closed_none")
    changes = explain.run_tool_changes(steps_a, steps_b)
    return any(c["change"] == "removed" and c["name"] == "send_email"
               for c in changes), "expected a removal, got %r" % (changes,)


def t_diff_still_reports_a_real_addition():
    """The inverse control."""
    (meta_a, steps_a), (meta_b, steps_b) = _two_runs("closed_none", "one_tool")
    changes = explain.run_tool_changes(steps_a, steps_b)
    return any(c["change"] == "added" and c["name"] == "send_email"
               for c in changes), "expected an addition, got %r" % (changes,)


# --- obligations, not evaluator names -----------------------------------------

def t_an_evaluator_carries_its_subject():
    a = ev.did_not_call("refund.issue")
    b = ev.did_not_call("send_email")
    return (a.obligation != b.obligation and a.obligation), \
        "%r vs %r" % (getattr(a, "obligation", None),
                      getattr(b, "obligation", None))


def t_a_custom_check_can_declare_a_stable_obligation():
    """Two checks sharing a name are two promises. Without an explicit id the
    comparison reports them as uncomparable rather than choosing one; with it,
    both stay visible even if the checks are later renamed."""
    default = ev.check(lambda x: True, name="no_pii")
    explicit = ev.check(lambda x: True, name="no_pii",
                        obligation="no_pii:customer_email")
    return (default.obligation == "no_pii"
            and explicit.obligation == "no_pii:customer_email"), \
        "%r / %r" % (default.obligation, explicit.obligation)


def _row(pairs, ok=False):
    return {"case": "support", "run_id": "r", "ok": ok, "verdict": "IDENTICAL",
            "steps": 1,
            "evaluation": {"results": [
                {"evaluator": e, "obligation": o, "status": s}
                for e, o, s in pairs]}}


def _frozen(pairs, ok=False):
    return {"case": "support", "run_id": "r", "ok": ok, "verdict": "IDENTICAL",
            "steps": 1,
            "failed_evaluators": [e for e, _o, s in pairs if s == "fail"],
            "evaluators": {e: s for e, _o, s in pairs},
            "obligations": {o: s for _e, o, s in pairs}}


REFUND = ("did_not_call", "did_not_call:refund.issue")
EMAIL = ("did_not_call", "did_not_call:send_email")
STEPS = ("max_steps", "max_steps:5")


def t_two_obligations_of_one_evaluator_stay_separate():
    """The reproduced P0-4A. Keyed by evaluator name, the second overwrote the
    first and a new violation of a *different* prohibition vanished."""
    base = {"runs": [_frozen([STEPS + ("fail",), REFUND + ("pass",),
                              EMAIL + ("pass",)])]}
    rows = [_row([STEPS + ("fail",), REFUND + ("fail",), EMAIL + ("pass",)])]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_.get("new_failures") == {"support": [REFUND[1]]}, \
        "new_failures=%r" % (cmp_.get("new_failures"),)


def t_declaration_order_cannot_change_the_comparison():
    """The invariant. The same obligations in a different order are the same
    obligations, and the report must not move."""
    base = {"runs": [_frozen([STEPS + ("fail",), REFUND + ("pass",),
                              EMAIL + ("pass",)])]}
    a = ci.compare([_row([STEPS + ("fail",), REFUND + ("fail",),
                          EMAIL + ("pass",)])], base, key="case")
    b = ci.compare([_row([EMAIL + ("pass",), REFUND + ("fail",),
                          STEPS + ("fail",)])], base, key="case")
    return a == b, "%r != %r" % ({k: v for k, v in a.items() if v},
                                 {k: v for k, v in b.items() if v})


def t_a_legacy_baseline_does_not_invent_history():
    """An old baseline stored evaluator names. With two obligations of one
    type in the current suite, it cannot say which of them passed before —
    and must say so rather than pick one."""
    base = {"runs": [{"case": "support", "run_id": "r", "ok": False,
                      "verdict": "IDENTICAL", "steps": 1,
                      "failed_evaluators": ["max_steps"],
                      "evaluators": {"max_steps": "fail",
                                     "did_not_call": "pass"}}]}
    rows = [_row([STEPS + ("fail",), REFUND + ("fail",), EMAIL + ("pass",)])]
    cmp_ = ci.compare(rows, base, key="case")
    return (cmp_.get("legacy_uncomparable")
            and not cmp_.get("dropped_obligations")), \
        "legacy=%r dropped=%r" % (cmp_.get("legacy_uncomparable"),
                                  cmp_.get("dropped_obligations"))


def t_a_legacy_baseline_without_multiplicity_still_compares():
    """Negative control: one obligation per evaluator, so the old key is
    unambiguous and the comparison keeps working."""
    base = {"runs": [{"case": "support", "run_id": "r", "ok": False,
                      "verdict": "IDENTICAL", "steps": 1,
                      "failed_evaluators": ["max_steps"],
                      "evaluators": {"max_steps": "fail",
                                     "did_not_call": "pass"}}]}
    rows = [_row([STEPS + ("fail",), REFUND + ("fail",)])]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_.get("new_failures") == {"support": [REFUND[1]]}, \
        "new_failures=%r legacy=%r" % (cmp_.get("new_failures"),
                                       cmp_.get("legacy_uncomparable"))


def t_a_row_without_obligations_is_ambiguous_not_arbitrary():
    """A row written before obligations existed collapses two rules into one
    name. Last-write-wins made the answer depend on declaration order; this
    says it cannot tell, which is true, and says it the same way either way."""
    base = {"runs": [{"case": "support", "run_id": "r", "ok": False,
                      "steps": 1, "verdict": "IDENTICAL",
                      "evaluators": {"max_steps": "fail",
                                     "did_not_call": "pass"}}]}

    def legacy_row(pairs):
        return {"case": "support", "run_id": "r", "ok": False, "steps": 1,
                "verdict": "IDENTICAL",
                "evaluation": {"results": [{"evaluator": e, "status": s}
                                           for e, s in pairs]}}

    a = ci.compare([legacy_row([("max_steps", "fail"),
                                ("did_not_call", "fail"),
                                ("did_not_call", "pass")])], base, key="case")
    b = ci.compare([legacy_row([("max_steps", "fail"),
                                ("did_not_call", "pass"),
                                ("did_not_call", "fail")])], base, key="case")
    return (a == b and a.get("legacy_uncomparable")
            and not a.get("dropped_obligations")), \
        "a=%r b=%r" % ({k: v for k, v in a.items() if v},
                       {k: v for k, v in b.items() if v})


def t_a_dropped_obligation_is_still_seen():
    base = {"runs": [_frozen([STEPS + ("pass",), REFUND + ("pass",)], ok=True)]}
    rows = [_row([STEPS + ("pass",)], ok=True)]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_.get("dropped_obligations") == {"support": [REFUND[1]]}, \
        "%r" % (cmp_.get("dropped_obligations"),)


def t_a_lost_proof_is_still_seen():
    base = {"runs": [_frozen([REFUND + ("pass",)], ok=True)]}
    rows = [_row([REFUND + ("warn",)], ok=True)]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_.get("weakened") == {"support": [REFUND[1]]}, \
        "%r" % (cmp_.get("weakened"),)


def t_a_quiet_run_is_still_quiet():
    base = {"runs": [_frozen([STEPS + ("pass",)], ok=True)]}
    rows = [_row([STEPS + ("pass",)], ok=True)]
    noisy = {k: v for k, v in ci.compare(rows, base, key="case").items() if v}
    return not noisy, "expected silence, got %r" % (noisy,)


# --- the gate: explicit profiles, no silent change ----------------------------

def t_the_legacy_gate_is_unchanged():
    """A new violation inside an already-failing case does not fail the legacy
    build. That is the behaviour teams have; it stays, and it gets a name."""
    cmp_ = {"newly_changed": [], "new_failures": {"support": [REFUND[1]]},
            "weakened": {}, "dropped_obligations": {}, "new_failing": []}
    code, reasons = ci.gate(cmp_, [], profile=ci.LEGACY)
    return code == ci.EXIT_OK and not reasons, \
        "legacy gate moved: %s %r" % (code, reasons)


def t_the_protected_gate_blocks_a_new_violation():
    cmp_ = {"newly_changed": [], "new_failures": {"support": [REFUND[1]]},
            "weakened": {}, "dropped_obligations": {}, "new_failing": []}
    code, reasons = ci.gate(cmp_, [], profile=ci.PROTECTED)
    return code == ci.EXIT_CHANGED and reasons, \
        "protected gate did not block: %s %r" % (code, reasons)


def t_the_protected_gate_blocks_a_lost_proof():
    """PASS to UNKNOWN on an obligation that used to hold is a protection
    loss. This is not "every UNKNOWN fails" — an obligation that was never
    established does not block."""
    lost = ci.gate({"newly_changed": [], "new_failures": {},
                    "weakened": {"support": [REFUND[1]]},
                    "dropped_obligations": {}, "new_failing": []},
                   [], profile=ci.PROTECTED)
    never = ci.gate({"newly_changed": [], "new_failures": {}, "weakened": {},
                     "dropped_obligations": {}, "new_failing": []},
                    [{"case": "support", "ok": True,
                      "evaluation": {"results": [
                          {"evaluator": "did_not_call",
                           "obligation": REFUND[1], "status": "warn"}]}}],
                   profile=ci.PROTECTED)
    return (lost[0] == ci.EXIT_CHANGED and never[0] == ci.EXIT_OK), \
        "lost=%r never-established=%r" % (lost, never)


def t_the_protected_gate_blocks_a_removed_obligation():
    code, _ = ci.gate({"newly_changed": [], "new_failures": {},
                       "weakened": {},
                       "dropped_obligations": {"support": [REFUND[1]]},
                       "new_failing": []}, [], profile=ci.PROTECTED)
    return code == ci.EXIT_CHANGED, "a removed protection did not block"


def t_a_green_run_passes_both_profiles():
    """Negative control, and the one that matters for adoption."""
    quiet = {"newly_changed": [], "new_failures": {}, "weakened": {},
             "dropped_obligations": {}, "new_failing": []}
    return (ci.gate(quiet, [], profile=ci.LEGACY)[0] == ci.EXIT_OK
            and ci.gate(quiet, [], profile=ci.PROTECTED)[0] == ci.EXIT_OK), \
        "a quiet run did not pass"
