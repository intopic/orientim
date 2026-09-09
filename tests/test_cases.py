# -*- coding: utf-8 -*-
"""Cases, baselines and `orientim test` — every command and the whole workflow.

The CLI checks drive `cli.main([...])` and read stdout and the exit code,
because those two are the contract: a build reads the exit code and a person
reads the output. Assertions are on the lines somebody depends on, not on
formatting, which should stay free to change.
"""
import contextlib
import io as _io
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import baselines, cases, ci, cli

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/cases"

# The agent under test lives here, so a case can name it as module:function.
#
# Which version runs is an environment variable, not a module global, and that
# is not incidental: `orientim test` resolves module:function through the import
# system, so it gets its own copy of this module. A global here would be
# invisible to it, and a check that flipped one would quietly run the same
# version twice and conclude nothing had changed — a test that passes by
# testing nothing.
VERSION_ENV = "ORIENTIM_TEST_AGENT_VERSION"


def _version():
    return int(os.environ.get(VERSION_ENV, "1"))


@contextlib.contextmanager
def _as_version(n):
    """Stand in for somebody changing the code between two runs of the suite."""
    before = os.environ.get(VERSION_ENV)
    os.environ[VERSION_ENV] = str(n)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(VERSION_ENV, None)
        else:
            os.environ[VERSION_ENV] = before


def _model(c, wants):
    return c.post(B + "/v1/chat/completions", content=json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "where is 4471"}],
        "n_tools": len(wants), "which": wants,
    }).encode())


def agent(run):
    """A two-step agent whose second step depends on VERSION."""
    c = run.client()
    _model(c, ["lookup_order"])
    c.post(B + "/search", content=b'{"q":"4471"}')
    if _version() == 1:
        run.output = "order 4471 not found"
        return run.output
    c.post(B + "/send-email", content=b'{"to":"customer"}')
    run.output = "your order shipped"
    return run.output


def quiet_agent(run):
    """The same traffic, declaring no output.

    A case evaluates the replay, so a run with nothing to compare has to be one
    where the *replayed* function declares nothing — clearing it on the
    recording afterwards proves nothing.
    """
    c = run.client()
    _model(c, ["lookup_order"])
    c.post(B + "/search", content=b'{"q":"4471"}')


_AGENTS = {"agent": agent, "quiet_agent": quiet_agent}


def _load_agent(entry):
    return _AGENTS[(entry or "").rsplit(":", 1)[-1] or "agent"]


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    os.environ.pop(VERSION_ENV, None)


def _record():
    with orientim.record(root=ROOT, always=True, agent="cases-fixture") as h:
        agent(h)
    return h.path


def _suite(strict=True):
    return [cases.run(c, strict=strict, entry_loader=_load_agent)
            for c in cases.list_cases(ROOT)]


@contextlib.contextmanager
def _cli(argv):
    """Run the CLI, capture stdout, and return (out, exit_code)."""
    buf = _io.StringIO()
    box = {"code": 0}
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(argv)
    except SystemExit as e:
        box["code"] = e.code if isinstance(e.code, int) else 1
    box["out"] = buf.getvalue()
    yield box


# --- the case file ------------------------------------------------------------

def t_case_save_and_load():
    _fresh()
    path = _record()
    cases.save("basic", path, "tests.test_cases:agent", root=ROOT,
               description="the happy path",
               expect={"used_tool": ["lookup_order"], "max_steps": 4},
               tags={"team": "support"})
    c = cases.load("basic", ROOT)
    ok = (c["name"] == "basic" and c["entry"] == "tests.test_cases:agent"
          and c["recording"] == path and c["expect"]["max_steps"] == 4
          and c["tags"]["team"] == "support" and c["format"] == cases.FORMAT
          and c["run_id"])
    return ok, "stored %r" % ({k: c.get(k) for k in
                               ("name", "entry", "run_id", "expect")},)


def t_case_save_validates_before_writing():
    """A case that fails in CI is worse than one that refuses to save."""
    _fresh()
    path = _record()
    bad = []
    for kw, why in (
        (dict(recording="nope.jsonl", entry="a:b"), "missing recording"),
        (dict(recording=path, entry="no-colon"), "bad entry"),
        (dict(recording=path, entry="a:b", expect={"used_tols": ["x"]}),
         "typo in expectations"),
    ):
        try:
            cases.save("v", root=ROOT, **kw)
            bad.append(why)
        except (cases.CaseError, ValueError):
            pass
    wrote = os.path.exists(cases.path_for("v", ROOT))
    return (not bad and not wrote), \
        "accepted %r, wrote a file=%s" % (bad, wrote)


