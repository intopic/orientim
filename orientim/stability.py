# -*- coding: utf-8 -*-
"""Stability measurement — statistical process control, after Shewhart (1924).

Runs the same task N times and reports the spread of outcomes with control
limits. "It worked when I tried it" becomes one point on a chart.

The individuals-chart formula is the standard one:
    UCL = mean + 2.66 * MRbar        LCL = mean - 2.66 * MRbar
where MRbar is the mean absolute difference between consecutive runs.
"""
import collections
import hashlib
import re
import statistics
import time

from . import session


def _path_sig(steps):
    """Structural signature: which endpoints, in what order, with what status."""
    parts = []
    for s in steps:
        if s.get("t") != "http":
            continue
        endpoint = s.get("url", "").split("?")[0].rstrip("/").split("/")[-1]
        parts.append(f"{s.get('method')}:{endpoint}:{s.get('status')}")
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:10], parts


def _normalize(text):
    """Loose equivalence: lowercase, collapsed whitespace, digits masked.

    Not real semantic equivalence — that needs embeddings. This is the cheap
    approximation that catches "same answer, different number".
    """
    t = (text or "").lower()
    t = re.sub(r"\d+", "#", t)
    return " ".join(t.split())


def _limits(vals):
    """Control limits from the individuals chart."""
    if len(vals) < 2:
        return (0.0, 0.0, 0.0)
    mean = statistics.fmean(vals)
    mr = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    mrbar = statistics.fmean(mr) if mr else 0.0
    return (mean, max(mean - 2.66 * mrbar, 0.0), mean + 2.66 * mrbar)


def measure(fn, runs=30, root="runs/_stability", on_run=None):
    paths, outcomes, norm_outcomes = [], [], []
    steps_n, durations, results = [], [], []
    failed = 0

    for i in range(runs):
        t0 = time.monotonic()
        h = None
        try:
            with session.record(root=root) as h:
                out = fn(h)
                h.rec.trigger("stability")
        except Exception as e:
            out = f"<error:{type(e).__name__}>"
            failed += 1
        dur = (time.monotonic() - t0) * 1000.0
        if h is None:
            # record() itself failed. Reusing the previous run's holder here
            # would silently count that run twice.
            continue

        http = [s for s in h.rec.steps if s.get("t") == "http"]
        sig, parts = _path_sig(h.rec.steps)
        last_body = http[-1].get("body", "") if http else ""
        outcome = str(out) if out is not None else last_body

        paths.append(sig)
        outcomes.append(outcome)
        norm_outcomes.append(_normalize(outcome))
        steps_n.append(float(len(http)))
        durations.append(dur)
        results.append({"i": i, "sig": sig, "parts": parts, "out": outcome,
                        "steps": len(http), "ms": dur,
                        "run_id": h.rec.run_id, "path": h.path})
        if on_run:
            on_run(results[-1])

    def dist(seq):
        c = collections.Counter(seq)
        if not seq:
            # No run produced anything: runs=0, or every attempt failed before
            # record() yielded. most_common(1)[0] raised IndexError here, which
            # turned "nothing to measure" into a crash in the middle of a
            # command somebody ran to find out whether their agent was stable.
            return {"distinct": 0, "modal": None, "modal_n": 0,
                    "agreement": 0.0, "counter": c}
        top, n = c.most_common(1)[0]
        return {"distinct": len(c), "modal": top, "modal_n": n,
                "agreement": n / len(seq), "counter": c}

    s_mean, s_lo, s_hi = _limits(steps_n)
    d_mean, d_lo, d_hi = _limits(durations)
    out_of = [r for r in results
              if r["steps"] < s_lo or r["steps"] > s_hi
              or r["ms"] < d_lo or r["ms"] > d_hi]

    return {
        "runs": len(results),
        "requested": runs,
        "failed": failed,
        "root": root,
        "paths": dist(paths),
        "outcomes": dist(outcomes),
        "outcomes_loose": dist(norm_outcomes),
        "steps": {"mean": s_mean, "lcl": s_lo, "ucl": s_hi, "vals": steps_n},
        "duration": {"mean": d_mean, "lcl": d_lo, "ucl": d_hi, "vals": durations},
        "out_of_control": out_of,
        "results": results,
    }


