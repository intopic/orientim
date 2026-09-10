# -*- coding: utf-8 -*-
"""What a trace lets you conclude, and what it does not.

An evaluator asks a question of an execution. Whether the execution can answer
it is a separate question, and until now it was answered case by case inside
each evaluator — well in two of them, not at all in two others.

The defect that made this a module: `did_not_call("refund.issue")` returned
PASS on a replay whose model call was never answered. No tool call was
observed, so none was found, so the prohibition "held". It did not hold; it was
unobservable. A run in which the agent asked for a refund and a run in which it
did not are indistinguishable from a trace with no model response in it, and
reporting the safe one is a coin toss dressed as a verdict.

**Completeness is relative to the question.** There is no single "this run was
complete" flag, because the same trace is complete for one property and empty
for another. A replay that diverged at its first request still observed every
request the agent *emitted* — `note_miss` writes them down — so a question
about what the agent sent is answerable, while a question about what the model
replied is not. One enum cannot express that; a map from domain to completeness
can.

**A response arriving is not a response being legible.** Those are two facts
and they fail separately, which an independent audit found the hard way: an
HTTP 200 in an envelope the extractor does not recognise yields no tool calls,
and "no tool calls came out" was being read as "the model asked for none". So
`served_responses` is the transport fact and `model_tool_calls` the
interpretation one, and a prohibition needs both.

Everything here is derived from fields the trace already carries: `unmatched`,
`error`, `status`, `role`, `outcome`, the stored response body, and the ring
buffer's `dropped` count. Nothing new is captured, and nothing here parses a
URL, matches a request or touches the hash chain.

The four verdicts this licenses are kept apart deliberately:

    FAIL     a violation was observed. Always admissible: the trace never
             invents a step, so anything in it really happened.
    PASS     the property holds *and* the domain it reads was complete.
    UNKNOWN  the observation does not decide it. A fact about the observer.
    VACUOUS  the property held but nothing exercised it. A fact about the
             trace and the specification, and not implemented here — it
             applies to implication-shaped rules, which Orientim has none of.
"""

from . import model

# The domains a question can be about. A trace is complete or incomplete for
# each of them separately.
EMITTED = "emitted_requests"        # what the agent sent
RESPONSES = "served_responses"      # what came back, for every request
MODEL_RESPONSES = "model_responses"  # what came back, for model calls only
TOOL_VIEW = "model_tool_calls"      # what the model asked the agent to do
OUTPUT = "final_output"             # the answer the run declared
TIMING = "timing"                   # when each step ran, and for how long

DOMAINS = (EMITTED, RESPONSES, MODEL_RESPONSES, TOOL_VIEW, OUTPUT, TIMING)

_LABEL = {
    EMITTED: "the requests the agent sent",
    RESPONSES: "the responses it received",
    MODEL_RESPONSES: "the model's responses",
    TOOL_VIEW: "the tools the model asked for",
    OUTPUT: "the final answer",
    TIMING: "step timing",
}

# How a gap in each domain reads in a sentence. A response that never arrived
# and a response that arrived unreadable are different failures and a shared
# phrase for both would hide which one happened.
_GAP_VERB = {
    EMITTED: "are missing",
    RESPONSES: "went unanswered",
    MODEL_RESPONSES: "went unanswered",
    TOOL_VIEW: "could not be read",
    TIMING: "carry no timing",
}


def answered(step):
    """Did this step come back with a response at all?

    Three ways it does not, and they are not the same thing:

    * `unmatched` — a replay had no recorded step for this request and served a
      synthetic 599. Nothing was learned about what would have come back.
    * `error` — the request raised instead of answering.
    * `status == 0` — what the recorder writes when there was no HTTP response.

    A 4xx or 5xx *is* an answer. The server said no, and that is an observation
    like any other. Only the absence of a response is an absence of evidence.
    """
    s = step or {}
    if s.get("unmatched") or s.get("error"):
        return False
    return (s.get("status") or 0) != 0


