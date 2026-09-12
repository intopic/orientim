# -*- coding: utf-8 -*-
"""Artifact integrity v1: two answers, an obligation, and one read.

What the hashes already in a recording cover was measured in
`lab/artifact_integrity.py`: not the stored body, not the stored response
headers, not the recorded context — each of which decides something. So this
adds one value of its own, with its own name, and changes nothing about
`body_sha`, the chain, matching or replay.

Three things these tests exist to pin, because each was a correction:

    presence is not truth
        `MISSING` is the field not being there. `None`, `{}` and `""` are a
        field that is there and unusable, and a case that declares one of
        those has still declared an obligation.

    an anchor nobody can verify is not permission
        where a case declares an anchor, every profile refuses when the
        artifact cannot be checked against it — including when the file's own
        descriptor is renumbered, which was a bypass that deleting it was not.

    release is earned
        protected releases only on INTACT|ABSENT + MATCH. A decision taken
        before the check has run is an error, not a release.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import baselines, cases, ci, integrity, session, store

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/integrity"
CALLS = {"n": 0}


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    CALLS["n"] = 0


def _agent(h):
    CALLS["n"] += 1
    h.client().post(B + "/echo", content=json.dumps({"step": "one"}).encode())
    h.output = "done"


def _record():
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        _agent(h)
    return h.path


def _rewrite(path, fn, name="edited"):
    """Edit the parsed form and write it back, as a person with an editor would."""
    lines = [ln for ln in open(path, "rb").read().split(b"\n") if ln.strip()]
    meta = json.loads(lines[0].decode("utf-8"))["_meta"]
    steps = [json.loads(ln.decode("utf-8")) for ln in lines[1:]]
    meta, steps = fn(meta, steps)
    out = os.path.join(ROOT, name + ".jsonl")
    body = [json.dumps(s, default=str).encode("utf-8") for s in steps]
    head = json.dumps({"_meta": meta}, default=str).encode("utf-8")
    with open(out, "wb") as f:
        f.write(b"\n".join([head] + body) + b"\n")
    return out


def _swap(steps):
    for s in steps:
        if s.get("t") == "http":
            s["body"] = (s.get("body") or "").replace("one", "two")
            return s
    return None


def _case(path, anchor=integrity.MISSING, name="c"):
    case = {"format": 1, "name": name, "recording": path,
            "run_id": "run_" + name,
            "entry": "tests.test_integrity:_agent", "input": None,
            "expect": {}, "tags": {}}
    if anchor is not integrity.MISSING:
        case["recording_digest"] = anchor
    return case


def _run(case, profile):
    return cases.run(case, entry_loader=lambda _spec: _agent,
                     integrity_profile=profile)


def _anchor(path):
    snap, _parsed = store.read_snapshot(path)
    return integrity.anchor_of(snap)


# --- what it writes and reads -------------------------------------------------

def t_a_new_recording_carries_a_descriptor_that_matches():
    path = _record()
    meta, _steps = store.load(path)
    desc = (meta or {}).get("integrity") or {}
    snap, parsed = store.read_snapshot(path)
    return (desc.get("scheme") == integrity.SCHEME and desc.get("v") == 1
            and desc.get("steps") == len(snap.step_lines)
            and snap.self_state == integrity.INTACT
            and parsed is not None), \
        "descriptor=%r self=%s" % (desc, snap.self_state)


def t_the_descriptor_does_not_change_the_chain_or_the_replay():
    """It is metadata beside the steps. Nothing about a replay moves."""
    path = _record()
    d = session.replay(path, _agent, strict=True)
    meta, steps = store.load(path)
    from orientim import chain
    root = chain.build_steps([s for s in steps if s.get("t") == "http"])[1]
    return (d.diagnosis[0] == "IDENTICAL" and d.recorded_root == root), \
        "%s recorded_root=%r chain=%r" % (d.diagnosis[0], d.recorded_root, root)


def t_a_case_written_now_stores_an_anchor():
    path = _record()
    p = cases.save("anchored", path, "tests.test_integrity:_agent", root=ROOT)
    case = json.load(open(p, encoding="utf-8"))
    anchor = case.get("recording_digest") or {}
    return (anchor.get("scheme") == integrity.SCHEME and anchor.get("digest")
            and anchor["digest"] == _anchor(path)["digest"]), \
        "anchor=%r" % (anchor,)


# --- presence is not truth ----------------------------------------------------

def t_an_anchor_field_that_is_present_and_empty_is_still_an_obligation():
    """`{}`, `None` and `""` are values, not absence. Truthiness would have
    read every one of them as "no anchor was declared" and let legacy run."""
    path = _record()
    bad = []
    for label, value in (("{}", {}), ("None", None), ('""', ""),
                         ("no digest", {"scheme": integrity.SCHEME, "v": 1,
                                        "algo": "sha256"})):
        for profile in ("protected", "legacy"):
            row = _run(_case(path, anchor=value), profile)
            if row["verdict"] != "FIXTURE_REFUSED" or not row.get("refused"):
                bad.append("%s/%s -> %s" % (label, profile, row["verdict"]))
    return not bad, "; ".join(bad) or "four present-and-invalid anchors refused"


def t_a_missing_anchor_field_is_not_an_obligation():
    """The one case that is absence: legacy runs it and says it is unverified,
    protected refuses it."""
    path = _record()
    legacy = _run(_case(path), "legacy")
    protected = _run(_case(path), "protected")
    return (legacy["verdict"] == "IDENTICAL" and legacy["ok"]
            and legacy["integrity"]["anchor"] == "NO_ANCHOR"
            and protected["verdict"] == "FIXTURE_REFUSED"), \
        "legacy=%s protected=%s" % (legacy["verdict"], protected["verdict"])


# --- the obligation -----------------------------------------------------------

def t_an_edited_body_with_a_recomputed_descriptor_is_refused():
    """The counterexample the anchor exists for: a perfectly self-consistent
    forgery."""
    path = _record()
    anchor = _anchor(path)

    def edit(meta, steps):
        _swap(steps)
        lines = [json.dumps(s, default=str).encode("utf-8") for s in steps]
        meta[integrity.KEY] = integrity.descriptor(
            {k: v for k, v in meta.items() if k != integrity.KEY}, lines)
        return meta, steps

    other = _rewrite(path, edit, "resealed")
    snap, _p = store.read_snapshot(other)
    integrity.check(snap, anchor)
    rows = [_run(_case(other, anchor=anchor), p) for p in
            ("protected", "legacy")]
    return (snap.self_state == integrity.INTACT
            and snap.anchor_state == integrity.MISMATCH
            and all(r["verdict"] == "FIXTURE_REFUSED" for r in rows)), \
        "self=%s anchor=%s verdicts=%r" % (
            snap.self_state, snap.anchor_state, [r["verdict"] for r in rows])


def t_renumbering_the_descriptor_does_not_bypass_the_anchor():
    """The bypass review found: an unknown descriptor version used to stop the
    anchor check and leave legacy releasing the file."""
    path = _record()
    anchor = _anchor(path)

    def edit(meta, steps):
        _swap(steps)
        meta[integrity.KEY] = dict(meta[integrity.KEY], v=99)
        return meta, steps

    other = _rewrite(path, edit, "renumbered")
    snap, _p = store.read_snapshot(other)
    integrity.check(snap, anchor)
    rows = [_run(_case(other, anchor=anchor), p) for p in
            ("protected", "legacy")]
    return (snap.self_state == integrity.UNSUPPORTED
            and snap.anchor_state == integrity.MISMATCH
            and all(r["verdict"] == "FIXTURE_REFUSED" for r in rows)), \
        "self=%s anchor=%s verdicts=%r" % (
            snap.self_state, snap.anchor_state, [r["verdict"] for r in rows])


def t_deleting_the_descriptor_does_not_bypass_the_anchor():
    path = _record()
    anchor = _anchor(path)
    other = _rewrite(path, lambda m, s: (
        {k: v for k, v in m.items() if k != integrity.KEY}, s), "stripped")
    snap, _p = store.read_snapshot(other)
    integrity.check(snap, anchor)
    return (snap.self_state == integrity.ABSENT
            and snap.anchor_state == integrity.MISMATCH), \
        "self=%s anchor=%s" % (snap.self_state, snap.anchor_state)


def t_an_unanchored_recording_a_case_anchored_is_still_verified():
    """ABSENT + MATCH is a release: the anchor subsumes the descriptor.

    The same recording with its descriptor removed, anchored to what it
    actually contains, is used by both profiles.
    """
    path = _record()
    other = _rewrite(path, lambda m, s: (
        {k: v for k, v in m.items() if k != integrity.KEY}, s), "bare")
    anchor = _anchor(other)
    row = _run(_case(other, anchor=anchor), "protected")
    return (row["verdict"] == "IDENTICAL" and row["ok"]
            and row["integrity"]["self"] == integrity.ABSENT
            and row["integrity"]["anchor"] == integrity.MATCH), \
        "verdict=%s integrity=%r" % (row["verdict"], row.get("integrity"))


# --- before use, and the same snapshot ---------------------------------------

def t_protected_refuses_before_the_agent_runs():
    """Not after the run, and not a verdict about the agent: the entry point
    is never called."""
    path = _record()
    CALLS["n"] = 0
    row = _run(_case(path), "protected")          # no anchor declared
    return (row["verdict"] == "FIXTURE_REFUSED" and CALLS["n"] == 0
            and not row["ok"]), \
        "verdict=%s the agent ran %d time(s)" % (row["verdict"], CALLS["n"])


def t_the_replay_consumes_the_snapshot_it_was_handed():
    """A path that verified is not a path that can be read again, so replay
    takes the content. Given a snapshot with one step removed, it replays that
    — not what the file holds."""
    path = _record()
    meta, steps = store.load(path)
    fewer = [s for s in steps if s.get("t") != "http"]
    d = session.replay(path, _agent, strict=True, snapshot=(meta, fewer))
    full = session.replay(path, _agent, strict=True)
    return (d.n_recorded == 0 and full.n_recorded > 0), \
        "snapshot replay saw %d steps, the file has %d" % (
            d.n_recorded, full.n_recorded)


# --- a refusal is not a regression -------------------------------------------

def t_a_refused_recording_is_in_no_movement_bucket():
    path = _record()
    row = _run(_case(path, anchor={}), "legacy")
    before = ci.compare([row], {"runs": [
        {"case": "c", "run_id": row["run_id"], "ok": True,
         "verdict": "IDENTICAL", "failed_evaluators": []}]})
    buckets = ("newly_changed", "still_changed", "fixed", "new_failing",
               "new_failures", "weakened")
    empty = all(not before.get(b) for b in buckets)
    return (row["run_id"] in (before.get("refused") or []) and empty
            and not row.get("evaluation", {}).get("failed")), \
        "refused=%r buckets=%r" % (before.get("refused"),
                                   {b: before.get(b) for b in buckets})


def t_the_baseline_records_which_artifact_a_row_was_measured_from():
    path = _record()
    anchor = _anchor(path)
    row = _run(_case(path, anchor=anchor), "protected")
    baselines.create("b", [row], root=ROOT, strict=True)
    frozen = baselines.load("b", root=ROOT)
    run = (frozen.get("runs") or [{}])[0]
    return (row.get("fixture_digest") == anchor["digest"]
            and run.get("fixture_digest") == anchor["digest"]), \
        "row=%r baseline=%r" % (row.get("fixture_digest"),
                                run.get("fixture_digest"))


# --- and the decision function itself ----------------------------------------

def t_release_is_a_positive_condition():
    """A snapshot nobody checked, and an unknown profile, are errors."""
    path = _record()
    snap, _p = store.read_snapshot(path)
    unchecked = integrity.decide(snap, "protected")
    integrity.check(snap, _anchor(path))
    unknown = integrity.decide(snap, "prod")
    released = integrity.decide(snap, "protected")
    return (unchecked[0] == integrity.SUITE_ERROR
            and unknown[0] == integrity.SUITE_ERROR
            and released[0] == integrity.RELEASE), \
        "unchecked=%r unknown=%r released=%r" % (unchecked, unknown, released)


# --- baseline comparability ---------------------------------------------------

def _row(case, ok, digest, verdict="IDENTICAL"):
    return {"case": case, "run_id": "run_" + case, "ok": ok,
            "verdict": verdict, "fixture_digest": digest,
            "evaluation": {"results": []}}


def t_a_different_fixture_is_not_a_movement():
    """Two rows measured from two recordings are not two measurements of one
    thing, so no movement word is available — including `fixed`."""
    rows = [_row("c", True, "aaa")]
    base = {"runs": [{"case": "c", "run_id": "run_c", "ok": False,
                      "verdict": "NEW_CALL", "fixture_digest": "bbb",
                      "failed_evaluators": []}]}
    cmp_ = ci.compare(rows, base)
    buckets = ("newly_changed", "still_changed", "fixed", "new_failing",
               "new_failures", "weakened")
    code, reasons = ci.gate(cmp_, rows, profile=ci.LEGACY)
    return ("run_c" in (cmp_.get("fixture_changed") or [])
            and all(not cmp_.get(b) for b in buckets)
            and code == ci.EXIT_CHANGED
            and any("different recording" in r for r in reasons)), \
        "changed=%r buckets=%r code=%s reasons=%r" % (
            cmp_.get("fixture_changed"),
            {b: cmp_.get(b) for b in buckets}, code, reasons)


def t_a_baseline_without_fixture_identity_is_declared_not_assumed():
    """Every baseline written before this carries no fixture identity. The
    comparison still happens — turning all of them into refusals would be a
    migration dressed as rigour — but it is declared, never read as proof
    that the same artifact was measured twice."""
    rows = [_row("c", False, "aaa", verdict="NEW_CALL")]
    base = {"runs": [{"case": "c", "run_id": "run_c", "ok": True,
                      "verdict": "IDENTICAL", "failed_evaluators": []}]}
    cmp_ = ci.compare(rows, base)
    text = " ".join(ci.obligation_lines(cmp_))
    return ("run_c" in (cmp_.get("fixture_unknown") or [])
            and "run_c" in (cmp_.get("newly_changed") or [])
            and "not established" in text), \
        "unknown=%r newly=%r text=%r" % (
            cmp_.get("fixture_unknown"), cmp_.get("newly_changed"),
            text[:120])


def t_the_same_fixture_compares_as_it_always_did():
    """The control: equal digests, and the comparison is the old one."""
    rows = [_row("c", False, "aaa", verdict="NEW_CALL")]
    base = {"runs": [{"case": "c", "run_id": "run_c", "ok": True,
                      "verdict": "IDENTICAL", "fixture_digest": "aaa",
                      "failed_evaluators": []}]}
    cmp_ = ci.compare(rows, base)
    return ("run_c" in (cmp_.get("newly_changed") or [])
            and not cmp_.get("fixture_changed")
            and not cmp_.get("fixture_unknown")), \
        "newly=%r changed=%r unknown=%r" % (
            cmp_.get("newly_changed"), cmp_.get("fixture_changed"),
            cmp_.get("fixture_unknown"))


# --- creating a case ----------------------------------------------------------

def t_a_case_takes_its_metadata_and_its_anchor_from_one_read():
    path = _record()
    p = cases.save("one_read", path, "tests.test_integrity:_agent", root=ROOT)
    case = json.load(open(p, encoding="utf-8"))
    snap, parsed = store.read_snapshot(path)
    meta, _steps = parsed
    return (case["run_id"] == meta.get("run_id")
            and case["recording_digest"]["digest"]
            == integrity.anchor_of(snap)["digest"]), \
        "case run_id=%r meta run_id=%r" % (case.get("run_id"),
                                           meta.get("run_id"))


def t_a_case_that_cannot_be_anchored_is_not_saved():
    """Not saved quietly without one: an unanchored case is a case the
    protected profile will refuse, with nothing to say when it lost it."""
    path = _record()
    lines = open(path, "rb").read().split(b"\n")
    two = os.path.join(ROOT, "twometa.jsonl")
    with open(two, "wb") as f:
        f.write(b"\n".join([lines[0], lines[0]] + lines[1:]))
    try:
        cases.save("unanchorable", two, "tests.test_integrity:_agent",
                   root=ROOT)
        return False, "the case was saved anyway"
    except cases.CaseError as e:
        saved = os.path.exists(cases.path_for("unanchorable", ROOT))
        return (not saved and "cannot be read safely" in str(e)), \
            "raised %r, file written=%s" % (str(e)[:80], saved)


# --- the command --------------------------------------------------------------

MARK = os.path.join(ROOT, "the-entry-point-ran")


def _cli_agent(h):
    """The entry point the command resolves. It leaves a mark when it runs.

    A file rather than a counter: the command imports this module through the
    import system as `tests.test_integrity`, which is a different module
    object from the one the test itself is running in.
    """
    with open(MARK, "a", encoding="utf-8") as f:
        f.write("ran\n")
    h.client().post(B + "/echo", content=json.dumps({"step": "one"}).encode())
    h.output = "done"


def _cli(argv):
    """cli.main, with stdout captured and the exit code recovered."""
    import contextlib
    import io as _io
    from orientim import cli
    buf = _io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


def _reseal(meta, steps):
    _swap(steps)
    lines = [json.dumps(s, default=str).encode("utf-8") for s in steps]
    meta[integrity.KEY] = integrity.descriptor(
        {k: v for k, v in meta.items() if k != integrity.KEY}, lines)
    return meta, steps


def t_the_command_runs_an_anchored_case_and_blocks_a_changed_one():
    """The real command, twice, on the same case.

    First as it was saved: the recording matches its anchor, the entry point
    runs, and the build is green. Then with the recording rewritten and its
    descriptor recomputed — a self-consistent forgery — under
    `--fixtures protected`: the entry point is never called, the report says
    the recording was not released, and the exit code blocks CI.
    """
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        _cli_agent(h)
    path = h.path
    cases.save("cli", path, "tests.test_integrity:_cli_agent", root=ROOT)

    os.remove(MARK)
    green, code_green = _cli(["--root", ROOT, "test", "--fixtures",
                              "protected"])
    ran_green = os.path.exists(MARK)

    # the same case, its recording rewritten under it
    lines = [ln for ln in open(path, "rb").read().split(b"\n") if ln.strip()]
    meta = json.loads(lines[0].decode("utf-8"))["_meta"]
    steps = [json.loads(ln.decode("utf-8")) for ln in lines[1:]]
    meta, steps = _reseal(meta, steps)
    with open(path, "wb") as f:
        f.write(b"\n".join(
            [json.dumps({"_meta": meta}, default=str).encode("utf-8")]
            + [json.dumps(s, default=str).encode("utf-8") for s in steps])
            + b"\n")

    if os.path.exists(MARK):
        os.remove(MARK)
    red, code_red = _cli(["--root", ROOT, "test", "--fixtures", "protected"])
    ran_red = os.path.exists(MARK)

    ok = (code_green == ci.EXIT_OK and ran_green
          and code_red != ci.EXIT_OK and not ran_red
          and "not released" in red.lower())
    return ok, ("green: exit %s ran=%s | red: exit %s ran=%s\n%s"
                % (code_green, ran_green, code_red, ran_red, red[-400:]))


def t_the_legacy_profile_still_runs_an_unanchored_case():
    """The migration path: a case with no anchor at all is usable, and the
    command says it is unverified rather than pretending otherwise."""
    _fresh()
    with orientim.record(root=ROOT, always=True) as h:
        _cli_agent(h)
    p = cases.save("legacy", h.path, "tests.test_integrity:_cli_agent",
                   root=ROOT)
    case = json.load(open(p, encoding="utf-8"))
    case.pop("recording_digest", None)          # a case written before anchors
    with open(p, "w", encoding="utf-8") as f:
        json.dump(case, f)

    os.remove(MARK)
    out, code = _cli(["--root", ROOT, "test", "--fixtures", "legacy"])
    ran = os.path.exists(MARK)
    strict, code_strict = _cli(["--root", ROOT, "test", "--fixtures",
                                "protected"])
    return (code == ci.EXIT_OK and ran and code_strict != ci.EXIT_OK), \
        "legacy exit %s ran=%s | protected exit %s" % (code, ran, code_strict)
