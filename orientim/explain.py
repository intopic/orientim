# -*- coding: utf-8 -*-
"""What changed between two steps, and what followed it.

Three kinds of question, kept apart on purpose:

    field differences   model configuration, tool calls, bodies, the answer
    ordering            what moved, and what that means for what came after
    consequence         what was observed after a change — never why

The last one is where a tool like this is most tempting to lie. It is easy to
print "root cause: the temperature change", and it would often even be right.
But nothing in an HTTP recording establishes that one difference *caused*
another; what the records establish is that one came before the other, or that
one names the other. So a link is emitted with the evidence that supports it and
an explicit flag for whether it is established. When it is not:

    consequence observed, causal link not established

which is the honest sentence and, for somebody debugging, a more useful one than
a confident guess they have to verify anyway.
"""
import difflib
import json

from . import model

# Request-side model fields worth calling out by name, in the order a person
# reads them.
MODEL_FIELDS = ("model", "temperature", "top_p", "max_tokens", "seed", "stream",
                "tools_offered")
SERVED_FIELDS = ("model_served", "stop_reason")
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")

MAX_BODY_FIELDS = 25
MAX_TEXT_LINES = 12
MAX_VALUE = 200


def _short(v):
    if isinstance(v, str):
        return v if len(v) <= MAX_VALUE else v[:MAX_VALUE] + "…"
    if isinstance(v, (list, dict)):
        try:
            s = json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            s = str(v)
        return s if len(s) <= MAX_VALUE else s[:MAX_VALUE] + "…"
    return v


def _fields(a, b, names):
    out = []
    a, b = a or {}, b or {}
    for name in names:
        was, now = a.get(name), b.get(name)
        if was != now:
            out.append({"field": name, "was": _short(was), "now": _short(now)})
    return out


# --- model calls --------------------------------------------------------------

def model_changes(step_a, step_b, request_only=False):
    """Which model settings moved, by name.

    "the request body differs" is true and useless. "temperature 0.0 to 0.7" is
    the same fact in the form somebody can act on.

    request_only skips everything that comes from the response. Use it when one
    side never got one — otherwise the report says "output_tokens 8 -> None"
    about a call that was never answered, which reads as a change in the model
    and is nothing of the kind.
    """
    a, b = step_a or {}, step_b or {}
    out = _fields(a.get("model"), b.get("model"), MODEL_FIELDS)
    if request_only:
        return out
    out += _fields(a.get("served"), b.get("served"), SERVED_FIELDS)
    out += _fields((a.get("served") or {}).get("usage"),
                   (b.get("served") or {}).get("usage"), USAGE_FIELDS)
    return out


# --- tool calls ---------------------------------------------------------------

def _calls_of(step):
    return list(((step or {}).get("served") or {}).get("tool_calls") or [])


def tool_changes(calls_a, calls_b):
    """Align two lists of tool calls by name and say what happened to each.

    Four outcomes, and the fourth is the one an index-based comparison cannot
    express: the same tools in a different order.
    """
    names_a = [c.get("name") for c in calls_a]
    names_b = [c.get("name") for c in calls_b]
    out = []
    matcher = difflib.SequenceMatcher(None, names_a, names_b, autojunk=False)
    pending_removed = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                a, b = calls_a[i1 + k], calls_b[j1 + k]
                if a.get("arguments") != b.get("arguments"):
                    out.append({"change": "arguments", "name": a.get("name"),
                                "was": _short(a.get("arguments")),
                                "now": _short(b.get("arguments")),
                                "was_kind": a.get("arguments_kind"),
                                "now_kind": b.get("arguments_kind"),
                                "was_partial": a.get("partial", False),
                                "now_partial": b.get("partial", False),
                                "step_a": a.get("step"), "step_b": b.get("step")})
                elif a.get("partial") != b.get("partial"):
                    out.append({"change": "partial", "name": a.get("name"),
                                "was_partial": a.get("partial", False),
                                "now_partial": b.get("partial", False),
                                "step_a": a.get("step"), "step_b": b.get("step")})
        elif tag in ("replace", "delete"):
            for a in calls_a[i1:i2]:
                pending_removed.append(a)
            if tag == "replace":
                for b in calls_b[j1:j2]:
                    out.append({"change": "added", "name": b.get("name"),
                                "arguments": _short(b.get("arguments")),
                                "arguments_kind": b.get("arguments_kind"),
                                "partial": b.get("partial", False),
                                "step_b": b.get("step")})
        elif tag == "insert":
            for b in calls_b[j1:j2]:
                out.append({"change": "added", "name": b.get("name"),
                            "arguments": _short(b.get("arguments")),
                            "arguments_kind": b.get("arguments_kind"),
                            "partial": b.get("partial", False),
                            "step_b": b.get("step")})

    # A tool that left one position and appears in another is one move.
    added = {r["name"]: r for r in out if r["change"] == "added"}
    for a in pending_removed:
        name = a.get("name")
        if name in added:
            moved = added.pop(name)
            out = [r for r in out if r is not moved]
            out.append({"change": "reordered", "name": name,
                        "step_a": a.get("step"), "step_b": moved.get("step_b"),
                        "arguments_changed":
                            a.get("arguments") != moved.get("arguments")})
        else:
            out.append({"change": "removed", "name": name,
                        "arguments": _short(a.get("arguments")),
                        "arguments_kind": a.get("arguments_kind"),
                        "partial": a.get("partial", False),
                        "step_a": a.get("step")})
    return out


