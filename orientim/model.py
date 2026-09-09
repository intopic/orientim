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
        return info or None
    except Exception:
        # Metadata is a convenience. It never breaks a recording.
        return None


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
