# -*- coding: utf-8 -*-
"""Two executions, side by side, with the reason and what followed it.

Three questions, three answers, and this module is the third:

    replay      where did it change
    evaluation  which rule broke
    diff        how did the execution change, and what followed

Nothing here is a parallel system. It reads the alignment from `align`, the
field-level differences from `explain`, the execution model from a recording or
from `Divergence.replay_steps`, and evaluation results from `evaluate`. It adds
one thing of its own: putting them in an order a person can read.

The comparison is an explanation layer and never a verdict. It does not touch
the hash chain, the lookup key, or how a replay decides a match — if the
alignment here were wrong the report would be confusing, and nothing would be
judged incorrectly.
"""
import json
import sys

from . import (align, chain, concurrency, explain, model, store)

SCHEMA = 1


def _glyphs():
    """Arrows where the console can print them, ASCII where it cannot."""
    fancy = {"down": "\u2193", "moved": "\u21bb", "arrow": "\u2192",
             "dash": "\u2014"}
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        for v in fancy.values():
            v.encode(enc)
        return fancy
    except (UnicodeEncodeError, LookupError, TypeError):
        return {"down": "v", "moved": "~", "arrow": "->", "dash": "-"}


G = _glyphs()


def _label(step):
    if not step:
        return ""
    url = (step.get("url") or "").split("?")[0].rstrip("/")
    tail = url.split("/")[-1] or "/"
    role = step.get("role")
    mark = "*" if role == model.MODEL else " "
    return "%s%s %s" % (mark, step.get("method", "?"), tail)


def _side(meta, steps, name):
    http = [s for s in steps if s.get("t") == "http"]
    return {
        "run_id": (meta or {}).get("run_id", name),
        "name": name,
        "steps": len(http),
        "trigger": (meta or {}).get("trigger"),
        "agent": (meta or {}).get("agent"),
        "runtime": (meta or {}).get("runtime"),
        "outcome": (meta or {}).get("outcome"),
    }