def run_tool_changes(steps_a, steps_b):
    """The same comparison over whole runs, which is what an evaluator asks about."""
    return tool_changes(model.tool_calls_in(steps_a), model.tool_calls_in(steps_b))


def unreadable_tool_view(steps_a, steps_b):
    """Whether a tool comparison between these two runs can carry any meaning.

    A tool request lives in a model *response*. When every model call on one
    side went unanswered - what a replay does to a run that diverged at its
    first request - that side has no responses, so every tool the other side
    requested compares as "removed" whatever the agent did. The statement is
    true of the run and worthless as evidence about the change, and a reader
    takes it for a finding.

    `model_changes(request_only=True)` already refuses the same trade for the
    same reason. Returns None when the comparison is readable, otherwise which
    side is blind and the names the other side did request - the names are
    context, not a claim about what moved.

    The side is `"a"` or `"b"`, not a label: the caller names the two sides and
    only the caller knows whether B is "now", a replay, or a recording from
    last March.
    """
    for name, steps, other in (("a", steps_a, steps_b),
                               ("b", steps_b, steps_a)):
        calls = [s for s in (steps or [])
                 if s.get("t") == "http" and s.get("role") == model.MODEL]
        if calls and all(s.get("unmatched") for s in calls):
            return {"side": name, "model_calls": len(calls),
                    "named_by_the_other_side":
                        sorted(set(model.tool_names_in(other)))}
    return None


# --- bodies -------------------------------------------------------------------
# Everything read here is already redacted: a recording stores request and
# response bodies through transport.redact_body before they reach disk. Nothing
# in this module parses for secrets, and nothing should — a second redaction
# implementation is a second one to get wrong, and it would drift from the first.

def _flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, "%s.%s" % (prefix, k) if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, "%s[%d]" % (prefix, i)))
    else:
        out[prefix or "."] = obj
    return out


def body_diff(text_a, text_b):
    """A structured difference where the bodies are JSON, a text one otherwise.

    Returns None when they are equal, so a caller can test for a difference
    without reading the shape.
    """
    if (text_a or "") == (text_b or ""):
        return None
    a, b = model._as_json(text_a), model._as_json(text_b)
    if isinstance(a, (dict, list)) and isinstance(b, (dict, list)):
        fa, fb = _flatten(a), _flatten(b)
        added = [{"path": k, "value": _short(fb[k])}
                 for k in sorted(set(fb) - set(fa))]
        removed = [{"path": k, "value": _short(fa[k])}
                   for k in sorted(set(fa) - set(fb))]
        changed = [{"path": k, "was": _short(fa[k]), "now": _short(fb[k])}
                   for k in sorted(set(fa) & set(fb)) if fa[k] != fb[k]]
        total = len(added) + len(removed) + len(changed)
        return {"kind": "json", "added": added[:MAX_BODY_FIELDS],
                "removed": removed[:MAX_BODY_FIELDS],
                "changed": changed[:MAX_BODY_FIELDS],
                "n": total,
                "truncated": total > MAX_BODY_FIELDS}
    lines = list(difflib.unified_diff(
        (text_a or "").splitlines(), (text_b or "").splitlines(),
        lineterm="", n=1))[2:]
    return {"kind": "text", "lines": [ln[:MAX_VALUE] for ln in lines[:MAX_TEXT_LINES]],
            "n": len(lines), "truncated": len(lines) > MAX_TEXT_LINES}


# --- the final answer ---------------------------------------------------------

def output_diff(out_a, out_b):
    """Compare two declared outputs, without ever overclaiming.

    The digest is taken over the whole value before truncation, so "changed" and
    "unchanged" are always safe to say. What is *shown* may be a prefix, and
    when it is, the report says so rather than letting a reader assume the
    visible text is the whole difference.
    """
    if not out_a and not out_b:
        return {"state": "not declared",
                "note": "neither run declared an output"}
    if not out_a or not out_b:
        which = "the second" if out_a else "the first"
        return {"state": "unknown",
                "note": "only %s run declared an output, so there is nothing "
                        "to compare" % ("the first" if out_a else "the second"),
                "missing": which}
    for side in (out_a, out_b):
        if side.get("kind") == "unavailable":
            return {"state": "unknown",
                    "note": "an output could not be captured (%s)"
                            % side.get("error", "unknown")}

    same = out_a.get("sha") == out_b.get("sha")
    truncated = bool(out_a.get("truncated") or out_b.get("truncated"))
    d = {
        "state": "unchanged" if same else "changed",
        "sha_a": out_a.get("sha"), "sha_b": out_b.get("sha"),
        "len_a": out_a.get("len"), "len_b": out_b.get("len"),
        "kind_a": out_a.get("kind"), "kind_b": out_b.get("kind"),
        "truncated": truncated,
    }
    if same:
        return d
    d["a"] = _short(out_a.get("value"))
    d["b"] = _short(out_b.get("value"))
    if truncated:
        d["note"] = ("the stored answers are truncated, so the digests are "
                     "authoritative and the text shown is a prefix")
    else:
        structured = body_diff(out_a.get("value"), out_b.get("value"))
        if structured and structured["kind"] == "json":
            d["structured"] = structured
    return d


