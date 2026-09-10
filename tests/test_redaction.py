# -*- coding: utf-8 -*-
"""Redaction, asserted against the bytes on disk.

Every check here writes a real recording and then greps the **file**. Nothing
re-implements a redaction rule to compute what the file "should" contain: a
helper that mirrors the production logic passes for the same reason the
production code fails, and a secrets test that cannot fail is worse than none.

That is not hypothetical here. The suite already had a secrets test that passed
because it searched for three strings that happened not to be in the
environment, while every recording contained a full copy of os.environ.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import store

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/redaction"

# One distinctive string per surface, so a leak names the surface it came from.
SECRETS = {
    "nested": "sk-live-NESTED-0001",
    "deep_list": "sk-live-DEEPLIST-0002",
    "tool_args": "sk-live-TOOLARGS-0003",
    "long": "sk-live-LONGVALUE-0004",
    "response": "sk-live-RESPONSE-0005",
    "form": "sk-live-FORM-0006",
    "userinfo": "sk-live-USERINFO-0007",
    "query": "sk-live-QUERY-0008",
    "header": "sk-live-HEADER-0009",
    "env": "sk-live-ENVVAR-0010",
}


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _raw(fn, **kw):
    """Record, then hand back the file exactly as it was written."""
    with orientim.record(root=ROOT, always=True, **kw) as h:
        fn(h)
    return open(h.path, encoding="utf-8").read(), h.path


def _leaked(raw, *names):
    return [n for n in names if SECRETS[n] in raw]


# --- request bodies -----------------------------------------------------------

def t_secret_nested_in_json():
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "query": "orders",
            "auth": {"credentials": {"api_key": SECRETS["nested"]}},
        }).encode())

    raw, _p = _raw(agent)
    return (not _leaked(raw, "nested") and "<redacted>" in raw), \
        "leaked %r" % (_leaked(raw, "nested"),)


def t_secret_inside_a_list_of_objects():
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "accounts": [{"name": "a"},
                         {"name": "b", "access_token": SECRETS["deep_list"]}],
        }).encode())

    raw, _p = _raw(agent)
    return not _leaked(raw, "deep_list"), "leaked %r" % (_leaked(raw, "deep_list"),)


def t_secret_in_json_that_arrived_as_a_string():
    """A JSON document nested inside a JSON body as a string value.

    This is where OpenAI puts tool-call arguments, and the outer redaction walk
    used to see one opaque string and never look inside.
    """
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "tool_calls": [{"function": {
                "name": "lookup",
                "arguments": json.dumps({"q": "orders",
                                         "api_key": SECRETS["tool_args"]}),
            }}],
        }).encode())

    raw, _p = _raw(agent)
    return not _leaked(raw, "tool_args"), "leaked %r" % (_leaked(raw, "tool_args"),)


def t_secret_survives_a_long_value():
    """Size must not be a way past the rule."""
    _fresh()
    # Large enough that the secret sits well past any plausible scan window,
    # small enough that the lab server can echo it back. Its limit, not ours.
    padding = "x" * 20000

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "context": padding,
            "client_secret": SECRETS["long"],
            "more": padding,
        }).encode())

    raw, _p = _raw(agent)
    return not _leaked(raw, "long"), "leaked %r" % (_leaked(raw, "long"),)


def t_malformed_json_body_is_stored_without_crashing():
    """A body that is not JSON must still be recorded, and still be replayable."""
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=b'{"broken": ')

    raw, path = _raw(agent)
    d = orientim.replay(path, agent)
    return (d.ok and "broken" in raw), "verdict %s" % d.diagnosis[0]


def t_form_encoded_secret():
    """OAuth token exchange is form-encoded, the likeliest way a secret arrives."""
    _fresh()

    def agent(h):
        h.client().post(B + "/echo",
                        content=("grant_type=client_credentials&client_secret=%s"
                                 % SECRETS["form"]).encode(),
                        headers={"Content-Type":
                                 "application/x-www-form-urlencoded"})

    raw, _p = _raw(agent)
    return not _leaked(raw, "form"), "leaked %r" % (_leaked(raw, "form"),)


# --- responses ----------------------------------------------------------------

def t_secret_in_a_response_body():
    """A token endpoint answers with the token. That answer is stored."""
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "refresh_token": SECRETS["response"]}).encode())

    raw, _p = _raw(agent)
    return not _leaked(raw, "response"), "leaked %r" % (_leaked(raw, "response"),)


# --- URLs ---------------------------------------------------------------------

def t_credential_in_a_query_parameter():
    _fresh()

    def agent(h):
        h.client().post(B + "/echo?api_key=" + SECRETS["query"], content=b"{}")

    raw, _p = _raw(agent)
    return not _leaked(raw, "query"), "leaked %r" % (_leaked(raw, "query"),)


def t_userinfo_in_a_url_inside_a_body():
    """A callback URL carrying user:password@, as a value in a JSON body."""
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "callback": "https://svc:%s@hooks.example.com/x" % SECRETS["userinfo"],
        }).encode())

    raw, _p = _raw(agent)
    return not _leaked(raw, "userinfo"), "leaked %r" % (_leaked(raw, "userinfo"),)


def t_a_secret_in_a_url_path_is_a_declared_limit():
    """Not redacted, and documented as not redacted.

    Nothing distinguishes a Slack webhook token from an ordinary path segment.
    docs/recordings.md says so; this is the check that keeps the claim and the
    behaviour pointing the same way, so the day it changes, one of them fails.
    """
    _fresh()
    marker = "T00000-B11111-XXXXXXXXXXXXXXXXXXXXXXXX"

    def agent(h):
        h.client().post(B + "/echo/" + marker, content=b"{}")

    raw, _p = _raw(agent)
    docs = open("docs/recordings.md", encoding="utf-8").read()
    return (marker in raw and "path" in docs and "Slack webhook" in docs), \
        "in the file=%s, stated in docs=%s" % (marker in raw,
                                               "Slack webhook" in docs)


# --- headers ------------------------------------------------------------------

def t_request_headers_are_never_stored():
    """Authorization is used to make the call and then forgotten."""
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=b"{}", headers={
            "Authorization": "Bearer " + SECRETS["header"],
            "X-Api-Key": SECRETS["header"],
        })

    raw, path = _raw(agent)
    meta, steps = store.load(path)
    http = [s for s in steps if s.get("t") == "http"]
    return (not _leaked(raw, "header") and "authorization" not in raw.lower()
            and http[0].get("hdr_fp")), \
        "leaked=%r, fingerprint kept=%s" % (_leaked(raw, "header"),
                                            bool(http[0].get("hdr_fp")))


def t_response_set_cookie_is_dropped():
    _fresh()

    def agent(h):
        h.client().get(B + "/redirect")

    raw, path = _raw(agent)
    meta, steps = store.load(path)
    headers = {}
    for s in steps:
        if s.get("t") == "http":
            headers.update({k.lower(): v for k, v in (s.get("headers") or {}).items()})
    return ("set-cookie" not in headers and "sk-IN-LOCATION-HEADER" not in raw), \
        "response headers kept: %r" % (sorted(headers),)


# --- environment --------------------------------------------------------------

def t_environment_is_not_captured_by_default():
    _fresh()
    os.environ["ORIENTIM_LEAK_PROBE"] = SECRETS["env"]
    try:
        def agent(h):
            h.client().post(B + "/echo", content=b"{}")

        raw, _p = _raw(agent)
        return not _leaked(raw, "env"), "leaked %r" % (_leaked(raw, "env"),)
    finally:
        os.environ.pop("ORIENTIM_LEAK_PROBE", None)


def t_a_secret_shaped_env_name_is_refused_even_when_asked_for():
    _fresh()
    os.environ["PROBE_API_KEY"] = SECRETS["env"]
    try:
        import warnings

        def agent(h):
            h.client().post(B + "/echo", content=b"{}")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            raw, _p = _raw(agent, env=["PROBE_API_KEY"])
        warned = any("PROBE_API_KEY" in str(w.message) for w in caught)
        return (not _leaked(raw, "env") and warned), \
            "leaked=%r warned=%s" % (_leaked(raw, "env"), warned)
    finally:
        os.environ.pop("PROBE_API_KEY", None)


# --- the whole surface --------------------------------------------------------

def t_no_secret_reaches_any_surface():
    """One run touching every surface at once, then grep the file, the report,
    the viewer page and the diff.

    A leak in any of the things a person shares is the same leak.
    """
    _fresh()
    from orientim import diff, viewer

    def agent(h):
        c = h.client()
        c.post(B + "/echo?api_key=" + SECRETS["query"], content=json.dumps({
            "auth": {"api_key": SECRETS["nested"]},
            "tool_calls": [{"function": {"name": "t", "arguments": json.dumps(
                {"password": SECRETS["tool_args"]})}}],
            "callback": "https://u:%s@h.example/x" % SECRETS["userinfo"],
            "refresh_token": SECRETS["response"],
        }).encode(), headers={"Authorization": "Bearer " + SECRETS["header"]})
        h.output = "done"

    raw, path = _raw(agent)
    page = open(viewer.build(path, open_browser=False), encoding="utf-8").read()
    cmp_ = diff.compare(path, path)
    blob = diff.as_json(cmp_) + diff.report(cmp_)

    leaks = []
    for surface, text in (("file", raw), ("viewer", page), ("diff", blob)):
        for name in SECRETS:
            if SECRETS[name] in text:
                leaks.append("%s:%s" % (surface, name))
    return not leaks, "leaks %r" % (leaks,)


def t_redaction_does_not_break_replay():
    """The lookup key is computed from the real bytes, so redaction is free."""
    _fresh()

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "api_key": SECRETS["nested"], "q": "orders"}).encode())

    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    d = orientim.replay(h.path, agent)
    return d.ok, "verdict %s" % d.diagnosis[0]
