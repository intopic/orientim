# -*- coding: utf-8 -*-
"""`orientim ci` — replay a suite of recordings against the current code.

This is the whole automation surface. It replays every recording in a store
against one entry point, prints a summary a person can read in a terminal or a
pull request, writes a machine-readable report, and exits non-zero when
behaviour changed.

The report it writes is deliberately narrow: run ids, verdicts, hashes, counts.
No URLs, no bodies, no prompts. That is not politeness — it is what makes the
file safe to hand to a build system, an artifact store, or a service, without
anyone having to audit it first.
"""
import os
import time

from . import session, store


EXIT_OK = 0
EXIT_CHANGED = 1
EXIT_CANNOT_RUN = 2

SCHEMA = 1


def _github():
    """What the CI system already knows about this build."""
    return {
        "repo": os.environ.get("GITHUB_REPOSITORY"),
        "commit": os.environ.get("GITHUB_SHA"),
        "branch": (os.environ.get("GITHUB_HEAD_REF")
                   or os.environ.get("GITHUB_REF_NAME")),
        "run": os.environ.get("GITHUB_RUN_ID"),
    }


def replay_all(root, fn, strict=True, on_result=None):
    """Replay every recording under `root`. Returns one row each."""
    rows = []
    for meta in sorted(store.list_runs(root),
                       key=lambda m: m.get("started_at") or 0):
        run_id = meta.get("run_id")
        if not run_id:
            continue
        path = store.locator_for(root, run_id + ".jsonl")
        t0 = time.monotonic()
        try:
            d = session.replay(path, fn, strict=strict)
            row = {
                "run_id": run_id,
                "ok": bool(d.ok),
                "verdict": d.diagnosis[0],
                "index": d.index,
                "recorded_root": d.recorded_root,
                "replay_root": d.replay_root,
                "steps": d.n_recorded,
                "tags": meta.get("tags") or {},
            }
        except Exception as e:
            # A recording that cannot even be loaded is a failure of this
            # build, not something to skip quietly.
            row = {"run_id": run_id, "ok": False, "verdict": "REPLAY_ERROR",
                   "index": None, "recorded_root": "", "replay_root": "",
                   "steps": meta.get("steps", 0), "tags": meta.get("tags") or {},
                   "error": "%s: %s" % (type(e).__name__, e)}
        row["ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        rows.append(row)
        if on_result:
            on_result(row)
    return rows


def report(rows, strict, entry, baseline=None):
    """The machine-readable half. Ids, verdicts and hashes — nothing else."""
    changed = [r for r in rows if not r["ok"]]
    out = {
        "schema": SCHEMA,
        "created_at": time.time(),
        "tool": "orientim",
        "entry": entry,
        "strict": bool(strict),
        "totals": {"replayed": len(rows), "identical": len(rows) - len(changed),
                   "changed": len(changed)},
        "runs": [{k: r[k] for k in ("run_id", "ok", "verdict", "index",
                                    "recorded_root", "replay_root", "steps", "ms")}
                 for r in rows],
    }
    out.update({k: v for k, v in _github().items() if v})
    if baseline is not None:
        out["against_baseline"] = compare(rows, baseline)
    return out


def _statuses(row):
    """evaluator -> status, for one live row of a suite run."""
    results = (row.get("evaluation") or {}).get("results") or []
    return {r.get("evaluator"): r.get("status")
            for r in results if r.get("evaluator")}


def _frozen_statuses(prev):
    """The same, out of a baseline row, plus whether it is the whole picture.

    Baselines written before obligations were compared carry only
    `failed_evaluators`. That is enough to tell a new failure from a known one
    — a name absent from it was not failing — and not enough to tell a rule
    that was dropped from a rule that was never there, or a PASS that decayed
    into UNKNOWN. The flag says which of those questions this baseline can
    answer, so an old file gets the answers it supports and no others.
    """
    full = prev.get("evaluators")
    if isinstance(full, dict) and full:
        return dict(full), True
    return {n: "fail" for n in (prev.get("failed_evaluators") or [])}, False


def compare(rows, baseline, key="run_id", scope=None):
    """What changed since a previous report — the base branch, usually.

    Without this a build only knows whether it is green today. With it, it
    knows which runs *started* failing on this change, which is the question a
    reviewer is actually asking.

    `key` names the field that identifies a row across two runs. It is the run
    id for `orientim ci`, which replays a whole store, and the case name for
    `orientim test`, where the same case may point at a different recording
    over time. One comparison, two callers — a second implementation of this
    would be a second set of edge cases to get wrong.

    `scope`, when given, names the keys this run was asked to cover. A run
    filtered to one case knows nothing about the rest of the baseline, and
    calling those cases "gone now" is false and buries the one line the reader
    asked for. Left None — what a whole-suite run passes — the run is taken to
    cover everything, and a key in the baseline that did not come back really
    has gone.
    """
    was = {r[key]: r for r in baseline.get("runs", []) if key in r}
    newly, fixed, still, added, new_failing = [], [], [], [], []
    new_failures, dropped, weakened = {}, {}, {}
    for r in rows:
        k = r.get(key)
        prev = was.get(k)
        if prev is None:
            added.append(k)
            if not r["ok"]:
                # New *and* already red. `new_recordings` alone says only that
                # nobody had it before, which reads like housekeeping.
                new_failing.append(k)
            continue
        if not r["ok"] and prev["ok"]:
            newly.append(k)
        elif r["ok"] and not prev["ok"]:
            fixed.append(k)
        elif not r["ok"]:
            still.append(k)
        # Below the case verdict. A case that was red and stayed red can have
        # acquired an entirely different violation, and `False -> False` is
        # where that goes to die: the one number a reviewer looks at did not
        # move, so nothing asks them to look at the rule that did.
        now = _statuses(r)
        before, full = _frozen_statuses(prev)
        fresh = sorted(n for n, s in now.items()
                       if s == "fail" and before.get(n) != "fail")
        if fresh:
            new_failures[k] = fresh
        if not full:
            # An older baseline recorded only the failures, so absence of a
            # name means "not failing", not "not checked". Claiming a dropped
            # obligation or a lost proof from that would be inventing one.
            continue
        missing = sorted(n for n in before if n not in now)
        if missing:
            dropped[k] = missing
        lost = sorted(n for n, s in now.items()
                      if s == "warn" and before.get(n) == "pass")
        if lost:
            weakened[k] = lost
    seen = {r.get(key) for r in rows}
    gone = [k for k in was
            if k not in seen and (scope is None or k in scope)]
    return {"newly_changed": newly, "fixed": fixed,
            "still_changed": still, "new_recordings": added,
            "missing_recordings": gone, "new_failing": new_failing,
            "new_failures": new_failures, "dropped_obligations": dropped,
            "weakened": weakened}


def obligation_lines(cmp_):
    """The part of a comparison that is about rules rather than cases.

    Written once and rendered by all three summaries. A case verdict is one
    bit, and these are the movements that bit cannot carry: a rule that started
    failing inside a case that was already red, a rule that quietly left the
    suite, and a rule that stopped being provable without failing.

    None of them changes an exit code. They are here to be seen.
    """
    out = []
    for key, label in (("new_failures", "Rules that started failing"),
                       ("dropped_obligations", "In the baseline, not checked now"),
                       ("weakened", "Lost their proof (pass to unknown)")):
        moved = cmp_.get(key) or {}
        if moved:
            out.append("  %s: %s"
                       % (label, "; ".join("%s (%s)" % (k, ", ".join(v))
                                           for k, v in sorted(moved.items()))))
    if cmp_.get("new_failing"):
        out.append("  New and already failing: "
                   + ", ".join(cmp_["new_failing"]))
    return out


def summary(rows, strict, baseline_cmp=None, width=74):
    """The half a person reads. Terminal, or a pull request comment."""
    changed = [r for r in rows if not r["ok"]]
    L = ["", "=" * width]
    L.append("  ORIENTIM CI  ·  %d recording(s)  ·  %s key"
             % (len(rows), "strict" if strict else "loose"))
    L.append("=" * width)
    L.append("")
    if not rows:
        L.append("  No recordings found. Nothing was replayed.")
        L.append("")
        return "\n".join(L)

    for r in rows:
        mark = "ok " if r["ok"] else "!! "
        note = "" if r["ok"] else "  %s%s" % (
            r["verdict"],
            "" if r["index"] is None else " @ step %d" % r["index"])
        tags = " ".join("%s=%s" % kv for kv in sorted(r["tags"].items()))
        L.append("  %s %-16s %3d steps  %7.1f ms  %s%s"
                 % (mark, r["run_id"], r["steps"], r["ms"], tags, note))
        if r.get("error"):
            L.append("       %s" % r["error"])
    L.append("")
    L.append("-" * width)
    if changed:
        L.append("  %d of %d recordings changed behaviour."
                 % (len(changed), len(rows)))
    else:
        L.append("  All %d recordings replayed identically." % len(rows))

    if baseline_cmp:
        b = baseline_cmp
        if b["newly_changed"]:
            L.append("  NEW on this change: " + ", ".join(b["newly_changed"]))
        if b["fixed"]:
            L.append("  Fixed on this change: " + ", ".join(b["fixed"]))
        if b["still_changed"]:
            L.append("  Already changing before this: "
                     + ", ".join(b["still_changed"]))
        if b["new_recordings"]:
            L.append("  Recordings not in the baseline: "
                     + ", ".join(b["new_recordings"]))
        L += obligation_lines(b)
    L.append("-" * width)
    L.append("")
    return "\n".join(L)


# --- GitHub surfaces --------------------------------------------------------
# Both are plain files and environment variables, so nothing here needs a
# token, an App, or an account.

def annotate(rows):
    """Workflow annotations, so a failure shows on the pull request itself."""
    lines = []
    for r in rows:
        if r["ok"]:
            continue
        where = "" if r["index"] is None else " at step %d" % r["index"]
        lines.append("::error title=%s::%s%s — %s"
                     % (r["run_id"], r["verdict"], where,
                        r.get("error", "behaviour changed since this was recorded")))
    return lines


def step_summary(rows, strict, baseline_cmp=None):
    """Markdown for GITHUB_STEP_SUMMARY — the job's own page."""
    changed = [r for r in rows if not r["ok"]]
    head = ("### Orientim — %d of %d recordings changed"
            % (len(changed), len(rows))) if changed else \
           ("### Orientim — all %d recordings replayed identically" % len(rows))
    md = [head, "", "| run | steps | verdict | step |", "|---|---:|---|---:|"]
    for r in rows:
        md.append("| `%s` | %d | %s | %s |"
                  % (r["run_id"], r["steps"],
                     "identical" if r["ok"] else "**%s**" % r["verdict"],
                     "" if r["index"] is None else r["index"]))
    if baseline_cmp and baseline_cmp["newly_changed"]:
        md += ["", "**New on this change:** "
               + ", ".join("`%s`" % r for r in baseline_cmp["newly_changed"])]
    md += ["", "_%s key. Replay makes no network calls._"
           % ("Strict" if strict else "Loose")]
    return "\n".join(md)


def emit(rows, strict, baseline_cmp=None):
    """Write the GitHub surfaces if we are inside a workflow."""
    for line in annotate(rows):
        print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(step_summary(rows, strict, baseline_cmp) + "\n")
        except OSError:
            pass
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        changed = sum(1 for r in rows if not r["ok"])
        try:
            with open(out, "a", encoding="utf-8") as f:
                f.write("replayed=%d\n" % len(rows))
                f.write("changed=%d\n" % changed)
                f.write("identical=%d\n" % (len(rows) - changed))
        except OSError:
            pass
