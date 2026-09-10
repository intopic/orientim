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


# --- the shapes ---------------------------------------------------------------
# Each of these is a shape the documentation claims is supported, so each gets a
# check that would fail if it were not.

def t_single_agent_execution_is_all_sequential():
    """One agent, one call at a time: no groups, and nothing to report.

    The baseline case, and the one most users have. A concurrency reader that
    invented structure here would make every ordinary run noisy.
    """
    _fresh()

    def sequential(run):
        c = run.client()
        for name in ("a", "b", "c"):
            c.post(B + "/slow?step=" + name, content=b"{}")
        run.output = "done"

    with orientim.record(root=ROOT, always=True, agent={"name": "solo"}) as h:
        sequential(h)
    meta, steps = store.load(h.path)
    shape = K.describe(steps, meta)
    d = orientim.replay(h.path, sequential)
    return (shape["groups"] == [] and shape["concurrent"] == 0
            and shape["sequential"] == 3 and d.ok), \
        "%d group(s), %d sequential, replay %s" % (
            len(shape["groups"]), shape["sequential"], d.diagnosis[0])


def t_three_parallel_calls_are_one_group():
    """A group is not limited to two, and is found by overlap, not by count."""
    _fresh()

    def fan_out(run):
        c = run.client()
        with cf.ThreadPoolExecutor(max_workers=3) as pool:
            futures = []
            for name in ("research", "risk", "support"):
                futures.append(pool.submit(
                    lambda n: c.post(B + "/slow?child=" + n, content=b"{}"),
                    name))
                time.sleep(0.05)
            for f in futures:
                f.result()
        run.output = "done"

    with orientim.record(root=ROOT, always=True, agent={"name": "fan-out"}) as h:
        fan_out(h)
    meta, steps = store.load(h.path)
    shape = K.describe(steps, meta)
    return (len(shape["groups"]) == 1 and len(shape["groups"][0]) == 3), \
        "groups %r" % (shape["group_labels"],)


def t_nested_execution_records_only_its_own_layer():
    """An agent whose tool is another recording agent, on one thread.

    The inner run is its own recording; the outer sees only its own call.
    Neither contains the other, which is the property a fleet view would have
    to join across and which nothing joins today.
    """
    _fresh()

    def inner(run):
        c = run.client()
        c.post(B + "/slow?in=1", content=b"{}")
        c.post(B + "/slow?in=2", content=b"{}")
        run.output = "inner done"

    def outer(run):
        run.client().post(B + "/search", content=json.dumps({"q": "outer"}).encode())
        with orientim.record(root=ROOT, always=True,
                             agent={"name": "inner"}) as sub:
            inner(sub)
        run.output = "outer done"
        return sub.path

    with orientim.record(root=ROOT, always=True, agent={"name": "outer"}) as h:
        inner_path = outer(h)

    om, outer_steps = store.load(h.path)
    im, inner_steps = store.load(inner_path)
    outer_http = [x for x in outer_steps if x.get("t") == "http"]
    inner_http = [x for x in inner_steps if x.get("t") == "http"]
    inner_replay = orientim.replay(inner_path, inner)

    return (len(outer_http) == 1 and len(inner_http) == 2
            and not any("in=" in (x.get("url") or "") for x in outer_http)
            and {i["agent"] for i in K.describe(inner_steps, im)["invocations"]}
            == {"inner"}
            and inner_replay.ok), \
        "outer %d call(s), inner %d, inner replays %s" % (
            len(outer_http), len(inner_http), inner_replay.diagnosis[0])