def compare_executions(meta_a, steps_a, meta_b, steps_b, strict=True,
                       name_a="A", name_b="B", evaluation=None, verdict=None):
    """The comparison, over two executions in memory.

    `evaluation` is a list of evaluate.Result dicts for the second execution, so
    a case run can hand in what its rules said. `verdict` is a replay diagnosis
    code, for the same reason.
    """
    field = "key_strict" if strict else "key_loose"
    http_a = [s for s in steps_a if s.get("t") == "http"]
    http_b = [s for s in steps_b if s.get("t") == "http"]

    plan = align.plan(http_a, http_b, field)
    entries = plan["entries"]
    counts = align.summarise(entries)
    first = align.first_difference(entries)

    _ca, root_a = chain.build_steps(http_a, field)
    _cb, root_b = chain.build_steps(http_b, field)

    rows, model_all, later = [], [], 0
    seen_first = False
    for e in entries:
        a = http_a[e["a"]] if e.get("a") is not None else None
        b = http_b[e["b"]] if e.get("b") is not None else None
        row = {"op": e["op"], "a": e.get("a"), "b": e.get("b"),
               "label_a": _label(a), "label_b": _label(b),
               "status_a": (a or {}).get("status"),
               "status_b": (b or {}).get("status"),
               "role": (b or a or {}).get("role")}
        if e["op"] == align.REORDERED:
            row["moved_from"] = e.get("moved_from")
            row["moved_to"] = e.get("moved_to")
            row["also_changed"] = e.get("also_changed", False)
        if e["op"] != align.SAME:
            if seen_first:
                later += 1
            seen_first = True
        if a is not None and b is not None and e["op"] != align.SAME:
            # One side may be a request the replay could not match. It has no
            # response, so comparing the response side would report "12 tokens
            # -> None" about something that never happened. The request side is
            # real and is compared as usual.
            no_response = bool(a.get("unmatched") or b.get("unmatched"))
            row["unmatched"] = no_response
            row["why"] = _why(a, b, field)
            mc = explain.model_changes(a, b, request_only=no_response)
            if mc:
                row["model"] = mc
                model_all.extend(mc)
            if not no_response:
                tc = explain.step_tool_changes(a, b)
                if tc:
                    row["tools"] = tc
            rq = explain.body_diff(a.get("req"), b.get("req"))
            if rq:
                row["request_body"] = rq
            if not no_response and not a.get("b64") and not b.get("b64"):
                rs = explain.body_diff(a.get("body"), b.get("body"))
                if rs:
                    row["response_body"] = rs
        rows.append(row)

    # When things ran, beside what. A reader over t0/ms, which are already
    # stored and already outside the digest, so this cannot move a replay
    # verdict — see concurrency.py.
    conc = concurrency.compare(steps_a, steps_b, meta_a, meta_b)
    conc_decision = concurrency.policy(conc)

    # Not "no tools changed" - "this pair cannot answer that question". The
    # difference matters: the first is a finding, the second is a limit, and
    # printing the first when the second is true is how a reader concludes an
    # agent stopped calling a tool it in fact still calls.
    tools_blind = explain.unreadable_tool_view(steps_a, steps_b)
    tools_run = [] if tools_blind else explain.run_tool_changes(steps_a, steps_b)
    out_diff = explain.output_diff((meta_a or {}).get("outcome"),
                                   (meta_b or {}).get("outcome"))
    runtime = model.runtime_differences((meta_a or {}).get("runtime"),
                                        (meta_b or {}).get("runtime"))
    chain_ = explain.consequences(first, model_all, tools_run, out_diff,
                                  evaluation=evaluation, later_differences=later)

    identical = (counts[align.CHANGED] == 0 and counts[align.INSERTED] == 0
                 and counts[align.DELETED] == 0 and counts[align.REORDERED] == 0
                 and out_diff.get("state") in ("unchanged", "not declared"))
    # A concurrency finding sits beside `identical`, never inside it by
    # default: two runs whose calls and answers match are the same run as
    # far as the bytes go. Whether their scheduling differed is a separate
    # question, and `policy` — not this line — decides what it costs.
    if conc["findings"]:
        identical = identical and conc_decision["ok"]

    return {
        "schema": SCHEMA,
        "kind": "orientim-diff",
        "strict": bool(strict),
        "a": _side(meta_a, steps_a, name_a),
        "b": _side(meta_b, steps_b, name_b),
        "a_root": root_a, "b_root": root_b,
        "identical": identical,
        "counts": counts,
        "degraded": plan["degraded"],
        "degraded_reason": plan["reason"],
        "first_difference": first,
        "steps": rows,
        "model_changes": model_all,
        "tool_changes": tools_run,
        "tool_view_unreadable": tools_blind,
        "output": out_diff,
        "runtime_changes": runtime,
        "concurrency": conc,
        "concurrency_policy": conc_decision,
        "evaluation": list(evaluation or []),
        "verdict": verdict,
        "consequence": chain_,
    }


def merge_unmatched(steps, unmatched):
    """Put the requests a replay could not match back where they happened.

    Without this, a run whose requests all changed reports as a run that made no
    requests at all - every recorded step "deleted" - which is true of the
    matching and false about the agent.

    A merged-in request carries `unmatched: True` and status 599, because 599
    is what the agent actually received.
    """
    if not unmatched:
        return list(steps)
    out = list(steps)
    for u in unmatched:
        pos = u.get("order", len(out))
        at = len(out)
        for i, s in enumerate(out):
            if s.get("i") is not None and s["i"] >= pos:
                at = i
                break
        out.insert(at, u)
    return out


