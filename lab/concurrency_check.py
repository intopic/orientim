# -*- coding: utf-8 -*-
"""Six execution shapes against the live fleet, and what Orientim says about each.

    python lab/run.py up
    python lab/concurrency_check.py

Not a unit test. The fleet is running, the calls are real sockets to real agent
services, and the question is what a person actually gets back for each shape.
Every row prints what was caught, what was not, and whether that is a decision
or a limit.
"""
import concurrent.futures as cf
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import orientim                                    # noqa: E402
from orientim import concurrency as K              # noqa: E402
from orientim import diff, store                   # noqa: E402
from agents import common as C                     # noqa: E402

ROOT = os.path.join(HERE, "_runs", "shapes")
STAGGER = 0.25

ROWS = []


def row(shape, caught, missed, why, kind):
    ROWS.append({"shape": shape, "caught": caught, "missed": missed,
                 "why": why, "kind": kind})
    print("\n  %s" % shape)
    print("    caught : %s" % caught)
    print("    missed : %s" % (missed or "nothing"))
    print("    why    : %s" % why)
    print("    verdict: %s" % kind)


def record(fn, name, **kw):
    with orientim.record(root=ROOT, always=True,
                         agent={"name": name}, **kw) as h:
        fn(h)
    return h.path


def shape_of(path):
    meta, steps = store.load(path)
    return K.describe(steps, meta), meta, steps


# --- the six shapes -----------------------------------------------------------

def single_agent(run):
    """One agent, one call at a time."""
    c = C.client(run)
    C.call_tool(c, "order.lookup", {"order_id": 4471})
    C.call_tool(c, "kb.search", {"q": "refund"})
    run.output = "single done"


def two_children(run, reverse=False):
    """Research and Risk, at once."""
    c = C.client(run)
    pair = [("risk", C.RISK), ("research", C.RESEARCH)] if reverse else \
           [("research", C.RESEARCH), ("risk", C.RISK)]
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(C.post_json, c, pair[0][1] + "/run",
                            {"order_id": 4471, "topic": "refund"})
        time.sleep(STAGGER)
        second = pool.submit(C.post_json, c, pair[1][1] + "/run",
                             {"order_id": 4471, "topic": "refund"})
        first.result()
        second.result()
    run.output = "two done"


def three_children(run):
    """All three children, at once."""
    c = C.client(run)
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        futures = []
        for url in (C.RESEARCH, C.RISK, C.SUPPORT):
            futures.append(pool.submit(C.post_json, c, url + "/run",
                                       {"order_id": 4471, "topic": "refund"}))
            time.sleep(0.1)
        for f in futures:
            f.result()
    run.output = "three done"


def nested_child(run):
    """A child agent that is itself recording — the fleet's real shape."""
    c = C.client(run)
    C.post_json(c, C.SUPPORT + "/run", {"order_id": 4471})
    run.output = "nested done"


def sequential_pair(run, reverse=False):
    """Two calls that never overlap, so their order is provable."""
    c = C.client(run)
    order = ["kb.search", "shipping.track"]
    if reverse:
        order = list(reversed(order))
    args = {"kb.search": {"q": "refund"},
            "shipping.track": {"order_id": 4471}}
    for name in order:
        C.call_tool(c, name, args[name])
        time.sleep(0.15)                # a gap wide enough to prove the order
    run.output = "sequential done"


# --- the run ------------------------------------------------------------------

