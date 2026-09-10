# -*- coding: utf-8 -*-
"""The chain, end to end, and the regression corpus.

Each layer was built on the one below it and each has its own tests. That does
not prove they hold hands: a feature can pass every test of its own and still
only work when used alone. This file walks the whole chain once —

    record -> replay -> evaluation -> case -> baseline -> `orientim test` -> diff

— and then pins nine scenarios that between them cover every shape the
explanatory diff claims to distinguish. The nine are the project's regression
corpus: small, deterministic, and named after what they are, so a change in
behaviour points at a scenario rather than at a line number.
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
from orientim import (align, baselines, cases, ci, cli, diff, evaluate, model,
                      store)

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/contract"
CORPUS = "tests/_runs/corpus"

# The scenario the corpus agent plays, set from outside the module so the copy
# `orientim test` imports for itself sees the same value. See test_cases.py for
# why a module global would not.
SCENARIO_ENV = "ORIENTIM_CORPUS_SCENARIO"


def _model_body(**kw):
    body = {"model": kw.pop("model", "gpt-4o-mini"),
            "temperature": kw.pop("temperature", 0.0),
            "messages": [{"role": "user", "content": "where is 4471"}]}
    body.update(kw)
    return json.dumps(body).encode()


def corpus_agent(run):
    """One agent, nine behaviours, chosen by the environment.

    Every branch is deterministic: no clock, no randomness, no ordering that
    depends on timing. A corpus that flakes is a corpus nobody trusts.
    """
    scenario = os.environ.get(SCENARIO_ENV, "baseline")
    c = run.client()

    if scenario == "model_config":
        c.post(B + "/v1/chat/completions",
               content=_model_body(model="gpt-4o", temperature=0.7, n_tools=1))
        c.post(B + "/search", content=b'{"q":"4471"}')
        run.output = "order 4471 not found"
        return run.output

    if scenario == "tool_arguments":
        # Same call shape, different arguments coming back from the model.
        c.post(B + "/v1/chat/completions", content=_model_body(n_tools=2))
        c.post(B + "/search", content=b'{"q":"4471"}')
        run.output = "order 4471 not found"
        return run.output

    c.post(B + "/v1/chat/completions", content=_model_body(n_tools=1))

    if scenario == "inserted":
        c.post(B + "/chat-stable", content=b"{}")

    if scenario == "reordered":
        c.post(B + "/drift", content=b"{}")
        c.post(B + "/search", content=b'{"q":"4471"}')
    elif scenario != "deleted":
        c.post(B + "/search", content=b'{"q":"4471"}')

    if scenario == "reordered":
        pass
    elif scenario == "inserted":
        c.post(B + "/drift", content=b"{}")

    run.output = ("your order shipped" if scenario == "output_changed"
                  else "order 4471 not found")
    return run.output


def _load_agent(_entry):
    return corpus_agent


@contextlib.contextmanager
def _scenario(name):
    before = os.environ.get(SCENARIO_ENV)
    os.environ[SCENARIO_ENV] = name
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(SCENARIO_ENV, None)
        else:
            os.environ[SCENARIO_ENV] = before


def _fresh(root=ROOT):
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)


def _record(root=ROOT):
    with orientim.record(root=root, always=True, agent="corpus") as h:
        corpus_agent(h)
    return h.path


def _cli(argv):
    buf = _io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


# --- the chain ----------------------------------------------------------------

def t_record_produces_a_replayable_execution_model():
    """record -> replay, and the recording carries the execution model."""
    _fresh()
    with _scenario("baseline"):
        path = _record()
        meta, steps = store.load(path)
        http = [s for s in steps if s.get("t") == "http"]
        d = orientim.replay(path, corpus_agent)
    ok = (d.ok and meta["format"] == store.FORMAT
          and [s["role"] for s in http] == ["model", "tool"]
          and model.tool_calls_in(steps)
          and meta["outcome"]["value"] == "order 4471 not found"
          and meta["agent"]["name"] == "corpus" and meta["runtime"]["python"])
    return ok, "verdict %s, roles %r" % (d.diagnosis[0],
                                         [s.get("role") for s in http])


def t_replay_feeds_evaluation():
    """replay -> evaluation, over the steps the replay produced."""
    _fresh()
    with _scenario("baseline"):
        path = _record()
        d = orientim.replay(path, corpus_agent)
        ex = evaluate.Execution.of(
            {"outcome": d.replay_output},
            diff.merge_unmatched(d.replay_steps, d.unmatched_requests))
        report = evaluate.evaluate(ex, [evaluate.used_tool("lookup_order"),
                                        evaluate.no_step_failed(),
                                        evaluate.output_matches("not found")])
    return report.ok and len(report.passed) == 3, report.report()


def t_evaluation_feeds_a_case():
    """evaluation -> case: the expectations become a file, and it runs."""
    _fresh()
    with _scenario("baseline"):
        path = _record()
        cases.save("chain", path, "tests.test_contract:corpus_agent", root=ROOT,
                   expect={"used_tool": ["lookup_order"],
                           "output_matches": "not found",
                           "no_step_failed": True})
        row = cases.run(cases.load("chain", ROOT), entry_loader=_load_agent)
    return (row["ok"] and row["evaluation"]["passed"] == 3), \
        "case row %r" % ({k: row[k] for k in ("ok", "verdict")},)


def t_case_feeds_a_baseline():
    """case -> baseline: what the suite said, stored under a name."""
    _fresh()
    with _scenario("baseline"):
        path = _record()
        cases.save("chain", path, "tests.test_contract:corpus_agent", root=ROOT,
                   expect={"used_tool": ["lookup_order"]})
        rows = [cases.run(c, entry_loader=_load_agent)
                for c in cases.list_cases(ROOT)]
        baselines.create("main", rows, root=ROOT)
        b = baselines.load("main", ROOT)
    return (b["totals"]["cases"] == 1 and b["runs"][0]["case"] == "chain"
            and b["runs"][0]["ok"]), "baseline %r" % (b["totals"],)


def t_baseline_feeds_orientim_test():
    """baseline -> `orientim test`: green before, one named failure after."""
    _fresh()
    with _scenario("baseline"):
        path = _record()
        cases.save("chain", path, "tests.test_contract:corpus_agent", root=ROOT,
                   expect={"used_tool": ["lookup_order"],
                           "output_matches": "not found"})
        frozen, code_frozen = _cli(["--root", ROOT, "baseline", "create", "main"])

    report_path = os.path.join(ROOT, "after.json")
    with _scenario("output_changed"):
        after, code_after = _cli(["--root", ROOT, "test", "--baseline", "main",
                                  "--report", report_path])

    blob = json.load(open(report_path, encoding="utf-8"))
    ok = (code_frozen == 0 and "All 1 case(s) passed" in frozen
          and code_after == ci.EXIT_CHANGED
          and blob["against_baseline"]["newly_changed"] == ["chain"])
    return ok, "frozen exit %s, after exit %s, newly changed %r" % (
        code_frozen, code_after, blob["against_baseline"]["newly_changed"])


def t_test_feeds_the_explanatory_diff():
    """`orientim test` -> diff: the same failure, explained."""
    _fresh()
    with _scenario("baseline"):
        path = _record()
        cases.save("chain", path, "tests.test_contract:corpus_agent", root=ROOT,
                   expect={"output_matches": "not found"})
    with _scenario("output_changed"):
        cmp_ = diff.compare_case(cases.load("chain", ROOT),
                                 entry_loader=_load_agent, root=ROOT)
    text = diff.report(cmp_)
    failed = [r["evaluator"] for r in cmp_["evaluation"]
              if r["status"] == "fail"]
    ok = (not cmp_["identical"] and cmp_["output"]["state"] == "changed"
          and "output_matches" in failed and cmp_["consequence"]["nodes"]
          and "CONSEQUENCE" in text)
    return ok, "output %s, failed %r" % (cmp_["output"]["state"], failed)


def t_the_whole_chain_in_one_flow():
    """Every link at once, in the order a person meets them."""
    _fresh()
    with _scenario("baseline"):
        path = _record()                                   # record
        assert orientim.replay(path, corpus_agent).ok      # replay
        cases.save("full", path, "tests.test_contract:corpus_agent", root=ROOT,
                   expect={"used_tool": ["lookup_order"],   # evaluation
                           "output_matches": "not found",
                           "no_step_failed": True})         # case
        _out, code = _cli(["--root", ROOT, "baseline", "create", "main"])
        if code != 0:
            return False, "baseline create exited %s" % code

    with _scenario("model_config"):                         # somebody changes it
        out, code = _cli(["--root", ROOT, "test", "--baseline", "main"])
        cmp_ = diff.compare_case(cases.load("full", ROOT),
                                 entry_loader=_load_agent, root=ROOT)

    ok = (code == ci.EXIT_CHANGED and "NEW on this change" in out
          and cmp_["model_changes"]
          and {c["field"] for c in cmp_["model_changes"]} >= {"model",
                                                              "temperature"})
    return ok, "exit %s, model fields %r" % (
        code, sorted({c["field"] for c in cmp_["model_changes"]}))


# --- the regression corpus ----------------------------------------------------
# Nine scenarios, one row each: what the second run does, and what the diff has
# to say about it. Adding a shape here is how a new diff capability gets a
# permanent home.

CORPUS_SCENARIOS = [
    ("unchanged", "baseline", {"identical": True}),
    ("model config change", "model_config",
     {"model_fields": {"model", "temperature"}}),
    ("tool argument change", "tool_arguments", {"tool_changes": True}),
    ("inserted step", "inserted", {"op": align.INSERTED}),
    ("deleted step", "deleted", {"op": align.DELETED}),
    ("reordered steps", "reordered", {"reordered_or_changed": True}),
    ("output changed", "output_changed", {"output": "changed"}),
]


def _corpus_pair(scenario):
    """Record the baseline and the scenario, and diff the two recordings."""
    with _scenario("baseline"):
        a = _record(CORPUS)
    with _scenario(scenario):
        b = _record(CORPUS)
    return diff.compare(a, b), a, b


def t_corpus_covers_every_diff_shape():
    """One check, seven scenarios, so a regression names the shape it broke."""
    _fresh(CORPUS)
    failures = []
    for label, scenario, expect in CORPUS_SCENARIOS:
        try:
            cmp_, _a, _b = _corpus_pair(scenario)
        except Exception as e:
            failures.append("%s: raised %s" % (label, type(e).__name__))
            continue
        ops = {r["op"] for r in cmp_["steps"]}
        if "identical" in expect and cmp_["identical"] != expect["identical"]:
            failures.append("%s: identical=%s" % (label, cmp_["identical"]))
        if "op" in expect and expect["op"] not in ops:
            failures.append("%s: ops %r, wanted %s" % (label, sorted(ops),
                                                       expect["op"]))
        if "model_fields" in expect:
            got = {c["field"] for c in cmp_["model_changes"]}
            if not expect["model_fields"] <= got:
                failures.append("%s: model fields %r" % (label, sorted(got)))
        if expect.get("tool_changes") and not cmp_["tool_changes"]:
            failures.append("%s: no tool changes reported" % label)
        if "output" in expect and cmp_["output"]["state"] != expect["output"]:
            failures.append("%s: output %s" % (label, cmp_["output"]["state"]))
        if expect.get("reordered_or_changed"):
            if not ({align.REORDERED, align.INSERTED, align.DELETED} & ops):
                failures.append("%s: ops %r" % (label, sorted(ops)))
    return not failures, "; ".join(failures) or \
        "%d scenarios behave as pinned" % len(CORPUS_SCENARIOS)


def t_corpus_unmatched_request_shape():
    """The eighth shape: a request the replay cannot match.

    Kept separate because it needs a replay rather than two recordings.
    """
    _fresh(CORPUS)
    with _scenario("baseline"):
        path = _record(CORPUS)
        cases.save("unmatched", path, "tests.test_contract:corpus_agent",
                   root=CORPUS)
    with _scenario("model_config"):
        cmp_ = diff.compare_case(cases.load("unmatched", CORPUS),
                                 entry_loader=_load_agent, root=CORPUS)
    unmatched = [r for r in cmp_["steps"] if r.get("unmatched")]
    return (unmatched and cmp_["counts"][align.DELETED] == 0), \
        "%d unmatched row(s), %d deleted" % (len(unmatched),
                                             cmp_["counts"][align.DELETED])


def t_corpus_evaluation_failure_shape():
    """The ninth: an evaluation failure attached to an observed change."""
    _fresh(CORPUS)
    with _scenario("baseline"):
        path = _record(CORPUS)
        cases.save("evalfail", path, "tests.test_contract:corpus_agent",
                   root=CORPUS, expect={"output_matches": "not found"})
    with _scenario("output_changed"):
        cmp_ = diff.compare_case(cases.load("evalfail", CORPUS),
                                 entry_loader=_load_agent, root=CORPUS)
    failed = [r for r in cmp_["evaluation"] if r["status"] == "fail"]
    linked = [l for l in cmp_["consequence"]["links"] if l["established"]]
    return (failed and linked), "failed %r, established links %d" % (
        [r["evaluator"] for r in failed], len(linked))


def t_corpus_is_deterministic():
    """Recording the same scenario twice must produce the same chain root.

    A corpus whose fixtures drift is a corpus that fails for reasons nobody
    can act on.
    """
    _fresh(CORPUS)
    roots = []
    for _ in range(2):
        with _scenario("baseline"):
            path = _record(CORPUS)
        _meta, steps = store.load(path)
        from orientim import chain
        http = [s for s in steps if s.get("t") == "http"]
        roots.append(chain.build_steps(http)[1])
    return roots[0] == roots[1], "roots %s / %s" % (roots[0][:12], roots[1][:12])