def t_nested_record_drops_calls_from_worker_threads():
    """A LIMIT, pinned so it cannot drift into a surprise.

    Inside a nested record(), a call made from a *worker thread* is recorded by
    neither run. With two regions open and a thread carrying no context of its
    own, scope.current() refuses to guess — which is right, because guessing
    would file the call under the wrong agent and produce a recording that lies.
    Refusing means it is filed under nothing.

    Narrow, and worth knowing exactly how narrow: nesting on one thread works
    (the check above), and concurrency without nesting works (every other check
    here). It is only the two together.

    The drop is not silent at replay — an empty recording reports
    NOTHING_CAPTURED — but nothing at *record* time says a call went nowhere.
    Documented in docs/concurrency.md.
    """
    _fresh()

    def concurrent(run):
        c = run.client()
        with cf.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(lambda: c.post(B + "/slow?in=1", content=b"{}"))
            time.sleep(0.08)
            f2 = pool.submit(lambda: c.post(B + "/slow?in=2", content=b"{}"))
            f1.result()
            f2.result()
        run.output = "done"

    # Alone, the same function records both calls and finds the group.
    with orientim.record(root=ROOT, always=True, agent={"name": "solo"}) as solo:
        concurrent(solo)
    solo_meta, solo_steps = store.load(solo.path)
    solo_shape = K.describe(solo_steps, solo_meta)

    # Nested, the worker threads are attributed to neither region.
    def outer(run):
        run.client().post(B + "/search", content=b"{}")
        with orientim.record(root=ROOT, always=True,
                             agent={"name": "inner"}) as sub:
            concurrent(sub)
        run.output = "outer done"
        return sub.path

    with orientim.record(root=ROOT, always=True, agent={"name": "outer"}) as h:
        nested_path = outer(h)

    outer_http = [x for x in store.load(h.path)[1] if x.get("t") == "http"]
    inner_http = [x for x in store.load(nested_path)[1] if x.get("t") == "http"]
    verdict = orientim.replay(nested_path, concurrent).diagnosis[0]

    return (len(solo_shape["groups"]) == 1          # alone: works
            and len(inner_http) == 0                 # nested: dropped
            and len(outer_http) == 1                 # and not stolen either
            and verdict == "NOTHING_CAPTURED"), \
        "alone %d group(s); nested inner %d call(s), outer %d, replay %s" % (
            len(solo_shape["groups"]), len(inner_http), len(outer_http),
            verdict)


def t_parallel_child_agents_over_http():
    """Two child agents called at once, over real sockets and a real pool."""
    _fresh()

    def supervisor(run):
        c = run.client()
        c.post(B + "/v1/chat/completions", content=json.dumps({
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "plan"}]}).encode())
        with cf.ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(lambda: c.post(B + "/slow?agent=research",
                                             content=b"{}"))
            time.sleep(STAGGER)
            two = pool.submit(lambda: c.post(B + "/slow?agent=risk",
                                             content=b"{}"))
            one.result()
            two.result()
        run.output = "delegated"

    with orientim.record(root=ROOT, always=True,
                         agent={"name": "supervisor"}) as h:
        supervisor(h)
    meta, steps = store.load(h.path)
    shape = K.describe(steps, meta)
    roles = [s.get("role") for s in steps if s.get("t") == "http"]
    d = orientim.replay(h.path, supervisor)
    return (len(shape["groups"]) == 1 and len(shape["groups"][0]) == 2
            and roles[0] == "model" and d.ok), \
        "roles %r, group %r, replay %s" % (roles, shape["group_labels"],
                                           d.diagnosis[0])


# --- the invariants -----------------------------------------------------------

