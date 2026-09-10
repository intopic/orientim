# -*- coding: utf-8 -*-
"""Concurrency semantics: seeing A||B become B||A without touching replay.

The four things this has to demonstrate, and each has a check of its own:

  1. replay determinism is unchanged — the verdict is what it always was
  2. the diff can see the reordering
  3. a policy decides whether that ordering matters
  4. nothing claims a cause

The agent runs two children concurrently through a real thread pool against the
lab server, and the two versions differ only in which one is started first.
"""
import concurrent.futures as cf
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import concurrency as K
from orientim import diff, store

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/concurrency"

# How far ahead the first of the pair starts. Long enough that the order is the
# code's decision and not the scheduler's; short enough that they still overlap,
# which is the case under test.
STAGGER = 0.12

ORDER_ENV = "ORIENTIM_CONC_ORDER"


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


SAME_URL_ENV = "ORIENTIM_CONC_SAME_URL"


def _slow(client, name):
    """A call long enough to overlap with its sibling, and the same length
    every time.

    /slow sleeps a fixed 0.4s. The first draft used /chat-stable, which sleeps
    a random 0.18-0.55s, and the tests built on it were a coin toss dressed as
    assertions.
    """
    if os.environ.get(SAME_URL_ENV) == "1":
        # Both children on one endpoint, told apart only by their body. This
        # is the harder shape: the alignment pairs them by method and URL, so a
        # swap reads as changed steps rather than as a move.
        return client.post(B + "/slow",
                           content=json.dumps({"child": name}).encode())
    return client.post(B + "/slow?child=" + name,
                       content=json.dumps({"child": name}).encode())


def agent(run):
    """Research || Risk, then Support. The pair's start order is the variable."""
    c = run.client()
    pair = ["research", "risk"]
    if os.environ.get(ORDER_ENV) == "reversed":
        pair = list(reversed(pair))

    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_slow, c, pair[0])
        time.sleep(STAGGER)
        second = pool.submit(_slow, c, pair[1])
        first.result()
        second.result()

    _slow(c, "support")
    run.output = "handled"
    return run.output


def _record(order, same_url=False):
    before = {k: os.environ.get(k) for k in (ORDER_ENV, SAME_URL_ENV)}
    os.environ[ORDER_ENV] = order
    os.environ[SAME_URL_ENV] = "1" if same_url else "0"
    try:
        with orientim.record(root=ROOT, always=True,
                             agent={"name": "supervisor"}) as h:
            agent(h)
        return h.path
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _pair(same_url=False):
    _fresh()
    return (_record("normal", same_url), _record("reversed", same_url))


# --- 1. replay is untouched ---------------------------------------------------

def t_replay_determinism_is_unchanged():
    """The verdict a replay reaches must be exactly what it was before.

    Both directions: the recording replays IDENTICAL against the code that
    made it, and — the important half — it *also* replays IDENTICAL against
    the reordered code, because replay serves recorded steps in the recorded
    order. That is the promise this work must not break, so it is asserted
    rather than assumed.
    """
    a, _b = _pair()
    same = orientim.replay(a, agent)

    before = os.environ.get(ORDER_ENV)
    os.environ[ORDER_ENV] = "reversed"
    try:
        reordered = orientim.replay(a, agent)
    finally:
        if before is None:
            os.environ.pop(ORDER_ENV, None)
        else:
            os.environ[ORDER_ENV] = before

    return (same.ok and reordered.ok), \
        "same code %s, reordered code %s" % (same.diagnosis[0],
                                             reordered.diagnosis[0])


def t_no_concurrency_field_is_in_the_digest():
    """The structural guarantee, checked rather than trusted."""
    from orientim import chain
    added = {"t0", "ms", "worker", "i"}
    return not (added & set(chain.DIGEST_FIELDS)), \
        "digest fields %r" % (chain.DIGEST_FIELDS,)


# --- 2. the diff can see it ---------------------------------------------------

def t_the_diff_sees_the_reordering():
    a, b = _pair()
    cmp_ = diff.compare(a, b)
    conc = cmp_["concurrency"]
    kinds = [f["kind"] for f in conc["findings"]]
    return (K.PARALLEL_ORDER_CHANGED in kinds), \
        "findings %r, groups %r -> %r" % (kinds, conc["a"]["group_labels"],
                                          conc["b"]["group_labels"])


def t_the_overlapping_pair_is_one_group():
    a, _b = _pair()
    meta, steps = store.load(a)
    shape = K.describe(steps, meta)
    return (len(shape["groups"]) == 1 and len(shape["groups"][0]) == 2
            and shape["sequential"] >= 1), \
        "groups %r, sequential %d" % (shape["group_labels"],
                                      shape["sequential"])


def t_distinguishable_calls_reorder_at_both_levels():
    """When the two concurrent calls have different URLs, both views see it.

    The alignment reports one REORDERED — it already did that before any of
    this — and the concurrency view adds what the alignment cannot know: they
    *overlapped*, so the order is scheduling rather than sequencing, and the
    finding is weak.
    """
    a, b = _pair(same_url=False)
    cmp_ = diff.compare(a, b)
    c = cmp_["counts"]
    findings = cmp_["concurrency"]["findings"]
    return (c["REORDERED"] == 1 and c["INSERTED"] == 0 and c["DELETED"] == 0
            and [f["kind"] for f in findings] == [K.PARALLEL_ORDER_CHANGED]
            and findings[0]["strength"] == "weak"), \
        "step counts %r, concurrency %r" % (c, [f["kind"] for f in findings])