def _why(a, b, field):
    """The narrowest true statement about why two aligned steps differ."""
    if b.get("unmatched"):
        # Present in the recording is not the same as available: replay serves
        # steps in the recorded order, so a step can exist and still be
        # unreachable when the agent asks for it out of turn.
        return "not matched, so the agent got a 599"
    if a.get("unmatched"):
        return "not matched on the other side"
    if a.get(field) != b.get(field):
        return "different request"
    if a.get("hdr_fp") != b.get("hdr_fp"):
        return "different request headers"
    if a.get("status") != b.get("status"):
        return "different status"
    if a.get("body_sha") != b.get("body_sha"):
        return "different response body"
    if a.get("error") != b.get("error"):
        return "different error"
    return "different"


# --- entry points -------------------------------------------------------------

def compare(path_a, path_b, strict=True):
    """Two recordings on disk."""
    meta_a, steps_a = store.load(path_a)
    meta_b, steps_b = store.load(path_b)
    return compare_executions(
        meta_a, steps_a, meta_b, steps_b, strict=strict,
        name_a=(meta_a or {}).get("run_id", path_a),
        name_b=(meta_b or {}).get("run_id", path_b))


def compare_case(case, strict=True, entry_loader=None, root="runs"):
    """A case's recording against a fresh replay of it, with its rules applied.

    This is the shape the question usually arrives in: not "these two files"
    but "this case used to pass, what is it doing now". Replay says where,
    evaluation says which rule, and this puts both next to the execution.
    """
    from . import cases
    row = cases.run(case, strict=strict, entry_loader=entry_loader,
                    keep_execution=True)
    meta_a, steps_a = store.load(case["recording"])
    steps_b = merge_unmatched(row.get("_replay_steps") or [],
                              row.get("_unmatched") or [])
    meta_b = {"run_id": (case.get("run_id") or "replay") + "_now",
              "outcome": row.get("_replay_output"),
              "agent": case.get("agent"),
              "runtime": model.runtime_info()}
    out = compare_executions(
        meta_a, steps_a, meta_b, steps_b, strict=strict,
        name_a=case.get("run_id") or "recorded", name_b="now",
        evaluation=(row.get("evaluation") or {}).get("results"),
        verdict=row.get("verdict"))
    out["case"] = case.get("name")
    out["ok"] = row.get("ok")
    out["reason"] = row.get("reason")
    return out


# --- reporting ----------------------------------------------------------------

_OP_MARK = {align.SAME: "   ", align.CHANGED: " ~ ", align.INSERTED: " + ",
            align.DELETED: " - ", align.REORDERED: " " + G["moved"] + " "}


def _pos(e, key):
    v = e.get(key)
    return "  " if v is None else "%2d" % v