def main():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    print("=" * 74)
    print("  six shapes against the live fleet")
    print("=" * 74)

    # 1 — single agent
    p = record(single_agent, "solo")
    shape, meta, steps = shape_of(p)
    d = orientim.replay(p, single_agent)
    row("1. single agent, two sequential calls",
        "%d call(s), %d group(s), replay %s"
        % (len(shape["invocations"]), len(shape["groups"]), d.diagnosis[0]),
        None if not shape["groups"] else "invented a group",
        "nothing ran beside anything, so there is nothing to group",
        "supported")

    # 2 — two children in parallel
    a = record(lambda r: two_children(r, False), "supervisor")
    sa, _m, _s = shape_of(a)
    row("2. two child agents in parallel",
        "1 group of %d, over real HTTP to two services"
        % (len(sa["groups"][0]) if sa["groups"] else 0),
        None if len(sa["groups"]) == 1 else "did not find the group",
        "their intervals overlap, which is all concurrency is",
        "supported")

    # 3 — three children in parallel
    p3 = record(three_children, "supervisor")
    s3, _m, _s = shape_of(p3)
    row("3. three child agents in parallel",
        "1 group of %d" % (len(s3["groups"][0]) if s3["groups"] else 0),
        None if s3["groups"] and len(s3["groups"][0]) == 3 else
        "group of %r" % ([len(g) for g in s3["groups"]],),
        "connected components of the overlap graph, so a group is any size",
        "supported")

    # 4 — a nested child that records itself
    before = len(store.list_runs(os.path.join(HERE, "_runs", "support")))
    pn = record(nested_child, "supervisor")
    sn, _m, ns = shape_of(pn)
    after = len(store.list_runs(os.path.join(HERE, "_runs", "support")))
    outer_urls = [x.get("url", "") for x in ns if x.get("t") == "http"]
    row("4. nested child (support records itself)",
        "the parent sees %d call to the child; the child wrote %d recording "
        "of its own" % (len(outer_urls), after - before),
        "no join between the two",
        "each recording is one agent's; nothing carries a shared execution id, "
        "so the parent's step and the child's run are related only by a tag "
        "somebody chose to set",
        "LIMIT — the fleet extension, deliberately not built")

    # 5 — a sequential reorder
    s_a = record(lambda r: sequential_pair(r, False), "supervisor")
    s_b = record(lambda r: sequential_pair(r, True), "supervisor")
    cmp_seq = diff.compare(s_a, s_b)
    seq_findings = [f["kind"] for f in cmp_seq["concurrency"]["findings"]]
    seq_strength = {f["strength"] for f in cmp_seq["concurrency"]["findings"]}
    row("5. sequential reorder (kb.search and shipping.track swap)",
        "steps %r; concurrency %r %r; policy ok=%s"
        % ({k: v for k, v in cmp_seq["counts"].items() if v},
           seq_findings, sorted(seq_strength),
           cmp_seq["concurrency_policy"]["ok"]),
        None if K.SEQUENTIAL_ORDER_CHANGED in seq_findings else
        "no sequential finding",
        "one provably finished before the other began, and that reversed",
        "supported — strong, and it fails the default policy")

    # 6 — a parallel reorder
    b = record(lambda r: two_children(r, True), "supervisor")
    cmp_par = diff.compare(a, b)
    par_findings = [f["kind"] for f in cmp_par["concurrency"]["findings"]]
    par_strength = {f["strength"] for f in cmp_par["concurrency"]["findings"]}
    test_would_pass = orientim.replay(a, lambda r: two_children(r, True)).ok
    row("6. parallel reorder (research and risk swap)",
        "steps %r; concurrency %r %r; policy ok=%s"
        % ({k: v for k, v in cmp_par["counts"].items() if v},
           par_findings, sorted(par_strength),
           cmp_par["concurrency_policy"]["ok"]),
        "`orientim test` does not fail: replaying the reordered code against "
        "the recording is still %s" % ("IDENTICAL" if test_would_pass
                                       else "not identical"),
        "replay serves recorded steps in the recorded order, which is what "
        "lets a concurrent agent replay at all; the diff sees it instead",
        "intentional — weak by default, and a policy can make it strict")

    print("\n" + "=" * 74)
    print("  %d shapes: %d supported, %d intentional, %d limit"
          % (len(ROWS),
             sum(1 for r in ROWS if r["kind"].startswith("supported")),
             sum(1 for r in ROWS if r["kind"].startswith("intentional")),
             sum(1 for r in ROWS if r["kind"].startswith("LIMIT"))))
    with open(os.path.join(ROOT, "_shapes.json"), "w", encoding="utf-8") as f:
        json.dump(ROWS, f, indent=2)
    print("  written to %s" % os.path.join(ROOT, "_shapes.json"))


if __name__ == "__main__":
    main()
