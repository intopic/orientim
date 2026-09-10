# -*- coding: utf-8 -*-
"""What every agent in the company shares: the loop, the ports, the variant.

The loop is a real one. An agent sends a conversation to the model, and while
the model asks for tools it runs them, appends the results as `tool` messages,
and sends the conversation back. It stops when the model answers. That is the
shape of every agent framework, written out so nothing is hidden behind one.

`VARIANT` is how v1 becomes v2. One environment variable, read at call time —
not at import — because `orientim test` resolves an entry point through the
import system and gets its own copy of the module. A module-level constant here
would be invisible to it, and a regression check would quietly compare v1 with
itself.
"""
import json
import os

MODEL_PORT = int(os.environ.get("LAB_MODEL_PORT", 9101))
TOOLS_PORT = int(os.environ.get("LAB_TOOLS_PORT", 9102))
STATE_PORT = int(os.environ.get("LAB_STATE_PORT", 9103))
SUPERVISOR_PORT = int(os.environ.get("LAB_SUPERVISOR_PORT", 9200))
RESEARCH_PORT = int(os.environ.get("LAB_RESEARCH_PORT", 9201))
SUPPORT_PORT = int(os.environ.get("LAB_SUPPORT_PORT", 9202))
RISK_PORT = int(os.environ.get("LAB_RISK_PORT", 9203))

MODEL = "http://127.0.0.1:%d" % MODEL_PORT
TOOLS = "http://127.0.0.1:%d" % TOOLS_PORT
STATE = "http://127.0.0.1:%d" % STATE_PORT
RESEARCH = "http://127.0.0.1:%d" % RESEARCH_PORT
SUPPORT = "http://127.0.0.1:%d" % SUPPORT_PORT
RISK = "http://127.0.0.1:%d" % RISK_PORT

MAX_TURNS = 6


def variant():
    """Which version of the company is running. See lab/variants.py."""
    return os.environ.get("LAB_VARIANT", "v1")


def on(name):
    return variant() == name


def model_name(default="gpt-4o-mini"):
    """Regression 1: the model changes."""
    return "gpt-4o" if on("model_change") else default


def call_model(client, messages, plan, style="plain", tools=None):
    """One turn. The wire shape is OpenAI's, so the recording looks like a
    recording of a real agent."""
    body = {
        "model": model_name(),
        "temperature": 0.0,
        "messages": messages,
        "plan": plan,
        "style": style,
        "tools": [{"type": "function", "function": {"name": t}}
                  for t in (tools or [])],
    }
    r = client.post(MODEL + "/v1/chat/completions",
                    content=json.dumps(body, sort_keys=True).encode(),
                    timeout=60.0)
    return r.json()["choices"][0]["message"]


def call_tool(client, name, args):
    r = client.post(TOOLS + "/tool/" + name,
                    content=json.dumps(args, sort_keys=True).encode(),
                    timeout=60.0)
    return r.json()


def loop(client, task, plan, style="plain", tools=None):
    """The execution loop: model, tools, model, until an answer.

    Returns (answer, transcript). The transcript is what the agent believed,
    which is not the same as what crossed the wire — that is Orientim's job.
    """
    messages = [{"role": "system", "content": "you are an agent of the company"},
                {"role": "user", "content": task}]
    used = []
    for _turn in range(MAX_TURNS):
        message = call_model(client, messages, plan, style=style, tools=tools)
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content") or "", used
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": calls})
        for tc in calls:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            result = call_tool(client, name, args)
            used.append({"tool": name, "args": args})
            messages.append({"role": "tool", "name": name,
                             "tool_call_id": tc["id"],
                             "content": json.dumps(result, sort_keys=True)})
    return "gave up after %d turns" % MAX_TURNS, used


# LAB FINDING 1 — orientim._Holder.client() cannot be given a timeout.
#
#     def client(self, **kw):
#         c = httpx.Client(timeout=10.0, **kw)
#
# `timeout` is hard-coded and then **kw is splatted on top, so
# `run.client(timeout=60)` raises TypeError: got multiple values for keyword
# argument 'timeout'. Ten seconds is the wrong default for an agent that
# delegates to other agents, and there is no way to say so. The workaround is
# per-request timeouts, which httpx supports; the fix belongs in Orientim.

def client(run):
    """A client the recorder owns, so it is closed with the block."""
    return run.client()


def post_json(client_, url, payload, timeout=90.0):
    """Per-request timeout, because the client's cannot be set. See finding 1."""
    r = client_.post(url, content=json.dumps(payload, sort_keys=True).encode(),
                     timeout=timeout)
    return r.json()