def t_indistinguishable_urls_are_where_it_earns_its_keep():
    """Two concurrent calls to one endpoint, told apart only by their body.

    The alignment pairs them by method and URL, so a swap reads as two changed
    steps — noise that grows with the size of the parallel group and never
    names what happened. The concurrency view reports the same event as one
    reordering, because its invocation id includes the request key.
    """
    a, b = _pair(same_url=True)
    cmp_ = diff.compare(a, b)
    c = cmp_["counts"]
    findings = cmp_["concurrency"]["findings"]
    return (c["CHANGED"] == 2 and c["REORDERED"] == 0
            and len(findings) == 1
            and findings[0]["kind"] == K.PARALLEL_ORDER_CHANGED), \
        "the byte view says %r; concurrency says %r" % (
            c, [f["kind"] for f in findings])


# --- 3. a policy decides ------------------------------------------------------

def t_policy_decides_whether_order_matters():
    a, b = _pair()
    conc = K.compare(*[store.load(p)[1] for p in (a, b)])
    lenient = K.policy(conc)                                  # the default
    strict = K.policy(conc, parallel_order_matters=True)
    return (lenient["ok"] and not strict["ok"]
            and lenient["reported"] and strict["failed"]), \
        "default ok=%s, strict ok=%s" % (lenient["ok"], strict["ok"])


def t_a_weak_finding_does_not_fail_a_diff_by_default():
    """`identical` is about the bytes. Scheduling is reported beside it."""
    a, b = _pair(same_url=False)
    cmp_ = diff.compare(a, b)
    text = diff.report(cmp_)
    # REORDERED is a step-level fact and it does make the runs non-identical.
    # What is being checked here is narrower: the *concurrency* finding is weak,
    # so the policy passes and the report still says what it saw.
    return (cmp_["concurrency_policy"]["ok"]
            and cmp_["concurrency"]["findings"] and "CONCURRENCY" in text), \
        "policy ok=%s with %d finding(s)" % (
            cmp_["concurrency_policy"]["ok"],
            len(cmp_["concurrency"]["findings"]))


def t_a_provable_sequencing_change_is_strong():
    """Two calls that never overlapped, swapped, is a different claim.

    Built from intervals rather than recorded, because producing a guaranteed
    non-overlap from real threads would be a race dressed as a test.
    """
    def s(i, url, t0, ms):
        return {"t": "http", "i": i, "method": "POST", "url": url,
                "key_loose": url, "t0": t0, "ms": ms}

    first = [s(0, "/a", 0.0, 100), s(1, "/b", 0.5, 100)]
    second = [s(0, "/b", 0.0, 100), s(1, "/a", 0.5, 100)]
    conc = K.compare(first, second)
    kinds = [f["kind"] for f in conc["findings"]]
    strengths = {f["strength"] for f in conc["findings"]}
    return (kinds == [K.SEQUENTIAL_ORDER_CHANGED] and strengths == {"strong"}
            and not K.policy(conc)["ok"]), \
        "findings %r %r" % (kinds, strengths)


def t_concurrency_change_is_its_own_finding():
    """Overlapping in one run and sequential in the other is neither of the
    other two, and is strong: the agent stopped doing two things at once."""
    def s(i, url, t0, ms):
        return {"t": "http", "i": i, "method": "POST", "url": url,
                "key_loose": url, "t0": t0, "ms": ms}

    together = [s(0, "/a", 0.0, 400), s(1, "/b", 0.1, 400)]
    apart = [s(0, "/a", 0.0, 100), s(1, "/b", 0.5, 100)]
    kinds = [f["kind"] for f in K.compare(together, apart)["findings"]]
    return kinds == [K.CONCURRENCY_CHANGED], "findings %r" % (kinds,)


# --- 4. no false causal claims ------------------------------------------------

def t_nothing_claims_a_cause():
    a, b = _pair()
    cmp_ = diff.compare(a, b)
    blob = json.dumps(cmp_["concurrency"], default=str).lower()
    text = diff.report(cmp_).lower()
    forbidden = [w for w in ("root cause", "caused by", "because of")
                 if w in blob or w in text]
    every_finding_says_so = all(
        K.CAUSALITY in f["note"] for f in cmp_["concurrency"]["findings"])
    return (not forbidden and every_finding_says_so
            and K.CAUSALITY in cmp_["concurrency"]["note"]), \
        "forbidden %r, every finding carries the note=%s" % (
            forbidden, every_finding_says_so)


def t_timing_that_proves_nothing_says_nothing():
    """Without t0 there is no interval, so there is no relation to report."""
    def s(i, url):
        return {"t": "http", "i": i, "method": "POST", "url": url,
                "key_loose": url}

    conc = K.compare([s(0, "/a"), s(1, "/b")], [s(0, "/b"), s(1, "/a")])
    return conc["findings"] == [], "findings %r" % (conc["findings"],)


def t_identical_calls_are_not_told_apart():
    """Two indistinguishable calls that swap are indistinguishable.

    They share a request identity, so #0 and #1 are assigned by position and a
    swap of the two is invisible. That is the correct answer — nothing in the
    recording separates them — and it is asserted so nobody later mistakes the
    silence for a bug.
    """
    def s(i, t0):
        return {"t": "http", "i": i, "method": "POST", "url": "/same",
                "key_loose": "/same", "t0": t0, "ms": 100}

    conc = K.compare([s(0, 0.0), s(1, 0.05)], [s(0, 0.0), s(1, 0.05)])
    return conc["findings"] == [], "findings %r" % (conc["findings"],)


# --- the run-level shape ------------------------------------------------------

def t_worker_index_is_recorded_and_per_run():
    """Steps carry which worker issued them, numbered within the run."""
    a, _b = _pair()
    _meta, steps = store.load(a)
    workers = {s.get("worker") for s in steps if s.get("t") == "http"}
    return (len(workers) >= 2 and None not in workers
            and max(workers) < 8), "workers seen %r" % (sorted(workers),)
