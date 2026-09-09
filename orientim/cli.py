# -*- coding: utf-8 -*-
"""Find a run, scrub through it, and see the step where it went wrong."""
import argparse
import datetime as dt
import json
import os
import sys

from . import (baselines, cases, ci, conformance, diff, server, stability,
               store, viewer)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    BAR, DOT, WARN = "━", "◉", "▲"
except Exception:
    BAR, DOT, WARN = "-", "O", "!"



def _fmt(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_ls(a):
    runs = store.list_runs(a.root)
    shown = 0
    for m in runs:
        if a.status and m["status"] != a.status:
            continue
        if a.trace and (m.get("trace") or {}).get("trace_id") != a.trace:
            continue
        if a.on and not _fmt(m["started_at"]).startswith(a.on):
            continue
        if a.tag:
            k, _, v = a.tag.partition("=")
            if str(m["tags"].get(k, "")) != v:
                continue
        tags = " ".join(f"{k}={v}" for k, v in m["tags"].items())
        flag = "!" if m["status"] != "ok" else " "
        shown += 1
        print(f"{flag} {m['run_id']:<14} {_fmt(m['started_at'])}  "
              f"{m['steps']:>3} steps  {m['status']:<7} {tags}")
    if not runs:
        print("(no runs stored yet — nothing has triggered a capture)")
        print("  a run is written on an exception, a 5xx, an uncaptured source,")
        print("  or run.rec.trigger('...'). For every run, use always=True.")
    elif not shown:
        print("(no runs matched)")


def cmd_play(a):
    path = os.path.join(a.root, a.run_id + ".jsonl")
    meta, steps = store.load(path)
    http = [s for s in steps if s.get("t") == "http"]
    print(f"\n  {meta['run_id']}   {_fmt(meta['started_at'])}   "
          f"{meta['status']}   trigger: {meta.get('trigger')}")
    if meta.get("tags"):
        print("  tags: " + ", ".join(f"{k}={v}" for k, v in meta["tags"].items()))
    print()

    n = len(http)
    for i, s in enumerate(http):
        pos = int(round(i / max(n - 1, 1) * 40))
        track = BAR * pos + DOT + BAR * (40 - pos)
        bad = s["status"] >= 400 or s["status"] == 0
        mark = WARN if bad else " "
        url = s["url"].split("/")[-1][:22]
        print(f"  {i:>2} {track} {mark} {s['method']:<5}{url:<24}{s['status']}")
        if a.step:
            body = s["body"][:110].replace("\n", " ")
            print(f"     {'':<41}   → {body}")
    print()
    for i, s in enumerate(http):
        try:
            j = json.loads(s["body"])
        except Exception:
            continue
        if j.get("hits") in (None, [], "") and "search" in s["url"]:
            print(f"  {WARN} step {i}: the search returned nothing — look at what\n     the agent did next\n")
            break


def cmd_view(a):
    path = os.path.join(a.root, a.run_id + ".jsonl")
    if a.entry:
        server.serve(path, a.entry, port=a.port, open_browser=not a.no_open)
        return
    out = viewer.build(path, open_browser=not a.no_open)
    print("timeline:", out)
    print("for live replay add:  --entry module:function")


def cmd_stability(a):
    fn = server._load_entry(a.entry)
    done = {"n": 0}
    CR = chr(13)

    def tick(r):
        done["n"] += 1
        sys.stdout.write(CR + "  run " + str(done["n"]) + "/" + str(a.runs) + "...")
        sys.stdout.flush()

    m = stability.measure(fn, runs=a.runs, root=a.root + "/_stability", on_run=tick)
    sys.stdout.write(CR + " " * 44 + CR)
    print(stability.report(m, a.entry))


def cmd_diff(a):
    """Explain how two executions differ, and what followed.

    Two shapes, because the question arrives in two shapes: "these two files"
    and "this case used to pass — what is it doing now".
    """
    def _path(x):
        return x if ("/" in x or "\\" in x or x.endswith(".jsonl")) \
            else os.path.join(a.root, x + ".jsonl")

    if a.case:
        try:
            case = cases.load(a.case, a.root)
        except cases.CaseError as e:
            print("  %s" % e)
            sys.exit(ci.EXIT_CANNOT_RUN)
        cmp_ = diff.compare_case(case, strict=not a.loose, root=a.root)
    else:
        if not a.a or not a.b:
            print("  give two recordings, or --case NAME")
            sys.exit(ci.EXIT_CANNOT_RUN)
        cmp_ = diff.compare(_path(a.a), _path(a.b), strict=not a.loose)

    if a.json:
        print(diff.as_json(cmp_))
    else:
        print(diff.report(cmp_, verbose=a.verbose))
    # A diff is a question, not a gate: it exits 0 whether or not the two runs
    # differ. `orientim test` is what a build should fail on.


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit)
        n /= 1024.0


