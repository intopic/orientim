# -*- coding: utf-8 -*-
"""Concurrency semantics: what ran beside what, and what that does and does not prove.

The problem this exists for, in one line:

    A:  call research || call risk
    B:  call risk || call research

is a real difference in an agent, and a replay cannot see it. That is not a
defect in replay — replay serves recorded steps *in the recorded order*, which
is what lets an agent with four calls in flight reproduce deterministically at
all. The same mechanism absorbs a reordering: the agent asks in a new order, is
served in the old one, and produces the chain that was recorded.

So this is a **reader**, not a change to replay. Nothing here is consulted when
a replay decides a match, and nothing here can move a verdict. Everything it
needs is already in the recording:

    i     the sequence position, as the steps arrived
    t0    when the call started, seconds from the start of the run
    ms    how long it took

An interval per call, which is all concurrency is. None of those three fields is
in `chain.DIGEST_FIELDS`, and none of them is added by this module.

What is honest to say from an interval, and what is not
------------------------------------------------------

    a.end <= b.start        a happened before b.        PROVABLE
    intervals intersect     a and b overlapped.         PROVABLE
    a started before b      a was issued first.         PROVABLE, and weak

and nothing else. In particular:

    "a caused b"            NOT PROVABLE from timing, ever.

Two calls that overlap have no order worth defending: which one the scheduler
started first is a fact about the machine. Two calls where one provably finished
before the other began *were* sequenced, and a change there is a change in the
agent. The whole point of separating those two is that reporting them the same
way would make one of them noise and the other invisible.
"""
import hashlib


# The relation between two invocations in one run.
BEFORE = "before"          # a ended before b started — provable sequencing
AFTER = "after"
OVERLAP = "overlap"        # the intervals intersect — concurrent
UNKNOWN = "unknown"        # no timing to compare

# What changed between two runs.
PARALLEL_ORDER_CHANGED = "PARALLEL_ORDER_CHANGED"
SEQUENTIAL_ORDER_CHANGED = "SEQUENTIAL_ORDER_CHANGED"
CONCURRENCY_CHANGED = "CONCURRENCY_CHANGED"

CAUSALITY = ("timing establishes precedence, not cause: "
             "no causal claim is made here")


# --- one run ------------------------------------------------------------------

def interval(step):
    """(start, end) in seconds from the start of the run, or None.

    `ms` is the time from the response headers to the last byte, and `t0` is
    when the request went out, so the interval is the whole call as the agent
    experienced it.
    """
    t0 = step.get("t0")
    if t0 is None:
        return None
    ms = step.get("ms") or 0.0
    return (float(t0), float(t0) + float(ms) / 1000.0)


def invocation_id(step, seen=None):
    """A name for this call that survives being moved.

    Position cannot be part of it — the whole question is what happens when
    positions change — so it is the request identity plus an occurrence count
    among calls that share that identity. Two identical calls in one run get
    #0 and #1; if *those two* swap, nothing distinguishes them and nothing
    should, because they are the same call.
    """
    raw = "%s %s %s" % (step.get("method", "?"), step.get("url", ""),
                        step.get("key_loose", ""))
    base = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    if seen is None:
        return base + "#0"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return "%s#%d" % (base, n)


def invocations(steps, meta=None):
    """Every HTTP step as a logical invocation.

    `agent` comes from the recording's declared identity, so an invocation
    already carries who made it. That is the field a fleet view would join on
    later; nothing here does that, and nothing here pretends to.
    """
    agent = ((meta or {}).get("agent") or {}).get("name")
    seen, out = {}, []
    for step in steps:
        if step.get("t") != "http":
            continue
        span = interval(step)
        out.append({
            "id": invocation_id(step, seen),
            "seq": step.get("i"),
            "start": None if span is None else span[0],
            "end": None if span is None else span[1],
            "worker": step.get("worker"),
            "agent": agent,
            "role": step.get("role"),
            "label": "%s %s" % (step.get("method", "?"),
                                (step.get("url") or "").rsplit("/", 1)[-1]),
        })
    return out


def relation(a, b):
    """How two invocations of the same run stand to each other. Provable only."""
    if a.get("start") is None or b.get("start") is None:
        return UNKNOWN
    if a["end"] <= b["start"]:
        return BEFORE
    if b["end"] <= a["start"]:
        return AFTER
    return OVERLAP


