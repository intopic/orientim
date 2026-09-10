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

Everything here is derived from fields the trace already carries: `unmatched`,
`error`, `status`, `role`, `outcome`, and the ring buffer's `dropped` count.
Nothing new is captured, and nothing here parses a URL, matches a request or
touches the hash chain.

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
OUTPUT = "final_output"             # the answer the run declared
TIMING = "timing"                   # when each step ran, and for how long

DOMAINS = (EMITTED, RESPONSES, MODEL_RESPONSES, OUTPUT, TIMING)

_LABEL = {
    EMITTED: "the requests the agent sent",
    RESPONSES: "the responses it received",
    MODEL_RESPONSES: "the model's responses",
    OUTPUT: "the final answer",
    TIMING: "step timing",
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

        out = {
            EMITTED: list(evicted),
            RESPONSES: evicted + unanswered,
            MODEL_RESPONSES: evicted + model_unanswered,
            TIMING: evicted + untimed,
            OUTPUT: [],
        }
        outcome = self.meta.get("outcome")
        if not outcome:
            out[OUTPUT] = ["not declared"]
        elif isinstance(outcome, dict) and outcome.get("kind") == "unavailable":
            out[OUTPUT] = ["capture failed"]
        return out

    @staticmethod
    def _at(step):
        i = step.get("i")
        return i if i is not None else step.get("order", "?")

    # --- reading it -----------------------------------------------------------

    def complete(self, domain):
        """Is this domain fully observed?

        An unknown domain name is complete rather than an error: a custom check
        naming a domain this version does not know about should not be turned
        into a failure by that alone.
        """
        return not self._gaps.get(domain)

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
        gaps = self.gaps(domain)
        if not gaps:
            return "%s were fully observed" % _LABEL.get(domain, domain)
        if domain == OUTPUT:
            return ("the run did not declare a final answer"
                    if gaps == ["not declared"]
                    else "the final answer could not be captured")
        evicted = [g for g in gaps if isinstance(g, str)]
        steps = [g for g in gaps if not isinstance(g, str)]
        parts = []
        if steps:
            shown = ", ".join(str(s) for s in steps[:8])
            parts.append("%d step(s) went unanswered (at %s%s)"
                         % (len(steps), shown,
                            ", …" if len(steps) > 8 else ""))
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