def cmd_prune(a):
    rows = store.survey(a.root)
    total = sum(r["bytes"] for r in rows)
    victims = store.prune(a.root, keep=a.keep, older_than_days=a.older_than,
                          max_bytes=a.max_bytes, per_signature=a.per_signature,
                          dry_run=a.dry_run)
    if not any((a.keep, a.older_than, a.max_bytes, a.per_signature)):
        print("  %d recordings, %s. No rule given, so nothing was removed."
              % (len(rows), _human(total)))
        print("  --keep N  --older-than DAYS  --max-bytes N  --per-signature N")
        return
    freed = sum(r["bytes"] for r, _ in victims)
    verb = "would remove" if a.dry_run else "removed"
    print("  %d recordings, %s" % (len(rows), _human(total)))
    for r, why in victims[:20]:
        print("    %s %-16s %8s   %s"
              % ("-", r["run_id"], _human(r["bytes"]), "; ".join(why)))
    if len(victims) > 20:
        print("    ... and %d more" % (len(victims) - 20))
    print("  %s %d, freeing %s" % (verb, len(victims), _human(freed)))


def cmd_ci(a):
    """Replay a whole store against the current code, for a build to judge."""
    try:
        fn = server._load_entry(a.entry)
    except Exception as e:
        print("  cannot load --entry %s: %s: %s" % (a.entry, type(e).__name__, e))
        sys.exit(ci.EXIT_CANNOT_RUN)

    root = a.recordings or a.root
    baseline = None
    if a.baseline:
        try:
            with open(a.baseline, encoding="utf-8") as f:
                baseline = json.load(f)
        except OSError as e:
            print("  cannot read --baseline %s: %s" % (a.baseline, e))
            sys.exit(ci.EXIT_CANNOT_RUN)

    rows = ci.replay_all(root, fn, strict=not a.loose)
    if not rows:
        print("  no recordings under %r — nothing to replay" % root)
        sys.exit(ci.EXIT_CANNOT_RUN)

    cmp_ = ci.compare(rows, baseline) if baseline else None
    print(ci.summary(rows, not a.loose, cmp_))
    ci.emit(rows, not a.loose, cmp_)

    if a.report:
        rep = ci.report(rows, not a.loose, a.entry, baseline)
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2)
        print("  report: %s" % a.report)

    changed = [r for r in rows if not r["ok"]]
    if changed and not a.no_fail:
        sys.exit(ci.EXIT_CHANGED)


def cmd_conformance(a):
    rep = conformance.run(strict=a.strict)
    print(conformance.format_report(rep))
    if a.save:
        print("  saved:", conformance.save(rep, a.save))
    if rep["undeclared_failures"]:
        sys.exit(1)



# --- cases --------------------------------------------------------------------

def _case_path(a, x):
    """Accept a run id or a path, the way `diff` already does."""
    if "/" in x or "\\" in x or x.endswith(".jsonl"):
        return x
    return os.path.join(a.root, x + ".jsonl")


def _expect_from_args(a):
    spec = {}
    if a.expect_output is not None:
        spec["output_equals"] = a.expect_output
    if a.expect_match is not None:
        spec["output_matches"] = a.expect_match
    if a.used_tool:
        spec["used_tool"] = list(a.used_tool)
    if a.never_call:
        spec["did_not_call"] = list(a.never_call)
    if a.max_steps is not None:
        spec["max_steps"] = a.max_steps
    if a.no_step_failed:
        spec["no_step_failed"] = True
    return spec