def _spark(vals, lo, hi, width=44):
    """An individuals chart in the terminal: the points, and who fell outside."""
    if not vals:
        return ""
    vmin, vmax = min(vals + [lo]), max(vals + [hi])
    span = (vmax - vmin) or 1.0
    rows = 7
    grid = [[" "] * min(len(vals), width) for _ in range(rows)]
    step = max(len(vals) // width, 1)
    shown = vals[::step][:width]
    for x, v in enumerate(shown):
        y = rows - 1 - int((v - vmin) / span * (rows - 1))
        out = v < lo or v > hi
        grid[y][x] = "!" if out else "o"
    lines = []
    for y, row in enumerate(grid):
        val = vmax - (vmax - vmin) * y / (rows - 1)
        lines.append(f"   {val:8.0f} |{''.join(row)}")
    return "\n".join(lines)


def report(m, name=""):
    p, o, ol = m["paths"], m["outcomes"], m["outcomes_loose"]
    L = []
    L.append("")
    L.append("=" * 62)
    L.append(f"  STABILITY  ·  {name}  ·  {m['runs']} runs")
    L.append("=" * 62)
    L.append("")
    if m.get("failed"):
        # 30 runs that all raised look like perfect agreement otherwise: one
        # outcome, one path, 100%. Say it before the numbers, not after.
        L.append(f"  !!  {m['failed']} of {m.get('requested', m['runs'])} runs "
                 f"ended in an exception")
        L.append("")
    L.append(f"  Distinct paths            {p['distinct']}")
    L.append(f"  Distinct outcomes         {o['distinct']}   "
             f"({ol['distinct']} after normalising)")
    L.append("")
    agree = o["agreement"] * 100
    bar = "#" * int(agree / 100 * 34)
    L.append(f"  Agreement with modal      {agree:5.1f}%  |{bar:<34}|")
    diff = m["runs"] - o["modal_n"]
    if diff:
        L.append(f"                            {diff} of {m['runs']} runs differed")
    L.append("")
    s, d = m["steps"], m["duration"]
    L.append(f"  Steps      mean {s['mean']:6.2f}     control limits {s['lcl']:.2f} - {s['ucl']:.2f}")
    L.append(f"  Duration   mean {d['mean']:6.0f}ms   control limits {d['lcl']:.0f} - {d['ucl']:.0f} ms")
    L.append("")
    L.append("  Individuals chart — duration (ms)")
    L.append(_spark(d["vals"], d["lcl"], d["ucl"]))
    L.append("")
    n_out = len(m["out_of_control"])
    if n_out:
        L.append(f"  !!  {n_out} run(s) outside the control limits")
    else:
        L.append("  OK  every run inside the control limits")
    if p["distinct"] > 1:
        L.append("")
        L.append("  Paths seen:")
        for sig, n in p["counter"].most_common(5):
            ex = next(r for r in m["results"] if r["sig"] == sig)
            tag = "   <- modal" if sig == p["modal"] else ""
            L.append(f"    {n:>3}x  {' -> '.join(ex['parts'][:5])}{tag}")

    # Find the odd run, then hand over the command to go inside it.
    odd = [r for r in m["results"] if r["sig"] != p["modal"]]
    if odd or m["out_of_control"]:
        L.append("")
        L.append("  The odd runs — go inside them:")
        seen = set()
        for r in m["out_of_control"] + odd:
            if r["run_id"] in seen:
                continue
            seen.add(r["run_id"])
            why = []
            if r in m["out_of_control"]:
                why.append("outside control limits")
            if r["sig"] != p["modal"]:
                why.append("non-modal path")
            L.append(f"    Orientim view {r['run_id']} --root {m.get('root', '')}"
                     f"    # {', '.join(why)}")
            if len(seen) >= 3:
                break
    L.append("")
    return "\n".join(L)
