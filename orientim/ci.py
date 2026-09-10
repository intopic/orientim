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

# How much of the obligation history a baseline file can answer for. Older
# files are not wrong, they are less detailed, and the comparison gives each
# one the answers it supports rather than guessing the rest.
OBLIGATIONS = "obligations"          # every promise and what it said
EVALUATORS_ONLY = "evaluators"       # keyed by evaluator name
FAILURES_ONLY = "failed_evaluators"  # only what was failing

# Two results under one identity, disagreeing. Never a status a rule returned:
# a marker that this row cannot say what the rule said.
AMBIGUOUS = "ambiguous"

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
    """obligation -> status, for one live row of a suite run.

    Keyed by the obligation, not the evaluator. Two `did_not_call` rules over
    different tools are two promises; keyed by name the second overwrote the
    first, so a new violation of one prohibition vanished into a case that was
    already failing for an unrelated reason — and which one survived depended
    on the order the rules happened to be declared in.
    """
    results = (row.get("evaluation") or {}).get("results") or []
    out = {}
    for r in results:
        key = r.get("obligation") or r.get("evaluator")
        if not key:
            continue
        if key in out and out[key] != r.get("status"):
            # Two results claiming one identity and disagreeing. With
            # obligations stamped this means a row from an older writer that
            # only had evaluator names, and last-write-wins would make the
            # answer depend on declaration order. Say ambiguous instead.
            out[key] = AMBIGUOUS
        elif key not in out:
            out[key] = r.get("status")
    return out


def _frozen_statuses(prev):
    """The same, out of a baseline row, plus how much it can answer.

    Three generations of baseline, and each supports fewer questions than the
    last one written:

    `obligations`        every promise and what it said. Everything works.
    `evaluators`         keyed by evaluator name, so obligations of the same
                         type were already collapsed when it was written.
    `failed_evaluators`  only the failures. A name absent from it was not
                         failing, which still separates a new failure from a
                         known one.

    Returns (map, level). Nothing here reconstructs a historical PASS that the
    file does not contain: a rule that was never recorded is not a rule that
    passed, and inventing the difference is how a comparison starts lying about
    the past.
    """
    full = prev.get("obligations")
    if isinstance(full, dict) and full:
        return dict(full), OBLIGATIONS
    named = prev.get("evaluators")
    if isinstance(named, dict) and named:
        return dict(named), EVALUATORS_ONLY
    return ({n: "fail" for n in (prev.get("failed_evaluators") or [])},
            FAILURES_ONLY)


