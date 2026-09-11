# -*- coding: utf-8 -*-
"""Who a replay is, and whether a recorded response may be released to it.

A replay matches a request against the recorded queue and hands back what came
back last time. Matching is a lookup; releasing is a decision, and until now
there was only the lookup. Measured, that left three tiers:

    the request body        refused before anything was served
    a caller-context header served, consumed, acted on, reported afterwards
    a credential            served, consumed, never reported

The middle row is what this module exists for. An agent replayed as one tenant
was handed another tenant's recorded response, parsed it, and branched on it,
and a verdict appeared only after the run had finished. A verdict that names a
mismatch and a decision that refuses to serve one are not the same event, and
the difference is whether the agent already acted on somebody else's data.

**The invariant.** No fixture is released until every mandatory obligation of
the active replay contract has been evaluated. Only an obligation-complete
contract releases one.

**What this is not.** Not a policy engine, not an identity provider, not a
question about the live world. A contract is a handful of named relations and
the answer is local: these two runs agree about the things this contract says
matter. `ELIGIBLE_UNDER_CONTRACT` never means *the live system would answer
the same now* — that claim is not available from a recording, and reading it
that way is the mistake this vocabulary exists to prevent.

**Legacy is not eligible; it is unmediated.** A contract that declares no
obligations has not established anything, and a replay that runs under one has
not passed a check — it has not taken one. Those are different facts and they
get different words, because the first thing a report or a UI will want to do
with a green legacy run is call it safe.

**What proves a relation.** Today, exactly one thing: the application said so,
on both sides, and the recording kept the statement outside the hash chain.
That is `declared_unchained`, and the name is the whole disclosure. It is
evidence against a misconfigured replay — the suite re-run for another tenant
that quietly gets the first tenant's fixtures — and it is not evidence against
an edited recording, because nothing here is chained. See docs/limits.md.

Three things are deliberately absent in this version, each because the honest
version of it needs something that does not exist yet:

    credential relations   would need a keyed local commitment. A redacted
                           credential is worse than none: two different
                           secrets can collapse to one stored value and then
                           compare *equal*.
    per-request context    a run-level statement cannot speak for a run whose
                           requests had different callers, so a recording that
                           ran on several workers is answered UNKNOWN.
    semantic normalization exact strings only. "ACME" is not "acme" until
                           somebody decides whose casing rules apply.
"""
import re

RELATIONS = ("tenant", "subject", "actor", "session", "workload")

# Named, known, and refused. Leaving it out of RELATIONS entirely would answer
# a developer who reaches for it with "no such relation", which is true and
# unhelpful; this says why, and the why is the design.
UNSUPPORTED = {
    "credential":
        "a credential relation needs a privacy-preserving evidence source — a "
        "keyed local commitment — and has none here yet. Storing the value "
        "redacted would be worse than storing nothing: two different secrets "
        "can redact to the same string and then compare equal, which is a "
        "false ELIGIBLE manufactured by the privacy measure itself.",
}

# How a relation's value is known. One kind, and its name says both halves of
# what is weak about it: declared by the application rather than observed on
# the wire, and stored outside the chain that protects the steps.
DECLARED_UNCHAINED = "declared_unchained"

# The contract that asks for nothing: what every replay has always run under.
LEGACY = "legacy"

ELIGIBLE = "ELIGIBLE_UNDER_CONTRACT"
INELIGIBLE = "INELIGIBLE"
UNKNOWN = "UNKNOWN"
ANALYSIS_ERROR = "ANALYSIS_ERROR"
LEGACY_UNMEDIATED = "LEGACY_UNMEDIATED"

# Values that must not be written into a recording. The env-var rule, applied
# to a context: an ambiguous case errs towards refusing, because a rejected
# tenant name is a rename away and a recorded key cannot be un-shared.
_SECRETISH = re.compile(
    r"(?i)(bearer\s|basic\s|^sk-|^ghp_|^xox[abprs]-|^ey[A-Za-z0-9_-]{10,}\.|"
    r"secret|password|passwd|private[_-]?key|api[_-]?key|token|credential)")


class ContextError(ValueError):
    """A context or contract that cannot mean anything.

    Raised at the call site. Never turned into a verdict about the caller: a
    misconfigured contract is a fact about the configuration, and answering it
    with INELIGIBLE would blame the wrong thing.
    """


