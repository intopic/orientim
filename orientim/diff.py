# -*- coding: utf-8 -*-
"""Two recordings, side by side.

replay() answers "does the code still do what it did". This answers "what did
Tuesday do that Wednesday did not" — two runs of the same agent, neither of
them the code you are holding. It is the question people ask second, and the
chain already had everything needed to answer it.
"""
from . import chain, store


def _label(step):
    url = (step.get("url") or "").split("?")[0].rstrip("/")
    return "%s %s" % (step.get("method", "?"), url.split("/")[-1] or "/")


def compare(path_a, path_b, strict=True):
    """Structural comparison of two recordings."""
    field = "key_strict" if strict else "key_loose"
    meta_a, steps_a = store.load(path_a)
    meta_b, steps_b = store.load(path_b)
    http_a = [s for s in steps_a if s.get("t") == "http"]
    http_b = [s for s in steps_b if s.get("t") == "http"]

    ca, root_a = chain.build_steps(http_a, field)
    cb, root_b = chain.build_steps(http_b, field)
    idx = chain.first_divergence(ca, cb)

    rows = []
    for i in range(max(len(http_a), len(http_b))):
        a = http_a[i] if i < len(http_a) else None
        b = http_b[i] if i < len(http_b) else None
        if a is None:
            state = "only in B"
        elif b is None:
            state = "only in A"
        elif chain.step_digest(a, field) == chain.step_digest(b, field):
            state = "same"
        elif a.get(field) != b.get(field):
            state = "different request"
        elif a.get("hdr_fp") != b.get("hdr_fp"):
            state = "different headers"
        elif a.get("status") != b.get("status"):
            state = "different status"
        else:
            state = "different response"
        rows.append({"i": i, "state": state,
                     "a": _label(a) if a else "",
                     "b": _label(b) if b else "",
                     "status_a": a.get("status") if a else None,
                     "status_b": b.get("status") if b else None})

    return {
        "a": {"run_id": (meta_a or {}).get("run_id", path_a), "steps": len(http_a),
              "root": root_a, "trigger": (meta_a or {}).get("trigger")},
        "b": {"run_id": (meta_b or {}).get("run_id", path_b), "steps": len(http_b),
              "root": root_b, "trigger": (meta_b or {}).get("trigger")},
        "identical": idx is None,
        "index": idx,
        "rows": rows,
        "strict": strict,
    }


def report(cmp, width=74):
    a, b = cmp["a"], cmp["b"]
    L = ["", "=" * width]
    L.append("  DIFF  ·  %s  vs  %s" % (a["run_id"], b["run_id"]))
    L.append("=" * width)
    L.append("")
    L.append("  A  %-16s %2d steps  %s  %s"
             % (a["run_id"], a["steps"], a["root"][:12], a["trigger"] or ""))
    L.append("  B  %-16s %2d steps  %s  %s"
             % (b["run_id"], b["steps"], b["root"][:12], b["trigger"] or ""))
    L.append("")
    if cmp["identical"]:
        L.append("  OK  The two runs are the same run. Same steps, same order,")
        L.append("      same responses. Nothing to look at.")
        L.append("")
        return "\n".join(L)

    L.append("  !!  They diverge at step %d." % cmp["index"])
    L.append("")
    for r in cmp["rows"]:
        mark = "  " if r["state"] == "same" else "->"
        note = "" if r["state"] == "same" else "   " + r["state"]
        L.append("  %s %2d  %-24s %-24s%s"
                 % (mark, r["i"], r["a"] or "—", r["b"] or "—", note))
    L.append("")
    if not cmp["strict"]:
        L.append("  (loose key: whitespace, key order and float rounding ignored)")
        L.append("")
    return "\n".join(L)