def cmd_case_save(a):
    try:
        p = cases.save(a.name, _case_path(a, a.recording), a.entry,
                       root=a.root, expect=_expect_from_args(a),
                       description=a.description or "",
                       tags=dict(kv.split("=", 1) for kv in (a.tag or [])))
    except cases.CaseError as e:
        print("  %s" % e)
        sys.exit(ci.EXIT_CANNOT_RUN)
    except ValueError as e:
        print("  --tag must be key=value: %s" % e)
        sys.exit(ci.EXIT_CANNOT_RUN)
    print("  saved case %r -> %s" % (a.name, p))
    spec = _expect_from_args(a)
    if spec:
        for k, v in sorted(spec.items()):
            print("    expects %s: %s" % (k, v))
    else:
        print("    no expectations declared — this case checks only that the")
        print("    run still replays. Add --used-tool, --never-call, "
              "--expect-output ...")


def cmd_case_list(a):
    rows = cases.list_cases(a.root)
    if not rows:
        print("  no cases under %s" % os.path.join(a.root, cases.DIRNAME))
        print("  orientim case save <recording> --name <name> "
              "--entry module:function")
        return
    for c in rows:
        if c.get("broken"):
            print("  !  %-20s %s" % (c["name"], c["broken"]))
            continue
        n = len(c.get("expect") or {})
        tags = " ".join("%s=%s" % kv for kv in sorted((c.get("tags") or {}).items()))
        print("     %-20s %-28s %d expectation(s)  %s"
              % (c["name"], c.get("entry", "?"), n, tags))
        if c.get("description"):
            print("       %s" % c["description"])


def cmd_case_delete(a):
    try:
        print("  deleted %s" % cases.delete(a.name, a.root))
    except cases.CaseError as e:
        print("  %s" % e)
        sys.exit(ci.EXIT_CANNOT_RUN)


def cmd_case_run(a):
    try:
        case = cases.load(a.name, a.root)
    except cases.CaseError as e:
        print("  %s" % e)
        sys.exit(ci.EXIT_CANNOT_RUN)
    row = cases.run(case, strict=not a.loose)
    print(cases.summary([row], not a.loose))
    sys.exit(ci.EXIT_OK if row["ok"] else ci.EXIT_CHANGED)


def cmd_case_run_all(a):
    a.case = None
    a.baseline = None
    a.report = None
    a.no_evidence = False
    a.no_fail = False
    return cmd_test(a)


# --- baselines ----------------------------------------------------------------

def cmd_baseline_create(a):
    rows = cases.run_all(a.root, strict=not a.loose)
    if not rows:
        print("  no cases to freeze — save one first")
        sys.exit(ci.EXIT_CANNOT_RUN)
    print(cases.summary(rows, not a.loose))
    p = baselines.create(a.name, rows, root=a.root, strict=not a.loose,
                         note=a.note or "")
    failed = [r for r in rows if not r["ok"]]
    print("  baseline %r written to %s" % (a.name, p))
    if failed:
        # Freezing a red suite is legitimate — it is how you record where you
        # are before starting to fix it — but it must not happen silently.
        print("  note: %d of %d case(s) were failing when this was frozen."
              % (len(failed), len(rows)))


def cmd_baseline_list(a):
    rows = baselines.list_baselines(a.root)
    if not rows:
        print("  no baselines under %s"
              % os.path.join(a.root, baselines.DIRNAME))
        print("  orientim baseline create <name>")
        return
    for b in rows:
        if b.get("broken"):
            print("  !  %-20s %s" % (b["name"], b["broken"]))
            continue
        t = b.get("totals") or {}
        print("     %-20s %s  %d case(s), %d failing  %s"
              % (b["name"], _fmt(b.get("created_at") or 0),
                 t.get("cases", 0), t.get("failed", 0),
                 (b.get("commit") or "")[:12]))
        if b.get("note"):
            print("       %s" % b["note"])


def cmd_baseline_delete(a):
    try:
        print("  deleted %s" % baselines.delete(a.name, a.root))
    except baselines.BaselineError as e:
        print("  %s" % e)
        sys.exit(ci.EXIT_CANNOT_RUN)


