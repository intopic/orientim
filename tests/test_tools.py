# -*- coding: utf-8 -*-
"""Tool calls: what the model asked the agent to do.

Recorded through the real transport wherever the shape allows it, so these
exercise the path a user's traffic takes rather than a function call.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import chain, model, store

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/tools"


def _ask(h, path, **body):
    body.setdefault("model", "gpt-4o-mini")
    body.setdefault("messages", [{"role": "user", "content": "where is 4471"}])
    return h.client().post(B + path, content=json.dumps(body).encode())


def _record(fn):
    with orientim.record(root=ROOT) as h:
        fn(h)
        h.rec.trigger("tools")
    return h


def _steps(path):
    _meta, steps = store.load(path)
    return [s for s in steps if s.get("t") == "http"]


def _calls(path):
    _meta, steps = store.load(path)
    return model.tool_calls_in(steps)


def _sse(events):
    body = "".join("data: " + json.dumps(e) + chr(10) * 2 for e in events)
    return body + "data: [DONE]" + chr(10) * 2


# --- the two shapes that must work --------------------------------------------

def t_openai_tool_calls():
    """choices[].message.tool_calls, arguments as a JSON string."""
    h = _record(lambda h: _ask(h, "/v1/chat/completions", n_tools=1))
    calls = _calls(h.path)
    if len(calls) != 1:
        return False, "expected one call, got %r" % (calls,)
    c = calls[0]
    ok = (c["name"] == "lookup_order" and c["id"] == "call_0"
          and c["arguments_kind"] == "json"
          and c["arguments"].get("order_id") == 4471)
    return ok, "recorded %r" % (c,)


def t_anthropic_tool_use():
    """content[] with type tool_use, arguments already an object."""
    h = _record(lambda h: _ask(h, "/v1/messages", model="claude-sonnet"))
    calls = _calls(h.path)
    if len(calls) != 1:
        return False, "expected one call, got %r" % (calls,)
    c = calls[0]
    ok = (c["name"] == "lookup_order" and c["id"] == "toolu_0"
          and c["arguments_kind"] == "json"
          and c["arguments"] == {"order_id": 4471})
    return ok, "recorded %r" % (c,)


def t_multiple_tool_calls():
    """Three in one response, in order, each keeping its own arguments."""
    h = _record(lambda h: _ask(h, "/v1/chat/completions", n_tools=3))
    calls = _calls(h.path)
    names = [c["name"] for c in calls]
    ids = [c.get("arguments", {}).get("order_id") for c in calls]
    ok = (names == ["lookup_order", "send_email", "web_search"]
          and ids == [4471, 4472, 4473])
    return ok, "names %r, order_ids %r" % (names, ids)


def t_no_tool_calls():
    """An ordinary answer carries no tool_calls key at all."""
    h = _record(lambda h: _ask(h, "/v1/chat/completions"))
    step = _steps(h.path)[0]
    served = step.get("served") or {}
    return ("tool_calls" not in served and _calls(h.path) == []), \
        "served %r" % (served,)


def t_unknown_shape_invents_nothing():
    """A documented-by-nobody response must yield nothing, not a guess.

    The lab route answers with `actions: [{invoke: send_email}]`, which is
    obviously a tool call to a human and is not a shape this knows. Reading it
    would mean guessing at every JSON array in every response.
    """
    h = _record(lambda h: _ask(h, "/v1/chat/completions", unknown_shape=True))
    calls = _calls(h.path)
    served = (_steps(h.path)[0].get("served") or {})
    return (calls == [] and "tool_calls" not in served), \
        "extracted %r from an unknown shape" % (calls,)


def t_unparseable_arguments_are_kept_as_text():
    """A model that emits malformed JSON is a real failure worth seeing."""
    h = _record(lambda h: _ask(h, "/v1/chat/completions", n_tools=1,
                               broken_args=True))
    calls = _calls(h.path)
    if len(calls) != 1:
        return False, "expected one call, got %r" % (calls,)
    c = calls[0]
    ok = (c["arguments_kind"] == "text"
          and c["arguments"] == "{not valid json"
          and c["name"] == "lookup_order")
    return ok, "recorded %r" % (c,)


def t_empty_arguments():
    """OpenAI sends "" for a tool that takes none. That is {}, not text."""
    calls = model.tool_calls_of({"choices": [{"message": {"tool_calls": [
        {"id": "c", "type": "function",
         "function": {"name": "get_time", "arguments": ""}}]}}]})
    ok = (len(calls) == 1 and calls[0]["arguments"] == {}
          and calls[0]["arguments_kind"] == "json")
    return ok, "recorded %r" % (calls,)


# --- provenance ---------------------------------------------------------------

def t_call_links_to_the_model_step():
    """Each call names the model step whose response asked for it."""
    def agent(h):
        _ask(h, "/v1/chat/completions")          # a model call, no tools
        h.client().post(B + "/search", content=b'{"q":1}')   # a tool call
        _ask(h, "/v1/chat/completions", n_tools=2)     # the one that asks

    h = _record(agent)
    steps = _steps(h.path)
    asked_at = [s["i"] for s in steps if (s.get("served") or {}).get("tool_calls")]
    calls = _calls(h.path)
    ok = (len(asked_at) == 1 and len(calls) == 2
          and all(c["step"] == asked_at[0] for c in calls))
    return ok, "requested at step %r, calls carry %r" % (
        asked_at, [c["step"] for c in calls])


# --- streaming ----------------------------------------------------------------

def t_streamed_openai_tool_calls():
    """Fragments keyed by index, reassembled into the arguments as sent."""
    served = model.describe_model_response(_sse([
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a",
             "function": {"name": "lookup_order", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"order_'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'id": 4471}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_b",
             "function": {"name": "send_email", "arguments": '{"to":"x"}'}}]}}]},
    ]))
    calls = (served or {}).get("tool_calls") or []
    names = [c["name"] for c in calls]
    ok = (names == ["lookup_order", "send_email"]
          and calls[0]["arguments"] == {"order_id": 4471}
          and not any(c.get("partial") for c in calls))
    return ok, "reassembled %r" % (calls,)


def t_streamed_anthropic_tool_use():
    served = model.describe_model_response(_sse([
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_1",
                           "name": "lookup_order"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"order_id"'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": ": 4471}"}},
    ]))
    calls = (served or {}).get("tool_calls") or []
    ok = (len(calls) == 1 and calls[0]["name"] == "lookup_order"
          and calls[0]["arguments"] == {"order_id": 4471})
    return ok, "reassembled %r" % (calls,)


def t_cut_off_stream_is_marked_partial():
    """A stream that stopped mid-argument is kept as it came, and labelled.

    Guessing the closing brace would hand an evaluator arguments the model
    never finished asking for.
    """
    served = model.describe_model_response(_sse([
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "t", "name": "search"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"q": "unfinis'}},
    ]))
    calls = (served or {}).get("tool_calls") or []
    ok = (len(calls) == 1 and calls[0].get("partial") is True
          and calls[0]["arguments_kind"] == "text"
          and calls[0]["arguments"] == '{"q": "unfinis')
    return ok, "recorded %r" % (calls,)


# --- safety and invariants ----------------------------------------------------

def t_credentials_inside_arguments_are_redacted():
    """The gap this feature opened, closed in the same change.

    A response body is redacted before it is stored, but OpenAI puts arguments
    in a JSON string *inside* that body — so the outer walk sees one opaque
    string and never looks in. Parsing them without redacting again would
    surface a credential that used to be buried.
    """
    h = _record(lambda h: _ask(h, "/v1/chat/completions", n_tools=1))
    raw = open(h.path, encoding="utf-8").read()
    calls = _calls(h.path)
    args = calls[0].get("arguments") or {}
    return ("sk-INSIDE-ARGS" not in raw and args.get("api_key") == "<redacted>"), \
        "secret in file=%s, stored arguments %r" % (
            "sk-INSIDE-ARGS" in raw, args)


def t_tool_calls_do_not_touch_the_chain():
    """Informative only: adding or lying about them cannot move a verdict."""
    def agent(h):
        _ask(h, "/v1/chat/completions", n_tools=2)

    h = _record(agent)
    meta, steps = store.load(h.path)
    http = [s for s in steps if s.get("t") == "http"]
    _, before = chain.build_steps(http)

    for s in http:
        served = s.get("served")
        if served and served.get("tool_calls"):
            served["tool_calls"] = [{"name": "a_tool_that_was_never_called",
                                     "arguments": {"x": 1},
                                     "arguments_kind": "json"}]
    _, after = chain.build_steps(http)

    lied = os.path.join(ROOT, "lied_tools.jsonl")
    with open(lied, "wb") as f:
        f.write(("\n".join([json.dumps({"_meta": meta})]
                           + [json.dumps(s) for s in steps]) + "\n").encode())
    d = orientim.replay(lied, agent)
    return (before == after and d.ok), \
        "root %s -> %s, replay %s" % (before[:12], after[:12], d.diagnosis[0])


def t_extraction_survives_hostile_shapes():
    """None of these may raise, and none may produce a call out of nothing."""
    cases = [
        None, [], "text", 42,
        {"choices": "not a list"},
        {"choices": [{"message": {"tool_calls": "nope"}}]},
        {"choices": [{"message": {"tool_calls": [None, 7, {}]}}]},
        {"content": [{"type": "tool_use"}]},                 # no name
        {"content": "not a list"},
        {"output": [{"type": "function_call"}]},             # no name
        {"candidates": [{"content": {"parts": [{"functionCall": {}}]}}]},
    ]
    for c in cases:
        try:
            got = model.tool_calls_of(c)
        except Exception as e:
            return False, "%r raised %s" % (c, type(e).__name__)
        if got:
            return False, "%r invented %r" % (c, got)
    return True, "%d hostile shapes, nothing raised, nothing invented" % len(cases)


def t_runaway_tool_calls_are_capped():
    """A response asking for a thousand tools is a runaway, not data."""
    calls = model.tool_calls_of({"choices": [{"message": {"tool_calls": [
        {"id": "c%d" % i, "type": "function",
         "function": {"name": "t%d" % i, "arguments": "{}"}}
        for i in range(500)]}}]})
    return len(calls) == model.MAX_TOOL_CALLS, "kept %d" % len(calls)
