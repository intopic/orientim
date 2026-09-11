# -*- coding: utf-8 -*-
"""Saved cases: a recording, an entry point, and what is expected of it.

`orientim ci` replays every recording in a store against **one** entry point.
That is right for a store of production captures and wrong for a suite: real
agents have more than one entry point, and a recording kept as a regression test
is kept for a specific reason that the file itself does not record.

A case is where that reason lives:

    recording   what happened, once
    entry       how to run it again
    expect      what has to still be true

The expectations are **data**, not code, because a case is a file. The
vocabulary is deliberately small — `evaluate.SPEC_KEYS`. Anything more specific
belongs in a `check()` in the caller's own test, which is where code belongs.

Nothing here re-implements replay or evaluation. A case run is
`session.replay()` followed by `evaluate.evaluate()` over the steps the replay
produced, and the row it returns is the same shape `ci.py` uses, so
`ci.compare` works on it unchanged.
"""
import json
import os
import re
import time

from . import ci, evaluate, model, session, store

FORMAT = 1
DIRNAME = "cases"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CaseError(Exception):
    """Something about a case is wrong, and the message says what."""


def _dir(root):
    return os.path.join(root or "runs", DIRNAME)


def path_for(name, root):
    check_name(name)
    return os.path.join(_dir(root), name + ".json")


def check_name(name):
    """A case name becomes a file name, so it is checked rather than trusted."""
    if not name or not _NAME_RE.match(name):
        raise CaseError(
            "%r is not a usable case name: use letters, digits, dot, dash or "
            "underscore, up to 64 characters" % (name,))
    return name


# --- the file -----------------------------------------------------------------

