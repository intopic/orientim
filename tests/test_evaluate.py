# -*- coding: utf-8 -*-
"""Evaluators, against executions recorded through the real transport.

Every check here builds its execution by recording an agent, not by handing the
evaluator a dict. An evaluator that only works on hand-built fixtures is an
evaluator that has never seen the field names a recording actually uses.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import evaluate as ev

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/evaluate"


def _ask(h, path, **body):
    body.setdefault("model", "gpt-4o-mini")
    body.setdefault("messages", [{"role": "user", "content": "where is 4471"}])
    return h.client().post(B + path, content=json.dumps(body).encode())


def _run(fn, output=None):
    """Record an agent and hand back its execution."""
    with orientim.record(root=ROOT) as h:
        fn(h)
        if output is not None:
            h.output = output
        h.rec.trigger("evaluate")
    return ev.Execution.load(h.path)


def _asked_for_a_tool(h):
    """One model call whose response requests lookup_order."""
    _ask(h, "/v1/chat/completions", n_tools=1)


# --- output -------------------------------------------------------------------

def t_output_equals_same_and_different():
    e = _run(_asked_for_a_tool, output="order 4471 shipped monday")
    same = ev.output_equals("order 4471 shipped monday")(e)
    other = ev.output_equals("order 4471 is lost")(e)
    ok = (same.status == ev.PASS and other.status == ev.FAIL
          and other.evidence.get("actual") == "order 4471 shipped monday"
          and other.evidence.get("expected") == "order 4471 is lost")
    return ok, "same=%s other=%s (%s)" % (same.status, other.status, other.reason)


def t_output_equals_without_a_declared_output():
    """No output declared is a question we cannot answer, not a failure."""
    e = _run(_asked_for_a_tool)
    r = ev.output_equals("anything")(e)
    return (r.status == ev.WARN and "run.output" in json.dumps(r.evidence)), \
        "%s: %s" % (r.status, r.reason)


def t_output_matches():
    e = _run(_asked_for_a_tool, output="order 4471 shipped on monday")
    hit = ev.output_matches(r"order \d+ shipped")(e)
    miss = ev.output_matches(r"refund issued")(e)
    ok = (hit.status == ev.PASS and hit.evidence["matched"] == "order 4471 shipped"
          and miss.status == ev.FAIL)
    return ok, "hit=%s (%r), miss=%s" % (
        hit.status, hit.evidence.get("matched"), miss.status)


def t_output_matches_truncated_is_a_warning():
    """A pattern that might match past the storage cut cannot be called failed."""
    e = _run(_asked_for_a_tool, output="x" * (70 * 1024) + " NEEDLE")
    r = ev.output_matches("NEEDLE")(e)
    return (r.status == ev.WARN and r.evidence.get("truncated")), \
        "%s: %s" % (r.status, r.reason)


# --- tools --------------------------------------------------------------------

def t_used_tool_present_and_missing():
    e = _run(lambda h: _ask(h, "/v1/chat/completions", n_tools=2))
    found = ev.used_tool("send_email")(e)
    absent = ev.used_tool("refund_order")(e)
    ok = (found.status == ev.PASS
          and found.evidence["calls"][0]["arguments"]["order_id"] == 4472
          and absent.status == ev.FAIL
          and absent.evidence["requested"] == ["lookup_order", "send_email"])
    return ok, "found=%s absent=%s (%s)" % (
        found.status, absent.status, absent.reason)


def t_used_tool_evidence_answers_the_question():
    """The definition of done: the answer carries the arguments, not a boolean."""
    e = _run(_asked_for_a_tool)
    r = ev.used_tool("lookup_order")(e)
    call = (r.evidence.get("calls") or [{}])[0]
    ok = (r.status == ev.PASS and call.get("name") == "lookup_order"
          and call.get("arguments", {}).get("order_id") == 4471
          and call.get("step") is not None)
    return ok, "evidence %r" % (call,)


def t_used_tool_when_nothing_asked():
    """Distinguish 'asked for others' from 'asked for nothing'."""
    e = _run(lambda h: _ask(h, "/v1/chat/completions"))
    r = ev.used_tool("lookup_order")(e)
    return (r.status == ev.FAIL and r.evidence["requested"] == []
            and "no tools at all" in r.reason), "%s: %s" % (r.status, r.reason)


def t_did_not_call_forbidden_tool():
    clean = _run(lambda h: _ask(h, "/v1/chat/completions", n_tools=1))
    dirty = _run(lambda h: _ask(h, "/v1/chat/completions", n_tools=2))
    good = ev.did_not_call("send_email")(clean)
    bad = ev.did_not_call("send_email")(dirty)
    ok = (good.status == ev.PASS and bad.status == ev.FAIL
          and bad.evidence["calls"][0]["name"] == "send_email")
    return ok, "clean=%s dirty=%s (%s)" % (good.status, bad.status, bad.reason)


# --- shape of the run ---------------------------------------------------------

def t_max_steps_catches_a_loop():
    def looping(h):
        for _ in range(6):
            h.client().post(B + "/search", content=b'{"q":1}')

    e = _run(looping)
    under = ev.max_steps(10)(e)
    over = ev.max_steps(3)(e)
    ok = (under.status == ev.PASS and over.status == ev.FAIL
          and over.evidence["steps"] == 6 and over.evidence["limit"] == 3)
    return ok, "under=%s over=%s (%s)" % (under.status, over.status, over.reason)


def t_max_steps_is_honest_about_a_truncated_ring():
    """With steps evicted, the count is a floor. Say so instead of guessing."""
    with orientim.record(root=ROOT, ring=3) as h:
        for _ in range(8):
            h.client().post(B + "/search", content=b'{"q":1}')
        h.rec.trigger("evaluate")
    e = ev.Execution.load(h.path)
    r = ev.max_steps(5)(e)
    return (r.status == ev.WARN and r.evidence["dropped"] > 0), \
        "%s: %s" % (r.status, r.reason)


def t_no_step_failed():
    good = _run(lambda h: h.client().post(B + "/search", content=b'{"q":1}'))
    bad = _run(lambda h: h.client().post(B + "/ratelimit", content=b"{}"))
    a = ev.no_step_failed()(good)
    b = ev.no_step_failed()(bad)
    if b.status != ev.FAIL:
        # /ratelimit answers 429 only on some calls; skip rather than flake.
        return a.status == ev.PASS, "clean run passed; the 429 did not fire"
    return (a.status == ev.PASS and b.evidence["failed"][0]["status"] >= 400), \
        "clean=%s failing=%s (%s)" % (a.status, b.status, b.reason)


def t_failed_step_from_an_exception():
    """A call that raised has status 0, not an HTTP status. It still failed."""
    def unreachable(h):
        try:
            h.client().post("http://127.0.0.1:9/nothing", content=b"{}",
                            timeout=0.4)
        except Exception:
            pass

    e = _run(unreachable)
    r = ev.no_step_failed()(e)
    return (r.status == ev.FAIL and r.evidence["failed"][0]["status"] == 0), \
        "%s: %s" % (r.status, r.reason)


# --- custom -------------------------------------------------------------------

def t_custom_evaluator_forms():
    e = _run(_asked_for_a_tool, output="fine")

    forms = {
        "bool": ev.check(lambda x: len(x.http) == 1, name="bool"),
        "tuple": ev.check(lambda x: (False, "two calls expected"), name="tuple"),
        "string": ev.check(lambda x: "not allowed", name="string"),
        "result": ev.check(
            lambda x: ev.Result(ev.WARN, "result", "cannot tell"), name="result"),
        "none": ev.check(lambda x: None, name="none"),
    }
    got = {k: f(e).status for k, f in forms.items()}
    want = {"bool": ev.PASS, "tuple": ev.FAIL, "string": ev.FAIL,
            "result": ev.WARN, "none": ev.WARN}
    return got == want, "statuses %r" % (got,)


def t_custom_evaluator_that_raises_fails_itself():
    """A crashing check must never be mistaken for a passing one."""
    e = _run(_asked_for_a_tool)

    def broken(_x):
        raise ValueError("bad expectation")

    r = ev.check(broken, name="broken")(e)
    return (r.status == ev.FAIL and r.evidence["raised"] == "ValueError"), \
        "%s: %s" % (r.status, r.reason)


def t_custom_evaluator_reads_tool_arguments():
    """The real reason a custom evaluator exists."""
    e = _run(lambda h: _ask(h, "/v1/chat/completions", n_tools=2))

    def only_for_this_order(x):
        wrong = [c for c in x.tool_calls
                 if (c.get("arguments") or {}).get("order_id") not in (4471, 4472)]
        if wrong:
            return False, "called with unexpected orders: %r" % wrong
        return True, "every tool call named a known order"

    r = ev.check(only_for_this_order, name="orders")(e)
    return r.status == ev.PASS, "%s: %s" % (r.status, r.reason)


# --- together -----------------------------------------------------------------

def t_evaluators_combine_into_one_report():
    """All of them run, always: the shape of the whole set is the diagnosis."""
    def agent(h):
        _ask(h, "/v1/chat/completions", n_tools=2)
        h.client().post(B + "/search", content=b'{"q":1}')
        h.client().post(B + "/ratelimit", content=b"{}")

    e = _run(agent, output="order 4471 shipped")
    report = ev.evaluate(e, [
        ev.output_matches(r"4471"),
        ev.used_tool("lookup_order"),
        ev.used_tool("refund_order"),        # fails
        ev.did_not_call("send_email"),       # fails
        ev.max_steps(10),
        ev.check(lambda x: len(x.model_steps) >= 1, name="asked_a_model"),
    ])
    names = [r.evaluator for r in report.failed]
    ok = (len(report.results) == 6 and not report.ok
          and sorted(names) == ["did_not_call", "used_tool"]
          and len(report.passed) == 4)
    return ok, "%d results, failed=%r, ok=%s" % (
        len(report.results), names, report.ok)


def t_report_is_machine_readable_and_human_readable():
    e = _run(_asked_for_a_tool, output="done")
    report = ev.evaluate(e, [ev.used_tool("lookup_order"),
                             ev.used_tool("nope")])
    blob = json.loads(report.as_json())
    text = report.report()
    ok = (blob["ok"] is False and len(blob["results"]) == 2
          and blob["results"][0]["evidence"]["calls"][0]["name"] == "lookup_order"
          and "lookup_order" in text and "nope" in text)
    return ok, "json keys %r" % (sorted(blob),)


def t_a_warning_does_not_fail_a_report():
    """Failing a build on a question nobody could answer teaches people to
    ignore the tool."""
    e = _run(_asked_for_a_tool)          # no declared output
    report = ev.evaluate(e, [ev.output_equals("anything"),
                             ev.used_tool("lookup_order")])
    return (report.ok and len(report.warnings) == 1), \
        "ok=%s warnings=%d" % (report.ok, len(report.warnings))


def t_evaluate_accepts_a_path():
    with orientim.record(root=ROOT) as h:
        _asked_for_a_tool(h)
        h.output = "done"
        h.rec.trigger("evaluate")
    report = ev.evaluate(h.path, [ev.used_tool("lookup_order")])
    return report.ok, "%s" % (report.report(),)


def t_evaluation_reads_a_migrated_recording():
    """Backwards compatibility, end to end.

    A format 3 file gets its typed steps and its tool calls on read, so an
    evaluator answers about a recording made before any of this existed.
    """
    from orientim import store
    with orientim.record(root=ROOT) as h:
        _asked_for_a_tool(h)
        h.rec.trigger("evaluate")

    meta, steps = store.load(h.path)
    meta = dict(meta)
    meta["format"] = 3
    for key in ("outcome", "agent", "runtime", "migrated_from"):
        meta.pop(key, None)
    old = []
    for s in steps:
        s = dict(s)
        if s.get("t") == "http":
            for key in ("role", "model", "served"):
                s.pop(key, None)
        old.append(s)
    p = os.path.join(ROOT, "as_v3.jsonl")
    with open(p, "wb") as f:
        f.write(("\n".join([json.dumps({"_meta": meta})]
                           + [json.dumps(s) for s in old]) + "\n").encode())

    r = ev.used_tool("lookup_order")(ev.Execution.load(p))
    return r.status == ev.PASS, "%s: %s" % (r.status, r.reason)
