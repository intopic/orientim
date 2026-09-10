# -*- coding: utf-8 -*-
"""The execution model: what the agent did, not only what crossed the wire.

A recording used to answer one question — which HTTP calls happened, in what
order, with what bytes. That is enough to detect *that* behaviour changed and
not enough to say *what* changed. This module adds the four things a reader
needs to answer the second question:

    typed steps    was this call the model thinking, or the agent acting
    model metadata which model, at what temperature, with which tools offered
    final output   what the agent actually returned
    agent metadata whose agent this was, and at what version

Everything here is derived, guarded and optional. Nothing in this module is
allowed to decide whether a replay matches: classification is a *hint* written
alongside a step, never an input to the hash chain (see chain.DIGEST_FIELDS,
which is an allowlist for exactly this reason). If every function here returned
None, replay would behave identically to format 3.

That is deliberate. Provider detection is a heuristic over URLs and request
shapes, and heuristics are wrong sometimes. A wrong hint costs a misleading
label in a report. A wrong match would cost a false IDENTICAL, so the hint is
never allowed near the match.
"""
import hashlib
import json
import os
import platform
import re
import sys

try:
    from urllib.parse import urlsplit
except ImportError:                                     # pragma: no cover - py2
    from urlparse import urlsplit                       # type: ignore

# --- step kinds ---------------------------------------------------------------

MODEL = "model"        # a call to an LLM inference endpoint
TOOL = "tool"          # any other call the agent made to the outside world
UNKNOWN = "unknown"    # we were not able to decide (recordings that predate this)

# Hosts whose model endpoints do not look like anyone else's, so the host alone
# is enough. Bedrock and Gemini carry the model name in the path rather than the
# body, which is why they need naming here at all.
_MODEL_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.mistral.ai",
    "api.cohere.ai",
    "api.cohere.com",
    "api.groq.com",
    "api.together.xyz",
    "api.deepseek.com",
    "api.fireworks.ai",
    "openrouter.ai",
)

# Host suffixes, for providers that give every customer their own subdomain.
_MODEL_HOST_SUFFIXES = (
    ".openai.azure.com",
    ".api.cognitive.microsoft.com",
    ".aiplatform.googleapis.com",
)

# A host that merely *contains* one of these is treated as a provider. Bedrock
# puts the region in the middle of the name.
_MODEL_HOST_PARTS = ("bedrock-runtime", "bedrock.")

# Path shapes that mean inference wherever they are served from. This is what
# covers a self-hosted vLLM, an Ollama, a LiteLLM proxy, or a company gateway
# in front of OpenAI — all of which are the common case in production and none
# of which can be recognised by hostname.
_MODEL_PATHS = (
    "/chat/completions",
    "/completions",
    "/responses",
    "/embeddings",
    "/v1/messages",
    "/v1/complete",
    "/api/chat",            # ollama
    "/api/generate",        # ollama
    "/api/embeddings",      # ollama
)

# Keys whose presence in a request body says "this is an inference request".
# Used to confirm a path match, so that an app of one's own serving
# /v1/messages is not labelled a model call.
_MODEL_BODY_KEYS = ("model", "messages", "prompt", "input", "contents")


def _host_is_provider(host):
    host = (host or "").lower().split(":")[0]
    if host in _MODEL_HOSTS:
        return True
    if host.endswith(_MODEL_HOST_SUFFIXES):
        return True
    return any(part in host for part in _MODEL_HOST_PARTS)


def _path_is_inference(path):
    p = (path or "").lower().rstrip("/")
    if p.endswith(_MODEL_PATHS):
        return True
    # Gemini: /v1beta/models/gemini-2.0-flash:generateContent
    # Bedrock: /model/anthropic.claude-3/invoke
    return (":generatecontent" in p or ":streamgeneratecontent" in p
            or p.endswith("/invoke") or p.endswith("/invoke-with-response-stream")
            or p.endswith("/converse") or p.endswith("/converse-stream"))


# Every inference API in existence takes a POST. A GET to a provider is the
# agent fetching something — the model list, a file, a batch status — which is
# the agent acting, not the model thinking.
_MODEL_METHODS = ("POST", "PUT", "PATCH")


def classify(url, body=None, method="POST"):
    """Decide whether a step is the model thinking or the agent acting.

    A heuristic, and documented as one. Two signals have to agree before a call
    is labelled a model call unless the host is unmistakable: the path has to
    look like inference, and the body has to look like a prompt. Everything
    else the agent does over HTTP is a tool call — which is true by
    construction, since it left the process to affect something outside it.
    """
    try:
        parts = urlsplit(url or "")
    except Exception:
        return UNKNOWN
    if (method or "POST").upper() not in _MODEL_METHODS:
        return TOOL
    if _host_is_provider(parts.netloc):
        return MODEL
    if _path_is_inference(parts.path):
        obj = _as_json(body)
        if isinstance(obj, dict) and any(k in obj for k in _MODEL_BODY_KEYS):
            return MODEL
        # A path that looks like inference on a host we do not know, with a body
        # that does not look like a prompt, is somebody else's API. Say tool.
    return TOOL