def t_replay_matching_does_not_read_the_concurrency_layer():
    """Strip every concurrency field from a recording and it still replays.

    Stronger than asserting the digest field list: this removes t0, ms and
    worker outright and demands the same verdict, so a future reader that
    started depending on them would fail here.
    """
    _fresh()

    def agent_(run):
        c = run.client()
        c.post(B + "/search", content=b'{"q":1}')
        c.post(B + "/slow?x=1", content=b"{}")
        run.output = "done"

    with orientim.record(root=ROOT, always=True) as h:
        agent_(h)
    before = orientim.replay(h.path, agent_)

    meta, steps = store.load(h.path)
    for s in steps:
        for field in ("t0", "ms", "worker"):
            s.pop(field, None)
    stripped = os.path.join(ROOT, "no_timing.jsonl")
    with open(stripped, "wb") as f:
        f.write(("\n".join([json.dumps({"_meta": meta})]
                           + [json.dumps(s) for s in steps]) + "\n").encode())

    after = orientim.replay(stripped, agent_)
    shape = K.describe(store.load(stripped)[1])
    return (before.ok and after.ok
            and before.diagnosis[0] == after.diagnosis[0]
            and shape["groups"] == []), \
        "with timing %s, without %s, groups without timing %r" % (
            before.diagnosis[0], after.diagnosis[0], shape["groups"])


def t_orientim_test_semantics_are_unchanged_by_a_reorder():
    """The contract `orientim test` keeps: it asks whether the code reproduces
    the recording, and by the recorded-order rule a reordering does."""
    from orientim import cases
    _fresh()

    def agent_(run):
        c = run.client()
        pair = ["research", "risk"]
        if os.environ.get(ORDER_ENV) == "reversed":
            pair = list(reversed(pair))
        with cf.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(lambda: c.post(B + "/slow?child=" + pair[0],
                                            content=b"{}"))
            time.sleep(STAGGER)
            f2 = pool.submit(lambda: c.post(B + "/slow?child=" + pair[1],
                                            content=b"{}"))
            f1.result()
            f2.result()
        run.output = "done"

    os.environ.pop(ORDER_ENV, None)
    with orientim.record(root=ROOT, always=True) as h:
        agent_(h)
    cases.save("reorder", h.path, "x:agent", root=ROOT,
               expect={"no_step_failed": True})

    os.environ[ORDER_ENV] = "reversed"
    try:
        row = cases.run(cases.load("reorder", ROOT),
                        entry_loader=lambda e: agent_)
    finally:
        os.environ.pop(ORDER_ENV, None)
    return (row["ok"] and row["verdict"] == "IDENTICAL"), \
        "the case says %s (%s)" % (row["verdict"], row["reason"])


def t_client_timeout_is_backward_compatible():
    """Silent callers get what they always got; an explicit one is honoured."""
    _fresh()
    seen = {}

    def agent_(run):
        default = run.client()
        explicit = run.client(timeout=45.0)
        seen["default"] = default.timeout.read
        seen["explicit"] = explicit.timeout.read
        default.post(B + "/search", content=b'{"q":1}')
        run.output = "done"

    with orientim.record(root=ROOT, always=True) as h:
        agent_(h)
    return (seen["default"] == 10.0 and seen["explicit"] == 45.0), \
        "default %r, explicit %r" % (seen["default"], seen["explicit"])


def t_case_input_is_per_case_and_needs_no_environment():
    """Two cases, one process, two different inputs, and no variable between.

    The environment-variable workaround failed exactly here: whichever value
    was set last won, so a suite silently ran every case against one input.
    """
    from orientim import cases
    _fresh()
    asked = []

    def agent_(run):
        q = run.input["q"]
        asked.append(q)
        run.client().post(B + "/search",
                          content=json.dumps({"q": q}).encode())
        run.output = "asked %s" % q

    made = []
    for q in ("alpha", "beta"):
        with orientim.record(root=ROOT, always=True, input={"q": q}) as h:
            agent_(h)
        cases.save(q, h.path, "x:agent", root=ROOT)
        made.append(q)

    del asked[:]
    rows = [cases.run(cases.load(name, ROOT), entry_loader=lambda e: agent_)
            for name in made]
    env_leak = [k for k in os.environ if k.startswith("ORIENTIM_CASE")]
    return (asked == ["alpha", "beta"] and all(r["ok"] for r in rows)
            and not env_leak), \
        "replayed inputs %r, verdicts %r" % (
            asked, [r["verdict"] for r in rows])
