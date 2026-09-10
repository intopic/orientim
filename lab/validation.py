# -*- coding: utf-8 -*-
"""Product validation: what is Orientim actually worth on a real fleet?

    python lab/run.py up
    python lab/validation.py

For each of the ten regressions: freeze a baseline on v1, inject the change,
run `orientim test`, run `orientim diff`, and grade what came back.

The grading rule, and it is the point of the exercise:

    A regression is NOT "detected" because a command exited non-zero.

An exit code says something moved. A developer needs to know *what*, and the
only evidence of that is output naming the change. So every regression carries
a list of signals that must appear — the model name, the tool name, the changed
argument — and a run that exits 1 without them is recorded as DETECTED WITHOUT
EVIDENCE, which is a different and much weaker result.

Three measurements per regression:

    test         `orientim test --case X --baseline main`, the CI gate
    diff-replay  `orientim diff --case X`, explaining against a fresh replay
    diff-record  a live run under the regression, diffed against the baseline
                 recording — the only view that can see a concurrency change

and the time each took, because "how long to find out" is half of what a
regression tool is worth.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import run as L                                      # noqa: E402
from scenarios import BY_NAME                        # noqa: E402
from variants import VARIANTS                        # noqa: E402
from orientim import diff, store                     # noqa: E402

OUT = os.path.join(HERE, "_runs", "_validation.json")

# What must appear for a change to count as explained. Lowercased substrings;
# every one of them has to be present somewhere in the combined output.
EVIDENCE = {
    "model_change":   ["model config", "gpt-4o-mini -> gpt-4o"],
    "tool_change":    ["tool decision", "order.lookup", "kb.search"],
    "arg_change":     ["arguments changed", "include_history"],
    # "no longer requested" is what the diff says; the first draft of this
    # grader looked for "not requested" and marked two real findings as
    # evidence-free. A grading string that is stricter than the product is a
    # measurement error, not a product defect.
    "tool_unused":    ["risk.score", "no longer requested"],
    "forbidden_tool": ["refund.issue", "requested"],
    "output_change":  ["output", "dear customer"],
    "new_call":       ["order.lookup", "inserted"],
    "call_removed":   ["shipping.track", "no longer requested"],
    "order_change":   ["reordered"],
    "parallel_order": ["parallel_order_changed"],
}

# Which agent owns each regression, and the scenario that shows it. Evaluation
# is per agent, so a change inside the risk agent is invisible in the
# supervisor's tool calls and obvious in the risk agent's own.
OWNER = {
    "model_change":   ("supervisor", "shipped-order"),
    "tool_change":    ("support",    "shipped-order"),
    "arg_change":     ("support",    "shipped-order"),
    "tool_unused":    ("risk",       "risk-must-run"),
    "forbidden_tool": ("support",    "no-refund-without-a-human"),
    "output_change":  ("support",    "shipped-order"),
    "new_call":       ("research",   "shipping-tracked"),
    "call_removed":   ("research",   "shipping-tracked"),
    "order_change":   ("research",   "shipping-tracked"),
    "parallel_order": ("supervisor", "parallel-children"),
}


def say(*a):
    print(*a, flush=True)


def cli(agent, *args):
    """One CLI call against one agent's suite, timed."""
    root = os.path.join(HERE, "_runs", agent)
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "orientim.cli", "--root", root]
                       + list(args), cwd=HERE, env=L._env(L.running_variant()
                                                          or "v1"),
                       capture_output=True, text=True)
    return {"exit": r.returncode, "out": r.stdout + r.stderr,
            "ms": round((time.monotonic() - t0) * 1000)}


def baseline_recording(agent, scenario):
    """The recording the baseline case points at."""
    from orientim import cases
    root = os.path.join(HERE, "_runs", agent)
    return cases.load(scenario, root)["recording"]


def live_run(scenario):
    """Run the scenario against the fleet as it is now, and keep the recording."""
    task = dict(BY_NAME[scenario]["task"], scenario=scenario)
    return L.ask(task)["_recording"]


def child_recording(agent, scenario):
    """The newest recording that agent wrote for that scenario."""
    root = os.path.join(HERE, "_runs", agent)
    best = None
    for meta in sorted(store.list_runs(root),
                       key=lambda m: m.get("started_at") or 0):
        if (meta.get("tags") or {}).get("scenario") == scenario:
            best = store.locator_for(root, meta["run_id"] + ".jsonl")
    return best


def grade(variant, text):
    """Which required signals are present, and which are not."""
    low = text.lower()
    want = EVIDENCE[variant]
    found = [w for w in want if w.lower() in low]
    return found, [w for w in want if w.lower() not in low]


