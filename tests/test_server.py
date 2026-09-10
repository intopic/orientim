# -*- coding: utf-8 -*-
"""The live replay server: start it, use it, stop it.

`server.serve` blocked on serve_forever, so nothing exercised the live-replay
endpoint at all — including its authorisation. That is the surface where a page
in a browser can ask a local process to run the user's code, so "untested" was
the wrong state for it to be in.
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import server

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/server"


def agent(run):
    run.client().post(B + "/search", content=b'{"q":"4471"}')
    return "answered"


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _record():
    with orientim.record(root=ROOT, always=True) as h:
        agent(h)
    return h.path


def _started(path):
    """An ephemeral port, so two of these can never collide."""
    srv, url, token = server.build_server(path, "tests.test_server:agent",
                                          port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, url, token, thread


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _post(url, payload, token=None, origin=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Orientim-Token", token)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body[:200]}


# --- start, serve, stop -------------------------------------------------------

def t_server_starts_on_an_ephemeral_port():
    _fresh()
    srv, url, token, _t = _started(_record())
    try:
        port = srv.server_address[1]
        return (port > 0 and url.endswith("%d/" % port) and len(token) > 20), \
            "bound %s, token %d chars" % (url, len(token))
    finally:
        srv.shutdown()
        srv.server_close()


def t_the_page_is_served():
    _fresh()
    srv, url, _token, _t = _started(_record())
    try:
        status, body = _get(url)
        return (status == 200 and "<html" in body.lower()
                and "Orientim" in body), "status %s, %d bytes" % (status, len(body))
    finally:
        srv.shutdown()
        srv.server_close()


def t_unknown_path_is_not_found():
    _fresh()
    srv, url, _token, _t = _started(_record())
    try:
        try:
            _get(url + "nope")
            return False, "an unknown path was served"
        except urllib.error.HTTPError as e:
            return e.code == 404, "status %s" % e.code
    finally:
        srv.shutdown()
        srv.server_close()


def t_clean_shutdown():
    """The port has to come back, or a second `orientim view` fails."""
    _fresh()
    path = _record()
    srv, url, _token, thread = _started(path)
    port = srv.server_address[1]
    _get(url)
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)

    srv2, _url2, _token2 = server.build_server(
        path, "tests.test_server:agent", port=port)
    try:
        rebound = srv2.server_address[1]
        return (not thread.is_alive() and rebound == port), \
            "thread stopped=%s, port %d rebound" % (not thread.is_alive(), rebound)
    finally:
        srv2.server_close()


# --- authorisation ------------------------------------------------------------

def t_replay_without_a_token_is_refused():
    """The endpoint runs the user's code. It does not run it for anyone."""
    _fresh()
    srv, url, _token, _t = _started(_record())
    try:
        status, body = _post(url + "api/replay", {})
        return (status == 403 and body.get("error") == "forbidden"), \
            "status %s, body %r" % (status, body)
    finally:
        srv.shutdown()
        srv.server_close()


def t_replay_with_a_wrong_token_is_refused():
    _fresh()
    srv, url, _token, _t = _started(_record())
    try:
        status, _b = _post(url + "api/replay", {}, token="not-the-token")
        return status == 403, "status %s" % status
    finally:
        srv.shutdown()
        srv.server_close()


def t_replay_from_another_origin_is_refused():
    """A page on some other site must not be able to drive this."""
    _fresh()
    srv, url, token, _t = _started(_record())
    try:
        status, _b = _post(url + "api/replay", {}, token=token,
                           origin="http://evil.example")
        return status == 403, "status %s" % status
    finally:
        srv.shutdown()
        srv.server_close()


def _progress(url, token):
    req = urllib.request.Request(url + "api/progress")
    req.add_header("X-Orientim-Token", token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def t_authorized_replay_runs_and_reports():
    _fresh()
    srv, url, token, _t = _started(_record())
    try:
        status, body = _post(url + "api/replay", {"strict": True}, token=token,
                             origin=url.rstrip("/"))
        if status != 200 or not body.get("started"):
            return False, "start returned %s: %r" % (status, body)

        # monotonic, not time(): a replay shims time.time() process-wide, so a
        # polling loop in this thread would consume the recorded clock entries
        # the agent is about to ask for and turn its own wait into an
        # UNCAPTURED_CLOCK verdict. monotonic is deliberately not shimmed.
        deadline = time.monotonic() + 25
        state = {}
        while time.monotonic() < deadline:
            state = _progress(url, token)
            if state.get("done"):
                break
            time.sleep(0.1)

        result = state.get("result") or {}
        return (state.get("done") and result.get("code") == "IDENTICAL"
                and state.get("events")), \
            "done=%s verdict=%s events=%d" % (
                state.get("done"), result.get("code"),
                len(state.get("events") or []))
    finally:
        srv.shutdown()
        srv.server_close()


def t_progress_is_also_gated():
    """Reading progress is gated too.

    It exposes the URLs a replay touched, so it is not a public read even
    though it runs nothing.
    """
    _fresh()
    srv, url, token, _t = _started(_record())
    try:
        try:
            _get(url + "api/progress")
            return False, "progress was readable without a token"
        except urllib.error.HTTPError as e:
            refused = e.code == 403
        allowed = _progress(url, token)
        return (refused and "running" in allowed), \
            "refused=%s, with token keys %r" % (refused, sorted(allowed)[:4])
    finally:
        srv.shutdown()
        srv.server_close()


def t_a_malformed_body_is_a_clean_error():
    """Not a traceback, and not a reset connection."""
    _fresh()
    srv, url, token, _t = _started(_record())
    try:
        req = urllib.request.Request(url + "api/replay", data=b"{not json",
                                     method="POST")
        req.add_header("X-Orientim-Token", token)
        try:
            urllib.request.urlopen(req, timeout=5)
            return False, "malformed JSON was accepted"
        except urllib.error.HTTPError as e:
            return e.code == 400, "status %s" % e.code
    finally:
        srv.shutdown()
        srv.server_close()
