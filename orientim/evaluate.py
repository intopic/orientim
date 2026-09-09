# -*- coding: utf-8 -*-
"""Evaluators: asking a recorded execution a question, and getting evidence back.

A replay answers "did anything change". That is the regression question, and it
is not the only one people have. The others sound like:

    did it still call the tool it is supposed to call
    did it call the one it must never call
    did it loop
    did any of its calls fail
    is the answer still the answer

Those are properties of the execution, not of the diff between two executions,
so they are asked here rather than in a replay.

Two rules this module is built around.

**It reads the execution model, it does not re-derive it.** Nothing here parses
a URL, matches a request or touches the hash chain. Every question is answered
from fields the recording already decided — `role`, `served.tool_calls`,
`outcome`, `status`. Two places deciding what a model call is would eventually
be two places disagreeing about it.

**A result is never a bare boolean.** "Failed" without the reason is a support
ticket. Every evaluator returns what it looked at and what it found, so the
answer to `used_tool("lookup_order")` is not `False` but *"no tool call named
lookup_order; the model asked for send_email at step 4"*.
"""
import json
import re

from . import model, store

PASS = "pass"
FAIL = "fail"
WARN = "warn"


class Result:
    """One evaluator's answer, with its evidence."""

    __slots__ = ("status", "evaluator", "reason", "evidence")

    def __init__(self, status, evaluator, reason, evidence=None):
        self.status = status
        self.evaluator = evaluator
        self.reason = reason
        self.evidence = evidence or {}

    @property
    def ok(self):
        """A warning is not a failure. It is a question we could not close."""
        return self.status != FAIL

    def as_dict(self):
        return {"status": self.status, "evaluator": self.evaluator,
                "reason": self.reason, "evidence": self.evidence}

    def __repr__(self):
        return "<%s %s: %s>" % (self.status, self.evaluator, self.reason)


class Execution:
    """A completed run, read through the execution model.

    Built from a recording, or from any (meta, steps) pair — which is the seam
    a replay-and-evaluate command will use later without this module needing to
    know anything about replay.
    """

    def __init__(self, meta, steps):
        self.meta = meta or {}
        self.steps = steps or []
        self.http = [s for s in self.steps if s.get("t") == "http"]
        self.model_steps = [s for s in self.http if s.get("role") == model.MODEL]
        self.tool_steps = [s for s in self.http if s.get("role") == model.TOOL]
        self.tool_calls = model.tool_calls_in(self.steps)
        self.output = self.meta.get("outcome")
        self.agent = self.meta.get("agent")
        self.runtime = self.meta.get("runtime")
        self.status = self.meta.get("status")

    @classmethod
    def load(cls, path):
        meta, steps = store.load(path)
        return cls(meta, steps)

    @classmethod
    def of(cls, meta, steps):
        return cls(meta, steps)

    def calls_named(self, name):
        return [c for c in self.tool_calls if c.get("name") == name]

    def failed_steps(self):
        """Steps that did not come back cleanly.

        Status 0 is what the recorder writes when the request raised rather
        than answered, so it belongs here and not in an HTTP status range.
        """
        out = []
        for s in self.http:
            status = s.get("status") or 0
            if s.get("error") or status == 0 or status >= 400:
                out.append(s)
        return out

    def __repr__(self):
        return "<Execution %s: %d http, %d model, %d tool calls>" % (
            self.meta.get("run_id", "?"), len(self.http),
            len(self.model_steps), len(self.tool_calls))


# --- helpers ------------------------------------------------------------------

def _step_ref(step):
    """How a step is named in evidence: enough to find it, not the whole thing."""
    return {"step": step.get("i"), "method": step.get("method"),
            "url": step.get("url"), "status": step.get("status"),
            "error": step.get("error")}


def _call_ref(call):
    return {"step": call.get("step"), "name": call.get("name"),
            "arguments": call.get("arguments"),
            "arguments_kind": call.get("arguments_kind"),
            "partial": call.get("partial", False)}


def _no_output(name):
    return Result(
        WARN, name,
        "this run never declared an output, so there is nothing to compare",
        {"how_to_fix": "set run.output = ... inside the record() block"})


# --- evaluators ---------------------------------------------------------------
# Each is a factory returning fn(execution) -> Result, so they compose in a list
# and carry their own parameters in their name.

def output_equals(expected):
    """The answer is exactly this.

    Compared by digest over the whole value, never by the stored text: the
    stored text is truncated at 64 KB and two different long answers would
    compare equal below that line.
    """
    name = "output_equals"

    def check(ex):
        if not ex.output:
            return _no_output(name)
        want = model.capture_output(expected)
        got = ex.output
        if got.get("kind") == "unavailable":
            return Result(WARN, name,
                          "the recorded output could not be captured (%s)"
                          % got.get("error", "unknown"), {"recorded": got})
        if want and got.get("sha") == want.get("sha"):
            return Result(PASS, name, "the answer is unchanged",
                          {"value": got.get("value")})
        return Result(FAIL, name, "the answer is not what was expected",
                      {"expected": (want or {}).get("value"),
                       "actual": got.get("value"),
                       "truncated": got.get("truncated", False)})

    return check


