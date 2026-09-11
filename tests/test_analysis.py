# -*- coding: utf-8 -*-
"""The analyzer is not the agent, and a comparison must not confuse them.

Found while mapping a research report onto this repository, and it is a defect
in Orientim's own last change: TASK A made extraction stricter and bumped
`model.EXTRACTOR` to 2. A tool name reconstructed from a stream that never
closed used to be a witness; now it is not. Nothing about any recorded agent
moved, and yet:

    baseline (reading 1)     did_not_call:refund.issue   PASS
    current  (reading 2)     did_not_call:refund.issue   UNKNOWN

    comparison   weakened: a rule that held is no longer established
    gate         --gate protected exits 1

That sentence is false. The rule did not stop holding; this build stopped
being able to say. Six things are kept apart here, and the bug was collapsing
the middle four into one:

    current assessment      what this run says, now
    historical assessment   what the baseline file says
    analysis context        which analyzer said each of them
    comparability           whether those two contexts can be subtracted
    change classification   behaviour moved, or the analysis did
    gate disposition        what the build does about it

None of this makes a current FAIL quieter, and none of it makes a gate pass. A
rule failing now is a fact about now, and it blocks under the profile that
blocks on failures — with a reason that says what was established.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orientim import baselines, cases, ci, evaluate as ev

ROOT = "tests/_runs/analysis"

REFUND = "did_not_call:refund.issue"
STEPS = "max_steps:5"


def _older():
    """A baseline frozen before this build's extractor existed.

    Derived from the current context rather than written out, so a later
    version bump cannot silently make this fixture equal to the present.
    """
    now = ev.analysis()
    return dict(now, reading=now["reading"] - 1)


def _row(statuses, ok=True, verdict="IDENTICAL", case="support"):
    """A live row of a suite run, in the shape `cases.run_case` builds."""
    return {"case": case, "run_id": "r", "ok": ok, "verdict": verdict,
            "steps": 1, "replayed": verdict == "IDENTICAL",
            "evaluation": {"results": [
                {"evaluator": o.split(":", 1)[0], "obligation": o,
                 "status": s, "reason": ""} for o, s in statuses]}}


def _frozen(statuses, ok=True, case="support"):
    """A baseline row, in the shape `baselines.create` writes."""
    return {"case": case, "run_id": "r", "ok": ok, "verdict": "IDENTICAL",
            "steps": 1,
            "failed_evaluators": [o.split(":", 1)[0]
                                  for o, s in statuses if s == "fail"],
            "evaluators": {o.split(":", 1)[0]: s for o, s in statuses},
            "obligations": {o: s for o, s in statuses}}


def _base(rows, analysis=None):
    b = {"runs": rows}
    if analysis is not None:
        b["analysis"] = analysis
    return b


# --- the counterexample -------------------------------------------------------

def t_an_analyzer_upgrade_is_not_a_lost_proof():
    """The finding. PASS under the old extractor, UNKNOWN under the new one,
    and the same recording underneath. `weakened` says the protection was
    lost; what was lost is the ability to compare."""
    base = _base([_frozen([(REFUND, "pass")])], _older())
    cmp_ = ci.compare([_row([(REFUND, "warn")])], base, key="case")
    return (not cmp_.get("weakened")
            and cmp_.get("analysis_uncomparable") == {"support": [REFUND]}), (
        "weakened=%r analysis_uncomparable=%r"
        % (cmp_.get("weakened"), cmp_.get("analysis_uncomparable")))


def t_an_analyzer_upgrade_does_not_fail_the_build_on_a_lost_proof():
    """The gate half of the same movement. Nothing is failing, nothing was
    proven to have stopped holding, and `protected` has nothing to block on.

    This is the one direction the fix is allowed to soften, and only here: an
    UNKNOWN that was never shown to be a loss is not a finding."""
    base = _base([_frozen([(REFUND, "pass")])], _older())
    rows = [_row([(REFUND, "warn")])]
    cmp_ = ci.compare(rows, base, key="case")
    return ci.gate(cmp_, rows, profile=ci.PROTECTED)[0] == ci.EXIT_OK, (
        "an unattributed UNKNOWN failed the build: %r"
        % (ci.gate(cmp_, rows, profile=ci.PROTECTED),))


def t_an_analyzer_upgrade_is_not_a_proven_regression():
    """A case that passed in the baseline and fails now, where the only thing
    that failed is a rule this build reads differently. `newly_changed` means
    *started failing on this change*, and that has not been established."""
    base = _base([_frozen([(REFUND, "pass")], ok=True)], _older())
    cmp_ = ci.compare([_row([(REFUND, "fail")], ok=False)], base, key="case")
    return (cmp_["newly_changed"] == []
            and cmp_.get("analysis_changed") == ["support"]
            and not cmp_.get("new_failures")), (
        "newly_changed=%r analysis_changed=%r new_failures=%r"
        % (cmp_["newly_changed"], cmp_.get("analysis_changed"),
           cmp_.get("new_failures")))


def t_the_current_failure_still_fails_the_build():
    """And it must cost exactly what it cost before. The comparison lost the
    right to call it a regression; the run did not stop failing."""
    base = _base([_frozen([(REFUND, "pass")], ok=True)], _older())
    rows = [_row([(REFUND, "fail")], ok=False)]
    cmp_ = ci.compare(rows, base, key="case")
    legacy = ci.gate(cmp_, rows, profile=ci.LEGACY)
    protected = ci.gate(cmp_, rows, profile=ci.PROTECTED)
    return (legacy[0] == ci.EXIT_CHANGED
            and protected[0] == ci.EXIT_CHANGED), (
        "legacy=%r protected=%r" % (legacy, protected))


def t_the_reason_does_not_claim_the_agent_regressed():
    """What the build says when it blocks. A failure is a failure; "started
    failing on this change" is a claim about a comparison that did not
    happen."""
    base = _base([_frozen([(REFUND, "pass")], ok=True)], _older())
    rows = [_row([(REFUND, "fail")], ok=False)]
    said = " ".join(ci.gate(ci.compare(rows, base, key="case"), rows,
                            profile=ci.PROTECTED)[1]).lower()
    return ("started failing" not in said
            and "no longer established" not in said
            and "analy" in said), "the build said: %r" % (said,)


def t_a_rule_failing_now_still_blocks_the_protected_gate():
    """The other half, and the reason this is not a way to silence a build.

    Both runs are red for an unrelated reason, so no case verdict moves. A
    prohibition is failing now. `protected` blocks on it as a current fact,
    not as a comparison, and `legacy` still does not.
    """
    base = _base([_frozen([(STEPS, "fail"), (REFUND, "pass")], ok=False)],
                 _older())
    rows = [_row([(STEPS, "fail"), (REFUND, "fail")], ok=False)]
    cmp_ = ci.compare(rows, base, key="case")
    return (ci.gate(cmp_, rows, profile=ci.PROTECTED)[0] == ci.EXIT_CHANGED
            and ci.gate(cmp_, rows, profile=ci.LEGACY)[0] == ci.EXIT_OK
            and cmp_.get("analysis_uncomparable") == {"support": [REFUND]}), (
        "protected=%r legacy=%r cmp=%r"
        % (ci.gate(cmp_, rows, profile=ci.PROTECTED),
           ci.gate(cmp_, rows, profile=ci.LEGACY),
           {k: v for k, v in cmp_.items() if v}))


# --- negative controls: the same analyzer on both sides ------------------------

def t_a_real_regression_under_one_analyzer_still_blocks():
    """Required negative control. Nothing about the analyzer moved, so the
    comparison keeps every claim it had, and `protected` blocks on the new
    failure with the reason it always gave."""
    base = _base([_frozen([(REFUND, "pass")], ok=True)], ev.analysis())
    rows = [_row([(REFUND, "fail")], ok=False)]
    cmp_ = ci.compare(rows, base, key="case")
    code, reasons = ci.gate(cmp_, rows, profile=ci.PROTECTED)
    return (cmp_["newly_changed"] == ["support"]
            and cmp_.get("new_failures") == {"support": [REFUND]}
            and not cmp_.get("analysis_changed")
            and not cmp_.get("analysis_uncomparable")
            and code == ci.EXIT_CHANGED
            and any("not failing before" in r for r in reasons)), (
        "cmp=%r gate=%r" % ({k: v for k, v in cmp_.items() if v}, reasons))


def t_a_real_lost_proof_under_one_analyzer_still_blocks():
    """The same for PASS -> UNKNOWN. Under one analyzer that transition is
    about the run, and `protected` is the profile that treats it as a loss."""
    base = _base([_frozen([(REFUND, "pass")])], ev.analysis())
    rows = [_row([(REFUND, "warn")])]
    cmp_ = ci.compare(rows, base, key="case")
    return (cmp_.get("weakened") == {"support": [REFUND]}
            and ci.gate(cmp_, rows, profile=ci.PROTECTED)[0]
            == ci.EXIT_CHANGED), (
        "weakened=%r gate=%r" % (cmp_.get("weakened"),
                                 ci.gate(cmp_, rows, profile=ci.PROTECTED)))


def t_the_existing_comparison_is_unchanged_under_one_analyzer():
    """Negative control for the four keys that predate this. With one
    analyzer on both sides nothing here moves at all."""
    base = _base([_frozen([(STEPS, "pass")], ok=True, case="a"),
                  _frozen([(STEPS, "fail")], ok=False, case="b"),
                  _frozen([(STEPS, "pass")], ok=True, case="gone")],
                 ev.analysis())
    rows = [_row([(STEPS, "fail")], ok=False, case="a"),
            _row([(STEPS, "pass")], ok=True, case="b"),
            _row([(STEPS, "pass")], ok=True, case="new")]
    cmp_ = ci.compare(rows, base, key="case")
    return (cmp_["newly_changed"] == ["a"] and cmp_["fixed"] == ["b"]
            and cmp_["new_recordings"] == ["new"]
            and cmp_["missing_recordings"] == ["gone"]
            and not cmp_.get("analysis_changed")), (
        "the existing comparison moved: %r" % (cmp_,))


def t_a_quiet_run_under_one_analyzer_is_still_quiet():
    base = _base([_frozen([(STEPS, "pass")])], ev.analysis())
    cmp_ = ci.compare([_row([(STEPS, "pass")])], base, key="case")
    noisy = {k: v for k, v in cmp_.items()
             if v and k != "analysis"}
    return not noisy, "expected silence, got %r" % (noisy,)


# --- a baseline that says nothing about its analyzer ---------------------------

def t_a_baseline_without_an_analysis_context_is_not_assumed_to_match():
    """Every baseline written before this one has no stamp. The comparison
    must not read that as "the same analyzer" — which is the assumption that
    produced the finding in the first place."""
    base = _base([_frozen([(REFUND, "pass")])])
    cmp_ = ci.compare([_row([(REFUND, "warn")])], base, key="case")
    return (cmp_["analysis"]["comparable"] is False
            and cmp_["analysis"]["baseline"] is None
            and not cmp_.get("weakened")), (
        "analysis=%r weakened=%r"
        % (cmp_.get("analysis"), cmp_.get("weakened")))


def t_no_historical_analyzer_is_invented():
    """And nothing writes one in. An unstamped file stays unstamped: a
    baseline that never recorded its analyzer is not a baseline that recorded
    this one."""
    base = _base([_frozen([(REFUND, "pass")])])
    cmp_ = ci.compare([_row([(REFUND, "warn")])], base, key="case")
    return ("analysis" not in base
            and cmp_["analysis"]["baseline"] is None), (
        "baseline=%r reported=%r" % (base.get("analysis"),
                                     cmp_["analysis"]["baseline"]))


# --- what the analyzer cannot explain -----------------------------------------

def t_a_divergent_replay_is_still_attributed():
    """The scope of the doubt, and the reason it is narrow. A replay that
    diverged is a hash-chain fact: the requests moved. No version of the
    extractor or of an evaluator can produce or withdraw that, so the movement
    is attributed to the change even across analyzers."""
    base = _base([_frozen([(REFUND, "pass")], ok=True)], _older())
    rows = [_row([(REFUND, "pass")], ok=False, verdict="NEW_CALL")]
    cmp_ = ci.compare(rows, base, key="case")
    return (cmp_["newly_changed"] == ["support"]
            and not cmp_.get("analysis_changed")), (
        "newly_changed=%r analysis_changed=%r"
        % (cmp_["newly_changed"], cmp_.get("analysis_changed")))


def t_a_divergence_beside_a_failing_rule_is_still_attributed():
    """The same when both moved. The divergence alone explains the failure, so
    the case verdict is established whatever the rules say."""
    base = _base([_frozen([(REFUND, "pass")], ok=True)], _older())
    rows = [_row([(REFUND, "fail")], ok=False, verdict="NEW_CALL")]
    cmp_ = ci.compare(rows, base, key="case")
    return cmp_["newly_changed"] == ["support"], (
        "newly_changed=%r analysis_changed=%r"
        % (cmp_["newly_changed"], cmp_.get("analysis_changed")))


def t_a_dropped_obligation_survives_an_analyzer_change():
    """What is still comparable across analyzers: whether a rule is *there*.

    Which promises a suite makes comes from the case files, not from the
    extractor, so a rule the baseline checked and this suite does not is a
    fact about the suite either way.
    """
    base = _base([_frozen([(STEPS, "pass"), (REFUND, "pass")])], _older())
    cmp_ = ci.compare([_row([(STEPS, "pass")])], base, key="case")
    return cmp_.get("dropped_obligations") == {"support": [REFUND]}, (
        "dropped_obligations=%r" % (cmp_.get("dropped_obligations"),))


# --- the stamp itself ---------------------------------------------------------

def t_the_analysis_context_names_what_produces_a_status():
    ctx = ev.analysis()
    from orientim import model
    return (ctx["reading"] == model.EXTRACTOR
            and ctx["semantics"] == ev.SEMANTICS), "%r" % (ctx,)


def t_every_writer_stamps_the_analyzer():
    """A report and a baseline are both compared against later, so both carry
    it. A file that does not is the unstamped case above, forever."""
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    rows = [_row([(STEPS, "pass")])]
    baselines.create("main", rows, root=ROOT)
    frozen = baselines.load("main", ROOT)
    out = (frozen.get("analysis"),
           ci.report([], True, "m:f").get("analysis"),
           cases.report([], True).get("analysis"))
    shutil.rmtree(ROOT, ignore_errors=True)
    return all(a == ev.analysis() for a in out), "%r" % (out,)


def t_an_upgrade_in_place_still_fails_the_build():
    """The whole thing on disk, which is the shape a team meets it in: a
    baseline frozen by an older Orientim, a suite that fails today, and
    `baseline compare` — which exits on the same predicate the gate does,
    rather than on `newly_changed`, precisely so this keeps costing 1."""
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    p = baselines.create("main", [_row([(REFUND, "pass")])], root=ROOT)
    with open(p, encoding="utf-8") as f:
        frozen = json.load(f)
    frozen["analysis"] = _older()          # what an older build would have left
    with open(p, "w", encoding="utf-8") as f:
        json.dump(frozen, f)

    rows = [_row([(REFUND, "fail")], ok=False)]
    cmp_ = baselines.compare(rows, baselines.load("main", ROOT))
    said = baselines.describe(cmp_, frozen)
    shutil.rmtree(ROOT, ignore_errors=True)
    return (ci.gate(cmp_, rows, profile=ci.LEGACY)[0] == ci.EXIT_CHANGED
            and "started failing on this change" not in said
            and "Not compared" in said), (
        "gate=%r said=%r" % (ci.gate(cmp_, rows, profile=ci.LEGACY), said))


def t_a_baseline_frozen_now_compares_now():
    """The round trip, end to end: freeze with this build, compare with this
    build, and every claim is available again."""
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    baselines.create("main", [_row([(REFUND, "pass")])], root=ROOT)
    frozen = baselines.load("main", ROOT)
    cmp_ = baselines.compare([_row([(REFUND, "fail")], ok=False)], frozen)
    shutil.rmtree(ROOT, ignore_errors=True)
    return (cmp_["analysis"]["comparable"] is True
            and cmp_["newly_changed"] == ["support"]
            and cmp_.get("new_failures") == {"support": [REFUND]}), (
        "%r" % ({k: v for k, v in cmp_.items() if v},))
