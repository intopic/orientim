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

from . import evaluate, session, store


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


def _analysis_of(baseline):
    """The analyzer a baseline was written by, and whether it is ours.

    Returns (context or None, comparable). A file with no stamp is
    ANALYSIS CONTEXT UNKNOWN, never *the same as ours*: every baseline written
    before this existed has no stamp, and reading absence as agreement is the
    assumption that turned an extractor upgrade into a fleet of rules that had
    supposedly stopped holding. Nothing here infers a version from anything
    else in the file — a generation guessed from which keys are present is
    still a guess, and the whole point is not to invent a history.
    """
    frozen = baseline.get("analysis") if isinstance(baseline, dict) else None
    if not isinstance(frozen, dict) or not frozen:
        return None, False
    return frozen, frozen == evaluate.analysis()


def _fixture(row):
    """Which artifact a row was measured from, if the row says.

    Absent on every baseline written before artifact integrity existed, which
    is why its absence is declared rather than read as agreement.
    """
    if not isinstance(row, dict):
        return None
    return row.get("fixture_digest") or None


def _fixture_state(row, prev):
    """Whether these two rows measured the same artifact.

    Asked before any movement question, for the reason the refusal check is:
    two rows taken from two different recordings are not two measurements of
    one thing, and every movement word — fixed, started failing, still failing
    — would be a sentence about the agent that the evidence does not support.
    """
    now, before = _fixture(row), _fixture(prev)
    if now and before:
        return "same" if now == before else "changed"
    return "unknown"


def _refused(row):
    """Did the replay contract refuse to release this run's fixtures?

    A refusal is a harness outcome. The agent was handed nothing, so nothing
    about it was observed, and every claim this comparison can make is a claim
    about an agent. Read from the row the case layer wrote and from the
    verdict, so a row written by either generation answers the same.
    """
    if not isinstance(row, dict):
        return False
    return bool(row.get("refused")) or row.get("verdict") == "FIXTURE_REFUSED"


