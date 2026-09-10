# -*- coding: utf-8 -*-
"""Stability measurement: a headline feature that had no tests at all.

203 lines with its own CLI command, named in the README, and nothing exercised
it. The first thing writing these found was `measure(runs=0)` raising
IndexError — a crash in the middle of a command somebody ran to find out
whether their agent was stable.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orientim import stability

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/stability"


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _measure(fn, runs=6):
    return stability.measure(fn, runs=runs, root=ROOT)


# --- the two shapes it exists to tell apart -----------------------------------

def t_deterministic_agent_is_one_path():
    """Same calls every time: one path, full agreement, nothing out of control."""
    _fresh()

    def steady(h):
        h.client().post(B + "/search", content=b'{"q":1}')
        return "always the same"

    m = _measure(steady)
    ok = (m["runs"] == 6 and m["failed"] == 0
          and m["paths"]["distinct"] == 1
          and m["paths"]["agreement"] == 1.0
          and m["outcomes"]["distinct"] == 1)
    return ok, ("%d runs, %d path(s), agreement %.2f"
                % (m["runs"], m["paths"]["distinct"], m["paths"]["agreement"]))


def t_branching_agent_shows_the_branches():
    """An agent that takes a second call half the time has two paths.

    The branch is chosen by the run index rather than by chance, so the
    expected number of distinct paths is a fact and not a probability.
    """
    _fresh()
    state = {"n": 0}

    def branching(h):
        c = h.client()
        c.post(B + "/search", content=b'{"q":1}')
        state["n"] += 1
        if state["n"] % 2 == 0:
            c.post(B + "/chat-stable", content=b"{}")
            return "two calls"
        return "one call"

    m = _measure(branching, runs=6)
    counts = sorted(m["paths"]["counter"].values())
    ok = (m["paths"]["distinct"] == 2 and counts == [3, 3]
          and m["outcomes"]["distinct"] == 2
          and abs(m["paths"]["agreement"] - 0.5) < 1e-9)
    return ok, ("%d path(s), split %r, agreement %.2f"
                % (m["paths"]["distinct"], counts, m["paths"]["agreement"]))


def t_step_counts_and_paths_agree():
    """Two paths of different lengths must show up in the step counts too."""
    _fresh()
    state = {"n": 0}

    def branching(h):
        c = h.client()
        c.post(B + "/search", content=b'{"q":1}')
        state["n"] += 1
        if state["n"] > 2:
            c.post(B + "/chat-stable", content=b"{}")

    m = _measure(branching, runs=4)
    vals = sorted(m["steps"]["vals"])
    return (vals == [1.0, 1.0, 2.0, 2.0]
            and 1.0 < m["steps"]["mean"] < 2.0), "step counts %r" % (vals,)


# --- the statistics -----------------------------------------------------------

def t_control_limits_follow_the_individuals_chart():
    """UCL = mean + 2.66 * MRbar, LCL the same below, floored at zero.

    Checked against the formula by hand rather than against the code, so a
    change to the constant is caught rather than absorbed.
    """
    vals = [2.0, 4.0, 4.0, 6.0]
    mean, lcl, ucl = stability._limits(vals)
    mrbar = (2.0 + 0.0 + 2.0) / 3.0
    want_mean, want_ucl = 4.0, 4.0 + 2.66 * mrbar
    want_lcl = max(4.0 - 2.66 * mrbar, 0.0)
    ok = (abs(mean - want_mean) < 1e-9 and abs(ucl - want_ucl) < 1e-9
          and abs(lcl - want_lcl) < 1e-9)
    return ok, "mean=%.3f lcl=%.3f ucl=%.3f (mrbar=%.3f)" % (mean, lcl, ucl, mrbar)


def t_control_limits_never_go_below_zero():
    """A step count cannot be negative, so neither can its lower limit."""
    _mean, lcl, _ucl = stability._limits([1.0, 9.0, 1.0, 9.0])
    return lcl == 0.0, "lcl %.3f" % lcl


def t_control_limits_of_a_constant_series_are_the_mean():
    mean, lcl, ucl = stability._limits([3.0] * 5)
    return (mean, lcl, ucl) == (3.0, 3.0, 3.0), "limits %r" % ((mean, lcl, ucl),)


def t_control_limits_need_two_points():
    return stability._limits([5.0]) == (0.0, 0.0, 0.0) \
        and stability._limits([]) == (0.0, 0.0, 0.0), \
        "a single point has no moving range"


def t_out_of_control_points_are_identified():
    """A run outside the spread must be named, not averaged away."""
    _fresh()
    state = {"n": 0}

    def mostly_steady(h):
        c = h.client()
        state["n"] += 1
        n = 3 if state["n"] == 8 else 1
        for _ in range(n):
            c.post(B + "/search", content=b'{"q":1}')

    m = _measure(mostly_steady, runs=8)
    flagged = [r["steps"] for r in m["out_of_control"]]
    return (3 in flagged), "flagged step counts %r" % (flagged,)


def t_a_lone_extreme_outlier_can_hide_inside_its_own_limits():
    """A property of the individuals chart, asserted so nobody rediscovers it.

    One extreme point contributes TWO large moving ranges - the step up and the
    step back down - which inflates MRbar enough that the point falls inside
    the limits it just widened. Seven runs of one step and one run of six:

        mean 1.83, MRbar 2.00, UCL 7.15 - and the six is inside it.

    This is how the chart behaves, not a defect, and it is why `agreement` and
    `distinct` paths are the headline numbers rather than the limits. Recorded
    here and in docs/limits.md so it is a known limit rather than a surprise.
    """
    mean, lcl, ucl = stability._limits([1.0, 1.0, 1.0, 6.0, 1.0, 1.0])
    return (6.0 <= ucl and lcl == 0.0), \
        "mean=%.2f ucl=%.2f, so the lone 6 is inside" % (mean, ucl)


def t_loose_outcomes_ignore_numbers():
    """"same answer, different number" is the cheap equivalence it claims."""
    a = stability._normalize("Order 4471 shipped   on Monday")
    b = stability._normalize("order 9999 SHIPPED on monday")
    return a == b, "%r vs %r" % (a, b)


# --- failure -----------------------------------------------------------------

def t_a_failing_run_is_counted_not_hidden():
    """An agent that raises is a data point, and an honest one."""
    _fresh()
    state = {"n": 0}

    def sometimes_raises(h):
        h.client().post(B + "/search", content=b'{"q":1}')
        state["n"] += 1
        if state["n"] % 3 == 0:
            raise ValueError("bad day")
        return "fine"

    m = _measure(sometimes_raises, runs=6)
    errors = [o for o in m["outcomes"]["counter"] if "error" in str(o)]
    ok = (m["failed"] == 2 and m["runs"] == 6 and errors
          and m["outcomes"]["agreement"] < 1.0)
    return ok, "%d failed of %d, outcomes %r" % (
        m["failed"], m["runs"], sorted(m["outcomes"]["counter"])[:2])


def t_a_record_failure_does_not_double_count():
    """If record() itself fails there is no holder, and the previous run's
    must not be measured a second time.

    The guard is `if h is None: continue`, and this is what proves it: two of
    six runs never produce a holder, so six requested runs yield four results
    and the counts stay honest.
    """
    _fresh()
    real_record = stability.session.record
    state = {"n": 0}

    def flaky_record(*a, **kw):
        state["n"] += 1
        if state["n"] in (2, 5):
            raise RuntimeError("record() itself failed")
        return real_record(*a, **kw)

    stability.session.record = flaky_record
    try:
        m = stability.measure(
            lambda h: h.client().post(B + "/search", content=b'{"q":1}'),
            runs=6, root=ROOT)
    finally:
        stability.session.record = real_record

    ok = (m["requested"] == 6 and m["runs"] == 4 and m["failed"] == 2
          and len(m["results"]) == 4
          and len({r["run_id"] for r in m["results"]}) == 4)
    return ok, "requested %d, measured %d, failed %d, distinct run ids %d" % (
        m["requested"], m["runs"], m["failed"],
        len({r["run_id"] for r in m["results"]}))


def t_nothing_to_measure_does_not_crash():
    """runs=0, and the case where every attempt fails before it starts.

    This raised IndexError before: Counter.most_common(1)[0] on an empty
    sequence, inside a command whose whole purpose is to report on shaky runs.
    """
    _fresh()
    m = stability.measure(lambda h: None, runs=0, root=ROOT)
    text = stability.report(m, "x:y")
    ok = (m["runs"] == 0 and m["paths"]["distinct"] == 0
          and m["paths"]["agreement"] == 0.0 and m["paths"]["modal"] is None
          and "0 runs" in text)
    return ok, "runs=%d, report rendered %d chars" % (m["runs"], len(text))


def t_a_single_run_reports_without_limits():
    _fresh()
    m = _measure(lambda h: h.client().post(B + "/search", content=b'{"q":1}'),
                 runs=1)
    return (m["runs"] == 1 and m["steps"]["ucl"] == 0.0
            and m["paths"]["agreement"] == 1.0), \
        "one run, limits %r" % (m["steps"]["ucl"],)


# --- output -------------------------------------------------------------------

def t_report_says_what_it_measured():
    _fresh()
    m = _measure(lambda h: h.client().post(B + "/search", content=b'{"q":1}'),
                 runs=3)
    text = stability.report(m, "myapp.agent:run")
    return ("myapp.agent:run" in text and "3 runs" in text), \
        "report head: %r" % text.splitlines()[2][:70]


def t_every_result_points_at_its_recording():
    """A run in the chart has to be openable, or the chart is a dead end."""
    _fresh()
    m = _measure(lambda h: h.client().post(B + "/search", content=b'{"q":1}'),
                 runs=3)
    paths = [r["path"] for r in m["results"]]
    return (len(paths) == 3 and all(p and os.path.exists(p) for p in paths)), \
        "recordings on disk: %r" % ([bool(p and os.path.exists(p)) for p in paths],)