# --- consequence --------------------------------------------------------------
# A link is a pair of observations plus what supports it. `established` is true
# only when one record literally references the other — an evaluation whose own
# evidence names a step, or a tool call that is inside a step's response. Mere
# ordering is never established, however suggestive.

NOT_ESTABLISHED = "consequence observed, causal link not established"


def _node(kind, text, **extra):
    node = {"kind": kind, "text": text}
    node.update(extra)
    return node


def consequences(first, model_diffs, tool_diffs, out_diff, evaluation=None,
                 later_differences=0):
    """Order the observations and say which links the records support.

    Reads only what is already in front of it. There is no inference step, no
    scoring, and nothing generated from a language model — a chain of reasoning
    a person cannot check against the record would be worse than none.
    """
    nodes, links = [], []

    def add(node):
        nodes.append(node)
        return len(nodes) - 1

    start = None
    if model_diffs:
        names = ", ".join(d["field"] for d in model_diffs[:4])
        start = add(_node("model", "model configuration changed (%s)" % names,
                          step=first.get("b") if first else None,
                          fields=model_diffs))
    if first is not None and start is None:
        start = add(_node("step", "the first difference is at step %s"
                          % (first.get("b") if first.get("b") is not None
                             else first.get("a")),
                          step=first.get("b"), op=first.get("op")))

    tool_node = None
    if tool_diffs:
        bits = []
        for t in tool_diffs[:4]:
            if t["change"] == "removed":
                bits.append("%s no longer requested" % t["name"])
            elif t["change"] == "added":
                bits.append("%s newly requested" % t["name"])
            elif t["change"] == "arguments":
                bits.append("%s called with different arguments" % t["name"])
            elif t["change"] == "reordered":
                bits.append("%s requested at a different point" % t["name"])
            else:
                bits.append("%s changed" % t["name"])
        tool_node = add(_node("tool", "; ".join(bits), changes=tool_diffs))
        if start is not None:
            links.append({"from": start, "to": tool_node,
                          "relation": "earlier-in-run", "established": False,
                          "note": NOT_ESTABLISHED})

    if later_differences:
        after = add(_node("spread",
                          "%d later step(s) also differ" % later_differences,
                          count=later_differences))
        if start is not None:
            links.append({"from": start, "to": after,
                          "relation": "earlier-in-run", "established": False,
                          "note": NOT_ESTABLISHED})

    eval_nodes = []
    for res in (evaluation or []):
        if res.get("status") != "fail":
            continue
        idx = add(_node("evaluation", "%s failed: %s"
                        % (res.get("evaluator"), res.get("reason")),
                        evaluator=res.get("evaluator"),
                        evidence=res.get("evidence")))
        eval_nodes.append((idx, res))

    # An evaluation whose evidence names a tool that also changed is a link the
    # records establish: both sides name the same tool. That is not a claim
    # about cause, it is a statement that they are about the same thing.
    for idx, res in eval_nodes:
        subject = (res.get("evidence") or {}).get("tool")
        matched = False
        if subject and tool_node is not None:
            if any(t.get("name") == subject for t in tool_diffs):
                links.append({"from": tool_node, "to": idx,
                              "relation": "names-the-same-tool",
                              "established": True,
                              "note": "both records name %s" % subject})
                matched = True
        if not matched and start is not None:
            links.append({"from": start, "to": idx,
                          "relation": "earlier-in-run", "established": False,
                          "note": NOT_ESTABLISHED})

    if out_diff and out_diff.get("state") == "changed":
        out_node = add(_node("output", "the final answer changed",
                             sha_a=out_diff.get("sha_a"),
                             sha_b=out_diff.get("sha_b")))
        anchor = tool_node if tool_node is not None else start
        if anchor is not None:
            links.append({"from": anchor, "to": out_node,
                          "relation": "earlier-in-run", "established": False,
                          "note": NOT_ESTABLISHED})
        for idx, res in eval_nodes:
            if res.get("evaluator", "").startswith("output"):
                links.append({"from": out_node, "to": idx,
                              "relation": "same-subject", "established": True,
                              "note": "the evaluation is about the answer"})

    return {"nodes": nodes, "links": links,
            "established": all(l["established"] for l in links) if links else None}