def cmd_baseline_compare(a):
    try:
        base = baselines.load_any(a.name, a.root)
    except baselines.BaselineError as e:
        print("  %s" % e)
        sys.exit(ci.EXIT_CANNOT_RUN)
    rows = cases.run_all(a.root, strict=not a.loose)
    if not rows:
        print("  no cases to compare")
        sys.exit(ci.EXIT_CANNOT_RUN)
    cmp_ = baselines.compare(rows, base)
    print(cases.summary(rows, not a.loose))
    print(baselines.describe(cmp_, base))
    # Only a *new* failure fails this command. A case that was already red in
    # the baseline is not news, and failing on it would make the command
    # useless for the situation it exists for: adopting the tool on a suite
    # that is not green yet.
    sys.exit(ci.EXIT_CHANGED if cmp_["newly_changed"] else ci.EXIT_OK)


# --- test ---------------------------------------------------------------------

def cmd_test(a):
    """Replay every case, evaluate it, and compare to a baseline."""
    names = [a.case] if getattr(a, "case", None) else None
    rows = cases.run_all(a.root, strict=not a.loose, names=names)
    if not rows:
        print(cases.summary(rows, not a.loose))
        sys.exit(ci.EXIT_CANNOT_RUN)

    base, cmp_ = None, None
    if getattr(a, "baseline", None):
        try:
            base = baselines.load_any(a.baseline, a.root)
        except baselines.BaselineError as e:
            print("  %s" % e)
            sys.exit(ci.EXIT_CANNOT_RUN)
        cmp_ = baselines.compare(rows, base)

    print(cases.summary(rows, not a.loose, cmp_))

    if getattr(a, "report", None):
        rep = cases.report(rows, not a.loose, base,
                           evidence=not getattr(a, "no_evidence", False))
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, default=str)
        print("  report: %s" % a.report)

    if getattr(a, "no_fail", False):
        sys.exit(ci.EXIT_OK)
    # With a baseline, only what *this change* broke fails the build. Without
    # one, any failing case does.
    bad = cmp_["newly_changed"] if cmp_ else [r for r in rows if not r["ok"]]
    sys.exit(ci.EXIT_CHANGED if bad else ci.EXIT_OK)


