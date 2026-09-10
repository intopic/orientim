# -*- coding: utf-8 -*-
"""Observation validity: what a trace licenses, and what it does not.

Three claims have to stay apart, and every check here exists to keep them apart:

    OBSERVED VIOLATION   the forbidden thing is in the trace          -> FAIL
    OBSERVED ABSENCE     it is not, and we saw everywhere it could be -> PASS
    UNOBSERVED           it is not, and we did not see everywhere     -> UNKNOWN

The third used to be reported as the second. `did_not_call("refund.issue")`
returned PASS on a replay whose model call was never answered, which is the
same verdict it gives a run that genuinely never asked for a refund.

Incomplete observations here are produced by a real divergence, not by
hand-built dicts — the same discipline as `test_evaluate.py`, for the same
reason: field names a fixture invents are field names a recording may not use.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import diff, evaluate as ev, observation as obs, session

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/observation"


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _ask(h, n_tools=1, **extra):
    body = {"model": "gpt-4o-mini", "n_tools": n_tools,
            "messages": [{"role": "user", "content": "where is 4471"}]}
    body.update(extra)
    return h.client().post(B + "/v1/chat/completions",
                           content=json.dumps(body).encode())


def _record(fn, output="done"):
    """A real recording. Every domain complete, by construction."""
    with orientim.record(root=ROOT, always=True) as h:
        fn(h)
        if output is not None:
            h.output = output
    return h.path


def _replayed(path, fn):
    """The execution a replay produced, built the way `cases.run` builds it."""
    d = session.replay(path, fn, strict=True)
    steps = diff.merge_unmatched(d.replay_steps, d.unmatched_requests)
    return ev.Execution.of({"outcome": d.replay_output, "dropped": 0}, steps), d


def _verdict(execution, evaluator):
    return evaluator(execution)


# --- the model ----------------------------------------------------------------

def t_a_recording_is_complete_for_everything():
    """Nothing was unanswered, because it all really ran."""
    _fresh()
    path = _record(lambda h: _ask(h))
    ex = ev.Execution.load(path)
    missing = [d for d in obs.DOMAINS if not ex.observation.complete(d)]
    return missing == [], "incomplete for %r" % (missing,)


def t_completeness_is_relative_to_the_question():
    """The point of the whole model: one trace, two answers.

    A replay that diverged at its first request lost the model's responses and
    kept every request the agent sent, so a question about what was *sent* is
    answerable and a question about what came *back* is not. A single
    execution-wide flag cannot say that.
    """
    _fresh()

    def v1(h):
        _ask(h)

    def v2(h):
        _ask(h, extra_field=True)

    path = _record(v1)
    ex, _d = _replayed(path, v2)
    o = ex.observation
    return (o.complete(obs.EMITTED)
            and not o.complete(obs.RESPONSES)
            and not o.complete(obs.MODEL_RESPONSES)), \
        "emitted=%s responses=%s model=%s" % (
            o.complete(obs.EMITTED), o.complete(obs.RESPONSES),
            o.complete(obs.MODEL_RESPONSES))


def t_an_error_response_is_an_observation():
    """A 404 is an answer. Only the absence of one is an absence of evidence."""
    _fresh()
    path = _record(lambda h: h.client().post(B + "/nope", content=b"{}"))
    ex = ev.Execution.load(path)
    return (ex.observation.complete(obs.RESPONSES)
            and len(ex.failed_steps()) == 1), \
        "complete=%s failed=%d" % (ex.observation.complete(obs.RESPONSES),
                                   len(ex.failed_steps()))


def t_the_gap_says_where_it_is():
    """A build that says "unknown" and not where is a build nobody can act on."""
    _fresh()
    path = _record(lambda h: _ask(h))
    ex, _d = _replayed(path, lambda h: _ask(h, extra_field=True))
    why = ex.observation.why(obs.MODEL_RESPONSES)
    return ("unanswered" in why and "model" in why), why


# --- A: the unsafe PASS this pass exists to remove ----------------------------

def t_A_did_not_call_is_unknown_without_the_model_response():
    """The counterexample.

    The agent under v2 may or may not have asked for the forbidden tool. The
    model call diverged, so no response exists, so the trace cannot tell those
    two runs apart — and used to answer PASS for both.
    """
    _fresh()

    def v1(h):
        _ask(h)

    def v2(h):
        _ask(h, plan=["refund.issue"])

    path = _record(v1)
    ex, _d = _replayed(path, v2)
    r = _verdict(ex, ev.did_not_call("refund.issue"))
    return (r.status == ev.UNKNOWN
            and "refund.issue" in r.reason
            and r.evidence.get("observation") == obs.MODEL_RESPONSES), \
        "%s: %s" % (r.status, r.reason)


# --- B: an observed violation is always admissible ----------------------------

def t_B_did_not_call_fails_on_an_observed_request():
    """FAIL needs no completeness: the trace never invents a call."""
    _fresh()
    path = _record(lambda h: _ask(h, n_tools=1))
    ex = ev.Execution.load(path)
    name = ex.tool_calls[0]["name"]
    r = _verdict(ex, ev.did_not_call(name))
    return (r.status == ev.FAIL and name in r.reason), "%s: %s" % (r.status,
                                                                   r.reason)


# --- C: a complete clean run still passes -------------------------------------

def t_C_did_not_call_passes_on_a_complete_run():
    """The guard must not turn every prohibition into a shrug."""
    _fresh()
    path = _record(lambda h: _ask(h, n_tools=1))
    ex = ev.Execution.load(path)
    r = _verdict(ex, ev.did_not_call("refund.issue"))
    return (r.status == ev.PASS and "never requested" in r.reason), \
        "%s: %s" % (r.status, r.reason)


def t_C_a_replay_that_matched_still_passes():
    """And the same through a replay that reproduced, which is the real path.

    The agent has to declare the same answer both times or the run is
    OUTPUT_CHANGED and this would be testing something else.
    """
    _fresh()

    def agent(h):
        _ask(h, n_tools=1)
        h.output = "done"

    path = _record(agent, output=None)
    ex, d = _replayed(path, agent)
    r = _verdict(ex, ev.did_not_call("refund.issue"))
    return (d.ok and r.status == ev.PASS), "ok=%s %s: %s" % (d.ok, r.status,
                                                             r.reason)


# --- D and E: the replay's own 599 is not the agent's failure -----------------

def t_D_no_step_failed_is_unknown_on_a_synthetic_599():
    """A 599 is what the replay says when it has nothing to serve.

    Reporting it as "1 of 1 call(s) failed" is a statement about the replay
    wearing the clothes of a statement about the agent, and it is the line that
    fired in nine of ten lab regressions.
    """
    _fresh()
    path = _record(lambda h: _ask(h))
    ex, _d = _replayed(path, lambda h: _ask(h, extra_field=True))
    r = _verdict(ex, ev.no_step_failed())
    return (r.status == ev.UNKNOWN
            and ex.observed_failures() == []
            and len(ex.failed_steps()) == 1), \
        "%s: %s (real=%d, raw=%d)" % (r.status, r.reason,
                                      len(ex.observed_failures()),
                                      len(ex.failed_steps()))


def t_E_no_step_failed_still_fails_on_a_real_error():
    """The other half, and the one that proves this is not just tolerance."""
    _fresh()
    path = _record(lambda h: h.client().post(B + "/nope", content=b"{}"))
    ex = ev.Execution.load(path)
    r = _verdict(ex, ev.no_step_failed())
    return (r.status == ev.FAIL and "1 of 1" in r.reason), "%s: %s" % (r.status,
                                                                       r.reason)


def t_E_a_real_error_fails_even_beside_a_divergence():
    """A real failure is admissible whatever else is missing.

    Otherwise the guard would hide genuine errors behind an unrelated
    divergence, which would be a worse bug than the one it fixes.
    """
    _fresh()

    def v1(h):
        h.client().post(B + "/nope", content=b"{}")
        _ask(h)

    def v2(h):
        h.client().post(B + "/nope", content=b"{}")
        _ask(h, extra_field=True)

    path = _record(v1)
    ex, _d = _replayed(path, v2)
    r = _verdict(ex, ev.no_step_failed())
    return (r.status == ev.FAIL), "%s: %s" % (r.status, r.reason)


# --- used_tool: conservative, and no more aggressive than before ---------------

def t_used_tool_is_unknown_when_one_model_call_is_unanswered():
    """One lost response is one place the request could have been.

    The old rule asked whether *every* model call went unanswered, which is
    right for a run that collapsed at step 0 and wrong for a run that lost its
    second call out of two.
    """
    _fresh()

    def v1(h):
        _ask(h, n_tools=0)
        _ask(h, n_tools=0, turn=2)

    def v2(h):
        _ask(h, n_tools=0)
        _ask(h, n_tools=0, turn=2, extra_field=True)

    path = _record(v1)
    ex, _d = _replayed(path, v2)
    answered = [s for s in ex.model_steps if obs.answered(s)]
    r = _verdict(ex, ev.used_tool("lookup_order"))
    return (r.status == ev.UNKNOWN and len(answered) == 1), \
        "%s: %s (answered %d of %d)" % (r.status, r.reason, len(answered),
                                        len(ex.model_steps))


def t_used_tool_still_passes_on_an_observed_request():
    _fresh()
    path = _record(lambda h: _ask(h, n_tools=1))
    ex = ev.Execution.load(path)
    name = ex.tool_calls[0]["name"]
    r = _verdict(ex, ev.used_tool(name))
    return r.status == ev.PASS, "%s: %s" % (r.status, r.reason)


def t_used_tool_still_fails_on_a_complete_run_that_asked_for_something_else():
    _fresh()
    path = _record(lambda h: _ask(h, n_tools=1))
    ex = ev.Execution.load(path)
    r = _verdict(ex, ev.used_tool("nothing_like_this"))
    return r.status == ev.FAIL, "%s: %s" % (r.status, r.reason)


# --- the ones that were already sound -----------------------------------------

def t_output_evaluators_are_unknown_without_an_output():
    """Unchanged behaviour, made explicit so it cannot regress quietly."""
    _fresh()
    path = _record(lambda h: _ask(h), output=None)
    ex = ev.Execution.load(path)
    a = _verdict(ex, ev.output_equals("anything"))
    b = _verdict(ex, ev.output_matches("anything"))
    return (a.status == ev.UNKNOWN and b.status == ev.UNKNOWN
            and not ex.observation.complete(obs.OUTPUT)), \
        "%s / %s" % (a.status, b.status)


def t_max_steps_is_unaffected_by_a_divergence():
    """It reads what the agent *sent*, and a divergence does not take that away.

    `note_miss` records every request the replay could not answer, so the
    domain this evaluator depends on stays complete. That is why it needed no
    change, and this pins the reasoning rather than the outcome.
    """
    _fresh()

    def v1(h):
        _ask(h)

    def v2(h):
        _ask(h, extra_field=True)

    path = _record(v1)
    ex, _d = _replayed(path, v2)
    r = _verdict(ex, ev.max_steps(10))
    return (r.status == ev.PASS
            and ex.observation.complete(obs.EMITTED)), "%s: %s" % (r.status,
                                                                   r.reason)


def t_evaluators_declare_what_they_read():
    """The matrix is derivable rather than written down twice."""
    want = {
        "used_tool": (obs.MODEL_RESPONSES,),
        "did_not_call": (obs.MODEL_RESPONSES,),
        "output_equals": (obs.OUTPUT,),
        "output_matches": (obs.OUTPUT,),
        "max_steps": (obs.EMITTED,),
        "no_step_failed": (obs.RESPONSES,),
    }
    built = {"used_tool": ev.used_tool("t"),
             "did_not_call": ev.did_not_call("t"),
             "output_equals": ev.output_equals("x"),
             "output_matches": ev.output_matches("x"),
             "max_steps": ev.max_steps(3),
             "no_step_failed": ev.no_step_failed()}
    got = {k: getattr(v, "reads", None) for k, v in built.items()}
    return got == want, "%r" % (got,)


# --- custom checks --------------------------------------------------------------

def t_a_custom_check_without_a_declaration_still_runs():
    """No breaking change: an existing check keeps its exact behaviour.

    Guessing which domains someone else's code reads would turn working suites
    red for a reason their author never wrote down.
    """
    _fresh()
    path = _record(lambda h: _ask(h))
    ex, _d = _replayed(path, lambda h: _ask(h, extra_field=True))
    r = _verdict(ex, ev.check(lambda e: True, name="mine"))
    return r.status == ev.PASS, "%s: %s" % (r.status, r.reason)


def t_a_custom_check_that_declares_is_protected():
    _fresh()
    path = _record(lambda h: _ask(h))
    ex, _d = _replayed(path, lambda h: _ask(h, extra_field=True))
    r = _verdict(ex, ev.check(lambda e: True, name="mine",
                              reads=(obs.MODEL_RESPONSES,)))
    return (r.status == ev.UNKNOWN and r.evaluator == "mine"), \
        "%s: %s" % (r.status, r.reason)


def t_a_declared_custom_check_runs_when_its_domain_is_complete():
    _fresh()
    path = _record(lambda h: _ask(h))
    ex = ev.Execution.load(path)
    r = _verdict(ex, ev.check(lambda e: False, name="mine",
                              reads=(obs.MODEL_RESPONSES,)))
    return r.status == ev.FAIL, "%s: %s" % (r.status, r.reason)


# --- the whole point ------------------------------------------------------------

def t_the_three_claims_are_distinguishable():
    """OBSERVED VIOLATION, OBSERVED ABSENCE and UNOBSERVED, side by side.

    One evaluator, one tool name, three executions, three different verdicts.
    Before this pass, two of them were the same word.
    """
    _fresh()
    rule = ev.did_not_call("lookup_order")

    violated = ev.Execution.load(_record(lambda h: _ask(h, n_tools=1)))
    absent = ev.Execution.load(_record(lambda h: _ask(h, n_tools=0)))
    path = _record(lambda h: _ask(h, n_tools=0))
    unobserved, _d = _replayed(path, lambda h: _ask(h, n_tools=0, extra=1))

    got = (rule(violated).status, rule(absent).status, rule(unobserved).status)
    return got == (ev.FAIL, ev.PASS, ev.UNKNOWN), "%r" % (got,)