def measure(v):
    agent, scenario = OWNER[v["id"]]
    say("\n" + "=" * 74)
    say("  %2d. %-16s %s" % (v["n"], v["id"], v["what"]))
    say("      agent %s, case %s" % (agent, scenario))
    say("=" * 74)

    before = baseline_recording(agent, scenario)

    L.down(quiet=True)
    if L.up(v["id"], quiet=True) is None:
        return {"variant": v["id"], "error": "fleet did not start"}
    L.reset_effects()

    test = cli(agent, "test", "--case", scenario, "--baseline", "main")
    dr = cli(agent, "diff", "--case", scenario)

    # A live run under the regression, diffed against the baseline recording.
    # The only view that can see a concurrency change, because a replay serves
    # the recorded order by contract.
    rec_ms, rec_text = 0, ""
    try:
        t0 = time.monotonic()
        live_run(scenario)
        after = (child_recording(agent, scenario) if agent != "supervisor"
                 else child_recording("supervisor", scenario))
        if after and before:
            rec_text = diff.report(diff.compare(before, after))
        rec_ms = round((time.monotonic() - t0) * 1000)
    except Exception as e:
        rec_text = "record-and-diff failed: %s: %s" % (type(e).__name__, e)

    combined = test["out"] + dr["out"] + rec_text
    found, missing = grade(v["id"], combined)
    # Where the evidence came from. `orientim test` is the CI gate; if the
    # explanation is only in `orientim diff`, a red build does not tell a
    # developer what changed and the second command is not optional.
    from_test = not grade(v["id"], test["out"])[1]
    from_diff = not grade(v["id"], dr["out"] + rec_text)[1]
    source = ("test" if from_test else
              ("diff only" if from_diff else "incomplete"))

    # The criterion that matters for a person: does the command that fails the
    # build explain itself, without a second command being run first?
    if not missing and from_test:
        verdict = "DETECTED WITH ACTIONABLE EVIDENCE"
    elif not missing:
        verdict = "DETECTED, NEEDS orientim diff"
    elif test["exit"] != 0:
        verdict = "DETECTED WITHOUT EVIDENCE"
    else:
        verdict = "MISSED"

    row = {
        "n": v["n"], "variant": v["id"], "agent": agent, "case": scenario,
        "what": v["what"],
        "test_exit": test["exit"], "test_ms": test["ms"],
        "diff_replay_ms": dr["ms"], "diff_record_ms": rec_ms,
        "total_ms": test["ms"] + dr["ms"],
        "evidence_found": found, "evidence_missing": missing,
        "verdict": verdict,
        "effects": L.effects(),
        "failed_the_build": test["exit"] != 0,
        "evidence_source": source,
        "test_output": test["out"][-1200:],
        "diff_output": dr["out"][-2500:],
    }
    say("  orientim test  exit %d  %6d ms" % (test["exit"], test["ms"]))
    say("  orientim diff            %6d ms" % dr["ms"])
    say("  record + diff            %6d ms" % rec_ms)
    say("  evidence found  : %s" % ", ".join(found) or "none")
    say("  evidence missing: %s" % (", ".join(missing) or "none"))
    say("  --> %s (evidence from: %s)" % (verdict, source))
    return row


def false_positives():
    """v1 against its own baseline. Anything that fails here is noise."""
    say("\n" + "=" * 74)
    say("  false positives: v1 against its own baseline")
    say("=" * 74)
    L.down(quiet=True)
    L.up("v1", quiet=True)
    out = {}
    for agent, _root in L.SUITES:
        r = cli(agent, "test", "--baseline", "main")
        failed = [ln.strip() for ln in r["out"].splitlines()
                  if ln.strip().startswith("!!")]
        out[agent] = {"exit": r["exit"], "ms": r["ms"], "failures": failed}
        say("  %-11s exit %d  %6d ms  %d failing line(s)"
            % (agent, r["exit"], r["ms"], len(failed)))
        for ln in failed[:4]:
            say("      %s" % ln)
    return out


def main():
    if L.running_variant() is None:
        say("  start the fleet first:  python lab/run.py up")
        return 2

    say("\n  preparing a clean v1 baseline\n")
    L.down(quiet=True)
    L.up("v1", quiet=True)
    if L.record() != 0:
        return 2
    L.cases()
    L.baseline("main")

    fp = false_positives()
    rows = [measure(v) for v in VARIANTS]

    L.down(quiet=True)
    report(rows, fp)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"regressions": rows, "false_positives": fp}, f, indent=2)
    say("\n  written to %s" % OUT)
    return 0


def report(rows, fp):
    from orientim import cases as C
    actionable = [r for r in rows
                  if r["verdict"] == "DETECTED WITH ACTIONABLE EVIDENCE"]
    needs_diff = [r for r in rows if r["verdict"] == "DETECTED, NEEDS orientim diff"]
    weak = [r for r in rows if r["verdict"] == "DETECTED WITHOUT EVIDENCE"]
    missed = [r for r in rows if r["verdict"] == "MISSED"]
    n_cases = sum(len(C.list_cases(os.path.join(HERE, "_runs", a)))
                  for a, _ in L.SUITES)
    fp_count = sum(len(v["failures"]) for v in fp.values())
    avg = (sum(r["total_ms"] for r in rows) / len(rows)) if rows else 0

    say("\n" + "=" * 74)
    say("  PRODUCT VALIDATION LAB REPORT")
    say("=" * 74)
    say("  REGRESSIONS TESTED             %d" % len(rows))
    say("  EXPLAINED BY CI ITSELF         %d   (orientim test alone)"
        % len(actionable))
    say("  DETECTED, NEEDS orientim diff  %d" % len(needs_diff))
    say("  DETECTED WITHOUT EVIDENCE      %d" % len(weak))
    say("  MISSED                         %d" % len(missed))
    say("  FALSE POSITIVES                %d" % fp_count)
    say("  AVERAGE TIME TO DIAGNOSE       %.1f s" % (avg / 1000.0))
    say("  CASES CREATED                  %d across %d agents"
        % (n_cases, len(L.SUITES)))
    say("")
    say("  %-3s %-16s %-11s %-6s %-9s %s"
        % ("#", "regression", "agent", "exit", "time", "verdict"))
    say("  " + "-" * 88)
    for r in rows:
        say("  %-3d %-16s %-11s %-6d %6.1f s  %-24s %s"
            % (r["n"], r["variant"], r["agent"], r["test_exit"],
               r["total_ms"] / 1000.0, r["verdict"][:24],
               r.get("evidence_source", "?")))
        if r["evidence_missing"]:
            say("      missing: %s" % ", ".join(r["evidence_missing"]))
    say("")


if __name__ == "__main__":
    sys.exit(main())