def t_case_name_is_checked():
    """A case name becomes a file name, so it is checked rather than trusted."""
    _fresh()
    path = _record()
    for name in ("../escape", "a/b", "", "x" * 80, "-leading"):
        try:
            cases.save(name, path, "a:b", root=ROOT)
            return False, "accepted the name %r" % (name,)
        except cases.CaseError:
            pass
    return True, "five unusable names refused"


def t_case_list_and_delete():
    _fresh()
    path = _record()
    for n in ("alpha", "beta"):
        cases.save(n, path, "tests.test_cases:agent", root=ROOT)
    names = [c["name"] for c in cases.list_cases(ROOT)]
    cases.delete("alpha", ROOT)
    after = [c["name"] for c in cases.list_cases(ROOT)]
    try:
        cases.delete("alpha", ROOT)
        gone_twice = False
    except cases.CaseError:
        gone_twice = True
    return (names == ["alpha", "beta"] and after == ["beta"] and gone_twice), \
        "before %r, after %r" % (names, after)


def t_broken_case_is_reported_not_skipped():
    """A case file we cannot read must not silently disappear from the suite."""
    _fresh()
    path = _record()
    cases.save("fine", path, "tests.test_cases:agent", root=ROOT)
    os.makedirs(os.path.join(ROOT, cases.DIRNAME), exist_ok=True)
    with open(os.path.join(ROOT, cases.DIRNAME, "broken.json"), "w",
              encoding="utf-8") as f:
        f.write("{not json")
    rows = cases.list_cases(ROOT)
    broken = [c for c in rows if c.get("broken")]
    result = cases.run(broken[0], entry_loader=_load_agent) if broken else None
    return (len(rows) == 2 and len(broken) == 1 and result
            and not result["ok"] and result["verdict"] == "CASE_ERROR"), \
        "listed %d, broken %d" % (len(rows), len(broken))


# --- running a case -----------------------------------------------------------

def t_case_run_passes_when_nothing_moved():
    _fresh()
    path = _record()
    cases.save("green", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"],
                       "output_matches": "not found",
                       "max_steps": 4, "no_step_failed": True})
    row = cases.run(cases.load("green", ROOT), entry_loader=_load_agent)
    ev = row["evaluation"]
    return (row["ok"] and row["verdict"] == "IDENTICAL" and ev["failed"] == 0
            and ev["passed"] == 4), "row %r" % (
        {k: row[k] for k in ("ok", "verdict", "reason")},)


def t_case_fails_on_evaluation_alone():
    """A faithful replay of an agent that broke a promise is not a pass."""
    _fresh()
    path = _record()
    cases.save("promise", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})     # never requested
    row = cases.run(cases.load("promise", ROOT), entry_loader=_load_agent)
    return (row["verdict"] == "IDENTICAL" and not row["ok"]
            and row["evaluation"]["failed"] == 1
            and "refund_order" in row["reason"]), \
        "verdict %s, ok=%s, reason %r" % (row["verdict"], row["ok"],
                                          row["reason"])