def _analyzer_could_explain(row):
    """Could this row's failure have come from the analyzer, not the run?

    Only through an evaluator: a status is the one thing in a row that an
    extractor version or an evaluator's semantics decides. A replay that
    diverged is a hash-chain fact over bytes captured once, so a row that
    failed *there* is attributable whatever analyzer read it — which is what
    keeps this from swallowing every regression a build has.
    """
    results = (row.get("evaluation") or {}).get("results") or []
    if not (any(r.get("status") == evaluate.FAIL for r in results)
            or row.get("failed_evaluators")):
        return False
    replayed = row.get("replayed")
    if replayed is None:
        # An older row, or one from `ci.replay_all`, that does not separate
        # the two halves. Take the verdict, and treat an absent one as no
        # evidence of divergence rather than as evidence of one.
        replayed = row.get("verdict") in (None, "IDENTICAL")
    return bool(replayed)


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
        # Which Orientim read this, so a later comparison can tell a run that
        # moved from an analyzer that did. Two integers, no identifiers.
        "analysis": evaluate.analysis(),
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

    **Two things can move between a baseline and a run, and only one of them
    is the agent.** The other is Orientim. Every claim here that subtracts two
    readings — a case that started failing, a rule that started failing, a
    proof that was lost — is a claim about the code under test, and it holds
    only while both sides were read by the same analyzer. When they were not,
    those movements are reported under `analysis_changed` and
    `analysis_uncomparable` instead, and `analysis` says which contexts were
    compared. Nothing about the current run is softened by this: a case that
    fails still fails, a rule that fails still fails, and `gate` still blocks
    on both. What changes is what the build is allowed to say happened.
    """
    was = {r[key]: r for r in baseline.get("runs", []) if key in r}
    frozen_ctx, comparable = _analysis_of(baseline)
    newly, fixed, still, added, new_failing = [], [], [], [], []
    new_failures, dropped, weakened, uncomparable = {}, {}, {}, {}
    analysis_changed, analysis_blurred, refused = [], {}, []
    fixture_changed, unestablished = [], set()
    for r in rows:
        k = r.get(key)
        prev = was.get(k)
        if prev is None:
            added.append(k)
            if _refused(r):
                # New, and refused. Not "new and already failing": nothing
                # about it failed, because nothing about it ran.
                refused.append(k)
            elif not r["ok"]:
                # New *and* already red. `new_recordings` alone says only that
                # nobody had it before, which reads like housekeeping.
                new_failing.append(k)
            continue
        # A run the replay contract refused measured nothing at all: the agent
        # was handed no fixture, so there is no behaviour here to have moved.
        # It is filed before any of the movement questions, because every one
        # of them would answer it with a sentence about the agent — and the
        # first of those sentences is "started failing on this change".
        if _refused(r) or _refused(prev):
            refused.append(k)
            continue
        # And the same question about the evidence rather than the contract:
        # if the two rows were measured from two different artifacts, the
        # comparison is between two things and not between two runs of one.
        state = _fixture_state(r, prev)
        if state == "changed":
            fixture_changed.append(k)
            continue
        if state == "unknown":
            # Declared, and the comparison still happens — every baseline
            # written before this carries no fixture identity, and turning all
            # of them into refusals would be a migration disguised as rigour.
            # What it must not do is read as "the same fixture, proven".
            #
            # Collected now and filtered at the end to the cases a movement
            # word was actually used about. On a case where nothing moved,
            # nothing was claimed, so there is nothing to qualify — and a
            # green suite against an old baseline stays quiet, which is the
            # difference between a declaration and noise.
            unestablished.add(k)
        # A verdict that moved, and who moved it. `newly_changed` means
        # *started failing on this change*; when the two sides were read by
        # different analyzers and the failure is one an analyzer can produce,
        # that sentence is not established — the case still fails, and the
        # movement is filed as the analysis moving rather than the agent.
        if not r["ok"] and prev["ok"]:
            (newly if comparable or not _analyzer_could_explain(r)
             else analysis_changed).append(k)
        elif r["ok"] and not prev["ok"]:
            (fixed if comparable or not _analyzer_could_explain(prev)
             else analysis_changed).append(k)
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
        fresh, lost, missing = [], [], []
        if level is OBLIGATIONS:
            fresh = sorted(n for n, st in now.items()
                           if st == "fail" and before.get(n) != "fail")
            lost = sorted(n for n, st in now.items()
                          if st == "warn" and before.get(n) == "pass")
            missing = sorted(n for n in before if n not in now)
        else:
            # A baseline from before obligations were compared. It is keyed by
            # evaluator name, so where this run has two rules of one type the
            # file cannot say which of them held — and picking one would be
            # inventing a history. Those types are reported as uncomparable
            # and left out of every other answer; the rest compare normally.
            here = _by_evaluator(now)
            # Names this row cannot speak for: two rules of one type against a
            # baseline that only recorded the type, plus anything already
            # blurred. They are left out of every claim below — a rule we
            # cannot identify is not a rule that was dropped, and saying so
            # would trade one false certainty for another.
            ambiguous = sorted(set(blurred) | {n for n, sts in here.items()
                                               if len(sts) > 1 and n in before})
            if ambiguous:
                uncomparable[k] = ambiguous
            fresh = sorted(n for n, st in now.items()
                           if st == "fail"
                           and n.split(":", 1)[0] not in ambiguous
                           and before.get(n.split(":", 1)[0]) != "fail")
            if level is not FAILURES_ONLY:
                # `here` is keyed by evaluator name for this comparison, so a
                # baseline name still present under any obligation is not
                # missing. With only failures recorded, a name that is absent
                # was not *failing* — which is not the same as having been
                # checked, so neither of these is answerable from that file.
                missing = sorted(n for n in before
                                 if n not in ambiguous and n not in here)
                lost = sorted(n for n, st in now.items()
                              if st == "warn"
                              and n.split(":", 1)[0] not in ambiguous
                              and before.get(n.split(":", 1)[0]) == "pass")

        # Which of those this comparison is entitled to call a movement. A
        # status is a subtraction of two readings, and it only means something
        # when both sides were read the same way; when they were not, the pair
        # is reported as one this comparison cannot attribute — never as a
        # rule that started failing, and never as a proof that was lost.
        #
        # `missing` is exempt on purpose. Whether a rule is *there* comes from
        # the case file rather than from the extractor, so a promise the
        # baseline checked and this suite does not is a fact about the suite
        # under any analyzer.
        if missing:
            dropped[k] = missing
        if comparable:
            if fresh:
                new_failures[k] = fresh
            if lost:
                weakened[k] = lost
        elif fresh or lost:
            analysis_blurred[k] = sorted(set(fresh) | set(lost))
    seen = {r.get(key) for r in rows}
    gone = [k for k in was
            if k not in seen and (scope is None or k in scope)]
    # Only where a movement claim was made about the case. Anywhere else the
    # missing identity qualifies a sentence nobody said.
    claimed = set(newly) | set(fixed) | set(still) | set(new_failing)
    claimed |= set(new_failures) | set(weakened)
    fixture_unknown = sorted(unestablished & claimed)
    return {"newly_changed": newly, "fixed": fixed,
            "still_changed": still, "new_recordings": added,
            "missing_recordings": gone, "new_failing": new_failing,
            "new_failures": new_failures, "dropped_obligations": dropped,
            "weakened": weakened, "legacy_uncomparable": uncomparable,
            "analysis_changed": analysis_changed,
            "analysis_uncomparable": analysis_blurred,
            "refused": refused,
            "fixture_changed": fixture_changed,
            "fixture_unknown": fixture_unknown,
            "analysis": {"current": evaluate.analysis(),
                         "baseline": frozen_ctx, "comparable": comparable}}


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


def _failing_now(rows):
    """key -> the obligations failing in this run. A current fact, no history.

    Keyed under both identifiers a row carries, because `compare` is called
    with `run_id` by one caller and `case` by the other and a gate should not
    have to be told which.
    """
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        names = {n for n, st in _statuses(r).items() if st == evaluate.FAIL}
        for k in (r.get("case"), r.get("run_id")):
            if k:
                out[k] = names
    return out


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

    **A changed analyzer is not a way past either profile.** Where the
    comparison could not attribute a movement, what is left is a plain current
    fact — this case fails, this rule fails — and a current failure costs the
    build exactly what it cost before, under a reason that says only what was
    established. The single thing that stops blocking is a PASS to UNKNOWN
    across differing analyzers: nothing is failing there and no loss was ever
    shown, and failing a build on that is how a tool teaches people to turn it
    off.
    """
    if profile not in PROFILES:
        raise ValueError("unknown gate profile %r; known profiles are %s"
                         % (profile, ", ".join(PROFILES)))
    reasons = []
    if cmp_:
        if cmp_.get("newly_changed"):
            reasons.append("cases that started failing: "
                           + ", ".join(cmp_["newly_changed"]))
        failing = _failing_now(rows)
        red = set()
        for r in rows or []:
            if isinstance(r, dict) and not r.get("ok"):
                red.update(v for v in (r.get("case"), r.get("run_id")) if v)
        blocked_refusals = sorted(k for k in (cmp_.get("refused") or []))
        if blocked_refusals:
            # Blocks, and says what it is. A refused fixture means the run
            # established nothing, and a build that goes green on a case that
            # never ran is the worst outcome available here. What it must not
            # say is that the agent did anything: it was handed nothing.
            reasons.append("the replay contract refused these fixtures, so "
                           "nothing about the agent was measured: "
                           + ", ".join(blocked_refusals))
        moved = sorted(cmp_.get("fixture_changed") or [])
        if moved:
            # Blocks, and says what it is. A different artifact under the same
            # case is not a different agent: nothing here claims the agent
            # moved, only that these two rows cannot be compared.
            reasons.append("these cases were measured from a different "
                           "recording than the baseline was, so the two are "
                           "not comparable: " + ", ".join(moved))
        stale = sorted(k for k in (cmp_.get("analysis_changed") or [])
                       if k in red)
        if stale:
            reasons.append("cases failing now, against a baseline this "
                           "analyzer cannot be subtracted from: "
                           + ", ".join(stale))
        if profile == PROTECTED:
            for key, why in _PROTECTION_LOST:
                moved = cmp_.get(key) or {}
                if moved:
                    reasons.append("%s (%s)" % (
                        why, "; ".join("%s: %s" % (k, ", ".join(v))
                                       for k, v in sorted(moved.items()))))
            # The same rule one level down. A prohibition that is failing now
            # blocks whether or not its history can be read; a rule that only
            # stopped being provable does not, because that is the movement
            # this comparison just said it cannot attribute.
            blocked = {k: [n for n in names if n in failing.get(k, ())]
                       for k, names in
                       (cmp_.get("analysis_uncomparable") or {}).items()}
            blocked = {k: v for k, v in blocked.items() if v}
            if blocked:
                reasons.append(
                    "a rule is failing now, and this baseline cannot say "
                    "whether it failed before (%s)"
                    % "; ".join("%s: %s" % (k, ", ".join(v))
                                for k, v in sorted(blocked.items())))
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
    if cmp_.get("refused"):
        out.append("  Fixtures the replay contract refused (nothing about the "
                   "agent was measured): " + ", ".join(cmp_["refused"]))
    if cmp_.get("fixture_unknown"):
        out.append("  Fixture identity not established (this baseline "
                   "predates it, so the comparison assumes nothing): "
                   + ", ".join(cmp_["fixture_unknown"]))
    for key, label in (("fixture_changed",
                        "Measured from a different recording than the "
                        "baseline (not a comparison)"),
                       ("new_failures", "Rules that started failing"),
                       ("dropped_obligations", "In the baseline, not checked now"),
                       ("weakened", "Lost their proof (pass to unknown)"),
                       ("analysis_uncomparable",
                        "Read differently than the baseline read them "
                        "(the analyzer moved, so this is not a comparison)"),
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


def analysis_lines(cmp_):
    """Why a comparison is holding back, when it is.

    One line, and only when it changes what the rest of the output means: the
    build is being read by a different Orientim than the one that wrote the
    baseline, so the movements below are the analysis moving and not a claim
    about the agent. The remedy is a sentence long, so it is in the sentence.
    """
    a = cmp_.get("analysis") or {}
    if not a or a.get("comparable"):
        return []
    was, now = a.get("baseline"), a.get("current") or {}
    said = ("this baseline does not record which analyzer wrote it"
            if not was else
            "the baseline was written by %s, this build is %s"
            % (_ctx(was), _ctx(now)))
    L = ["  Not compared: %s." % said]
    if cmp_.get("analysis_changed"):
        L.append("  Moved, but not attributed to this change: "
                 + ", ".join(cmp_["analysis_changed"]))
    L.append("  Re-freeze the baseline to compare rule by rule again.")
    return L


def _ctx(ctx):
    return " ".join("%s %s" % (k, v) for k, v in sorted((ctx or {}).items()))


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
        L += obligation_lines(b) + analysis_lines(b)
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