def save(name, recording, entry, root="runs", expect=None, description=None,
         tags=None, overwrite=True, input=None):
    """Write a case. Validates before writing, so a bad case never lands.

    A case that fails to load later, in CI, is a much worse outcome than one
    that refuses to save now — so the recording is opened, the expectations are
    compiled, and only then is anything written.

    `input` is what the entry point should be replayed against, and it defaults
    to what the recording was made with. That is what makes a case
    self-contained: twelve scenarios differing only by which order they ask
    about share one entry point, instead of smuggling the difference through
    the environment — which works for one case at a time and silently gives
    every case the same value when a whole suite runs in one process.
    """
    check_name(name)
    if not os.path.exists(recording) and not recording.startswith(
            ("s3://", "memory://")):
        raise CaseError("no recording at %r" % recording)
    try:
        meta, _steps = store.load(recording)
    except Exception as e:
        raise CaseError("cannot read the recording at %r: %s: %s"
                        % (recording, type(e).__name__, e)) from e
    if not entry or ":" not in entry:
        raise CaseError("--entry must be module:function, got %r" % (entry,))
    evaluate.from_spec(expect)          # raises on an unknown expectation

    p = path_for(name, root)
    if os.path.exists(p) and not overwrite:
        raise CaseError("a case named %r already exists" % name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if input is None:
        # Restored, not the stored text: an entry point that reads
        # run.input["order_id"] must get a dict on both sides of a replay.
        input = model.restore((meta or {}).get("input"))
    case = {
        "format": FORMAT,
        "name": name,
        "recording": recording,
        "run_id": (meta or {}).get("run_id"),
        "entry": entry,
        "input": input,
        "expect": expect or {},
        "description": description or "",
        "tags": dict(tags or {}),
        "agent": (meta or {}).get("agent"),
        "created_at": time.time(),
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(case, f, indent=2, default=str)
    return p


def load(name, root="runs"):
    p = path_for(name, root)
    try:
        with open(p, encoding="utf-8") as f:
            case = json.load(f)
    except OSError:
        raise CaseError("no case named %r under %s"
                        % (name, _dir(root))) from None
    except ValueError as e:
        raise CaseError("case %r is not readable JSON: %s"
                        % (name, e)) from e
    if case.get("format", 1) > FORMAT:
        raise CaseError(
            "case %r was written by a newer version of Orientim (format %s)"
            % (name, case.get("format")))
    return case


def list_cases(root="runs"):
    """Every case, by name. A file we cannot read is reported, not skipped."""
    d = _dir(root)
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        name = fn[:-5]
        try:
            out.append(load(name, root))
        except CaseError as e:
            out.append({"name": name, "broken": str(e)})
    return out


def delete(name, root="runs"):
    p = path_for(name, root)
    if not os.path.exists(p):
        raise CaseError("no case named %r under %s" % (name, _dir(root)))
    os.remove(p)
    return p


# --- running one --------------------------------------------------------------

def _execution_of(divergence, case):
    """The execution to judge: what the code did *this time*.

    Evaluating the recording would answer questions about the past. The point
    of a case is the present, so the evaluators are pointed at what the replay
    produced and at what the replayed function returned.

    That includes the requests the replay could **not** match. They are not
    steps — the chain must never see them — but they are things the agent did,
    and leaving them out made `no_step_failed()` report "all 0 call(s)
    succeeded" for a run in which every single request came back 599.
    """
    from . import diff
    meta = {
        "run_id": case.get("run_id") or case.get("name"),
        "outcome": divergence.replay_output,
        "agent": case.get("agent"),
        "status": "ok" if divergence.ok else "changed",
        "dropped": 0,
    }
    steps = diff.merge_unmatched(divergence.replay_steps,
                                 divergence.unmatched_requests)
    return evaluate.Execution.of(meta, steps)


def run(case, strict=True, extra=None, entry_loader=None,
        keep_execution=False):
    """Replay one case and evaluate what came out. Returns a row.

    `extra` is for evaluators built in code — a `check()` a test wants to add on
    top of what the file declares.

    `keep_execution` attaches the replayed steps and the answer to the row, for
    a caller that wants to diff them. Off by default: a suite of a few hundred
    cases would otherwise hold every step of every run in memory at once, paid
    for by everyone including the callers that only read the verdict.
    """
    from . import server                  # local: server imports are heavier
    load_entry = entry_loader or server._load_entry
    name = case.get("name", "?")
    row = {
        "case": name,
        "run_id": case.get("run_id"),
        "recording": case.get("recording"),
        "entry": case.get("entry"),
        "tags": case.get("tags") or {},
        "ok": False,
        "verdict": "CASE_ERROR",
        "index": None,
        "recorded_root": "",
        "replay_root": "",
        "steps": 0,
        "evaluation": {"passed": 0, "failed": 0, "warnings": 0, "results": []},
    }
    if case.get("broken"):
        row["error"] = case["broken"]
        row["reason"] = case["broken"]
        return row

    t0 = time.monotonic()
    try:
        fn = load_entry(case["entry"])
    except Exception as e:
        row["error"] = "cannot load entry %r: %s: %s" % (
            case.get("entry"), type(e).__name__, e)
        row["reason"] = row["error"]
        row["ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        return row

    try:
        evaluators = evaluate.from_spec(case.get("expect")) + list(extra or [])
    except ValueError as e:
        row["error"] = "bad expectations: %s" % e
        row["reason"] = row["error"]
        row["ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        return row

    try:
        d = session.replay(case["recording"], fn, strict=strict,
                           input=case.get("input"))
    except Exception as e:
        row["verdict"] = "REPLAY_ERROR"
        row["error"] = "%s: %s" % (type(e).__name__, e)
        row["reason"] = row["error"]
        row["ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        return row

    report = evaluate.evaluate(_execution_of(d, case), evaluators)
    row.update({
        "verdict": d.diagnosis[0],
        "index": d.index,
        "recorded_root": d.recorded_root,
        "replay_root": d.replay_root,
        "steps": d.n_recorded,
        "replayed": bool(d.ok),
        "evaluation": {
            "passed": len(report.passed),
            "failed": len(report.failed),
            "warnings": len(report.warnings),
            "results": [r.as_dict() for r in report.results],
        },
        "runtime_changed": d.runtime_changed,
    })
    # A case passes when the run reproduced *and* nothing it promised broke.
    # Either alone would be a half-answer: a faithful replay of an agent that
    # now calls the wrong tool is not a pass, and a run that satisfies every
    # rule while its traffic changed underneath is not one either.
    row["ok"] = bool(d.ok) and report.ok
    row["reason"] = _reason(d, report)
    if not row["ok"]:
        # The evidence goes in the row that failed, so the command that fails
        # is the command that explains.
        row["evidence"] = _evidence(case, d, report)
    row["ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    if keep_execution:
        # Underscored, and excluded from `report()` and from a baseline by
        # construction: both build their rows from an explicit key list, so
        # execution detail can never leak into a file by being added here.
        row["_replay_steps"] = d.replay_steps
        row["_unmatched"] = d.unmatched_requests
        row["_replay_output"] = d.replay_output
    return row


def _evidence(case, divergence, report):
    """What changed, from what the run already produced.

    A failing case has both halves of a diff in hand — the recording it was
    made from, and the steps the replay produced — so the explanation costs
    one alignment and no new data. It used to be computed only by
    `orientim diff`, which meant a red build named a consequence
    (`no_step_failed`) and left the cause to a second command.

    Computed only for a failure, so a green suite pays nothing. Never raises:
    an explanation that breaks the run it is explaining would be worse than no
    explanation.
    """
    from . import align, diff
    try:
        meta_a, steps_a = store.load(case["recording"])
        steps_b = diff.merge_unmatched(divergence.replay_steps,
                                       divergence.unmatched_requests)
        cmp_ = diff.compare_executions(
            meta_a, steps_a,
            {"outcome": divergence.replay_output, "agent": case.get("agent")},
            steps_b, name_a="recorded", name_b="now")
    except Exception as e:
        return {"unavailable": "%s: %s" % (type(e).__name__, e)}

    out = {}
    if cmp_["model_changes"]:
        out["model"] = [{"field": c["field"], "was": c["was"], "now": c["now"]}
                        for c in cmp_["model_changes"][:6]]
    if cmp_.get("tool_view_unreadable"):
        # The replay never answered a model call, so it never saw a tool
        # request. Saying "no longer requested" here would be an artifact of
        # the divergence wearing the clothes of a finding.
        out["tools_unreadable"] = cmp_["tool_view_unreadable"]
    elif cmp_["tool_changes"]:
        out["tools"] = cmp_["tool_changes"][:6]
    if cmp_["output"].get("state") not in (None, "unchanged"):
        out["output"] = {k: cmp_["output"].get(k)
                         for k in ("state", "a", "b", "note")
                         if cmp_["output"].get(k) is not None}
    # What the agent *asked* differently, at the first step that changed.
    #
    # Needed because a divergence early in a run collapses everything after it:
    # the new request gets a synthetic 599, so its response never exists, so
    # the tool it would have requested never appears — and the tool comparison
    # is withheld above for exactly that reason. The request body is the half
    # that survives, and it is a fact about what the agent sent: no inference,
    # no cause. On the lab this is where four of the ten regressions are named.
    for r in cmp_["steps"]:
        if r["op"] == "SAME":
            continue
        body = r.get("request_body")
        if body and body.get("kind") == "json":
            fields = ([{"path": c["path"], "was": c["was"], "now": c["now"]}
                       for c in body.get("changed", [])[:5]]
                      + [{"path": c["path"], "now": c["value"], "was": None}
                         for c in body.get("added", [])[:3]]
                      + [{"path": c["path"], "was": c["value"], "now": None}
                         for c in body.get("removed", [])[:3]])
            if fields:
                out["request"] = {"step": r.get("b"), "fields": fields}
        break

    # Step counts only when a step actually moved. A clean replay reports
    # "same 3", which is true, occupies a line, and tells the reader nothing
    # they did not already have from the verdict — and the case in front of
    # them failed on an evaluator, not on the calls.
    moved = {k: v for k, v in cmp_["counts"].items() if v and k != align.SAME}
    if moved:
        out["steps"] = {k: v for k, v in cmp_["counts"].items() if v}
    if cmp_.get("concurrency", {}).get("findings"):
        out["concurrency"] = [{"kind": f["kind"], "strength": f["strength"],
                               "was": f["was"]["order"], "now": f["now"]["order"]}
                              for f in cmp_["concurrency"]["findings"][:3]]
    return out


def _reason(d, report):
    """One line saying why, whichever half went wrong."""
    if not d.ok:
        code, _title, msg, _action = d.diagnosis
        where = "" if d.index is None else " at step %d" % d.index
        return "%s%s — %s" % (code, where, msg.split(".")[0])
    if report.failed:
        return "; ".join(r.reason for r in report.failed[:3])
    return "replayed identically and every expectation held"


def run_all(root="runs", strict=True, on_result=None, names=None, extra=None):
    """Every case under `root`, in name order. Order is stable so a diff is."""
    rows = []
    for case in list_cases(root):
        if names and case.get("name") not in names:
            continue
        row = run(case, strict=strict, extra=extra)
        rows.append(row)
        if on_result:
            on_result(row)
    return rows


# --- reporting ----------------------------------------------------------------

def report(rows, strict, baseline=None, evidence=True):
    """The machine-readable half.

    Unlike `ci.report`, this one carries evaluation evidence by default —
    arguments, matched text, failing URLs. That is the point of it: a failure
    without evidence is the start of an investigation rather than the end of
    one. It also means the file is **not** the narrow, safe-to-hand-anywhere
    artefact `orientim ci --report` produces. Pass evidence=False to get that
    property back.
    """
    changed = [r for r in rows if not r["ok"]]
    runs = []
    for r in rows:
        row = {k: r.get(k) for k in
               ("case", "run_id", "recording", "entry", "ok", "verdict",
                "index", "recorded_root", "replay_root", "steps", "ms",
                "tags", "reason")}
        ev = r.get("evaluation") or {}
        results = ev.get("results") or []
        if not evidence:
            results = [{k: v for k, v in res.items() if k != "evidence"}
                       for res in results]
        row["evaluation"] = {"passed": ev.get("passed", 0),
                             "failed": ev.get("failed", 0),
                             "warnings": ev.get("warnings", 0),
                             "results": results}
        if r.get("error"):
            row["error"] = r["error"]
        if evidence and r.get("evidence"):
            row["evidence"] = r["evidence"]
        runs.append(row)

    out = {
        "schema": ci.SCHEMA,
        "kind": "orientim-test",
        "created_at": time.time(),
        "tool": "orientim",
        "strict": bool(strict),
        "totals": {
            "cases": len(rows),
            "passed": len(rows) - len(changed),
            "failed": len(changed),
            "warnings": sum((r.get("evaluation") or {}).get("warnings", 0)
                            for r in rows),
        },
        # A report is a baseline someone kept, so it records what a baseline
        # records: which analyzer produced these statuses.
        "analysis": evaluate.analysis(),
        "runs": runs,
    }
    out.update({k: v for k, v in ci._github().items() if v})
    if baseline is not None:
        out["against_baseline"] = ci.compare(rows, baseline, key="case")
    return out


_MARK = {evaluate.PASS: "ok ", evaluate.FAIL: "!! ", evaluate.WARN: " ? "}


def _evidence_lines(row):
    """The explanation, in the failure that needs it.

    Observations, in the order a person reads them. No claim about which caused
    which: a build that says "root cause" about something it inferred from
    ordering would be worse than one that says nothing.
    """
    # Presence, not truth: a failure always carries the key, and an empty
    # value is itself the finding — the run reproduced and something else
    # failed. A passing case has no key at all and prints nothing.
    if "evidence" not in row:
        return []
    ev = row["evidence"] or {}
    if ev.get("unavailable"):
        return ["evidence: could not be gathered (%s)" % ev["unavailable"]]

    L = ["evidence, from the same replay:"]
    for m in ev.get("model", []):
        L.append("  model config   %s: %s -> %s" % (m["field"], m["was"],
                                                    m["now"]))
    blind = ev.get("tools_unreadable")
    if blind:
        L.append("  tool decision  unknown here: all %d model call(s) went "
                 "unanswered, so no" % blind["model_calls"])
        L.append("                 response exists that a tool request could "
                 "have been in.")
        L.append("                 the recording requested: %s"
                 % (", ".join(blind["named_by_the_other_side"]) or "no tools"))
    for t in ev.get("tools", []):
        if t["change"] == "removed":
            L.append("  tool decision  %s no longer requested" % t["name"])
        elif t["change"] == "added":
            L.append("  tool decision  %s newly requested" % t["name"])
        elif t["change"] == "arguments":
            L.append("  tool decision  %s called with different arguments"
                     % t["name"])
        elif t["change"] == "reordered":
            L.append("  tool decision  %s requested at a different point"
                     % t["name"])
    req = ev.get("request") or {}
    for f in req.get("fields", []):
        if f["was"] is None:
            L.append("  request        step %s added %s = %r"
                     % (req.get("step"), f["path"], f["now"]))
        elif f["now"] is None:
            L.append("  request        step %s dropped %s (was %r)"
                     % (req.get("step"), f["path"], f["was"]))
        else:
            L.append("  request        step %s %s: %r -> %r"
                     % (req.get("step"), f["path"], f["was"], f["now"]))

    out = ev.get("output") or {}
    if out.get("state") == "changed":
        L.append("  output         %r -> %r" % (out.get("a"), out.get("b")))
    elif out.get("state"):
        L.append("  output         %s" % (out.get("note") or out["state"]))
    for c in ev.get("concurrency", []):
        L.append("  concurrency    %s (%s): %s -> %s"
                 % (c["kind"], c["strength"], " then ".join(c["was"]),
                    " then ".join(c["now"])))
    if ev.get("steps"):
        L.append("  steps          %s"
                 % ", ".join("%s %d" % (k.lower(), v)
                             for k, v in sorted(ev["steps"].items())))
    if len(L) == 1:
        return ["evidence: the calls and the answer are unchanged; "
                "the difference is in the verdict above"]
    return L


def summary(rows, strict, baseline_cmp=None, width=74, evidence=True):
    """The half a person reads.

    `evidence=False` drops the explanation block, which is the one part of this
    output that quotes what the agent sent and what it answered. A build log is
    stored the same way a report is, so the flag that keeps prompts out of one
    keeps them out of the other.
    """
    changed = [r for r in rows if not r["ok"]]
    warned = sum((r.get("evaluation") or {}).get("warnings", 0) for r in rows)
    L = ["", "=" * width]
    L.append("  ORIENTIM TEST  ·  %d case(s)  ·  %s key"
             % (len(rows), "strict" if strict else "loose"))
    L.append("=" * width)
    L.append("")
    if not rows:
        L.append("  No cases found. Save one with:")
        L.append("    orientim case save <recording> --name <name> "
                 "--entry module:function")
        L.append("")
        return "\n".join(L)

    for r in rows:
        mark = "ok " if r["ok"] else "!! "
        tags = " ".join("%s=%s" % kv for kv in sorted((r.get("tags") or {}).items()))
        L.append("  %s %-20s %3d steps  %8.1f ms  %s"
                 % (mark, r["case"], r.get("steps", 0), r.get("ms", 0.0), tags))
        if not r["ok"]:
            L.append("       %s" % r.get("reason", "changed"))
        for res in (r.get("evaluation") or {}).get("results", []):
            if res["status"] == evaluate.PASS:
                continue
            L.append("       %s %-16s %s"
                     % (_MARK[res["status"]], res["evaluator"], res["reason"]))
        if evidence:
            L += ["       " + line for line in _evidence_lines(r)]
        if r.get("runtime_changed"):
            moved = ", ".join("%s %s->%s" % (c["what"], c["was"] or "-",
                                             c["now"] or "-")
                              for c in r["runtime_changed"][:3])
            L.append("       note: the runtime moved since the recording: %s"
                     % moved)
    L.append("")
    L.append("-" * width)
    if changed:
        L.append("  %d of %d case(s) failed." % (len(changed), len(rows)))
    else:
        L.append("  All %d case(s) passed." % len(rows))
    if warned:
        L.append("  %d question(s) could not be answered — see ? above. "
                 "These do not fail the build." % warned)

    if baseline_cmp:
        b = baseline_cmp
        if b["newly_changed"]:
            L.append("  NEW on this change: " + ", ".join(b["newly_changed"]))
        if b["fixed"]:
            L.append("  Fixed on this change: " + ", ".join(b["fixed"]))
        if b["still_changed"]:
            L.append("  Already failing before this: "
                     + ", ".join(b["still_changed"]))
        if b["new_recordings"]:
            L.append("  Cases not in the baseline: "
                     + ", ".join(b["new_recordings"]))
        if b["missing_recordings"]:
            L.append("  In the baseline but gone now: "
                     + ", ".join(b["missing_recordings"]))
        L += ci.obligation_lines(b) + ci.analysis_lines(b)
    L.append("-" * width)
    L.append("")
    return "\n".join(L)