def t_case_fails_on_replay_alone():
    """And a run that satisfies every rule while its traffic changed is not one."""
    _fresh()
    path = _record()
    cases.save("traffic", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    with _as_version(2):                   # the code now makes an extra call
        row = cases.run(cases.load("traffic", ROOT), entry_loader=_load_agent)
    return (not row["ok"] and row["verdict"] != "IDENTICAL"
            and row["evaluation"]["failed"] == 0), \
        "verdict %s, evaluation failed=%d" % (
            row["verdict"], row["evaluation"]["failed"])


def t_case_evaluates_the_replay_not_the_recording():
    """The point of a case is the present.

    The recording is of v1. The evaluation must describe what the code does
    now, so a rule about the *current* run has to see the current run.
    """
    _fresh()
    path = _record()
    cases.save("now", path, "tests.test_cases:agent", root=ROOT,
               expect={"output_matches": "not found"})
    with _as_version(2):
        row = cases.run(cases.load("now", ROOT), entry_loader=_load_agent)
    failed = [r for r in row["evaluation"]["results"] if r["status"] == "fail"]
    ok = any(r["evaluator"] == "output_matches"
             and "your order shipped" in json.dumps(r["evidence"])
             for r in failed)
    return ok, "failures %r" % (failed,)


def t_case_run_error_is_a_failure_not_a_crash():
    _fresh()
    path = _record()
    cases.save("noentry", path, "nosuchmodule:nothing", root=ROOT)
    row = cases.run(cases.load("noentry", ROOT))
    return (not row["ok"] and row["verdict"] == "CASE_ERROR"
            and "cannot load entry" in row["error"]), "row %r" % (row.get("error"),)


def t_warnings_do_not_fail_a_case():
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        quiet_agent(h)
    cases.save("unanswerable", h.path, "tests.test_cases:quiet_agent",
               root=ROOT, expect={"output_equals": "anything"})
    row = cases.run(cases.load("unanswerable", ROOT), entry_loader=_load_agent)
    ev = row["evaluation"]
    return (row["ok"] and ev["warnings"] == 1 and ev["failed"] == 0), \
        "ok=%s warnings=%d failed=%d" % (row["ok"], ev["warnings"], ev["failed"])


# --- baselines ----------------------------------------------------------------

def t_baseline_is_a_stored_object():
    _fresh()
    path = _record()
    cases.save("b1", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    p = baselines.create("main", _suite(), root=ROOT, note="first")
    b = baselines.load("main", ROOT)
    ok = (os.path.exists(p) and b["name"] == "main" and b["note"] == "first"
          and b["kind"] == "orientim-baseline" and b["totals"]["cases"] == 1
          and b["runs"][0]["case"] == "b1" and b["created_at"] > 0)
    return ok, "stored %r" % ({k: b.get(k) for k in
                               ("name", "kind", "totals")},)


def t_baseline_keeps_no_evidence():
    """It lives in the repository forever; prompts and arguments should not."""
    _fresh()
    path = _record()
    cases.save("b2", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})
    p = baselines.create("main", _suite(), root=ROOT)
    raw = open(p, encoding="utf-8").read()
    b = baselines.load("main", ROOT)
    return ("evidence" not in raw and "arguments" not in raw
            and b["runs"][0]["failed_evaluators"] == ["used_tool"]), \
        "failed_evaluators %r" % (b["runs"][0].get("failed_evaluators"),)


def t_baseline_compare_names_what_moved():
    _fresh()
    path = _record()
    cases.save("moves", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    baselines.create("main", _suite(), root=ROOT)
    with _as_version(2):
        cmp_ = baselines.compare(_suite(), baselines.load("main", ROOT))
    return (cmp_["newly_changed"] == ["moves"] and not cmp_["fixed"]
            and not cmp_["still_changed"]), "comparison %r" % (cmp_,)


def t_baseline_separates_new_from_pre_existing():
    """The question a reviewer asks is what *this* change broke."""
    _fresh()
    path = _record()
    cases.save("green", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    cases.save("alreadyred", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})
    baselines.create("main", _suite(), root=ROOT)
    with _as_version(2):
        cmp_ = baselines.compare(_suite(), baselines.load("main", ROOT))
    return (cmp_["newly_changed"] == ["green"]
            and cmp_["still_changed"] == ["alreadyred"]), \
        "comparison %r" % (cmp_,)


def t_baseline_reuses_ci_compare():
    """One comparison, two callers — asserted, not just intended."""
    rows = [{"case": "a", "ok": False}, {"case": "b", "ok": True}]
    base = {"runs": [{"case": "a", "ok": True}, {"case": "c", "ok": True}]}
    theirs = ci.compare(rows, base, key="case")
    ours = baselines.compare(rows, base)
    return (ours == theirs and ours["newly_changed"] == ["a"]
            and ours["new_recordings"] == ["b"]
            and ours["missing_recordings"] == ["c"]), "%r" % (ours,)


def t_ci_compare_still_keys_on_run_id():
    """The existing caller must be untouched by the new parameter."""
    rows = [{"run_id": "r1", "ok": False}]
    base = {"runs": [{"run_id": "r1", "ok": True}]}
    return ci.compare(rows, base)["newly_changed"] == ["r1"], \
        "%r" % (ci.compare(rows, base),)


def t_baseline_accepts_a_report_path():
    """A team already using `orientim ci --baseline report.json` can point at it."""
    _fresh()
    path = _record()
    cases.save("p", path, "tests.test_cases:agent", root=ROOT)
    rep = cases.report(_suite(), True)
    out = os.path.join(ROOT, "report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rep, f)
    loaded = baselines.load_any(out, ROOT)
    return loaded["runs"][0]["case"] == "p", "loaded %r" % (loaded.get("kind"),)


# --- the report ---------------------------------------------------------------

def t_report_is_machine_readable():
    _fresh()
    path = _record()
    cases.save("r", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})
    rep = cases.report(_suite(), True)
    run = rep["runs"][0]
    ok = (rep["kind"] == "orientim-test" and rep["totals"]["failed"] == 1
          and run["case"] == "r" and run["ok"] is False
          and run["evaluation"]["results"][0]["status"] == "fail"
          and "evidence" in run["evaluation"]["results"][0]
          and json.loads(json.dumps(rep, default=str)))
    return ok, "totals %r" % (rep["totals"],)


def t_report_can_leave_evidence_out():
    """The narrow artefact `orientim ci --report` produces, on demand."""
    _fresh()
    path = _record()
    cases.save("r", path, "tests.test_cases:agent", root=ROOT,
               expect={"output_matches": "nothing like this"})
    rows = _suite()
    with_ev = json.dumps(cases.report(rows, True, evidence=True), default=str)
    without = json.dumps(cases.report(rows, True, evidence=False), default=str)
    return ("order 4471" in with_ev and "order 4471" not in without
            and '"status": "fail"' in without), \
        "with=%d bytes, without=%d bytes" % (len(with_ev), len(without))


def t_summary_shows_reason_and_failing_evaluators():
    _fresh()
    path = _record()
    cases.save("s", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"], "max_steps": 1})
    text = cases.summary(_suite(), True)
    return ("refund_order" in text and "max_steps" in text
            and "1 of 1 case(s) failed" in text), "summary:\n%s" % text


# --- the commands -------------------------------------------------------------

def t_cli_case_commands():
    _fresh()
    path = _record()
    with _cli(["--root", ROOT, "case", "save", path, "--name", "cli1",
               "--entry", "tests.test_cases:agent",
               "--used-tool", "lookup_order", "--max-steps", "4"]) as r:
        saved = r
    with _cli(["--root", ROOT, "case", "list"]) as r:
        listed = r
    with _cli(["--root", ROOT, "case", "delete", "cli1"]) as r:
        deleted = r
    with _cli(["--root", ROOT, "case", "list"]) as r:
        empty = r
    ok = ("saved case" in saved["out"] and saved["code"] == 0
          and "cli1" in listed["out"] and "lookup_order" in saved["out"]
          and "deleted" in deleted["out"]
          and "no cases" in empty["out"])
    return ok, "save=%r list=%r delete=%r" % (
        saved["out"].strip()[:60], listed["out"].strip()[:60],
        deleted["out"].strip()[:40])


def t_cli_case_save_refuses_a_bad_case():
    _fresh()
    with _cli(["--root", ROOT, "case", "save", "nope.jsonl", "--name", "x",
               "--entry", "a:b"]) as r:
        return (r["code"] == ci.EXIT_CANNOT_RUN
                and "no recording" in r["out"]), \
            "code=%s out=%r" % (r["code"], r["out"].strip()[:80])


def t_cli_test_exit_codes():
    """0 when green, 1 when a case fails, 2 when it cannot run at all."""
    _fresh()
    with _cli(["--root", ROOT, "test"]) as none:
        pass
    path = _record()
    cases.save("green", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    with _cli(["--root", ROOT, "test"]) as good:
        pass
    cases.save("red", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})
    with _cli(["--root", ROOT, "test"]) as bad:
        pass
    with _cli(["--root", ROOT, "test", "--no-fail"]) as forced:
        pass
    ok = (none["code"] == ci.EXIT_CANNOT_RUN and good["code"] == ci.EXIT_OK
          and bad["code"] == ci.EXIT_CHANGED and forced["code"] == ci.EXIT_OK)
    return ok, "none=%s green=%s red=%s no-fail=%s" % (
        none["code"], good["code"], bad["code"], forced["code"])


def t_cli_test_writes_a_report():
    _fresh()
    path = _record()
    cases.save("rep", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    out = os.path.join(ROOT, "test-report.json")
    with _cli(["--root", ROOT, "test", "--report", out]) as r:
        pass
    blob = json.load(open(out, encoding="utf-8"))
    return (r["code"] == ci.EXIT_OK and blob["kind"] == "orientim-test"
            and blob["runs"][0]["case"] == "rep"), \
        "code=%s report keys %r" % (r["code"], sorted(blob))


def t_cli_test_only_fails_on_what_this_change_broke():
    """With a baseline, a case that was already red is not this change's fault."""
    _fresh()
    path = _record()
    cases.save("alreadyred", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})
    with _cli(["--root", ROOT, "baseline", "create", "main"]) as created:
        pass
    with _cli(["--root", ROOT, "test"]) as nobase:
        pass
    with _cli(["--root", ROOT, "test", "--baseline", "main"]) as withbase:
        pass
    ok = (created["code"] == 0 and "were failing when this was frozen" in created["out"]
          and nobase["code"] == ci.EXIT_CHANGED
          and withbase["code"] == ci.EXIT_OK
          and "Already failing before this" in withbase["out"])
    return ok, "no baseline=%s, with baseline=%s" % (
        nobase["code"], withbase["code"])


def t_cli_baseline_commands():
    _fresh()
    path = _record()
    cases.save("bl", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    with _cli(["--root", ROOT, "baseline", "create", "main",
               "--note", "before"]) as created:
        pass
    with _cli(["--root", ROOT, "baseline", "list"]) as listed:
        pass
    with _cli(["--root", ROOT, "baseline", "compare", "main"]) as same:
        pass
    with _cli(["--root", ROOT, "baseline", "delete", "main"]) as deleted:
        pass
    with _cli(["--root", ROOT, "baseline", "compare", "main"]) as gone:
        pass
    ok = (created["code"] == 0 and "main" in listed["out"]
          and "before" in listed["out"]
          and same["code"] == ci.EXIT_OK and "Nothing moved" in same["out"]
          and deleted["code"] == 0
          and gone["code"] == ci.EXIT_CANNOT_RUN)
    return ok, "create=%s compare=%s delete=%s missing=%s" % (
        created["code"], same["code"], deleted["code"], gone["code"])


def t_cli_baseline_compare_fails_on_a_new_failure():
    _fresh()
    path = _record()
    cases.save("c", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    with _cli(["--root", ROOT, "baseline", "create", "main"]):
        pass
    with _as_version(2):
        with _cli(["--root", ROOT, "baseline", "compare", "main"]) as r:
            pass
    return (r["code"] == ci.EXIT_CHANGED
            and "started failing on this change" in r["out"]), \
        "code=%s" % r["code"]


def t_cli_case_run_and_run_all():
    _fresh()
    path = _record()
    cases.save("one", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    with _cli(["--root", ROOT, "case", "run", "one"]) as one:
        pass
    with _cli(["--root", ROOT, "case", "run-all"]) as everything:
        pass
    with _cli(["--root", ROOT, "case", "run", "nosuch"]) as missing:
        pass
    ok = (one["code"] == ci.EXIT_OK and "one" in one["out"]
          and everything["code"] == ci.EXIT_OK
          and missing["code"] == ci.EXIT_CANNOT_RUN
          and "no case named" in missing["out"])
    return ok, "run=%s run-all=%s missing=%s" % (
        one["code"], everything["code"], missing["code"])


def t_cli_test_single_case():
    _fresh()
    path = _record()
    cases.save("keep", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"]})
    cases.save("skip", path, "tests.test_cases:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})
    with _cli(["--root", ROOT, "test", "--case", "keep"]) as r:
        pass
    return (r["code"] == ci.EXIT_OK and "keep" in r["out"]
            and "skip" not in r["out"]), "code=%s" % r["code"]


# --- the whole thing ----------------------------------------------------------

def t_the_definition_of_done():
    """Record, save, baseline, change the code, and be told exactly what moved.

    One check for the workflow end to end, because each piece passing
    separately is not the same as the sequence working.
    """
    _fresh()
    path = _record()
    cases.save("order-support", path, "tests.test_cases:agent", root=ROOT,
               description="must look up, must not email",
               expect={"used_tool": ["lookup_order"],
                       "output_matches": "not found",
                       "max_steps": 4, "no_step_failed": True})
    with _cli(["--root", ROOT, "baseline", "create", "main"]) as frozen:
        pass
    if frozen["code"] != 0 or "All 1 case(s) passed" not in frozen["out"]:
        return False, "the baseline was not green: %r" % frozen["out"][-200:]

    out = os.path.join(ROOT, "after.json")
    with _as_version(2):                    # somebody changes the code
        with _cli(["--root", ROOT, "test", "--baseline", "main",
                   "--report", out]) as after:
            pass

    blob = json.load(open(out, encoding="utf-8"))
    against = blob.get("against_baseline") or {}
    failed = [r for r in blob["runs"] if not r["ok"]]
    reasons = " ".join(r.get("reason", "") for r in failed)
    evidence = json.dumps(blob, default=str)

    ok = (after["code"] == ci.EXIT_CHANGED
          and against["newly_changed"] == ["order-support"]
          and len(failed) == 1
          and "order-support" in after["out"]
          and "NEW on this change" in after["out"]
          # it says why, in both halves
          and reasons.strip()
          and "your order shipped" in evidence)
    return ok, ("exit=%s, newly_changed=%r, reason=%r"
                % (after["code"], against.get("newly_changed"), reasons[:90]))
