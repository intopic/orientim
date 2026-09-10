# -*- coding: utf-8 -*-
"""The explanatory diff: alignment, field differences, and consequence.

Most checks build steps directly, because alignment is a property of sequences
and a hand-built sequence says exactly what shape is under test. The ones about
tool calls, redaction and the whole workflow go through a real recording, since
those depend on what the recorder actually writes.
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import align, cases, diff, explain

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/diff"


def _step(i, url, body='{"ok":1}', req='{"q":1}', status=200, **kw):
    s = {"t": "http", "i": i, "method": "POST", "url": url, "status": status,
         "key_strict": "%s|%s" % (url, req), "key_loose": "%s|%s" % (url, req),
         "hdr_fp": "h", "body": body, "b64": False,
         "body_sha": str(hash(body) % 10 ** 12), "req": req, "role": "tool"}
    s.update(kw)
    return s


def _ops(a, b):
    return [e["op"] for e in align.align(a, b)]


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


# --- alignment ----------------------------------------------------------------

def t_unchanged_execution():
    a = [_step(0, "/a"), _step(1, "/b"), _step(2, "/c")]
    ops = _ops(a, [dict(s) for s in a])
    return ops == ["SAME"] * 3, "ops %r" % (ops,)


def t_insertion_does_not_smear():
    """The whole reason alignment exists.

    One call inserted near the front used to make every later step read as
    different. A run that changed in one place must report as a run that
    changed in one place.
    """
    a = [_step(0, "/a"), _step(1, "/c"), _step(2, "/d"), _step(3, "/e")]
    b = [_step(0, "/a"), _step(1, "/NEW"), _step(2, "/c"), _step(3, "/d"),
         _step(4, "/e")]
    ops = _ops(a, b)
    return ops == ["SAME", "INSERTED", "SAME", "SAME", "SAME"], "ops %r" % (ops,)


def t_deletion():
    a = [_step(0, "/a"), _step(1, "/b"), _step(2, "/c")]
    b = [_step(0, "/a"), _step(1, "/c")]
    ops = _ops(a, b)
    return ops == ["SAME", "DELETED", "SAME"], "ops %r" % (ops,)


def t_reorder_is_one_change_not_four():
    """model, A, B, model  vs  model, B, A, model."""
    a = [_step(0, "/model"), _step(1, "/toolA"), _step(2, "/toolB"),
         _step(3, "/model")]
    b = [_step(0, "/model"), _step(1, "/toolB"), _step(2, "/toolA"),
         _step(3, "/model")]
    entries = align.align(a, b)
    ops = [e["op"] for e in entries]
    moved = [e for e in entries if e["op"] == "REORDERED"]
    return (ops.count("REORDERED") == 1 and ops.count("SAME") == 3
            and len(ops) == 4 and moved[0]["moved_from"] != moved[0]["moved_to"]), \
        "ops %r" % (ops,)


def t_changed_step_is_not_a_delete_and_insert():
    """Same call, different body, aligns as one CHANGED."""
    a = [_step(0, "/a", req='{"t":0.0}'), _step(1, "/b")]
    b = [_step(0, "/a", req='{"t":0.7}'), _step(1, "/b")]
    ops = _ops(a, b)
    return ops == ["CHANGED", "SAME"], "ops %r" % (ops,)


def t_empty_sides():
    a = [_step(0, "/a")]
    return (_ops([], []) == [] and _ops(a, []) == ["DELETED"]
            and _ops([], a) == ["INSERTED"]), "empty comparisons handled"


# --- model metadata -----------------------------------------------------------

def t_model_config_change_is_named_by_field():
    a = _step(0, "/v1/chat/completions", role="model",
              model={"model": "gpt-x", "temperature": 0.0, "max_tokens": 100},
              served={"model_served": "gpt-x-2024",
                      "usage": {"input_tokens": 10, "output_tokens": 4}})
    b = _step(0, "/v1/chat/completions", role="model", req='{"t":1}',
              model={"model": "gpt-y", "temperature": 0.7, "max_tokens": 100},
              served={"model_served": "gpt-y-2024",
                      "usage": {"input_tokens": 12, "output_tokens": 4}})
    got = {c["field"]: (c["was"], c["now"]) for c in explain.model_changes(a, b)}
    ok = (got.get("model") == ("gpt-x", "gpt-y")
          and got.get("temperature") == (0.0, 0.7)
          and "max_tokens" not in got                    # unchanged, not listed
          and got.get("model_served") == ("gpt-x-2024", "gpt-y-2024")
          and got.get("input_tokens") == (10, 12))
    return ok, "changes %r" % (got,)


def t_unanswered_call_reports_no_response_change():
    """A request that was never answered has not changed its token count.

    Reporting "output_tokens 8 -> None" about a call that got no response reads
    as a change in the model and is nothing of the kind.
    """
    a = _step(0, "/v1/chat/completions", role="model",
              model={"model": "m", "temperature": 0.0},
              served={"usage": {"input_tokens": 8, "output_tokens": 3}})
    b = _step(0, "/v1/chat/completions", role="model",
              model={"model": "m", "temperature": 0.7}, unmatched=True)
    fields = [c["field"] for c in explain.model_changes(a, b, request_only=True)]
    return fields == ["temperature"], "fields %r" % (fields,)


# --- tool calls ---------------------------------------------------------------

def _calls(*specs):
    return [{"name": n, "arguments": a, "arguments_kind": "json", "step": i}
            for i, (n, a) in enumerate(specs)]


def t_tool_added_and_removed():
    got = explain.tool_changes(_calls(("lookup_order", {"id": 1})),
                               _calls(("send_email", {"to": "x"})))
    by = {c["change"]: c["name"] for c in got}
    return (by.get("removed") == "lookup_order"
            and by.get("added") == "send_email"), "changes %r" % (got,)


def t_tool_argument_change():
    got = explain.tool_changes(_calls(("lookup_order", {"order_id": 4471})),
                               _calls(("lookup_order", {"order_id": 9999})))
    ok = (len(got) == 1 and got[0]["change"] == "arguments"
          and "4471" in str(got[0]["was"]) and "9999" in str(got[0]["now"]))
    return ok, "changes %r" % (got,)


def t_multiple_tool_calls_in_one_response():
    got = explain.tool_changes(
        _calls(("a", {}), ("b", {}), ("c", {})),
        _calls(("a", {}), ("c", {}), ("d", {})))
    kinds = sorted((c["change"], c["name"]) for c in got)
    return kinds == [("added", "d"), ("removed", "b")], "changes %r" % (kinds,)


def t_tool_reorder_is_one_change():
    got = explain.tool_changes(_calls(("a", {}), ("b", {})),
                               _calls(("b", {}), ("a", {})))
    reordered = [c for c in got if c["change"] == "reordered"]
    return (len(got) == 1 and reordered), "changes %r" % (got,)


def t_partial_arguments_are_flagged_not_compared_away():
    a = [{"name": "s", "arguments": '{"q": "unfin', "arguments_kind": "text",
          "partial": True, "step": 0}]
    b = [{"name": "s", "arguments": {"q": "finished"}, "arguments_kind": "json",
          "partial": False, "step": 0}]
    got = explain.tool_changes(a, b)
    return (len(got) == 1 and got[0]["change"] == "arguments"
            and got[0]["was_partial"] is True
            and got[0]["now_partial"] is False), "changes %r" % (got,)


# --- bodies -------------------------------------------------------------------

def t_json_body_field_diff():
    d = explain.body_diff('{"a": 1, "b": 2, "gone": 3}',
                          '{"a": 1, "b": 9, "new": 4}')
    return (d["kind"] == "json"
            and [x["path"] for x in d["added"]] == ["new"]
            and [x["path"] for x in d["removed"]] == ["gone"]
            and d["changed"][0]["path"] == "b"
            and (d["changed"][0]["was"], d["changed"][0]["now"]) == (2, 9)), \
        "diff %r" % (d,)


def t_nested_and_list_paths():
    d = explain.body_diff('{"o": {"k": 1}, "l": [1, 2]}',
                          '{"o": {"k": 2}, "l": [1, 3]}')
    paths = sorted(x["path"] for x in d["changed"])
    return paths == ["l[1]", "o.k"], "paths %r" % (paths,)


def t_text_body_diff():
    d = explain.body_diff("one\ntwo\nthree", "one\nTWO\nthree")
    return (d["kind"] == "text" and any("TWO" in ln for ln in d["lines"])), \
        "diff %r" % (d,)


def t_identical_bodies_have_no_diff():
    return explain.body_diff('{"a":1}', '{"a":1}') is None, "equal bodies"


def t_diff_shows_only_redacted_values():
    """The diff reads what the recorder stored, and never re-derives redaction.

    A second redaction implementation would be a second one to get wrong and
    would drift from the first.
    """
    _fresh()
    secret = "sk-live-NEVER-IN-A-DIFF"

    def agent(h):
        h.client().post(B + "/echo",
                        content=json.dumps({"api_key": secret, "q": 1}).encode())

    with orientim.record(root=ROOT, always=True) as a:
        agent(a)

    def agent2(h):
        h.client().post(B + "/echo",
                        content=json.dumps({"api_key": secret, "q": 2}).encode())

    with orientim.record(root=ROOT, always=True) as b:
        agent2(b)

    cmp_ = diff.compare(a.path, b.path)
    blob = diff.as_json(cmp_) + diff.report(cmp_)

    # The strongest outcome available, and it falls out of redacting before
    # the diff rather than inside it: both sides store the credential as the
    # same placeholder, so the field cannot differ, so it is never shown at
    # all. The real change still is.
    recorded = open(a.path, encoding="utf-8").read()
    return (secret not in blob and "api_key" not in blob
            and "<redacted>" in recorded
            and '"q"' in blob), \
        ("secret in diff=%s, api_key mentioned=%s, real change shown=%s"
         % (secret in blob, "api_key" in blob, '"q"' in blob))


# --- the answer ---------------------------------------------------------------

def t_output_change_is_reported_with_digests():
    from orientim import model
    a = model.capture_output("order 4471 shipped")
    b = model.capture_output("order 4471 is lost")
    d = explain.output_diff(a, b)
    return (d["state"] == "changed" and d["sha_a"] != d["sha_b"]
            and d["a"] == "order 4471 shipped"
            and d["b"] == "order 4471 is lost"), "diff %r" % (d,)


def t_output_unchanged():
    from orientim import model
    a = model.capture_output("same")
    return explain.output_diff(a, model.capture_output("same"))["state"] \
        == "unchanged", "unchanged answers compare equal"


def t_output_truncation_makes_no_false_claim():
    """Two answers differing only past the storage limit still differ.

    The digest is over the whole value, so "changed" is safe to say. What is
    shown is a prefix, and the report says so instead of letting a reader treat
    the visible text as the whole difference.
    """
    from orientim import model
    big = "x" * (model.OUTPUT_LIMIT + 100)
    a = model.capture_output(big + "ONE")
    b = model.capture_output(big + "TWO")
    d = explain.output_diff(a, b)
    return (d["state"] == "changed" and d["truncated"] is True
            and "prefix" in d.get("note", "")
            and "structured" not in d), "diff %r" % ({k: d[k] for k in
                                                      ("state", "truncated")},)


def t_output_declared_on_one_side_only_is_unknown():
    from orientim import model
    d = explain.output_diff(model.capture_output("only here"), None)
    return d["state"] == "unknown" and "nothing" in d["note"], "diff %r" % (d,)


def t_output_structured_difference():
    from orientim import model
    a = model.capture_output({"status": "shipped", "day": "monday"})
    b = model.capture_output({"status": "lost", "day": "monday"})
    d = explain.output_diff(a, b)
    st = d.get("structured") or {}
    return (d["state"] == "changed" and st.get("kind") == "json"
            and st["changed"][0]["path"] == "status"), "diff %r" % (d,)


# --- consequence --------------------------------------------------------------

def t_consequence_never_claims_a_cause():
    """The rule the whole chain rests on."""
    ch = explain.consequences(
        {"op": "CHANGED", "a": 0, "b": 0},
        [{"field": "temperature", "was": 0.0, "now": 0.7}],
        [{"change": "removed", "name": "lookup_order"}],
        {"state": "changed", "sha_a": "aa", "sha_b": "bb"},
        evaluation=[{"status": "fail", "evaluator": "output_matches",
                     "reason": "no match", "evidence": {}}])
    ordering = [l for l in ch["links"] if l["relation"] == "earlier-in-run"]
    text = json.dumps(ch)
    return (ordering and all(not l["established"] for l in ordering)
            and explain.NOT_ESTABLISHED in text
            and "root cause" not in text.lower()), "chain %r" % (ch["links"],)


def t_consequence_marks_a_link_the_records_establish():
    """An evaluation whose own evidence names a tool that changed.

    Both records name the same tool. That is not a claim about cause; it is a
    statement that they are about the same thing, and it is checkable.
    """
    ch = explain.consequences(
        None, [], [{"change": "removed", "name": "lookup_order"}],
        {"state": "unchanged"},
        evaluation=[{"status": "fail", "evaluator": "used_tool",
                     "reason": "lookup_order was not requested",
                     "evidence": {"tool": "lookup_order"}}])
    established = [l for l in ch["links"] if l["established"]]
    return (len(established) == 1
            and established[0]["relation"] == "names-the-same-tool"
            and "lookup_order" in established[0]["note"]), \
        "links %r" % (ch["links"],)


def t_unknown_provenance_stays_unknown():
    """A tool change and an unrelated evaluation failure are not linked as one.

    The evaluation is about the answer; the tool change is about a tool. There
    is no record connecting them, so the link that is emitted must be the weak
    ordering one, not the established one.
    """
    ch = explain.consequences(
        None, [], [{"change": "removed", "name": "lookup_order"}],
        {"state": "unchanged"},
        evaluation=[{"status": "fail", "evaluator": "max_steps",
                     "reason": "9 calls, over the limit of 3",
                     "evidence": {"steps": 9, "limit": 3}}])
    established = [l for l in ch["links"] if l["established"]]
    return not established, "wrongly established %r" % (established,)


# --- everything together ------------------------------------------------------

def _model_body(**kw):
    body = {"model": kw.pop("model", "gpt-4o-mini"),
            "temperature": kw.pop("temperature", 0.0),
            "messages": [{"role": "user", "content": "where is 4471"}]}
    body.update(kw)
    return json.dumps(body).encode()


VERSION_ENV = "ORIENTIM_DIFF_VERSION"


def agent(run):
    """A model call and a tool call. Version 2 changes the model settings."""
    c = run.client()
    if os.environ.get(VERSION_ENV, "1") == "1":
        c.post(B + "/v1/chat/completions", content=_model_body(n_tools=1))
        run.output = "order 4471 not found"
    else:
        c.post(B + "/v1/chat/completions",
               content=_model_body(model="gpt-4o", temperature=0.7, n_tools=2))
        run.output = "your order shipped"
    c.post(B + "/search", content=b'{"q":"4471"}')
    return run.output


def _load_agent(_entry):
    return agent


def t_replay_evaluation_and_diff_together():
    """The three answers in one report: where, which rule, and how.

    This is the definition of done: a developer takes a case that used to pass,
    runs it after a change, and reads what changed, where, what followed, and
    which of those links the records actually support.
    """
    _fresh()
    os.environ.pop(VERSION_ENV, None)
    with orientim.record(root=ROOT, always=True, agent="diff-fixture") as h:
        agent(h)
    cases.save("order-support", h.path, "tests.test_diff:agent", root=ROOT,
               expect={"used_tool": ["lookup_order"],
                       "output_matches": "not found",
                       "no_step_failed": True})
    os.environ[VERSION_ENV] = "2"
    try:
        cmp_ = diff.compare_case(cases.load("order-support", ROOT),
                                 entry_loader=_load_agent, root=ROOT)
    finally:
        os.environ.pop(VERSION_ENV, None)

    text = diff.report(cmp_)
    fields = {c["field"] for c in cmp_["model_changes"]}
    failed = {r["evaluator"] for r in cmp_["evaluation"]
              if r["status"] == "fail"}
    ok = (not cmp_["identical"]
          and cmp_["case"] == "order-support"
          and {"model", "temperature"} <= fields
          and cmp_["output"]["state"] == "changed"
          and "output_matches" in failed
          and cmp_["verdict"]
          and cmp_["consequence"]["nodes"]
          and "MODEL CONFIG" in text and "OUTPUT" in text
          and "EVALUATION" in text and "CONSEQUENCE" in text
          and explain.NOT_ESTABLISHED in text)
    return ok, ("model fields %r, failed %r, verdict %s"
                % (sorted(fields), sorted(failed), cmp_["verdict"]))


def t_unmatched_requests_are_not_reported_as_deletions():
    """A run whose requests all changed did not stop making requests.

    replay_steps holds only matched steps, so without merging the unmatched
    ones back in the diff says "every step deleted" — true of the matching and
    false about the agent.
    """
    _fresh()
    os.environ.pop(VERSION_ENV, None)
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    cases.save("c", h.path, "tests.test_diff:agent", root=ROOT)
    os.environ[VERSION_ENV] = "2"
    try:
        cmp_ = diff.compare_case(cases.load("c", ROOT),
                                 entry_loader=_load_agent, root=ROOT)
    finally:
        os.environ.pop(VERSION_ENV, None)
    c = cmp_["counts"]
    return (cmp_["b"]["steps"] == cmp_["a"]["steps"] and c["DELETED"] == 0
            and c["CHANGED"] == 2), "counts %r, b steps %d" % (c, cmp_["b"]["steps"])


def t_a_case_that_did_not_move_diffs_as_unchanged():
    _fresh()
    os.environ.pop(VERSION_ENV, None)
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    cases.save("stable", h.path, "tests.test_diff:agent", root=ROOT,
               expect={"output_matches": "not found"})
    cmp_ = diff.compare_case(cases.load("stable", ROOT),
                             entry_loader=_load_agent, root=ROOT)
    return (cmp_["identical"] and cmp_["ok"]
            and "UNCHANGED" in diff.report(cmp_)), \
        "identical=%s counts %r" % (cmp_["identical"], cmp_["counts"])


def t_json_output_is_stable_and_complete():
    _fresh()
    os.environ.pop(VERSION_ENV, None)
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    cases.save("j", h.path, "tests.test_diff:agent", root=ROOT,
               expect={"used_tool": ["refund_order"]})
    cmp_ = diff.compare_case(cases.load("j", ROOT), entry_loader=_load_agent,
                             root=ROOT)
    blob = json.loads(diff.as_json(cmp_))
    want = ("schema", "kind", "a", "b", "identical", "counts", "steps",
            "model_changes", "tool_changes", "output", "evaluation",
            "consequence", "verdict", "degraded", "first_difference")
    missing = [k for k in want if k not in blob]
    links = blob["consequence"]["links"]
    return (not missing and blob["kind"] == "orientim-diff"
            and all("established" in l for l in links)), \
        "missing %r" % (missing,)


# --- performance --------------------------------------------------------------

def _bench_steps(n, distinct):
    return [_step(i, "https://api.example.com/v%d/thing" % (i % distinct))
            for i in range(n)]


def t_alignment_is_bounded():
    """10, 100 and 1000 steps, including the shape that used to be quadratic.

    An agent polling a handful of endpoints in a loop is thousands of steps
    drawn from a few distinct calls, which is the worst case for the matcher.
    Measured before the budget: 3000 such steps reordered took 110 seconds.
    """
    worst = []
    for n in (10, 100, 1000):
        for distinct in (5, n):
            a = _bench_steps(n, distinct)
            for name, b in (("identical", list(a)),
                            ("insert at head", [_step(0, "/new")] + list(a)),
                            ("reversed", list(reversed(a)))):
                t0 = time.perf_counter()
                align.plan(a, b)
                worst.append(((time.perf_counter() - t0) * 1000,
                              "%d/%d %s" % (n, distinct, name)))
    worst.sort(reverse=True)
    slowest, where = worst[0]
    return slowest < 2000, "slowest %.0f ms (%s)" % (slowest, where)


def t_degradation_is_announced():
    """When the budget bites, the report says so rather than quietly guessing."""
    a = _bench_steps(1200, 4)
    b = list(reversed(a))
    plan = align.plan(a, b)
    return (plan["degraded"] and "quadratic" in plan["reason"]
            and len(plan["entries"]) >= 1200), \
        "degraded=%s reason=%r" % (plan["degraded"], (plan["reason"] or "")[:60])


def t_small_runs_are_never_degraded():
    a = _bench_steps(100, 3)
    return not align.plan(a, list(reversed(a)))["degraded"], \
        "a 100-step run took the full path"
