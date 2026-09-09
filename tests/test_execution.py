# -*- coding: utf-8 -*-
"""Execution model v2: typed steps, model metadata, final output, migration.

The load-bearing test in this file is t_migration_preserves_chain. Everything
else in the format can be re-derived; a migration that changes a hash silently
reclassifies history, and there is no way to notice from the outside.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import chain, model, store

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/execution"

OPENAI_BODY = json.dumps({
    "model": "gpt-4o-mini",
    "temperature": 0.3,
    "max_tokens": 256,
    "stream": False,
    "messages": [{"role": "user", "content": "where is order 4471"}],
    "tools": [{"type": "function", "function": {"name": "lookup_order"}},
              {"type": "function", "function": {"name": "send_email"}}],
}).encode()


def _record(fn, **kw):
    with orientim.record(root=ROOT, **kw) as h:
        fn(h)
        h.rec.trigger("execution-model")
    return h


def _http(path):
    _meta, steps = store.load(path)
    return [s for s in steps if s.get("t") == "http"]


# --- typed steps --------------------------------------------------------------

def t_step_roles():
    """A model call and a tool call, told apart, in one recording."""
    def agent(h):
        c = h.client()
        c.post(B + "/v1/chat/completions", content=OPENAI_BODY)
        c.post(B + "/search", content=b'{"q":"4471"}')

    h = _record(agent)
    roles = [s.get("role") for s in _http(h.path)]
    return roles == ["model", "tool"], "roles recorded as %r" % (roles,)


def t_classification_does_not_touch_matching():
    """The hint is a hint: corrupt it and the replay must not notice.

    Classification is a heuristic over URLs, so it will be wrong sometimes. The
    contract that makes that acceptable is that it can never decide a match —
    enforced here by lying about it and demanding IDENTICAL anyway.
    """
    def agent(h):
        h.client().post(B + "/v1/chat/completions", content=OPENAI_BODY)

    h = _record(agent)
    meta, steps = store.load(h.path)
    for s in steps:
        if s.get("t") == "http":
            s["role"] = "tool" if s.get("role") == "model" else "model"
            s["model"] = {"model": "a-completely-different-model"}
    raw = ("\n".join([json.dumps({"_meta": meta})]
                     + [json.dumps(s) for s in steps]) + "\n").encode()
    lied = os.path.join(ROOT, "lied_about.jsonl")
    with open(lied, "wb") as f:
        f.write(raw)

    d = orientim.replay(lied, agent)
    return d.ok, "verdict %s (%s)" % (d.diagnosis[0], d.reason)


# --- model metadata -----------------------------------------------------------

def t_model_metadata():
    def agent(h):
        h.client().post(B + "/v1/chat/completions", content=OPENAI_BODY)

    h = _record(agent)
    m = _http(h.path)[0].get("model") or {}
    want = {"model": "gpt-4o-mini", "temperature": 0.3, "max_tokens": 256,
            "stream": False, "tools_offered": ["lookup_order", "send_email"]}
    missing = {k: (want[k], m.get(k)) for k in want if m.get(k) != want[k]}
    return not missing, ("recorded %r" % (m,) if missing
                         else "model, temperature, max_tokens, stream, 2 tools")


def t_response_metadata():
    """Tokens and the model that actually answered — which is not the one asked for."""
    def agent(h):
        h.client().post(B + "/v1/chat/completions", content=OPENAI_BODY)

    h = _record(agent)
    served = _http(h.path)[0].get("served") or {}
    usage = served.get("usage") or {}
    ok = (served.get("model_served") == "gpt-4o-mini-2024-07"
          and usage.get("input_tokens") == 11
          and usage.get("output_tokens") == 7
          and served.get("stop_reason") is None)   # top-level body has none
    return ok, "served %r" % (served,)


STREAM_BODY = json.dumps(dict(json.loads(OPENAI_BODY), stream=True)).encode()


def t_streamed_response_metadata():
    """Usage arrives in the last event of a stream, or not at all.

    Same URL as the non-streamed call, because that is how providers do it:
    the only thing that says "stream" is a field in the request body, which is
    also the field the metadata has to pick up.
    """
    def agent(h):
        with h.client().stream("POST", B + "/v1/chat/completions",
                               content=STREAM_BODY) as r:
            for _chunk in r.iter_bytes():
                pass

    h = _record(agent)
    served = _http(h.path)[0].get("served") or {}
    usage = served.get("usage") or {}
    asked = (_http(h.path)[0].get("model") or {}).get("stream")
    ok = (usage.get("input_tokens") == 11 and usage.get("output_tokens") == 7
          and served.get("model_served") == "gpt-4o-mini-2024-07"
          and asked is True)
    return ok, "served %r, stream flag recorded as %r" % (served, asked)


def t_metadata_survives_a_hostile_body():
    """A model endpoint answering junk must not break the recording."""
    cases = [b"", b"not json at all", b"[1,2,3]", b'{"model": {"nested": 1}}',
             b'{"tools": "not a list"}', b'{"temperature": "hot"}']
    for body in cases:
        try:
            model.classify(B + "/v1/chat/completions", body)
            model.describe_model_call(B + "/v1/chat/completions", body)
            model.describe_model_response(body.decode("utf-8", "replace"))
        except Exception as e:
            return False, "%r raised %s" % (body[:24], type(e).__name__)
    return True, "%d hostile bodies, no exception" % len(cases)


# --- final output -------------------------------------------------------------

def t_output_recorded_and_reproduced():
    def agent(h):
        r = h.client().post(B + "/v1/chat/completions", content=OPENAI_BODY)
        h.output = r.json()["choices"][0]["message"]["content"]

    h = _record(agent)
    meta, _ = store.load(h.path)
    out = meta.get("outcome") or {}
    if out.get("value") != "a stable answer":
        return False, "recorded outcome %r" % (out,)
    d = orientim.replay(h.path, agent)
    return d.ok and d.output_changed is False, \
        "replay verdict %s, output_changed=%s" % (d.diagnosis[0], d.output_changed)


def t_output_changed_is_its_own_verdict():
    """Same calls, different answer — the one divergence HTTP cannot explain."""
    def record_side(h):
        r = h.client().post(B + "/v1/chat/completions", content=OPENAI_BODY)
        h.output = r.json()["choices"][0]["message"]["content"]

    def replay_side(h):
        r = h.client().post(B + "/v1/chat/completions", content=OPENAI_BODY)
        # identical traffic, post-processing changed
        h.output = r.json()["choices"][0]["message"]["content"].upper()

    h = _record(record_side)
    d = orientim.replay(h.path, replay_side)
    code = d.diagnosis[0]
    return (not d.ok and code == "OUTPUT_CHANGED" and d.index is None), \
        "verdict %s, index %r, changed=%s" % (code, d.index, d.output_changed)


def t_undeclared_output_changes_nothing():
    """The whole backwards-compatibility argument, in one check.

    A run that never declares an output must reach exactly the verdict it
    reached before this feature existed, even when what it returns differs
    between the two runs.
    """
    state = {"n": 0}

    def agent(h):
        h.client().post(B + "/v1/chat/completions", content=OPENAI_BODY)
        state["n"] += 1
        return "answer number %d" % state["n"]       # differs every run

    h = _record(agent)
    d = orientim.replay(h.path, agent)
    return d.ok and not d.output_changed, \
        "verdict %s, changed=%s" % (d.diagnosis[0], d.output_changed)


def t_output_truncation_keeps_the_digest_honest():
    """Two long answers differing only past the storage limit must not compare equal."""
    a = model.capture_output("x" * (model.OUTPUT_LIMIT + 500))
    b = model.capture_output("x" * (model.OUTPUT_LIMIT + 499) + "y")
    same_stored = a["value"] == b["value"]
    return (same_stored and a["truncated"] and model.outputs_differ(a, b)), \
        ("stored halves equal=%s, digests differ=%s"
         % (same_stored, model.outputs_differ(a, b)))


def t_output_is_redacted():
    secret = "sk-live-DO-NOT-STORE-THIS"
    out = model.capture_output(
        json.dumps({"note": "done", "api_key": secret}),
        redactor=__import__("orientim.transport", fromlist=["x"]).redact_body)
    return secret not in json.dumps(out), "stored %r" % (out.get("value"),)


def t_unserialisable_output_is_marked_not_dropped():
    class Weird:
        def __repr__(self):
            return "<Weird object>"

    out = model.capture_output(Weird())
    return out and out["kind"] == "repr" and "Weird" in out["value"], \
        "captured %r" % (out,)


# --- agent metadata -----------------------------------------------------------

def t_agent_metadata():
    def agent(h):
        h.client().post(B + "/search", content=b"{}")

    h = _record(agent, agent="support-bot")
    meta, _ = store.load(h.path)
    by_name = (meta.get("agent") or {}).get("name")

    h2 = _record(agent, agent={"name": "support-bot", "version": "2.1.0",
                               "framework": "langgraph"})
    meta2, _ = store.load(h2.path)
    d = meta2.get("agent") or {}

    os.environ["ORIENTIM_AGENT"] = "from-the-environment"
    try:
        h3 = _record(agent)
        meta3, _ = store.load(h3.path)
        by_env = (meta3.get("agent") or {}).get("name")
    finally:
        os.environ.pop("ORIENTIM_AGENT", None)

    ok = (by_name == "support-bot" and d.get("version") == "2.1.0"
          and d.get("framework") == "langgraph"
          and by_env == "from-the-environment")
    return ok, "string=%r dict=%r env=%r" % (by_name, d, by_env)


# --- runtime ------------------------------------------------------------------

def t_runtime_recorded():
    def agent(h):
        h.client().post(B + "/search", content=b"{}")

    h = _record(agent)
    meta, _ = store.load(h.path)
    rt = meta.get("runtime") or {}
    libs = rt.get("libraries") or {}
    return ("python" in rt and "httpx" in libs), "runtime %r" % (rt,)


def t_runtime_difference_is_reported_not_enforced():
    """A library that moved is worth saying and impossible to replay."""
    was = {"python": "3.11.0", "libraries": {"httpx": "0.27.0", "openai": "1.2"}}
    now = {"python": "3.11.0", "libraries": {"httpx": "0.28.1", "openai": "1.2"}}
    diffs = model.runtime_differences(was, now)
    return (len(diffs) == 1 and diffs[0]["what"] == "httpx"
            and diffs[0]["was"] == "0.27.0"), "differences %r" % (diffs,)


# --- migration ----------------------------------------------------------------

def _downgrade_to_v3(path, out):
    """Write the same recording as format 3 would have written it."""
    meta, steps = store.load(path)
    meta = dict(meta)
    meta["format"] = 3
    for key in ("outcome", "agent", "runtime", "migrated_from"):
        meta.pop(key, None)
    old = []
    for s in steps:
        s = dict(s)
        if s.get("t") == "http":
            for key in ("role", "model", "served"):
                s.pop(key, None)
        old.append(s)
    raw = ("\n".join([json.dumps({"_meta": meta})]
                     + [json.dumps(s) for s in old]) + "\n").encode()
    with open(out, "wb") as f:
        f.write(raw)
    return out


def t_migration_preserves_chain():
    """The property the whole migration rests on.

    chain.DIGEST_FIELDS is an allowlist, so the fields format 4 adds are outside
    it and a migrated recording must hash exactly as it did before. If this ever
    fails, every recording in every user's history silently changes meaning, and
    nothing else in this suite would catch it.
    """
    def agent(h):
        c = h.client()
        c.post(B + "/v1/chat/completions", content=OPENAI_BODY)
        c.post(B + "/search", content=b'{"q":"4471"}')

    h = _record(agent)
    v3 = _downgrade_to_v3(h.path, os.path.join(ROOT, "as_v3.jsonl"))

    raw = open(v3, "rb").read()
    _m_raw, steps_raw = store.parse(raw, upgrade=False)
    m_new, steps_new = store.parse(raw)

    a = [s for s in steps_raw if s.get("t") == "http"]
    b = [s for s in steps_new if s.get("t") == "http"]
    _, root_before = chain.build_steps(a)
    _, root_after = chain.build_steps(b)
    return (root_before == root_after and m_new["format"] == store.FORMAT
            and m_new["migrated_from"] == 3), \
        "root %s -> %s, format %s" % (root_before[:12], root_after[:12],
                                      m_new.get("format"))


def t_v3_recording_still_replays():
    """End to end: a file written by the previous format replays IDENTICAL."""
    def agent(h):
        c = h.client()
        c.post(B + "/v1/chat/completions", content=OPENAI_BODY)
        c.post(B + "/search", content=b'{"q":"4471"}')

    h = _record(agent)
    v3 = _downgrade_to_v3(h.path, os.path.join(ROOT, "replay_v3.jsonl"))
    d = orientim.replay(v3, agent)
    return d.ok and not d.stale, \
        "verdict %s, stale=%s (%s)" % (d.diagnosis[0], d.stale, d.reason)


def t_migration_enriches_old_recordings():
    """An old file gains typed steps, because the request was always stored."""
    def agent(h):
        c = h.client()
        c.post(B + "/v1/chat/completions", content=OPENAI_BODY)
        c.post(B + "/search", content=b'{"q":"4471"}')

    h = _record(agent)
    v3 = _downgrade_to_v3(h.path, os.path.join(ROOT, "enrich_v3.jsonl"))
    _meta, steps = store.load(v3)
    http = [s for s in steps if s.get("t") == "http"]
    roles = [s.get("role") for s in http]
    named = (http[0].get("model") or {}).get("model")
    return roles == ["model", "tool"] and named == "gpt-4o-mini", \
        "roles %r, model %r" % (roles, named)


def t_pre_migration_formats_stay_stale():
    """Honesty at the boundary: below format 3 there is nothing to migrate.

    Format 2 stored a digest computed a different way. Silently 'migrating' it
    would answer a question it was never asked, so it keeps reporting stale.
    """
    def agent(h):
        h.client().post(B + "/search", content=b"{}")

    h = _record(agent)
    meta, steps = store.load(h.path)
    meta = dict(meta)
    meta["format"] = 2
    p = os.path.join(ROOT, "as_v2.jsonl")
    with open(p, "wb") as f:
        f.write(("\n".join([json.dumps({"_meta": meta})]
                           + [json.dumps(s) for s in steps]) + "\n").encode())

    m2, _ = store.load(p)
    d = orientim.replay(p, agent)
    return (m2["format"] == 2 and "migrated_from" not in m2
            and d.stale and d.diagnosis[0] == "STALE_FORMAT"), \
        "format %s, verdict %s" % (m2.get("format"), d.diagnosis[0])


def t_migration_never_raises():
    """A malformed recording is returned untouched, not crashed on."""
    bad = [
        ({"format": "three"}, []),
        ({"format": 3}, [{"t": "http"}]),                    # no url, no req
        ({"format": 3}, [{"t": "http", "url": None, "req": None}]),
        ({"format": 3}, [{"t": "http", "url": B + "/v1/chat/completions",
                          "req": "{not json"}]),
        (None, []),
    ]
    for meta, steps in bad:
        try:
            store.migrate(meta, steps)
        except Exception as e:
            return False, "%r raised %s" % (meta, type(e).__name__)
    return True, "%d malformed recordings, no exception" % len(bad)


def t_reading_metadata_skips_enrichment():
    """`ls` and retention read metadata only, and must not pay for step typing.

    Enrichment roughly doubles the cost of reading an old recording. Over a
    store of a few thousand that is the difference between `orientim ls` feeling
    instant and not, and none of the fields it adds can change a signature — the
    chain is computed from an allowlist they are not in.
    """
    def agent(h):
        c = h.client()
        c.post(B + "/v1/chat/completions", content=OPENAI_BODY)
        c.post(B + "/search", content=b'{"q":"4471"}')

    h = _record(agent)
    v3 = _downgrade_to_v3(h.path, os.path.join(ROOT, "lazy_v3.jsonl"))
    raw = open(v3, "rb").read()

    lean_meta, lean_steps = store.parse(raw, enrich_steps=False)
    full_meta, full_steps = store.parse(raw)

    lean_http = [x for x in lean_steps if x.get("t") == "http"]
    full_http = [x for x in full_steps if x.get("t") == "http"]

    ok = (lean_meta["format"] == store.FORMAT             # metadata still migrated
          and not any("role" in x for x in lean_http)     # steps left alone
          and all("role" in x for x in full_http)
          and store.signature(lean_meta, lean_steps)
              == store.signature(full_meta, full_steps))
    return ok, ("format %s, roles %r, signatures %s"
                % (lean_meta.get("format"),
                   [x.get("role") for x in lean_http],
                   "equal" if store.signature(lean_meta, lean_steps)
                   == store.signature(full_meta, full_steps) else "DIFFER"))


def t_classification_ignores_non_post():
    """A GET to a provider is the agent fetching, not the model thinking."""
    cases = [
        (("https://api.openai.com/v1/models", b"", "GET"), "tool"),
        (("https://api.openai.com/v1/chat/completions",
          b'{"model":"gpt-4o"}', "POST"), "model"),
        (("https://gw.internal/v1/chat/completions",
          b'{"model":"llama","messages":[]}', "POST"), "model"),
        (("https://my-app.example/v1/messages", b'{"page":2}', "POST"), "tool"),
        (("https://tools.internal/orders/4471", b"", "GET"), "tool"),
    ]
    wrong = [(a, want, model.classify(*a)) for a, want in cases
             if model.classify(*a) != want]
    return not wrong, ("misclassified %r" % (wrong,) if wrong
                       else "%d URLs classified as expected" % len(cases))


def t_repr_output_does_not_drift():
    """An object without __repr__ must not report a changed answer every run.

    repr() of such an object ends in a memory address, which is different on
    every run. Comparing two of those by digest would report OUTPUT_CHANGED
    forever on an answer that never moved. The address is provably not part of
    the value, so it is normalised away — and only in reprs we generated
    ourselves, never in a string the caller built.
    """
    class Summary:
        pass

    a = model.capture_output(Summary())
    b = model.capture_output(Summary())
    drifted = model.outputs_differ(a, b)

    mine = model.capture_output("the transaction id was 0xDEADBEEF")
    kept = "0xDEADBEEF" in mine["value"]

    real = model.outputs_differ(model.capture_output("one"),
                               model.capture_output("two"))
    return (not drifted and kept and real),         ("repr drifted=%s, own hex kept=%s, real difference seen=%s"
         % (drifted, kept, real))