def main(argv=None):
    p = argparse.ArgumentParser(prog="Orientim")
    p.add_argument("--root", default="runs")
    sub = p.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("ls"); l.add_argument("--status"); l.add_argument("--on")
    l.add_argument("--tag")
    l.add_argument("--trace", help="find the run behind an OpenTelemetry trace id")
    l.set_defaults(f=cmd_ls)
    pl = sub.add_parser("play"); pl.add_argument("run_id")
    pl.add_argument("--step", action="store_true"); pl.set_defaults(f=cmd_play)
    v = sub.add_parser("view"); v.add_argument("run_id")
    v.add_argument("--no-open", action="store_true")
    v.add_argument("--entry", help="module:function that starts the agent, e.g. myapp.agent:run")
    v.add_argument("--port", type=int, default=8740)
    v.set_defaults(f=cmd_view)
    st = sub.add_parser("stability")
    st.add_argument("--entry", required=True,
                    help="module:function that starts the agent, e.g. myapp.agent:run")
    st.add_argument("--runs", type=int, default=30)
    st.set_defaults(f=cmd_stability)
    df = sub.add_parser("diff", help="explain how two executions differ")
    df.add_argument("a", nargs="?", help="a run id, or a path to a recording")
    df.add_argument("b", nargs="?")
    df.add_argument("--case", metavar="NAME",
                    help="diff a case's recording against a fresh replay of it")
    df.add_argument("--json", action="store_true",
                    help="machine-readable output")
    df.add_argument("--verbose", action="store_true",
                    help="show unchanged steps too")
    df.add_argument("--loose", action="store_true",
                    help="ignore whitespace, key order and float rounding")
    df.set_defaults(f=cmd_diff)
    pr = sub.add_parser("prune", help="apply a retention policy")
    pr.add_argument("--keep", type=int, metavar="N",
                    help="keep only the newest N recordings")
    pr.add_argument("--older-than", type=float, metavar="DAYS",
                    help="remove anything older than this")
    pr.add_argument("--max-bytes", type=int, metavar="N",
                    help="keep newest recordings within this budget")
    pr.add_argument("--per-signature", type=int, metavar="N",
                    help="keep at most N copies of each distinct failure")
    pr.add_argument("--dry-run", action="store_true",
                    help="say what would go, remove nothing")
    pr.set_defaults(f=cmd_prune)
    c = sub.add_parser("ci", help="replay a whole store and judge the build")
    c.add_argument("--entry", required=True,
                   help="module:function that starts the agent")
    c.add_argument("--recordings", metavar="PATH",
                   help="where the recordings are (defaults to --root)")
    c.add_argument("--report", metavar="PATH",
                   help="write a machine-readable report: ids, verdicts, hashes")
    c.add_argument("--baseline", metavar="PATH",
                   help="an earlier report, to say what changed on THIS change")
    c.add_argument("--loose", action="store_true",
                   help="ignore whitespace, key order and float rounding")
    c.add_argument("--no-fail", action="store_true",
                   help="report but always exit 0")
    c.set_defaults(f=cmd_ci)
    cs = sub.add_parser("case", help="save and run a recording as a test case")
    cssub = cs.add_subparsers(dest="sub", required=True)
    csv_ = cssub.add_parser("save", help="turn a recording into a case")
    csv_.add_argument("recording", help="a run id, or a path to a recording")
    csv_.add_argument("--name", required=True)
    csv_.add_argument("--entry", required=True,
                      help="module:function that starts the agent")
    csv_.add_argument("--description", help="why this case is kept")
    csv_.add_argument("--tag", action="append", metavar="K=V")
    csv_.add_argument("--expect-output", metavar="TEXT",
                      help="the answer must be exactly this")
    csv_.add_argument("--expect-match", metavar="REGEX",
                      help="the answer must match this")
    csv_.add_argument("--used-tool", action="append", metavar="NAME",
                      help="the model must ask for this tool (repeatable)")
    csv_.add_argument("--never-call", action="append", metavar="NAME",
                      help="the model must never ask for this tool (repeatable)")
    csv_.add_argument("--max-steps", type=int, metavar="N")
    csv_.add_argument("--no-step-failed", action="store_true")
    csv_.set_defaults(f=cmd_case_save)
    csl = cssub.add_parser("list"); csl.set_defaults(f=cmd_case_list)
    csd = cssub.add_parser("delete"); csd.add_argument("name")
    csd.set_defaults(f=cmd_case_delete)
    csr = cssub.add_parser("run"); csr.add_argument("name")
    csr.add_argument("--loose", action="store_true")
    csr.set_defaults(f=cmd_case_run)
    csa = cssub.add_parser("run-all", help="every case, like `orientim test`")
    csa.add_argument("--loose", action="store_true")
    csa.set_defaults(f=cmd_case_run_all)

    bl = sub.add_parser("baseline", help="freeze what the suite says today")
    blsub = bl.add_subparsers(dest="sub", required=True)
    blc = blsub.add_parser("create", help="run every case and store the result")
    blc.add_argument("name")
    blc.add_argument("--note", help="why this baseline exists")
    blc.add_argument("--loose", action="store_true")
    blc.set_defaults(f=cmd_baseline_create)
    bll = blsub.add_parser("list"); bll.set_defaults(f=cmd_baseline_list)
    bld = blsub.add_parser("delete"); bld.add_argument("name")
    bld.set_defaults(f=cmd_baseline_delete)
    blx = blsub.add_parser("compare",
                           help="run every case and say what moved since")
    blx.add_argument("name", help="a baseline name, or a path to a report")
    blx.add_argument("--loose", action="store_true")
    blx.set_defaults(f=cmd_baseline_compare)

    t = sub.add_parser("test",
                       help="run every case: replay, evaluate, compare")
    t.add_argument("--case", metavar="NAME", help="just this one")
    t.add_argument("--baseline", metavar="NAME|PATH",
                   help="fail only on what THIS change broke")
    t.add_argument("--report", metavar="PATH",
                   help="write the machine-readable result")
    t.add_argument("--no-evidence", action="store_true",
                   help="leave prompts, answers and tool arguments out of "
                        "--report")
    t.add_argument("--loose", action="store_true",
                   help="ignore whitespace, key order and float rounding")
    t.add_argument("--no-fail", action="store_true",
                   help="report but always exit 0")
    t.set_defaults(f=cmd_test)

    cf = sub.add_parser("conformance")
    cf.add_argument("--save", metavar="PATH", help="write the report as JSON")
    cf.add_argument("--strict", action="store_true",
                    help="demand identical request bytes instead of normalised")
    cf.set_defaults(f=cmd_conformance)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