class Observation:
    """A trace, and what it is complete for.

    Built from the same `(meta, steps)` pair an Execution is built from, so it
    costs one pass over the steps and no new data.
    """

    __slots__ = ("meta", "steps", "http", "_gaps")

    def __init__(self, meta, steps):
        self.meta = meta or {}
        self.steps = list(steps or [])
        self.http = [s for s in self.steps if s.get("t") == "http"]
        self._gaps = self._compute()

    # --- computing it ---------------------------------------------------------

    def _compute(self):
        """Where each domain has a hole, as a list of step indices.

        The ring buffer is the one gap that is not attributable to a step: when
        it evicts, the requests it dropped are gone entirely, so *every* domain
        derived from steps is incomplete and none of them can say where.
        """
        dropped = int(self.meta.get("dropped") or 0)
        evicted = ["%d evicted" % dropped] if dropped else []

        unanswered = [self._at(s) for s in self.http if not answered(s)]
        model_unanswered = [self._at(s) for s in self.http
                            if s.get("role") == model.MODEL
                            and not answered(s)]
        untimed = [self._at(s) for s in self.http
                   if s.get("t0") is None or s.get("ms") is None]
        # From the extraction itself, not from a second look at the body. A
        # step whose response never arrived is already a gap in
        # MODEL_RESPONSES, so it is not counted twice here; everything else —
        # an unsupported envelope, a bound that was reached, a stream that
        # never closed, a step we could not place — comes back from the one
        # parse that also produced the calls.
        unreadable = [self._at(s) for s in self.http
                      if not (s.get("role") == model.MODEL and not answered(s))
                      and not model.tool_evidence(s)["complete"]]

        out = {
            EMITTED: list(evicted),
            RESPONSES: evicted + unanswered,
            MODEL_RESPONSES: evicted + model_unanswered,
            TOOL_VIEW: evicted + model_unanswered + unreadable,
            TIMING: evicted + untimed,
            OUTPUT: [],
        }
        outcome = self.meta.get("outcome")
        if not outcome:
            out[OUTPUT] = ["not declared"]
        elif not isinstance(outcome, dict):
            pass
        elif outcome.get("kind") == "unavailable":
            out[OUTPUT] = ["capture failed"]
        elif outcome.get("truncated"):
            # The stored answer is a prefix of the real one. `output_equals`
            # is unaffected — it compares a digest taken over the whole value
            # before the cut — but anything reading the stored text is reading
            # part of an answer, and the cut is an artificial end of string.
            out[OUTPUT] = ["truncated"]
        return out

    @staticmethod
    def _at(step):
        i = step.get("i")
        return i if i is not None else step.get("order", "?")

    # --- reading it -----------------------------------------------------------

    def complete(self, domain):
        """Is this domain fully observed?

        A name this version does not know is **not** complete. It used to be —
        an unknown domain had no gaps, and no gaps meant complete — so a typo
        in a declaration bought silence instead of protection, which is the
        one thing this module exists to prevent. Nothing can be established
        about a question nobody here understands.
        """
        if domain not in self._gaps:
            return False
        return not self._gaps[domain]

    def gaps(self, domain):
        """Where the holes are, for the evidence block of a result."""
        return list(self._gaps.get(domain) or [])

    def incomplete(self, domains):
        """The first domain in `domains` that is not complete, or None."""
        for d in domains or ():
            if not self.complete(d):
                return d
        return None

    def why(self, domain):
        """One sentence a person can act on, naming the domain and the gap."""
        if domain not in self._gaps:
            return ("%r is not an observation domain this version knows about"
                    % (domain,))
        gaps = self.gaps(domain)
        if not gaps:
            return "%s were fully observed" % _LABEL.get(domain, domain)
        if domain == OUTPUT:
            return {
                "not declared": "the run did not declare a final answer",
                "capture failed": "the final answer could not be captured",
                "truncated": "the final answer was stored as a prefix and the "
                             "rest of it is not in the trace",
            }.get(gaps[0], "the final answer is incomplete")
        evicted = [g for g in gaps if isinstance(g, str)]
        steps = [g for g in gaps if not isinstance(g, str)]
        parts = []
        if steps:
            shown = ", ".join(str(s) for s in steps[:8])
            parts.append("%d step(s) %s (at %s%s)"
                         % (len(steps), _GAP_VERB.get(domain, "are missing"),
                            shown, ", …" if len(steps) > 8 else ""))
        if evicted:
            parts.append("the ring buffer %s step(s)" % evicted[0])
        return "%s are incomplete: %s" % (_LABEL.get(domain, domain),
                                          " and ".join(parts))

    def as_dict(self):
        return {"complete": [d for d in DOMAINS if self.complete(d)],
                "incomplete": {d: self.gaps(d) for d in DOMAINS
                               if not self.complete(d)}}

    def __repr__(self):
        missing = [d for d in DOMAINS if not self.complete(d)]
        return "<Observation %s>" % ("complete" if not missing
                                     else "incomplete for " + ", ".join(missing))