def normalize(context):
    """A user's dict into the stored shape, or None.

    Refuses rather than redacts. A context is a set of names an operator can
    read in a report — a tenant, a session id, a service account — and nothing
    in it should ever need hiding. Redacting a value that did need hiding
    would leave two different secrets stored as one string and comparing
    equal, so a value that looks like a credential fails here instead.
    """
    if not context:
        return None
    if not isinstance(context, dict):
        raise ContextError("a replay context is a dict of relation -> value, "
                           "not %s" % type(context).__name__)
    for name in sorted(context):
        if name in UNSUPPORTED:
            raise ContextError("the %s relation is not supported yet: %s"
                               % (name, UNSUPPORTED[name]))
        if name not in RELATIONS:
            raise ContextError(
                "no such replay relation: %s. Known relations are %s"
                % (name, ", ".join(RELATIONS)))
    out = {}
    for name, value in context.items():
        if value is None:
            continue
        text = str(value)
        if _SECRETISH.search(text):
            raise ContextError(
                "the value given for %s looks like a credential, and a replay "
                "context is not a place for one. Use a name or an opaque "
                "local id that identifies the caller without being usable as "
                "it." % name)
        if len(text) > 200:
            raise ContextError("the value given for %s is longer than 200 "
                               "characters; a context holds identifiers, not "
                               "payloads" % name)
        out[name] = {"value": text, "evidence": DECLARED_UNCHAINED}
    return out or None


def obligations(contract):
    """The relations a contract makes mandatory.

    `legacy` makes none. That is not a contract that everything satisfies; it
    is the absence of one, and `mediate` says so in its own word.
    """
    if contract in (None, LEGACY):
        return ()
    if isinstance(contract, str):
        raise ContextError(
            "no such replay contract: %r. Pass %r, or a sequence of "
            "relations from %s" % (contract, LEGACY, ", ".join(RELATIONS)))
    try:
        return tuple(contract)
    except TypeError:
        raise ContextError("a contract is %r or a sequence of relations"
                           % LEGACY) from None


def _value(context, name):
    entry = (context or {}).get(name)
    if isinstance(entry, dict):
        return entry.get("value")
    return entry if isinstance(entry, str) else None


def mediate(contract, recorded, current, workers=1):
    """(verdict, reasons) — may a fixture be released to this replay?

    Every mandatory obligation is evaluated, not only up to the first
    complaint: a run with two context problems should be told about two. The
    ways not to release are kept apart because they ask different things of
    the reader — a contradiction is a fact about the two runs, a missing value
    is a fact about the evidence, and a broken contract is a fact about the
    configuration and not about the caller at all.

        both present and equal        the obligation is satisfied
        both present and different    INELIGIBLE
        either side missing           UNKNOWN
        an unusable contract          ANALYSIS_ERROR

    Equality is exact. "ACME" is not "acme" here, and it will not be until
    somebody decides whose casing and whose aliases apply — a normalisation
    rule invented in this function would be a policy nobody declared.

    `workers` is how many threads the recorded run used. A context stated once
    for a whole run cannot speak for a run whose requests had different
    callers, so a concurrent recording is answered UNKNOWN rather than
    certified by a statement that was never per-request.
    """
    try:
        names = obligations(contract)
    except ContextError as e:
        return ANALYSIS_ERROR, [str(e)]
    if not names:
        return LEGACY_UNMEDIATED, ["no contract: nothing was evaluated, and "
                                   "nothing is established"]

    bad = sorted(set(n for n in names if n not in RELATIONS))
    if bad:
        unsupported = [n for n in bad if n in UNSUPPORTED]
        if unsupported:
            return ANALYSIS_ERROR, [
                "%s: %s" % (n, UNSUPPORTED[n]) for n in unsupported]
        return ANALYSIS_ERROR, ["no such replay relation: " + ", ".join(bad)]

    broken, missing = [], []
    for name in names:
        was, now = _value(recorded, name), _value(current, name)
        if was is None or now is None:
            missing.append("the recording carries no %s" % name if was is None
                           else "this replay declares no %s" % name)
        elif was != now:
            broken.append("%s: recorded %s, replaying as %s" % (name, was, now))

    if broken:
        return INELIGIBLE, broken + missing
    if missing:
        return UNKNOWN, missing
    if (workers or 1) > 1:
        # Established for the run as a whole, and the run was not one thing.
        return UNKNOWN, [
            "this recording ran on %d workers, and a context stated once for "
            "a whole run cannot speak for requests that may have had "
            "different callers" % workers]
    # The strength travels with the verdict. Anything that prints a reason
    # then carries what the answer rests on, and nobody has to go and look up
    # what `declared_unchained` was short for.
    return ELIGIBLE, ["every obligation discharged from %s evidence: %s"
                      % (DECLARED_UNCHAINED, ", ".join(names))]


def describe(verdict, reasons):
    """One line, for a refusal the agent is about to be told about."""
    return "%s (%s)" % (verdict, "; ".join(reasons) if reasons else "no detail")