def _by_evaluator(statuses):
    """obligation-keyed -> evaluator-keyed, keeping the multiplicity count."""
    out = {}
    for key, status in statuses.items():
        name = key.split(":", 1)[0]
        out.setdefault(name, []).append(status)
    return out


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
    new_failures, dropped, weakened, uncomparable = {}, {}, {}, {}
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
        before, level = _frozen_statuses(prev)
        blurred = sorted(n for n, st in now.items() if st is AMBIGUOUS)
        if blurred:
            uncomparable[k] = blurred
            now = {n: st for n, st in now.items() if st is not AMBIGUOUS}
        if level is OBLIGATIONS:
            fresh = sorted(n for n, st in now.items()
                           if st == "fail" and before.get(n) != "fail")
            if fresh:
                new_failures[k] = fresh
            missing = sorted(n for n in before if n not in now)
            if missing:
                dropped[k] = missing
            lost = sorted(n for n, st in now.items()
                          if st == "warn" and before.get(n) == "pass")
            if lost:
                weakened[k] = lost
            continue

        # A baseline from before obligations were compared. It is keyed by
        # evaluator name, so where this run has two rules of one type the file
        # cannot say which of them held — and picking one would be inventing a
        # history. Those types are reported as uncomparable and left out of
        # every other answer; the rest compare normally.
        here = _by_evaluator(now)
        # Names this row cannot speak for: two rules of one type against a
        # baseline that only recorded the type, plus anything already blurred.
        # They are left out of every claim below — a rule we cannot identify is
        # not a rule that was dropped, and saying so would trade one false
        # certainty for another.
        ambiguous = sorted(set(blurred) | {n for n, sts in here.items()
                                           if len(sts) > 1 and n in before})
        if ambiguous:
            uncomparable[k] = ambiguous
        fresh = sorted(n for n, st in now.items()
                       if st == "fail"
                       and n.split(":", 1)[0] not in ambiguous
                       and before.get(n.split(":", 1)[0]) != "fail")
        if fresh:
            new_failures[k] = fresh
        if level is FAILURES_ONLY:
            # Only failures were recorded, so a name that is absent was not
            # failing — it was not necessarily *checked*. A dropped obligation
            # and a lost proof are both unanswerable from that.
            continue
        missing = sorted(n for n in before
                         if n not in ambiguous and n not in here)
        # `here` is keyed by evaluator name for this comparison, so a baseline
        # name still present under any obligation is not missing.
        if missing:
            dropped[k] = missing
        lost = sorted(n for n, st in now.items()
                      if st == "warn"
                      and n.split(":", 1)[0] not in ambiguous
                      and before.get(n.split(":", 1)[0]) == "pass")
        if lost:
            weakened[k] = lost
    seen = {r.get(key) for r in rows}
    gone = [k for k in was
            if k not in seen and (scope is None or k in scope)]
    return {"newly_changed": newly, "fixed": fixed,
            "still_changed": still, "new_recordings": added,
            "missing_recordings": gone, "new_failing": new_failing,
            "new_failures": new_failures, "dropped_obligations": dropped,
            "weakened": weakened, "legacy_uncomparable": uncomparable}


LEGACY = "legacy"        # what every build has today
PROTECTED = "protected"  # obligation-level protection losses fail
PROFILES = (LEGACY, PROTECTED)

# What the protected profile treats as losing a protection. Each is a movement
# a case verdict cannot carry, and each is a *loss* rather than a question:
# an obligation that was never established does not appear in any of them, so
# this is not "every UNKNOWN fails the build".
_PROTECTION_LOST = (
    ("new_failures", "a rule that was not failing before is failing now"),
    ("weakened", "a rule that held is no longer established"),
    ("dropped_obligations", "a rule the baseline checked is gone"),
)


def gate(cmp_, rows, profile=LEGACY):
    """(exit code, reasons) — what this comparison costs the build.

    Two profiles, both explicit, neither silent. `legacy` is what every build
    running today already does: with a baseline, only a case that *started*
    failing fails the build, so a new violation inside an already-red case is
    reported and does not block. That behaviour is not a bug to be quietly
    corrected out from under people — it is a policy, and it keeps working
    under a name.

    `protected` blocks on obligation-level protection losses as well. Truth and
    disposition stay separate: this decides what a build does about a finding,
    never what the finding is.
    """
    if profile not in PROFILES:
        raise ValueError("unknown gate profile %r; known profiles are %s"
                         % (profile, ", ".join(PROFILES)))
    reasons = []
    if cmp_:
        if cmp_.get("newly_changed"):
            reasons.append("cases that started failing: "
                           + ", ".join(cmp_["newly_changed"]))
        if profile == PROTECTED:
            for key, why in _PROTECTION_LOST:
                moved = cmp_.get(key) or {}
                if moved:
                    reasons.append("%s (%s)" % (
                        why, "; ".join("%s: %s" % (k, ", ".join(v))
                                       for k, v in sorted(moved.items()))))
            if cmp_.get("new_failing"):
                reasons.append("new and already failing: "
                               + ", ".join(cmp_["new_failing"]))
    else:
        failing = [r for r in rows or [] if not r.get("ok")]
        if failing:
            reasons.append("%d failing case(s)" % len(failing))
    return (EXIT_CHANGED if reasons else EXIT_OK), reasons


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
                       ("weakened", "Lost their proof (pass to unknown)"),
                       ("legacy_uncomparable",
                        "Not comparable to this baseline (it predates "
                        "per-rule identity)")):
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