def groups(invs):
    """Sets of invocations that were in flight together.

    Connected components of the overlap graph: A overlapping B and B
    overlapping C puts all three in one group even if A and C do not touch,
    which is the right reading — they were part of one burst of concurrency.
    """
    parent = {inv["id"]: inv["id"] for inv in invs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(invs):
        for b in invs[i + 1:]:
            if relation(a, b) == OVERLAP:
                ra, rb = find(a["id"]), find(b["id"])
                if ra != rb:
                    parent[ra] = rb

    out = {}
    for inv in invs:
        out.setdefault(find(inv["id"]), []).append(inv)
    # In start order, and sequential singletons dropped: a group of one is not
    # concurrency, it is a call.
    return [sorted(g, key=lambda x: (x["start"] is None, x["start"], x["seq"]))
            for g in out.values() if len(g) > 1]


def describe(steps, meta=None):
    """The concurrency shape of one run."""
    invs = invocations(steps, meta)
    gs = groups(invs)
    return {
        "invocations": invs,
        "groups": [[x["id"] for x in g] for g in gs],
        "group_labels": [[x["label"] for x in g] for g in gs],
        "concurrent": sum(len(g) for g in gs),
        "sequential": len(invs) - sum(len(g) for g in gs),
    }


# --- two runs -----------------------------------------------------------------

def compare(steps_a, steps_b, meta_a=None, meta_b=None):
    """What changed about *when* things ran, between two runs of one agent.

    Only invocations present in both are considered. Calls that appeared or
    disappeared are a different question and the alignment in `diff` already
    answers it; repeating it here would report one change twice.
    """
    a = {x["id"]: x for x in invocations(steps_a, meta_a)}
    b = {x["id"]: x for x in invocations(steps_b, meta_b)}
    shared = [i for i in a if i in b]

    findings = []
    for idx, ia in enumerate(shared):
        for ib in shared[idx + 1:]:
            ra = relation(a[ia], a[ib])
            rb = relation(b[ia], b[ib])
            if ra == UNKNOWN or rb == UNKNOWN:
                continue
            if ra == rb == OVERLAP:
                # Both runs ran them together. Did the start order flip?
                first_a = a[ia]["start"] <= a[ib]["start"]
                first_b = b[ia]["start"] <= b[ib]["start"]
                if first_a != first_b:
                    findings.append(_finding(
                        PARALLEL_ORDER_CHANGED, a, b, ia, ib,
                        "these two ran concurrently in both runs and started "
                        "in the other order",
                        # Deliberately weak. Which of two overlapping calls the
                        # scheduler started first is a fact about the machine
                        # unless the agent staggered them on purpose, and
                        # nothing in a recording says which of those it was.
                        strength="weak"))
            elif ra == rb:
                continue
            elif OVERLAP in (ra, rb):
                findings.append(_finding(
                    CONCURRENCY_CHANGED, a, b, ia, ib,
                    "they overlapped in one run and were sequential in the "
                    "other", strength="strong"))
            else:
                findings.append(_finding(
                    SEQUENTIAL_ORDER_CHANGED, a, b, ia, ib,
                    "one provably finished before the other began, and the "
                    "order is now reversed", strength="strong"))

    return {
        "findings": findings,
        "a": describe(steps_a, meta_a),
        "b": describe(steps_b, meta_b),
        "note": CAUSALITY,
    }


def _finding(kind, a, b, ia, ib, why, strength):
    return {
        "kind": kind,
        "strength": strength,
        "why": why,
        "note": CAUSALITY,
        "pair": [ia, ib],
        "labels": [a[ia]["label"], a[ib]["label"]],
        "was": {"relation": relation(a[ia], a[ib]),
                "order": [a[ia]["label"], a[ib]["label"]]
                if a[ia]["start"] <= a[ib]["start"]
                else [a[ib]["label"], a[ia]["label"]]},
        "now": {"relation": relation(b[ia], b[ib]),
                "order": [b[ia]["label"], b[ib]["label"]]
                if b[ia]["start"] <= b[ib]["start"]
                else [b[ib]["label"], b[ia]["label"]]},
        "agents": sorted({a[ia].get("agent"), a[ib].get("agent")} - {None}),
    }


# --- policy -------------------------------------------------------------------

def policy(comparison, parallel_order_matters=False,
           sequential_order_matters=True, concurrency_matters=True):
    """Decide whether a concurrency change is a failure. Not a verdict.

    The defaults say what the evidence supports. A flipped start order between
    two calls that overlap is scheduling until somebody says otherwise, so it
    is reported and does not fail. A reversal of two calls where one provably
    finished before the other began is a change in the agent, so it does.

    An agent that staggers its work deliberately — as the lab's supervisor does
    — can set parallel_order_matters=True and get the stricter reading. Nothing
    can decide that from the recording, which is exactly why it is a setting
    and not an inference.

    This is a policy over a comparison. It never reaches a replay verdict.
    """
    gate = {
        PARALLEL_ORDER_CHANGED: parallel_order_matters,
        SEQUENTIAL_ORDER_CHANGED: sequential_order_matters,
        CONCURRENCY_CHANGED: concurrency_matters,
    }
    failed = [f for f in comparison["findings"] if gate.get(f["kind"])]
    reported = [f for f in comparison["findings"] if not gate.get(f["kind"])]
    return {
        "ok": not failed,
        "failed": failed,
        "reported": reported,
        "settings": {"parallel_order_matters": parallel_order_matters,
                     "sequential_order_matters": sequential_order_matters,
                     "concurrency_matters": concurrency_matters},
    }


def report(comparison, decision=None, width=74):
    """The human half."""
    findings = comparison["findings"]
    L = ["  CONCURRENCY"]
    L.append("    %d group(s) then, %d now"
             % (len(comparison["a"]["groups"]), len(comparison["b"]["groups"])))
    if not findings:
        L.append("    Nothing moved: the same calls ran together, in the same "
                 "order.")
        L.append("")
        return "\n".join(L)

    for f in findings:
        mark = "!!" if decision and f in decision["failed"] else " ?"
        L.append("    %s %s (%s)" % (mark, f["kind"], f["strength"]))
        L.append("       %s" % f["why"])
        L.append("       was: %s" % " then ".join(f["was"]["order"]))
        L.append("       now: %s" % " then ".join(f["now"]["order"]))
    L.append("")
    L.append("    %s." % CAUSALITY)
    if decision:
        L.append("    policy: %s"
                 % ", ".join("%s=%s" % kv
                             for kv in sorted(decision["settings"].items())))
    L.append("")
    return "\n".join(L)