def report(cmp, width=74, verbose=False):
    """The half a person reads."""
    a, b = cmp["a"], cmp["b"]
    L = ["", "=" * width]
    title = cmp.get("case") or "%s  vs  %s" % (a["name"], b["name"])
    L.append("  %s: %s" % ("CHANGED" if not cmp["identical"] else "UNCHANGED",
                           title))
    L.append("=" * width)
    L.append("")
    L.append("  A  %-18s %3d steps  %s" % (a["name"], a["steps"],
                                           cmp["a_root"][:12]))
    L.append("  B  %-18s %3d steps  %s" % (b["name"], b["steps"],
                                           cmp["b_root"][:12]))
    if cmp.get("verdict"):
        L.append("  replay verdict: %s" % cmp["verdict"])
    L.append("")

    if cmp["identical"]:
        L.append("  OK  Same steps, same order, same responses, same answer.")
        L.append("")
        if (cmp.get("concurrency") or {}).get("findings"):
            # Same bytes, different scheduling. Worth saying even
            # when nothing failed: it is the one difference a byte
            # comparison cannot express.
            L.append(concurrency.report(cmp["concurrency"],
                                        cmp.get("concurrency_policy")))
        return "\n".join(L)

    c = cmp["counts"]
    L.append("  %d changed, %d inserted, %d deleted, %d reordered, %d unchanged"
             % (c[align.CHANGED], c[align.INSERTED], c[align.DELETED],
                c[align.REORDERED], c[align.SAME]))
    if cmp.get("degraded"):
        L.append("  NOTE: %s." % cmp["degraded_reason"])
    L.append("")

    n = 0
    for section in (_model_section(cmp), _tool_section(cmp),
                    _output_section(cmp), _evaluation_section(cmp)):
        if section:
            n += 1
            L.append("  %d. %s" % (n, section[0]))
            L += ["     " + line for line in section[1]]
            L.append("")

    L.append("  STEPS")
    for r in cmp["steps"]:
        if r["op"] == align.SAME and not verbose:
            continue
        note = r.get("why", "")
        if r["op"] == align.REORDERED:
            note = "moved from %s to %s%s" % (
                r.get("moved_from"), r.get("moved_to"),
                ", and changed" if r.get("also_changed") else "")
        L.append("    %s %s->%s  %-22s %-22s %s"
                 % (_OP_MARK.get(r["op"], "  "), _pos(r, "a"), _pos(r, "b"),
                    r["label_a"] or G["dash"], r["label_b"] or G["dash"],
                    note))
        for m in (r.get("model") or [])[:6]:
            L.append("            %s: %s -> %s"
                     % (m["field"], m["was"], m["now"]))
        for body_key, label in (("request_body", "request"),
                                ("response_body", "response")):
            bd = r.get(body_key)
            if bd:
                L += ["            " + line for line in _body_lines(bd, label)]
    L.append("")

    if (cmp.get("concurrency") or {}).get("findings"):
        L.append(concurrency.report(cmp["concurrency"],
                                    cmp.get("concurrency_policy")))
    L += _consequence_lines(cmp)
    if cmp.get("runtime_changes"):
        L.append("  RUNTIME")
        for rc in cmp["runtime_changes"][:6]:
            L.append("    %s: %s -> %s" % (rc["what"], rc["was"] or "-",
                                           rc["now"] or "-"))
        L.append("    (recorded, not replayable - see docs/limits.md)")
        L.append("")
    if not cmp["strict"]:
        L.append("  (loose key: whitespace, key order and float rounding ignored)")
        L.append("")
    return "\n".join(L)


def _model_section(cmp):
    if not cmp["model_changes"]:
        return None
    lines = ["%s: %s -> %s" % (m["field"], m["was"], m["now"])
             for m in cmp["model_changes"][:10]]
    return ("MODEL CONFIG", lines)


def _tool_section(cmp):
    blind = cmp.get("tool_view_unreadable")
    if blind:
        side = cmp.get(blind["side"], {}).get("name") or blind["side"].upper()
        return ("TOOL DECISION", [
            "not readable from this pair: all %d model call(s) on the %s side "
            "went unanswered," % (blind["model_calls"], side),
            "so no response exists that a tool request could have been in.",
            "the other side requested: %s"
            % (", ".join(blind["named_by_the_other_side"]) or "no tools"),
            "(what this run asked for is unknown, not unchanged - "
            "record both sides to compare)"])
    changes = cmp["tool_changes"]
    if not changes:
        return None
    lines = []
    for t in changes[:12]:
        if t["change"] == "removed":
            lines.append("no longer requested: %s(%s)"
                         % (t["name"], _args(t.get("arguments"))))
        elif t["change"] == "added":
            lines.append("newly requested:     %s(%s)"
                         % (t["name"], _args(t.get("arguments"))))
        elif t["change"] == "arguments":
            lines.append("%s arguments changed:" % t["name"])
            lines.append("  was: %s" % _args(t.get("was")))
            lines.append("  now: %s" % _args(t.get("now")))
            if t.get("was_partial") or t.get("now_partial"):
                lines.append("  (one side is a partial stream - "
                             "the fragment is what arrived, not what was meant)")
        elif t["change"] == "reordered":
            lines.append("%s moved: step %s -> step %s%s"
                         % (t["name"], t.get("step_a"), t.get("step_b"),
                            ", arguments also changed"
                            if t.get("arguments_changed") else ""))
        elif t["change"] == "partial":
            lines.append("%s: stream completeness changed (%s -> %s)"
                         % (t["name"], t.get("was_partial"), t.get("now_partial")))
    lines.append("(a tool request is what the model asked for; whether the "
                 "agent ran it")
    lines.append(" is not recorded - see docs/execution-model.md)")
    return ("TOOL DECISION", lines)