# --- model metadata -----------------------------------------------------------

def _as_json(raw):
    if raw is None or raw == "" or raw == b"":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw)
    except Exception:
        return None


def _model_from_path(path):
    """Bedrock and Gemini name the model in the URL, not the body."""
    p = path or ""
    for marker in ("/model/", "/models/"):
        if marker in p:
            tail = p.split(marker, 1)[1]
            # .../models/gemini-2.0-flash:generateContent -> gemini-2.0-flash
            # .../model/anthropic.claude-3/invoke         -> anthropic.claude-3
            name = tail.split("/")[0].split(":")[0]
            if name:
                return name
    # Azure: /openai/deployments/<deployment>/chat/completions
    if "/deployments/" in p:
        name = p.split("/deployments/", 1)[1].split("/")[0]
        if name:
            return name
    return None


def _first(obj, *names):
    for n in names:
        if isinstance(obj, dict) and obj.get(n) is not None:
            return obj[n]
    return None


def describe_model_call(url, body):
    """What was asked of the model. Scalars only, by allowlist.

    An allowlist rather than "copy the body minus the messages": a copy would
    eventually carry a field somebody puts a credential in, and the whole body
    is already stored — redacted — in the step. This exists so a diff can say
    "temperature moved from 0 to 0.7" without re-parsing a prompt.
    """
    obj = _as_json(body)
    try:
        parts = urlsplit(url or "")
    except Exception:
        parts = None

    info = {}
    name = None
    if isinstance(obj, dict):
        name = _first(obj, "model", "model_id", "modelId", "engine", "deployment")
    if not name and parts is not None:
        name = _model_from_path(parts.path)
    if name is not None:
        info["model"] = str(name)[:200]

    if isinstance(obj, dict):
        # Gemini nests the knobs one level down.
        gen = obj.get("generationConfig")
        gen = gen if isinstance(gen, dict) else {}
        # Bedrock's converse API nests them too.
        inf = obj.get("inferenceConfig")
        inf = inf if isinstance(inf, dict) else {}

        temp = _first(obj, "temperature")
        if temp is None:
            temp = _first(gen, "temperature")
        if temp is None:
            temp = _first(inf, "temperature")
        if isinstance(temp, (int, float)) and not isinstance(temp, bool):
            info["temperature"] = float(temp)

        top_p = _first(obj, "top_p", "topP")
        if top_p is None:
            top_p = _first(gen, "topP")
        if isinstance(top_p, (int, float)) and not isinstance(top_p, bool):
            info["top_p"] = float(top_p)

        mx = _first(obj, "max_tokens", "max_completion_tokens", "max_output_tokens")
        if mx is None:
            mx = _first(gen, "maxOutputTokens")
        if mx is None:
            mx = _first(inf, "maxTokens")
        if isinstance(mx, int) and not isinstance(mx, bool):
            info["max_tokens"] = mx

        seed = _first(obj, "seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            info["seed"] = seed

        stream = _first(obj, "stream")
        if isinstance(stream, bool):
            info["stream"] = stream
        elif parts is not None and "stream" in (parts.path or "").lower():
            info["stream"] = True

        # Which tools the agent *offered*. Names only — a tool schema is large,
        # frequently generated, and the thing worth diffing is whether the set
        # changed, not how a description was reworded.
        tools = obj.get("tools")
        if isinstance(tools, list):
            names = []
            for t in tools:
                if not isinstance(t, dict):
                    continue
                n = (_first(t, "name")
                     or _first(t.get("function") or {}, "name")
                     or _first(t.get("functionDeclarations") or {}, "name"))
                if n:
                    names.append(str(n)[:120])
            info["tools_offered"] = sorted(set(names))

    return info or None


# --- response-side metadata ---------------------------------------------------

_USAGE_FIELDS = (
    ("input_tokens", ("input_tokens", "prompt_tokens", "promptTokenCount",
                      "inputTokens")),
    ("output_tokens", ("output_tokens", "completion_tokens",
                       "candidatesTokenCount", "outputTokens")),
    ("total_tokens", ("total_tokens", "totalTokenCount", "totalTokens")),
)


def _usage_from(obj):
    if not isinstance(obj, dict):
        return None
    usage = None
    for key in ("usage", "usageMetadata"):
        cand = obj.get(key)
        if isinstance(cand, dict):
            usage = cand
            break
    if usage is None:
        return None
    out = {}
    for name, aliases in _USAGE_FIELDS:
        v = _first(usage, *aliases)
        if isinstance(v, int) and not isinstance(v, bool):
            out[name] = v
    return out or None


def _sse_objects(text, limit=400):
    """The JSON payloads of a server-sent-event body, best effort.

    Streamed responses are the normal case for an agent, and a stream that
    reports no usage at all is worse than one that reports the usage carried by
    its final events. Bounded, because a long stream is thousands of events and
    this runs while a response is being closed.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        obj = _as_json(payload)
        if isinstance(obj, dict):
            out.append(obj)
            if len(out) >= limit:
                break
    return out


# --- tool calls ---------------------------------------------------------------
# What the model asked the agent to *do*. Stored on the model step whose
# response carried it, which is the only link here that is a fact rather than a
# guess: the step index says which model turn requested the call. Matching a
# requested tool to the HTTP step that later executed it would be inference —
# a tool name is not a URL — and is not attempted. See docs/execution-model.md.

MAX_TOOL_CALLS = 50        # a response asking for more is a runaway, not data
MAX_ARG_CHARS = 8 * 1024   # per call


def _redact_structure(obj):
    """The body redaction rule, applied to something parsed out of a body.

    Imported inside the function rather than at module scope: transport imports
    this module, so the dependency only runs one way at import time.
    """
    try:
        from .transport import redact_value
        return redact_value(obj)
    except Exception:
        return obj


def _arguments(raw):
    """Normalise tool arguments, and say which of the two things they are.

    OpenAI sends them as a JSON string; Anthropic and Gemini send an object.
    Both are stored parsed when they parse, because an evaluator asking "was
    this called with order 4471" should not have to re-parse a string.

    When they do not parse — a truncated stream, or a model that emitted
    malformed JSON, which is a real and interesting failure — the text is kept
    exactly as it arrived, marked, and never guessed at.

    Whatever is parsed out is redacted again. The outer body was redacted
    before it was stored, but OpenAI puts arguments in a JSON string *inside*
    that body, so the outer walk saw one opaque string and never looked in.
    Parsing it without redacting would surface a credential that used to be
    buried.
    """
    if raw is None:
        return None, None
    if isinstance(raw, (dict, list)):
        return _redact_structure(raw), "json"
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return str(raw)[:MAX_ARG_CHARS], "text"
    parsed = _as_json(raw)
    if isinstance(parsed, (dict, list)):
        return _redact_structure(parsed), "json"
    if raw == "":
        return {}, "json"          # OpenAI sends "" for a no-argument tool
    return raw[:MAX_ARG_CHARS], "text"


def _call(name, args, call_id=None, complete=True):
    if not name:
        return None
    arguments, kind = _arguments(args)
    out = {"name": str(name)[:120]}
    if call_id:
        out["id"] = str(call_id)[:120]
    if arguments is not None:
        out["arguments"] = arguments
        out["arguments_kind"] = kind
    if not complete:
        # A stream we could not finish reassembling. Said out loud, because an
        # evaluator checking arguments needs to know it is looking at a
        # fragment rather than at what the model actually asked for.
        out["partial"] = True
    return out


def _openai_calls(message):
    """choices[].message.tool_calls — the chat completions shape."""
    out = []
    for tc in (message.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        c = _call(fn.get("name") or tc.get("name"),
                  fn.get("arguments", tc.get("arguments")),
                  tc.get("id"))
        if c:
            out.append(c)
    return out


def _anthropic_calls(content):
    """content[] entries whose type is tool_use."""
    out = []
    for block in (content or []):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        c = _call(block.get("name"), block.get("input"), block.get("id"))
        if c:
            out.append(c)
    return out


def _responses_api_calls(output):
    """The OpenAI Responses API puts them at the top level of output[]."""
    out = []
    for item in (output or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("function_call", "tool_call"):
            continue
        c = _call(item.get("name"), item.get("arguments"),
                  item.get("call_id") or item.get("id"))
        if c:
            out.append(c)
    return out


def _gemini_calls(candidates):
    """candidates[].content.parts[].functionCall."""
    out = []
    for cand in (candidates or []):
        if not isinstance(cand, dict):
            continue
        content = cand.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in (parts or []):
            fc = part.get("functionCall") if isinstance(part, dict) else None
            if not isinstance(fc, dict):
                continue
            c = _call(fc.get("name"), fc.get("args"))
            if c:
                out.append(c)
    return out


def tool_calls_of(obj):
    """Every tool the model asked for, from one parsed response body.

    Returns [] both for a response with no tool calls and for a response in a
    shape this does not know. The two are indistinguishable from here, and
    guessing which one it is would be exactly the invention this must not do:
    an unrecognised shape yields nothing rather than something wrong.
    """
    if not isinstance(obj, dict):
        return []
    out = []
    for choice in (obj.get("choices") or []):
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message")
        if isinstance(msg, dict):
            out += _openai_calls(msg)
    out += _anthropic_calls(obj.get("content"))
    out += _responses_api_calls(obj.get("output"))
    out += _gemini_calls(obj.get("candidates"))
    return out[:MAX_TOOL_CALLS]


def _streamed_tool_calls(events, closed=True):
    """Reassemble tool calls from an event stream.

    Streaming is the normal case for an agent, so refusing to look would leave
    tool-call metadata absent exactly where it is most wanted. Both providers
    send the name once and the arguments as fragments, keyed by an index.

    Anything that cannot be reassembled into valid JSON is kept as the text
    that arrived and marked `partial`. That is the honest end state for a
    stream that was cut off — and a stream that was cut off is itself worth
    seeing.
    """
    slots = {}      # (provider, index) -> what has arrived so far

    def slot(key):
        return slots.setdefault(key, {"name": "", "id": None, "buf": "",
                                      "seen_delta": False, "name_parts": 0})

    for ev in events:
        # OpenAI: choices[].delta.tool_calls[], fragments keyed by index
        for choice in (ev.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            for tc in (delta.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                s = slot(("openai", tc.get("index", 0)))
                fn = tc.get("function")
                fn = fn if isinstance(fn, dict) else {}
                if fn.get("name"):
                    # Concatenated, not overwritten. A provider that sends the
                    # name in fragments used to end up with the last fragment
                    # as the whole name — `send_` then `email` became `email`,
                    # a name the model never asked for. Whether the joined
                    # name can be trusted is decided below, by closure.
                    s["name"] += fn["name"]
                    s["name_parts"] += 1
                if tc.get("id"):
                    s["id"] = tc["id"]
                frag = fn.get("arguments")
                if isinstance(frag, str):
                    s["buf"] += frag
                    s["seen_delta"] = True

        # Anthropic: content_block_start names it, input_json_delta fills it
        etype = ev.get("type")
        if etype == "content_block_start":
            block = ev.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                s = slot(("anthropic", ev.get("index", 0)))
                s["name"] = block.get("name") or ""
                s["name_parts"] += 1
                s["id"] = block.get("id")
        elif etype == "content_block_delta":
            delta = ev.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                s = slot(("anthropic", ev.get("index", 0)))
                frag = delta.get("partial_json")
                if isinstance(frag, str):
                    s["buf"] += frag
                    s["seen_delta"] = True

    out = []
    for _key, s in slots.items():
        if not s["name"]:
            continue
        buf = s["buf"]
        complete = (not s["seen_delta"]) or isinstance(_as_json(buf), (dict, list))
        c = _call(s["name"], buf if s["seen_delta"] else None, s["id"],
                  complete=complete)
        if not c:
            continue
        # Is the *name* something we saw whole? Once an arguments fragment has
        # arrived for this index the name field is behind us, and a stream that
        # reached its terminator has nothing more to send. Otherwise the name
        # may be a prefix of a name, and a prefix is not a witness: it must
        # decide nothing, in either direction.
        c["name_confirmed"] = bool(closed or s["seen_delta"])
        out.append(c)
    return out


def _storable(calls):
    """The stored copy of the calls, in the shape the format already had.

    `name_confirmed` is decided by whether the *stream* closed, which is a
    property of the reading rather than of the call, and it is re-derived from
    the stored body every time anything asks. Writing it into the file would
    change the recording format to carry a fact the file already implies, so
    it stays out and the format stays where it was.
    """
    out = []
    for c in calls:
        c = dict(c)
        c.pop("name_confirmed", None)
        out.append(c)
    return out


def describe_model_response(text):
    """Usage counts and the model that actually answered.

    Returns None when nothing is recoverable, which includes every case where
    the body is not JSON and not a recognisable event stream. Never raises.
    """
    if not text:
        return None
    try:
        obj = _as_json(text)
        info = {}
        if isinstance(obj, dict):
            usage = _usage_from(obj)
            if usage:
                info["usage"] = usage
            served = obj.get("model")
            if isinstance(served, str):
                info["model_served"] = served[:200]
            stop = _first(obj, "stop_reason", "finish_reason")
            if isinstance(stop, str):
                info["stop_reason"] = stop[:60]
            # The same extraction the evaluators read, so the copy stored in
            # the file and the copy reasoned from can never be two answers.
            calls = _storable(extract_tool_calls(text)["calls"])
            if calls:
                info["tool_calls"] = calls
            return info or None

        if "data:" not in text[:4096]:
            return None
        # An event stream: usage arrives in the last events that carry it, the
        # model name in the first.
        events = _sse_objects(text)
        for ev in events:
            usage = _usage_from(ev)
            if usage:
                info.setdefault("usage", {}).update(usage)
            served = ev.get("model")
            if isinstance(served, str) and "model_served" not in info:
                info["model_served"] = served[:200]
            msg = ev.get("message")
            if isinstance(msg, dict):
                usage = _usage_from(msg)
                if usage:
                    info.setdefault("usage", {}).update(usage)
                served = msg.get("model")
                if isinstance(served, str) and "model_served" not in info:
                    info["model_served"] = served[:200]
            stop = _first(ev, "stop_reason", "finish_reason")
            if isinstance(stop, str) and "stop_reason" not in info:
                info["stop_reason"] = stop[:60]
        calls = _storable(extract_tool_calls(text)["calls"])
        if calls:
            info["tool_calls"] = calls
        return info or None
    except Exception:
        # Metadata is a convenience. It never breaks a recording.
        return None


def tool_calls_in(steps):
    """Every tool call in a run, in order, each carrying who asked for it.

    The link is the index of the model step whose response requested the call.
    That is a fact: the call was in that response. What is deliberately *not*
    here is a link to the HTTP step that later executed the tool — a tool name
    is not a URL, so that link would be a guess, and a guess presented as
    provenance is worse than no provenance.

    This is what the evaluation layer reads. It never re-derives anything about
    HTTP matching; it reads what the recording already decided.
    """
    out = []
    for step in steps or []:
        if step.get("t") != "http" or step.get("role") != MODEL:
            continue
        # From the extraction, not from `served.tool_calls`. The stored copy is
        # metadata written at capture time; reading it here would put a second
        # source of the same facts back in front of the evaluators, which is
        # the defect this module was reorganised to remove.
        for call in tool_evidence(step)["calls"]:
            row = dict(call)
            row["step"] = step.get("i")
            out.append(row)
    return out


def tool_names_in(steps):
    """Just the names, for a membership test that reads like one."""
    return [c.get("name") for c in tool_calls_in(steps) if c.get("name")]


# --- one extraction result: facts and how far they reach ----------------------
# `tool_calls_of` returns [] both for a response with no tool calls and for a
# response in a shape it does not know. That is the right answer for an
# extractor — inventing a call would be worse — and it is not an answer an
# evaluator may read as "the model asked for nothing".
#
# The first attempt at closing that gap put a second function beside the
# extractor to decide whether the extraction had been exhaustive. Two sources
# of the same truth disagree, and an audit found eight responses where the
# certifier was the more optimistic of the pair: a container key present with
# the wrong shape under it, a tool call past the event bound, a `[DONE]` the
# model had written into its own prose. So coverage now comes back from the
# same parse that produced the calls. There is one derivation, and its limits
# are part of its output.

EXTRACTOR = 2               # bump when the semantics of extraction change

# Why an enumeration is not exhaustive. Each of these is a fact about the
# reading, never about the agent.
UNSUPPORTED_SCHEMA = "unsupported_schema"   # no container we know
SCHEMA_MISMATCH = "schema_mismatch"         # the container is the wrong shape
LIMIT_REACHED = "limit_reached"             # more calls than we keep
EVENTS_TRUNCATED = "events_truncated"       # more events than we parse
CHANNEL_OPEN = "channel_open"               # the stream never said it was done
PARTIAL_CALL = "partial_call"               # a call we could not finish reading
UNCONFIRMED_NAME = "unconfirmed_name"       # a name that may be a prefix
NO_RESPONSE = "no_response"                 # nothing came back to read
BINARY_BODY = "binary_body"                 # stored as bytes, never parsed
NO_BODY = "no_body"
PARSE_ERROR = "parse_error"
UNPLACED_STEP = "unplaced_step"             # might have been a model response

SSE_EVENT_LIMIT = 400       # how many stream events we parse

# The containers every provider we support puts tool calls in, and the shape
# each one has to have. A key alone certifies nothing: `output` is where the
# Responses API puts a *list*, and a vendor that puts an object there is a
# vendor whose tool calls we cannot enumerate.
_CONTAINERS = ("choices", "content", "output", "candidates")

# Endpoints with no tool-call channel at all. An embeddings response cannot
# carry one, so "nothing here" is complete rather than unreadable.
_NO_TOOL_CHANNEL = ("/embeddings", "/api/embeddings")


def _sse_frames(text, limit=SSE_EVENT_LIMIT):
    """Parse an event stream into (events, terminated, truncated).

    `terminated` is true only when a `data:` frame *is* the terminator — not
    when those six characters appear somewhere in the body. A model asked to
    discuss the protocol will write `[DONE]` into its own content, and a
    substring search cannot tell that from the end of the stream.

    Every line is scanned for the terminator even after the JSON bound is
    reached, because knowing the stream closed is cheap and knowing it was
    truncated is the point.
    """
    events, terminated, truncated = [], False, False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            terminated = True
            continue
        if len(events) >= limit:
            truncated = True
            continue
        obj = _as_json(payload)
        if isinstance(obj, dict):
            events.append(obj)
    return events, terminated, truncated


def _channels_closed(events, terminated):
    """Did every channel that opened say it was finished?

    A stream carries more than one: OpenAI indexes choices, Anthropic indexes
    content blocks. One `finish_reason` closes one choice, and the first pass
    took it for the end of the response — so a tool call still arriving on
    choice 1 was certified as absent.
    """
    if terminated:
        return True
    if any(ev.get("type") == "message_stop" for ev in events):
        return True
    seen, closed = set(), set()
    for ev in events:
        if _first(ev, "stop_reason"):
            return True
        for choice in (ev.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            i = choice.get("index", 0)
            seen.add(i)
            if choice.get("finish_reason"):
                closed.add(i)
    return bool(seen) and seen == closed


def _container_calls(obj):
    """(calls, issues) from one parsed JSON body, checking shapes as it goes."""
    calls, issues, found = [], [], False
    for key, extract in (("choices", None), ("content", _anthropic_calls),
                         ("output", _responses_api_calls),
                         ("candidates", _gemini_calls)):
        if key not in obj:
            continue
        found = True
        value = obj.get(key)
        if not isinstance(value, list):
            issues.append(SCHEMA_MISMATCH)
            continue
        if key == "choices":
            for choice in value:
                if not isinstance(choice, dict):
                    issues.append(SCHEMA_MISMATCH)
                    continue
                msg = choice.get("message")
                if isinstance(msg, dict):
                    calls += _openai_calls(msg)
        else:
            calls += extract(value)
    if not found:
        issues.append(UNSUPPORTED_SCHEMA)
    return calls, issues


def extract_tool_calls(body, url=""):
    """The single semantic extraction: what was asked for, and how far we saw.

    Returns facts and coverage from one pass, so "no call was found" and "the
    whole set was enumerated" can never be two different opinions.

        calls       confirmed tool requests, each carrying `partial` and
                    `name_confirmed`
        complete    the enumeration is exhaustive for this response
        issues      why it is not, when it is not
        schema      what we read it as
        extractor   the version that read it
    """
    out = {"calls": [], "complete": True, "issues": [], "schema": None,
           "extractor": EXTRACTOR}

    def short(*issues):
        out["complete"] = False
        for i in issues:
            if i not in out["issues"]:
                out["issues"].append(i)
        return out

    path = ""
    try:
        path = urlsplit(url or "").path.lower().rstrip("/")
    except Exception:
        path = ""
    if path.endswith(_NO_TOOL_CHANNEL):
        out["schema"] = "no_tool_channel"
        return out
    if not body:
        return short(NO_BODY)

    try:
        obj = _as_json(body)
        if isinstance(obj, dict):
            out["schema"] = "json"
            calls, issues = _container_calls(obj)
            if len(calls) > MAX_TOOL_CALLS:
                # A bounded list is not an exhaustive one. Keeping the bound is
                # right; presenting what fits as the total is not.
                calls = calls[:MAX_TOOL_CALLS]
                issues = issues + [LIMIT_REACHED]
            for c in calls:
                c.setdefault("name_confirmed", True)
            out["calls"] = calls
            return short(*issues) if issues else out

        if "data:" not in body[:4096]:
            return short(UNSUPPORTED_SCHEMA)

        out["schema"] = "sse"
        events, terminated, truncated = _sse_frames(body)
        closed = _channels_closed(events, terminated)
        calls = _streamed_tool_calls(events, closed=closed)
        issues = []
        if truncated:
            issues.append(EVENTS_TRUNCATED)
        if not closed:
            issues.append(CHANNEL_OPEN)
        if len(calls) > MAX_TOOL_CALLS:
            calls = calls[:MAX_TOOL_CALLS]
            issues.append(LIMIT_REACHED)
        if any(c.get("partial") for c in calls):
            issues.append(PARTIAL_CALL)
        if any(not c.get("name_confirmed") for c in calls):
            issues.append(UNCONFIRMED_NAME)
        if not events and not terminated:
            issues.append(UNSUPPORTED_SCHEMA)
        out["calls"] = calls
        return short(*issues) if issues else out
    except Exception:
        return short(PARSE_ERROR)


# Request keys that mean inference on their own. `model` and `input` are not
# here: plenty of ordinary APIs take a field called either.
_PROMPT_KEYS = ("messages", "contents", "prompt")


def _looks_like_model_envelope(obj):
    """A response shaped like inference, whatever the path said."""
    if not isinstance(obj, dict):
        return False
    for key, inner in (("choices", "message"), ("content", "type"),
                       ("output", "type"), ("candidates", "content")):
        value = obj.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict) \
                and inner in value[0]:
            return True
    return False


def _could_be_model(step):
    """A step we did not label MODEL and cannot rule out as one.

    `classify` labels TOOL by *default* — anything it does not recognise as
    inference. That default is what made the sharpest counterexample: the same
    response body is a violation at `/v1/chat/completions` and used to be a
    silent pass at a vendor path, because a step nobody called a model call
    contributes no model responses to look through.

    The answer is not a longer list of paths. Either side can raise the
    question on its own terms:

    - a request carrying `messages`, `contents` or `prompt` is inference-shaped
      by itself. Nothing else posts a list of chat turns.
    - a weaker request signal — `model`, `input` — needs the response to be
      shaped like inference too, which is the same two-signals discipline
      `classify` uses to *label* a step.

    Requiring both everywhere was the first attempt and it left a hole: when
    the vendor is unknown on both sides, neither signal is available and the
    prohibition went back to passing in silence.

    None of this labels the step. Labelling it would be the guess this module
    refuses. It is only enough to stop claiming the model asked for nothing.
    """
    if (step.get("method") or "POST").upper() not in _MODEL_METHODS:
        return False
    req = _as_json(step.get("req"))
    if not isinstance(req, dict):
        return False
    if any(k in req for k in _PROMPT_KEYS):
        return True
    if not any(k in req for k in _MODEL_BODY_KEYS):
        return False
    if step.get("b64") or not step.get("body"):
        return False
    try:
        return _looks_like_model_envelope(_as_json(step.get("body")))
    except Exception:
        return False


def tool_evidence(step):
    """`extract_tool_calls` for one recorded step, plus what the step itself says.

    Reads only the stored body and fields the recording already carries, so an
    old recording is judged by exactly the rule a new one is, and nothing new
    has to be captured for any of this to work.
    """
    s = step or {}
    idle = {"calls": [], "complete": True, "issues": [], "schema": None,
            "extractor": EXTRACTOR}
    if s.get("t") != "http":
        return idle
    if s.get("role") != MODEL:
        if s.get("role") == UNKNOWN or _could_be_model(s):
            return {"calls": [], "complete": False,
                    "issues": [UNPLACED_STEP], "schema": None,
                    "extractor": EXTRACTOR}
        return idle
    if s.get("unmatched") or s.get("error") or not (s.get("status") or 0):
        return {"calls": [], "complete": False, "issues": [NO_RESPONSE],
                "schema": None, "extractor": EXTRACTOR}
    if s.get("b64"):
        return {"calls": [], "complete": False, "issues": [BINARY_BODY],
                "schema": None, "extractor": EXTRACTOR}
    return extract_tool_calls(s.get("body"), s.get("url") or "")


def run_evidence(steps):
    """Every step's evidence, and whether the run's tool set is enumerable.

    `complete` here is the conjunction: one response we could not read is one
    place a request could be, so the *set* is not established even when every
    other response was perfectly legible.
    """
    per, complete, issues = [], True, []
    for step in steps or []:
        if step.get("t") != "http":
            continue
        e = tool_evidence(step)
        per.append((step, e))
        if not e["complete"]:
            complete = False
            for i in e["issues"]:
                if i not in issues:
                    issues.append(i)
    return {"steps": per, "complete": complete, "issues": issues,
            "extractor": EXTRACTOR}


# --- final output -------------------------------------------------------------

OUTPUT_LIMIT = 64 * 1024

# `repr()` of an object without its own __repr__ ends in a memory address:
#   <Summary object at 0x000001F2A4C81D90>
# The address is different on every run, so comparing two such reprs by digest
# would report OUTPUT_CHANGED for every replay, forever, on an output that never
# actually moved. The address is provably not part of the value — nothing about
# the agent's answer is encoded in where Python happened to put the object — so
# it is normalised away. Only for repr captures, which we generated ourselves;
# an address inside a string the user built is their data and is left alone.
_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")


def capture_output(value, redactor=None):
    """How the agent's answer is written down.

    Three rules, all of them about not lying:

    - The digest is taken over the *whole* value before truncation, so a
      comparison stays correct for an answer too long to store. A truncated
      value that compared equal because both were cut at the same byte would be
      the worst possible failure mode for a tool that exists to detect change.
    - An object that is not JSON-serialisable is stored as its repr, marked as
      a repr, so nobody mistakes it for structured data.
    - Anything that cannot be captured at all is recorded as an explicit
      failure, not silently dropped.
    """
    if value is None:
        return None
    try:
        if isinstance(value, str):
            kind, text = "text", value
        elif isinstance(value, bytes):
            kind, text = "text", value.decode("utf-8", "replace")
        elif isinstance(value, (dict, list, int, float, bool)):
            kind = "json"
            text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              default=str)
        else:
            kind, text = "repr", repr(value)
    except Exception as e:
        return {"kind": "unavailable", "error": type(e).__name__}

    if kind == "repr":
        text = _ADDR_RE.sub("0x...", text)

    try:
        if redactor is not None:
            text = redactor(text)
    except Exception:
        pass

    full = text.encode("utf-8", "replace")
    out = {
        "kind": kind,
        "sha": hashlib.sha256(full).hexdigest()[:32],
        "len": len(full),
        "value": text[:OUTPUT_LIMIT],
    }
    if len(text) > OUTPUT_LIMIT:
        out["truncated"] = True
    return out


def restore(captured):
    """Turn a captured value back into what was passed in, where that is safe.

    `capture_output` stores a structure as JSON text, so reading it straight
    back would hand a replay a string where the recording had a dict — and the
    entry point that did `run.input["order_id"]` would work when recorded and
    break when replayed. Only `json` captures are parsed; a repr is text and
    stays text, because pretending otherwise would be inventing an object.
    """
    if not captured:
        return None
    if captured.get("kind") != "json":
        return captured.get("value")
    if captured.get("truncated"):
        # A prefix is not valid JSON and would parse into something that is not
        # what was recorded. Hand back the text and let the caller see why.
        return captured.get("value")
    parsed = _as_json(captured.get("value"))
    return parsed if parsed is not None else captured.get("value")


def outputs_differ(a, b):
    """Compare two captured outputs. None means one side was never captured."""
    if not a or not b:
        return None
    if a.get("kind") == "unavailable" or b.get("kind") == "unavailable":
        return None
    return a.get("sha") != b.get("sha")


# --- agent metadata -----------------------------------------------------------

def normalise_agent(agent):
    """Accept a name, or a dict, and store a dict.

    Agent identity cannot be inferred — a process does not know which product
    it is — so this is whatever the caller declared, and nothing else. The
    environment fallback exists so a deployment can declare it once instead of
    at every call site.
    """
    if agent is None:
        info = {}
    elif isinstance(agent, str):
        info = {"name": agent}
    elif isinstance(agent, dict):
        info = {str(k): v for k, v in agent.items()}
    else:
        info = {"name": str(agent)}

    for key, env in (("name", "ORIENTIM_AGENT"),
                     ("version", "ORIENTIM_AGENT_VERSION")):
        if not info.get(key):
            v = os.environ.get(env)
            if v:
                info[key] = v
    return info or None


# --- runtime ------------------------------------------------------------------

_TRACKED = ("httpx", "httpx2", "requests", "openai", "anthropic",
            "google-genai", "boto3", "urllib3")

_RUNTIME = None


def _dist_version(name):
    try:
        from importlib.metadata import version, PackageNotFoundError
    except ImportError:                                 # pragma: no cover
        return None
    try:
        return version(name)
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def runtime_info(refresh=False):
    """The versions the recording ran under.

    Source 20 — library version drift — is a declared limit: we cannot make a
    replay use the httpx that recorded it. Writing the versions down does not
    fix that, and is not meant to. It converts "your replay diverged and we
    cannot tell you why" into "your replay diverged and openai went 2.3 to
    3.0 in between", which is the sentence somebody actually needs.
    """
    global _RUNTIME
    if _RUNTIME is not None and not refresh:
        return _RUNTIME
    libs = {}
    for name in _TRACKED:
        v = _dist_version(name)
        if v:
            libs[name] = v
    info = {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "platform": platform.system().lower(),
        "libraries": libs,
    }
    try:
        from . import __version__
        info["orientim"] = __version__
    except Exception:
        pass
    _RUNTIME = info
    return info


def runtime_differences(a, b):
    """What changed between the runtime of a recording and of a replay."""
    out = []
    a, b = a or {}, b or {}
    for key in ("python", "orientim", "platform"):
        if a.get(key) and b.get(key) and a[key] != b[key]:
            out.append({"what": key, "was": a[key], "now": b[key]})
    la, lb = a.get("libraries") or {}, b.get("libraries") or {}
    for name in sorted(set(la) | set(lb)):
        was, now = la.get(name), lb.get(name)
        if was != now:
            out.append({"what": name, "was": was, "now": now})
    return out
