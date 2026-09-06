# -*- coding: utf-8 -*-
"""Find a run, scrub through it, and see the step where it went wrong."""
import argparse
import datetime as dt
import json
import os
import sys

from . import ci, diff, store, viewer, server, stability, conformance

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
    def _path(x):
        return x if ("/" in x or "\\" in x or x.endswith(".jsonl"))             else os.path.join(a.root, x + ".jsonl")
    print(diff.report(diff.compare(_path(a.a), _path(a.b), strict=not a.loose)))


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
    df = sub.add_parser("diff", help="compare two recordings of the same agent")
    df.add_argument("a"); df.add_argument("b")
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
    cf = sub.add_parser("conformance")
    cf.add_argument("--save", metavar="PATH", help="write the report as JSON")
    cf.add_argument("--strict", action="store_true",
                    help="demand identical request bytes instead of normalised")
    cf.set_defaults(f=cmd_conformance)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