def output_matches(pattern, flags=0):
    """The answer contains something matching this regular expression."""
    name = "output_matches"
    rx = re.compile(pattern, flags)

    def check(ex):
        if not ex.output:
            return _no_output(name)
        text = ex.output.get("value") or ""
        m = rx.search(text)
        if m:
            return Result(PASS, name, "the answer matches %r" % pattern,
                          {"matched": m.group(0)[:200], "pattern": pattern})
        if ex.output.get("truncated"):
            # The stored value is a prefix. A pattern that would have matched
            # past the cut is unknowable, and saying "fail" would be a claim we
            # cannot support.
            return Result(WARN, name,
                          "no match in the stored answer, which is truncated — "
                          "a match beyond %d bytes cannot be ruled out"
                          % len(text), {"pattern": pattern, "truncated": True})
        return Result(FAIL, name, "the answer does not match %r" % pattern,
                      {"pattern": pattern, "actual": text[:400]})

    return check


def used_tool(tool_name):
    """The model asked for this tool at least once."""
    name = "used_tool"

    def check(ex):
        hits = ex.calls_named(tool_name)
        if hits:
            return Result(PASS, name,
                          "%s was requested %d time(s)" % (tool_name, len(hits)),
                          {"tool": tool_name, "calls": [_call_ref(c) for c in hits]})
        asked = sorted({c.get("name") for c in ex.tool_calls if c.get("name")})
        if not ex.model_steps:
            return Result(WARN, name,
                          "no model call in this run, so no tool could have "
                          "been requested", {"tool": tool_name})
        if not ex.tool_calls:
            return Result(FAIL, name,
                          "%s was not requested; this run requested no tools at "
                          "all" % tool_name,
                          {"tool": tool_name, "requested": [],
                           "model_steps": [s.get("i") for s in ex.model_steps]})
        return Result(FAIL, name,
                      "%s was not requested; the model asked for %s"
                      % (tool_name, ", ".join(asked)),
                      {"tool": tool_name, "requested": asked,
                       "calls": [_call_ref(c) for c in ex.tool_calls]})

    return check


def did_not_call(tool_name):
    """The model never asked for this tool.

    "Asked for", not "executed". This module can see what the model requested,
    because the request is in the response body it sent back. Whether the agent
    then ran it is a decision made in code we do not observe — a name is not a
    URL, and pretending to link the two would be a guess wearing the clothes of
    evidence.

    For a prohibition that is the safer direction: a model that asked to send
    the email is worth knowing about even if something downstream refused.
    """
    name = "did_not_call"

    def check(ex):
        hits = ex.calls_named(tool_name)
        if not hits:
            return Result(PASS, name, "%s was never requested" % tool_name,
                          {"tool": tool_name,
                           "requested": sorted({c.get("name")
                                                for c in ex.tool_calls
                                                if c.get("name")})})
        return Result(FAIL, name,
                      "%s was requested %d time(s), at step(s) %s"
                      % (tool_name, len(hits),
                         ", ".join(str(c.get("step")) for c in hits)),
                      {"tool": tool_name, "calls": [_call_ref(c) for c in hits]})

    return check


def max_steps(n):
    """The run made at most n HTTP calls. The cheapest loop detector there is."""
    name = "max_steps"

    def check(ex):
        got = len(ex.http)
        dropped = ex.meta.get("dropped", 0)
        if dropped:
            # The ring buffer evicted steps, so the count is a floor, not a
            # total. If the floor already breaks the limit, that is still a
            # fact; if it does not, we genuinely do not know.
            if got > n:
                return Result(FAIL, name,
                              "at least %d calls, over the limit of %d "
                              "(%d more were evicted)" % (got, n, dropped),
                              {"steps": got, "limit": n, "dropped": dropped})
            return Result(WARN, name,
                          "%d calls kept and %d evicted by the ring buffer, so "
                          "the real total is unknown" % (got, dropped),
                          {"steps": got, "limit": n, "dropped": dropped})
        if got <= n:
            return Result(PASS, name, "%d calls, within the limit of %d"
                          % (got, n), {"steps": got, "limit": n})
        return Result(FAIL, name, "%d calls, over the limit of %d" % (got, n),
                      {"steps": got, "limit": n,
                       "urls": [s.get("url") for s in ex.http[:20]]})

    return check