def _args(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(v)


def _output_section(cmp):
    o = cmp["output"]
    if o.get("state") == "unchanged":
        return None
    if o.get("state") in ("not declared", "unknown"):
        return ("OUTPUT", [o.get("note", o["state"])])
    lines = ["expected: %s" % o.get("a"), "actual:   %s" % o.get("b"),
             "digest:   %s -> %s" % ((o.get("sha_a") or "")[:12],
                                     (o.get("sha_b") or "")[:12])]
    if o.get("truncated"):
        lines.append("(%s)" % o.get("note"))
    st = o.get("structured")
    if st:
        lines += _body_lines(st, "answer")
    return ("OUTPUT", lines)


def _body_lines(bd, label):
    lines = []
    if bd["kind"] == "json":
        for x in bd["added"][:6]:
            lines.append("%s + %s = %s" % (label, x["path"], x["value"]))
        for x in bd["removed"][:6]:
            lines.append("%s - %s (was %s)" % (label, x["path"], x["value"]))
        for x in bd["changed"][:6]:
            lines.append("%s ~ %s: %s -> %s" % (label, x["path"], x["was"],
                                                x["now"]))
        if bd.get("truncated"):
            lines.append("%s   ... %d field(s) in total" % (label, bd["n"]))
    else:
        for ln in bd["lines"][:8]:
            lines.append("%s %s" % (label, ln))
        if bd.get("truncated"):
            lines.append("%s   ... %d line(s) in total" % (label, bd["n"]))
    return lines


def _evaluation_section(cmp):
    failed = [r for r in cmp.get("evaluation") or [] if r.get("status") == "fail"]
    warned = [r for r in cmp.get("evaluation") or [] if r.get("status") == "warn"]
    if not failed and not warned:
        return None
    lines = ["FAIL %s: %s" % (r.get("evaluator"), r.get("reason"))
             for r in failed]
    lines += ["WARN %s: %s" % (r.get("evaluator"), r.get("reason"))
              for r in warned]
    return ("EVALUATION", lines)


def _consequence_lines(cmp):
    ch = cmp["consequence"]
    if not ch["nodes"]:
        return []
    L = ["  CONSEQUENCE"]
    order, seen = [], set()
    by_from = {}
    for link in ch["links"]:
        by_from.setdefault(link["from"], []).append(link)

    def walk(i, depth):
        if i in seen or depth > 6:
            return
        seen.add(i)
        order.append((i, depth))
        for link in by_from.get(i, []):
            walk(link["to"], depth + 1)

    for i in range(len(ch["nodes"])):
        walk(i, 0)

    for i, depth in order:
        node = ch["nodes"][i]
        arrow = "" if depth == 0 else "  " * depth + G["down"] + " "
        L.append("    %s%s" % (arrow, node["text"]))
    L.append("")
    established = [l for l in ch["links"] if l["established"]]
    if len(established) != len(ch["links"]):
        L.append("    These are observations in order, not a causal chain:")
        L.append("    %s." % explain.NOT_ESTABLISHED)
    if established:
        L.append("    Established by the records: "
                 + "; ".join(sorted({l["note"] for l in established})))
    L.append("")
    return L


def as_json(cmp, indent=2):
    return json.dumps(cmp, indent=indent, default=str)