def no_step_failed():
    """Every call came back with a non-error status and no exception."""
    name = "no_step_failed"

    def check(ex):
        bad = ex.failed_steps()
        if not bad:
            return Result(PASS, name, "all %d call(s) succeeded" % len(ex.http),
                          {"steps": len(ex.http)})
        return Result(FAIL, name, "%d of %d call(s) failed" % (len(bad), len(ex.http)),
                      {"failed": [_step_ref(s) for s in bad[:20]],
                       "steps": len(ex.http)})

    return check


def check(fn, name=None):
    """Wrap your own callable.

    It is handed the Execution and may return a Result, a bool, a (bool, reason)
    pair, or a string — a returned string is read as a failure reason, because
    a check that has something to say is saying what is wrong.

    A callable that raises is a failure of the check, not of the run, and says
    so: an evaluator that crashes must not be mistaken for a passing one.
    """
    label = name or getattr(fn, "__name__", None) or "custom"

    def run(ex):
        try:
            out = fn(ex)
        except Exception as e:
            return Result(FAIL, label,
                          "the check raised %s: %s" % (type(e).__name__,
                                                       str(e)[:200]),
                          {"raised": type(e).__name__})
        if isinstance(out, Result):
            return out
        if isinstance(out, tuple) and len(out) == 2:
            ok, reason = out
            return Result(PASS if ok else FAIL, label,
                          str(reason)[:400] or ("passed" if ok else "failed"))
        if isinstance(out, str):
            return Result(FAIL, label, out[:400])
        if out is None:
            return Result(WARN, label, "the check returned None, which is "
                                       "neither a pass nor a failure")
        return Result(PASS if out else FAIL, label,
                      "the check passed" if out else "the check failed")

    return run


# --- declarative form ---------------------------------------------------------
# A case is a file, so what it expects has to be data rather than code. This is
# the whole vocabulary; anything more specific is a `check()` written in the
# caller's own test, which is where code belongs.

def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


SPEC_KEYS = ("output_equals", "output_matches", "used_tool", "did_not_call",
             "max_steps", "no_step_failed")


def from_spec(spec):
    """Turn an `expect` block into evaluators.

    An unknown key raises. A typo in a case file that silently checked nothing
    would be worse than a case that fails to load: the case would go green
    forever and nobody would learn why until it mattered.
    """
    spec = spec or {}
    unknown = [k for k in spec if k not in SPEC_KEYS]
    if unknown:
        raise ValueError(
            "unknown expectation(s): %s. Known: %s"
            % (", ".join(sorted(unknown)), ", ".join(SPEC_KEYS)))

    out = []
    if "output_equals" in spec:
        out.append(output_equals(spec["output_equals"]))
    if "output_matches" in spec:
        out.append(output_matches(spec["output_matches"]))
    for name in _as_list(spec.get("used_tool")):
        out.append(used_tool(name))
    for name in _as_list(spec.get("did_not_call")):
        out.append(did_not_call(name))
    if spec.get("max_steps") is not None:
        out.append(max_steps(int(spec["max_steps"])))
    if spec.get("no_step_failed"):
        out.append(no_step_failed())
    return out


# --- running them -------------------------------------------------------------

class Report:
    """What a set of evaluators found, together."""

    def __init__(self, execution, results):
        self.execution = execution
        self.results = list(results)

    @property
    def failed(self):
        return [r for r in self.results if r.status == FAIL]

    @property
    def warnings(self):
        return [r for r in self.results if r.status == WARN]

    @property
    def passed(self):
        return [r for r in self.results if r.status == PASS]

    @property
    def ok(self):
        """Warnings do not fail a run.

        A warning means we could not answer, and failing a build on a question
        nobody could answer teaches people to ignore the tool.
        """
        return not self.failed

    def as_dict(self):
        return {"run_id": self.execution.meta.get("run_id"),
                "ok": self.ok,
                "results": [r.as_dict() for r in self.results]}

    def as_json(self, indent=2):
        return json.dumps(self.as_dict(), indent=indent, default=str)

    def report(self):
        mark = {PASS: "ok ", FAIL: "!! ", WARN: " ? "}
        lines = ["  %s %-16s %s" % (mark[r.status], r.evaluator, r.reason)
                 for r in self.results]
        head = "%s — %d passed, %d failed, %d unanswered" % (
            self.execution.meta.get("run_id", "run"), len(self.passed),
            len(self.failed), len(self.warnings))
        return "\n".join([head] + lines)

    def __repr__(self):
        return "<Report %s: %d/%d passed>" % (
            "ok" if self.ok else "failed", len(self.passed), len(self.results))


def evaluate(execution, evaluators):
    """Run every evaluator against one execution.

    All of them, always. Stopping at the first failure would hide the second
    one, and when an agent regresses it usually breaks more than one property
    at a time — the shape of the whole set is the diagnosis.
    """
    if isinstance(execution, str):
        execution = Execution.load(execution)
    return Report(execution, [ev(execution) for ev in (evaluators or [])])
