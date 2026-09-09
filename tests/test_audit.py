# -*- coding: utf-8 -*-
"""Pre-release audit: hunt for the things that break in someone else's hands."""
import concurrent.futures as cf
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
import labserver
from orientim import store, storage, viewer, chain

B = "http://127.0.0.1:8731"
FINDINGS = []


def check(name, fn):
    try:
        ok, note = fn()
    except Exception as e:
        ok, note = False, "raised %s: %s" % (type(e).__name__, str(e)[:90])
    FINDINGS.append((name, ok, note))
    print("  %s  %-42s %s" % ("ok " if ok else "BUG", name, note))


# 1 --- ring buffer overflow vs the hash chain -------------------------------
def t_ring():
    def many(h):
        c = h.client()
        for i in range(30):
            c.post(B + "/chat-stable", content=('{"i":%d}' % i).encode())

    with orientim.record(root="tests/_runs/audit", ring=10) as h:   # deliberately tiny
        many(h)
        h.rec.trigger("audit")
    meta, steps = store.load(h.path)
    http = [s for s in steps if s.get("t") == "http"]
    d = orientim.replay(h.path, many)
    # A truncated recording cannot replay. What matters is that it says so
    # plainly instead of failing as some unrelated divergence.
    code = d.diagnosis[0]
    return code == "TRUNCATED", ("%d of 30 steps kept, diagnosed %s"
                                 % (len(http), code))


# 2 --- step index under concurrency ------------------------------------------
def t_index_race():
    def parallel(h):
        c = h.client()
        with cf.ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda i: c.post(B + "/search",
                                         content=('{"q":%d}' % i).encode()),
                        range(24)))

    with orientim.record(root="tests/_runs/audit") as h:
        parallel(h)
        h.rec.trigger("audit")
    idx = [s["i"] for s in h.rec.steps]
    dupes = len(idx) - len(set(idx))
    return dupes == 0, ("%d duplicate step indices out of %d" % (dupes, len(idx))
                        if dupes else "all indices unique")


# 3 --- viewer against a non-local locator ------------------------------------
def t_viewer_s3():
    storage.reset_cache()
    with orientim.record(root="memory://audit") as h:
        h.client().post(B + "/chat-stable", content=b"{}")
        h.rec.trigger("audit")
    out = viewer.build(h.path, open_browser=False)
    return os.path.exists(out), "wrote viewer to %r" % out


# 4 --- secrets: what actually lands in the file ------------------------------
def t_secrets():
    def with_key(h):
        c = h.client(headers={"Authorization": "Bearer sk-SUPER-SECRET-123"})
        c.post(B + "/chat-stable?api_key=sk-IN-THE-URL-456",
               content=b'{"password":"hunter2"}')

    with orientim.record(root="tests/_runs/audit") as h:
        with_key(h)
        h.rec.trigger("audit")
    raw = open(h.path, encoding="utf-8").read()
    leaks = [s for s in ("sk-SUPER-SECRET-123", "sk-IN-THE-URL-456", "hunter2")
             if s in raw]
    return (not leaks), ("recording contains %s" % ", ".join(leaks) if leaks
                         else "no secrets in the file")


# 5 --- nested record() --------------------------------------------------------
def t_nested():
    with orientim.record(root="tests/_runs/audit") as outer:
        outer.client().post(B + "/chat-stable", content=b"{}")
        with orientim.record(root="tests/_runs/audit") as inner:
            inner.client().post(B + "/chat-stable", content=b"{}")
            inner.rec.trigger("audit")
        # The inner scope used to switch the clock shim off on its way out, so
        # everything the outer run did afterwards was recorded without it.
        before = sum(1 for x in outer.rec.steps if x.get("t") == "shim")
        time.time()
        after = sum(1 for x in outer.rec.steps if x.get("t") == "shim")
        outer.client().post(B + "/chat-stable", content=b"{}")
        outer.rec.trigger("audit")
    still_shimmed = after > before
    return (len(outer.rec.steps) > 0 and len(inner.rec.steps) > 0 and still_shimmed,
            "outer=%d steps, inner=%d steps, outer still shimmed afterwards=%s"
            % (len(outer.rec.steps), len(inner.rec.steps), still_shimmed))


# 6 --- corrupt recording ------------------------------------------------------
def t_corrupt():
    p = os.path.join("tests", "_runs", "audit", "corrupt.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write('{"_meta": {"run_id": "x"}}\nnot json at all\n')
    try:
        store.load(p)
        return False, "loaded corrupt file without complaint"
    except Exception as e:
        return True, "refused with %s" % type(e).__name__


# 7 --- missing entry point ----------------------------------------------------
def t_bad_entry():
    from orientim import server
    try:
        server._load_entry("no.such.module:nope")
        return False, "accepted a module that does not exist"
    except Exception as e:
        return True, "refused with %s" % type(e).__name__


# 8 --- python version floor ---------------------------------------------------
def t_py_floor():
    import ast, glob
    bad = []
    for f in glob.glob("orientim/*.py"):
        src = open(f, encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # match statements are 3.10+, declared floor is 3.9
            if node.__class__.__name__ == "Match":
                bad.append("%s: match statement (3.10+)" % f)
    return (not bad), ("; ".join(bad) if bad else "nothing above the 3.9 floor")


# 9 --- capture without touching the user's code -----------------------------
def t_foreign_client():
    """The whole adoption question: does it see a client we did not hand out?"""
    import httpx

    class FakeSDK:
        """How openai and anthropic build theirs: their own, inside themselves."""
        def __init__(self, base):
            self._c = httpx.Client(base_url=base,
                                   headers={"Authorization": "Bearer sk-SECRET"})

        def chat(self, q):
            return self._c.post("/chat-stable", json={"prompt": q}).json()

    def agent(h):
        sdk = FakeSDK(B)
        sdk.chat("first")
        sdk.chat("second")

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    n = len([s for s in h.rec.steps if s.get("t") == "http"])
    leaked = "sk-SECRET" in open(h.path, encoding="utf-8").read()
    d = orientim.replay(h.path, agent)
    ok = n == 2 and not leaked and d.ok
    return ok, "%d steps, auth leaked=%s, replay=%s" % (n, leaked, d.diagnosis[0])


# 10 --- async, which is most agent code --------------------------------------
def t_async():
    import asyncio, httpx

    async def _run():
        async with httpx.AsyncClient() as c:
            await c.post(B + "/chat-stable", content=b'{"a":1}')
            await c.post(B + "/search", content=b'{"q":"x"}')

    def agent(h):
        asyncio.run(_run())

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    n = len([s for s in h.rec.steps if s.get("t") == "http"])
    d = orientim.replay(h.path, agent)
    return (n == 2 and d.ok), "%d steps, replay=%s" % (n, d.diagnosis[0])


# --- regressions from the pre-publication audit ------------------------------
# Every one of these reproduced a defect that was in the code. They are here so
# it cannot come back, not to show that a feature works.

# 11 --- the whole environment used to be written into every recording --------
def t_env_not_captured():
    os.environ["AUDIT_FAKE_API_KEY"] = "sk-MUST-NOT-BE-WRITTEN"
    with orientim.record(root="tests/_runs/audit") as h:
        h.client().post(B + "/chat-stable", content=b"{}")
        h.rec.trigger("audit")
    raw = open(h.path, encoding="utf-8").read()
    meta, _ = store.load(h.path)
    ok = "sk-MUST-NOT-BE-WRITTEN" not in raw and not meta.get("env")
    return ok, "env stored in the file: %r" % (meta.get("env"),)


# 12 --- a named variable still replays, and is put back afterwards -----------
def t_env_opt_in():
    os.environ["AUDIT_MODE"] = "first"

    def agent(h):
        h.client().post(B + "/chat-stable",
                        content=json.dumps({"m": os.environ["AUDIT_MODE"]}).encode())

    with orientim.record(root="tests/_runs/audit", env=["AUDIT_MODE"]) as h:
        agent(h)
        h.rec.trigger("audit")
    os.environ["AUDIT_MODE"] = "changed"
    d = orientim.replay(h.path, agent)
    restored = os.environ["AUDIT_MODE"] == "changed"
    return (d.ok and restored), "replay=%s, live env restored=%s" % (
        d.diagnosis[0], restored)


# 13 --- a recording that captured nothing must not report IDENTICAL ----------
def t_nothing_captured():
    def agent(h):
        raise RuntimeError("failed before it called anything")

    try:
        with orientim.record(root="tests/_runs/audit") as h:
            agent(h)
    except RuntimeError:
        pass
    d = orientim.replay(h.path, agent)
    return (not d.ok and d.diagnosis[0] == "NOTHING_CAPTURED"), d.diagnosis[0]


# 13b --- traffic through a library we do not intercept is noticed and named ---
def t_unseen_library():
    def agent(h):
        h.client().post(B + "/chat-stable", content=b'{"a":1}')       # captured
        urllib.request.urlopen(B + "/chat-stable", data=b"{}").read()  # not

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    meta, _ = store.load(h.path)
    d = orientim.replay(h.path, agent)
    ok = (meta.get("unseen_n") == 1 and not d.ok
          and d.diagnosis[0] == "UNCAPTURED_LIBRARY")
    return ok, "%d uncaptured call(s) noticed, verdict %s" % (
        meta.get("unseen_n", 0), d.diagnosis[0])


# 14 --- a replay that crashes after the last step is not identical -----------
def t_replay_raises():
    boom = {"on": False}

    def agent(h):
        h.client().post(B + "/chat-stable", content=b'{"a":1}')
        if boom["on"]:
            raise ZeroDivisionError("a fix that broke the tail of the run")

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    boom["on"] = True
    d = orientim.replay(h.path, agent)
    return (not d.ok and d.diagnosis[0] == "REPLAY_RAISED"), d.diagnosis[0]


# 15 --- ...but the exception the recording ended on is not a divergence ------
def t_recorded_exception_replays():
    def agent(h):
        h.client().post(B + "/chat-stable", content=b'{"a":1}')
        raise ValueError("this is what the recording captured")

    try:
        with orientim.record(root="tests/_runs/audit") as h:
            agent(h)
    except ValueError:
        pass
    d = orientim.replay(h.path, agent)
    return d.ok, d.diagnosis[0]


# 15b --- a fixed bug is not the same as "nothing changed" --------------------
def t_fixed_and_still_broken():
    """The question a divergence report cannot answer: is the bug gone?"""
    crash = {"on": True}

    def agent(h):
        h.client().post(B + "/echo", content=b'{"a":1}')
        if crash["on"]:
            raise ValueError("the bug")
        return "ok"

    try:
        with orientim.record(root="tests/_runs/audit") as h:
            agent(h)
    except ValueError:
        pass

    crash["on"] = False
    fixed = orientim.replay(h.path, agent)
    crash["on"] = True
    still = orientim.replay(h.path, agent)
    ok = (fixed.diagnosis[0] == "FIXED" and fixed.ok
          and still.diagnosis[0] == "STILL_BROKEN" and still.ok)
    return ok, "crash removed=%s, crash kept=%s" % (
        fixed.diagnosis[0], still.diagnosis[0])


# 15c --- the wrong-answer case, which raises nothing at all ------------------
def t_fixed_via_check():
    """A 200 with a bad answer is the case the tool exists for."""
    good = {"on": False}

    def answerer(h):
        h.client().post(B + "/echo", content=b'{"q":1}')
        return "right" if good["on"] else "wrong"

    def looks_right(ans):
        return ans == "right"

    with orientim.record(root="tests/_runs/audit", always=True) as h:
        answerer(h)
    good["on"] = True
    fixed = orientim.replay(h.path, answerer, check=looks_right)
    good["on"] = False
    still = orientim.replay(h.path, answerer, check=looks_right)
    ok = (fixed.diagnosis[0] == "FIXED" and still.diagnosis[0] == "STILL_BROKEN")
    return ok, "check passes=%s, check fails=%s" % (
        fixed.diagnosis[0], still.diagnosis[0])


# 15d --- a fix that adds a call is not an uncaptured source ------------------
def t_new_call():
    extra = {"on": False}

    def grower(h):
        c = h.client()
        c.post(B + "/echo", content=b'{"s":1}')
        if extra["on"]:
            c.post(B + "/echo", content=b'{"s":2}')

    with orientim.record(root="tests/_runs/audit", always=True) as h:
        grower(h)
    extra["on"] = True
    d = orientim.replay(h.path, grower)
    return (not d.ok and d.diagnosis[0] == "NEW_CALL"), d.diagnosis[0]


# 16 --- two requests that differ only by header are not the same request -----
def t_header_blind_match():
    order = ["acme", "globex"]

    def agent(h):
        c = h.client()
        for t in order:
            c.post(B + "/chat-stable", headers={"X-Tenant": t}, content=b'{"q":1}')

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    order[:] = ["globex", "acme"]           # the code change
    d = orientim.replay(h.path, agent)
    return (not d.ok and d.diagnosis[0] == "HEADERS_CHANGED"), d.diagnosis[0]


# 17 --- overlapping record() must leave httpx as it found it -----------------
def t_patch_leak():
    import httpx
    pristine = httpx.Client.__init__
    g1, g2 = threading.Event(), threading.Event()

    def first():
        with orientim.record(root="tests/_runs/audit") as x:
            g1.set(); g2.wait(3)            # entered first, leaves first
            x.rec.trigger("audit")

    def second():
        g1.wait(3)
        with orientim.record(root="tests/_runs/audit") as y:
            g2.set(); time.sleep(0.25)      # entered second, leaves last
            y.rec.trigger("audit")

    ta, tb = threading.Thread(target=first), threading.Thread(target=second)
    ta.start(); tb.start(); ta.join(); tb.join()
    restored = httpx.Client.__init__ is pristine
    if not restored:
        httpx.Client.__init__ = pristine
    wrapped = getattr(httpx.Client()._transport, "orientim_wrapped", False)
    return (restored and not wrapped), ("__init__ restored=%s, later clients "
                                        "wrapped=%s" % (restored, wrapped))


# 17b --- an exception while building a client leaves httpx clean ------------
def t_init_exception():
    import httpx
    pristine = httpx.Client.__init__
    raised = None
    with orientim.record(root="tests/_runs/audit", always=True) as h:
        try:
            httpx.Client(this_is_not_a_valid_kwarg=1)   # httpx __init__ raises
        except TypeError as e:
            raised = type(e).__name__
        # capture must still work after a client construction blew up mid-block
        h.client().post(B + "/chat-stable", content=b"{}")
    restored = httpx.Client.__init__ is pristine
    if not restored:
        httpx.Client.__init__ = pristine
    wrapped = getattr(httpx.Client()._transport, "orientim_wrapped", False)
    n = len([s for s in h.rec.steps if s.get("t") == "http"])
    return (raised == "TypeError" and restored and not wrapped and n == 1), (
        "bad Client() raised %s, httpx restored=%s, later wrapped=%s, captured=%d"
        % (raised, restored, wrapped, n))


# 18 --- a response body cannot break out of the viewer's <script> ------------
def t_viewer_injection():
    payload = "</script><img src=x onerror=alert(1)>"

    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({"page": payload}).encode())

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    out = viewer.build(h.path, out="tests/_runs/audit/injection.html",
                       open_browser=False)
    page = open(out, encoding="utf-8").read()
    data = page[page.index("var S = "):page.index("var track=")]
    broke_out = "</script>" in data
    return (not broke_out), ("the recorded body closed the script tag"
                             if broke_out else "escaped inside the data block")


# 19 --- credentials in a form body and in the response ----------------------
def t_oauth_secrets():
    def agent(h):
        h.client().post(
            B + "/echo",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            content=b"grant_type=client_credentials&client_secret=SHHH-FORM-SECRET")

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    raw = open(h.path, encoding="utf-8").read()
    leaked = "SHHH-FORM-SECRET" in raw
    return (not leaked), ("client_secret is in the file" if leaked
                          else "redacted in request and response")


# 19b --- a credential in a response header (a redirect Location) ------------
def t_resp_header_secret():
    def agent(h):
        # follow_redirects=False: keep the 302 itself, whose stored response
        # headers hold the Location URL with a credential-shaped query param.
        h.client().get(B + "/redirect", follow_redirects=False)

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    raw = open(h.path, encoding="utf-8").read()
    leaked = "sk-IN-LOCATION-HEADER" in raw
    return (not leaked), ("Location header wrote the secret to the file" if leaked
                          else "redacted in the stored response header")


# 19c --- a credential in a URL that is a JSON *value*, not a field name ------
def t_body_url_userinfo():
    def agent(h):
        h.client().post(B + "/echo", content=json.dumps({
            "callback": "https://svc:hunter2@hooks.example.com/deliver",
            "note": "webhook set",
        }).encode())

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    raw = open(h.path, encoding="utf-8").read()
    leaked = "hunter2" in raw
    # The host survives so the URL is still recognisable in the recording.
    kept = "hooks.example.com" in raw
    return (not leaked and kept), (
        "user:pass@ in a JSON value leaked" if leaked
        else "userinfo stripped, host kept")


# 19d --- naming a secret-shaped env var is refused, not written -------------
def t_env_secret_blocked():
    import warnings
    os.environ["AUDIT_FAKE_OPENAI_API_KEY"] = "sk-MUST-NOT-BE-STORED"

    def agent(h):
        h.client().post(B + "/chat-stable", content=b"{}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with orientim.record(root="tests/_runs/audit", always=True,
                             env=["AUDIT_FAKE_OPENAI_API_KEY"]) as h:
            agent(h)
    os.environ.pop("AUDIT_FAKE_OPENAI_API_KEY", None)
    raw = open(h.path, encoding="utf-8").read()
    meta, _ = store.load(h.path)
    warned = any(issubclass(w.category, RuntimeWarning)
                 and "looks like a secret" in str(w.message) for w in caught)
    ok = ("sk-MUST-NOT-BE-STORED" not in raw and not meta.get("env") and warned)
    return ok, ("stored=%r, warned=%s" % (meta.get("env"), warned))


# 20 --- a binary response comes back as the same bytes ----------------------
def t_binary_roundtrip():
    seen = []

    def agent(h):
        seen.append(h.client().get(B + "/bytes").content)

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    orientim.replay(h.path, agent)
    same = len(seen) == 2 and seen[0] == seen[1]
    return same, "%d bytes recorded, identical on replay: %s" % (len(seen[0]), same)


# 20b --- recording a stream must not turn it into one lump -------------------
def t_streaming_passthrough():
    runs = []

    def agent(h):
        t0 = time.monotonic()
        arrivals = []
        with h.client().stream("GET", B + "/stream") as r:
            for chunk in r.iter_raw():
                if chunk:
                    arrivals.append(((time.monotonic() - t0) * 1000.0, chunk))
        runs.append(arrivals)

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    rec = runs[-1]
    # Four chunks, and the last one must not have arrived with the first: a
    # buffering recorder delivers all of them together at the end.
    live = len(rec) == 4 and (rec[-1][0] - rec[0][0]) > 40
    d = orientim.replay(h.path, agent)
    rep = runs[-1]
    shape = [c for _, c in rec] == [c for _, c in rep]
    return (live and shape and d.ok), (
        "%d chunks over %.0f ms while recording, boundaries preserved=%s, replay=%s"
        % (len(rec), rec[-1][0] - rec[0][0] if rec else 0, shape, d.diagnosis[0]))


# 20c --- realtime replay reproduces the recorded gaps ------------------------
def t_streaming_realtime():
    runs = []

    def agent(h):
        t0 = time.monotonic()
        arrivals = []
        with h.client().stream("GET", B + "/stream") as r:
            for chunk in r.iter_raw():
                if chunk:
                    arrivals.append((time.monotonic() - t0) * 1000.0)
        runs.append(arrivals)

    with orientim.record(root="tests/_runs/audit") as h:
        agent(h)
        h.rec.trigger("audit")
    orientim.replay(h.path, agent)
    fast = runs[-1]
    orientim.replay(h.path, agent, realtime=True)
    slow = runs[-1]
    spread = lambda v: (v[-1] - v[0]) if len(v) > 1 else 0.0
    return (spread(fast) < 20 and spread(slow) > 40), (
        "fast replay spans %.0f ms, realtime spans %.0f ms"
        % (spread(fast), spread(slow)))


# 21 --- the live replay server refuses a call without its token -------------
def t_server_csrf():
    from orientim import server as sv
    saved = sv.Handler.ctx
    sv.Handler.ctx = {"token": "the-real-token", "origin": "http://127.0.0.1:8740"}

    class Fake:
        def __init__(self, headers):
            self.headers = headers
        ctx = sv.Handler.ctx

    try:
        good = sv.Handler._authorised(Fake({"X-Orientim-Token": "the-real-token"}))
        none = sv.Handler._authorised(Fake({}))
        cross = sv.Handler._authorised(Fake({"X-Orientim-Token": "the-real-token",
                                             "Origin": "https://evil.example"}))
    finally:
        sv.Handler.ctx = saved
    return (good and not none and not cross), (
        "with token=%s, without=%s, cross-origin=%s" % (good, none, cross))


# --- retention, counterfactuals and integration ------------------------------

# 22 --- the flagship case: a wrong answer triggers nothing -------------------
def t_always():
    def wrong(h):
        h.client().post(B + "/chat-stable", content=b'{"q":1}')
        return "a wrong answer, returned with a 200"

    with orientim.record(root="tests/_runs/audit") as a:
        wrong(a)
    with orientim.record(root="tests/_runs/audit", always=True) as b:
        wrong(b)
    os.environ["ORIENTIM_ALWAYS"] = "1"
    try:
        with orientim.record(root="tests/_runs/audit") as c:
            wrong(c)
    finally:
        os.environ.pop("ORIENTIM_ALWAYS", None)
    return (a.path is None and b.path and c.path), (
        "default=%s, always=%s, env=%s" % (a.path, bool(b.path), bool(c.path)))


# 23 --- on_capture fires, and a broken hook does not reach the agent --------
def t_on_capture():
    import warnings
    seen = []

    def agent(h):
        h.client().post(B + "/chat-stable", content=b"{}")

    with orientim.record(root="tests/_runs/audit", always=True,
                         on_capture=lambda p, m: seen.append(m["run_id"])) as h:
        agent(h)

    def broken(path, meta):
        raise RuntimeError("the alerting is down")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with orientim.record(root="tests/_runs/audit", always=True,
                             on_capture=broken) as h2:
            agent(h2)
    survived = h2.path is not None and len(caught) == 1
    return (len(seen) == 1 and survived), (
        "hook called=%d, broken hook warned instead of raising=%s"
        % (len(seen), survived))


# 24 --- the generator state is stored only when the run drew from it --------
def t_random_state_conditional():
    import random

    # /echo, not /chat-stable: the lab server sleeps for a random interval,
    # and it runs in this process. Detection is process-wide by design — any
    # thread that draws from the shared generator during the block counts —
    # so a probe has to avoid a server that draws from it too.
    def plain(h):
        h.client().post(B + "/echo", content=b"{}")

    def rolls(h):
        h.client().post(B + "/echo",
                        content=json.dumps({"n": random.randint(1, 10 ** 9)}).encode())

    with orientim.record(root="tests/_runs/audit", always=True) as h1:
        plain(h1)
    with orientim.record(root="tests/_runs/audit", always=True) as h2:
        rolls(h2)
    m1, _ = store.load(h1.path)
    m2, _ = store.load(h2.path)
    d = orientim.replay(h2.path, rolls)
    ok = m1["random_state"] is None and m2["random_state"] and d.ok
    return ok, ("unused=%s, used=%s, randint replays=%s"
                % (m1["random_state"], bool(m2["random_state"]), d.diagnosis[0]))


# 25 --- a counterfactual sends the agent down the other branch --------------
def t_counterfactual():
    took = []

    def agent(h):
        c = h.client()
        hits = c.post(B + "/search", content=b'{"q":null}').json()
        if not hits.get("hits"):
            c.post(B + "/chat-stable", content=b'{"act":"apologise"}')
            took.append("apologised")
        else:
            c.post(B + "/chat-stable", content=b'{"act":"answer"}')
            took.append("answered")

    with orientim.record(root="tests/_runs/audit", always=True) as h:
        agent(h)
    recorded = took[-1]
    d = orientim.replay(h.path, agent,
                        patch={0: {"body": json.dumps({"hits": ["found"]})}})
    changed = took[-1]
    same = orientim.replay(h.path, agent,
                           patch={0: {"headers": {"X-Irrelevant": "1"}}})
    ok = (recorded == "apologised" and changed == "answered"
          and not d.ok and d.diagnosis[0] == "COUNTERFACTUAL"
          and same.diagnosis[0] == "COUNTERFACTUAL_SAME")
    return ok, "recorded=%s, patched=%s, verdicts=%s/%s" % (
        recorded, changed, d.diagnosis[0], same.diagnosis[0])


# 26 --- a patched replay is never reported as a reproduction ----------------
def t_counterfactual_never_ok():
    def agent(h):
        h.client().post(B + "/chat-stable", content=b"{}")

    with orientim.record(root="tests/_runs/audit", always=True) as h:
        agent(h)
    d = orientim.replay(h.path, agent, patch={0: {"status": 200}})
    refused = []
    for bad in ({7: {"status": 200}}, {0: {"nonsense": 1}}):
        try:
            orientim.replay(h.path, agent, patch=bad)
        except ValueError:
            refused.append(True)
    return (not d.ok and len(refused) == 2), (
        "ok=%s, out-of-range and unknown-field both refused=%s"
        % (d.ok, len(refused) == 2))


# 27 --- retention keeps one of each distinct failure, not five hundred ------
def t_prune_by_signature():
    import shutil
    root = "tests/_runs/prune_audit"
    shutil.rmtree(root, ignore_errors=True)
    storage.reset_cache()

    def one(h):
        h.client().post(B + "/chat-stable", content=b'{"same":1}')
        raise RuntimeError("the same bug")

    def two(h):
        h.client().post(B + "/search", content=b'{"q":"x"}')
        raise ValueError("a different bug")

    for fn, err, n in ((one, RuntimeError, 5), (two, ValueError, 2)):
        for _ in range(n):
            try:
                with orientim.record(root=root) as h:
                    fn(h)
            except err:
                pass
    before = len(store.survey(root))
    dry = store.prune(root, per_signature=1, dry_run=True)
    untouched = len(store.survey(root)) == before
    removed = store.prune(root, per_signature=1)
    after = store.survey(root)
    no_rule = store.prune(root) == []
    shutil.rmtree(root, ignore_errors=True)
    ok = (before == 7 and len(dry) == 5 and untouched
          and len(removed) == 5 and len(after) == 2 and no_rule)
    return ok, ("%d recordings -> %d after keeping one per distinct failure; "
                "dry run touched nothing=%s; empty policy is a no-op=%s"
                % (before, len(after), untouched, no_rule))


# 28 --- diff between two recordings ----------------------------------------
def t_diff():
    from orientim import diff

    def short(h):
        h.client().post(B + "/chat-stable", content=b'{"q":1}')

    def longer(h):
        c = h.client()
        c.post(B + "/chat-stable", content=b'{"q":1}')
        c.post(B + "/search", content=b'{"q":"more"}')

    with orientim.record(root="tests/_runs/audit", always=True) as a:
        short(a)
    with orientim.record(root="tests/_runs/audit", always=True) as b:
        longer(b)
    same = diff.compare(a.path, a.path)
    other = diff.compare(a.path, b.path)
    ok = (same["identical"] and same["index"] is None
          and not other["identical"] and other["index"] == 1
          and other["rows"][1]["state"] == "only in B")
    return ok, "self-diff identical=%s, a-vs-b diverges at %s" % (
        same["identical"], other["index"])


# 29 --- a recording can be found from the trace it belonged to --------------
def t_trace_link():
    os.environ["TRACEPARENT"] = ("00-4bf92f3577b34da6a3ce929d0e0e4736"
                                 "-00f067aa0ba902b7-01")
    try:
        with orientim.record(root="tests/_runs/audit", always=True) as h:
            h.client().post(B + "/chat-stable", content=b"{}")
    finally:
        os.environ.pop("TRACEPARENT", None)
    meta, _ = store.load(h.path)
    trace = (meta.get("trace") or {}).get("trace_id")
    return (trace == "4bf92f3577b34da6a3ce929d0e0e4736"), "trace_id=%s" % trace


# 30 --- clients handed out by the holder are closed with the block ----------
def t_holder_closes_clients():
    grabbed = []

    def agent(h):
        for _ in range(3):
            c = h.client()
            grabbed.append(c)
            c.post(B + "/chat-stable", content=b"{}")

    with orientim.record(root="tests/_runs/audit", always=True) as h:
        agent(h)
    leaked = [c for c in grabbed if not c.is_closed]
    return (not leaked), "%d clients handed out, %d still open" % (
        len(grabbed), len(leaked))

# 31 --- the opt-in cap is the only thing that deletes without a command ------
def t_auto_cap():
    import shutil
    root = "tests/_runs/cap_audit"
    shutil.rmtree(root, ignore_errors=True)
    storage.reset_cache()

    def agent(h):
        h.client().post(B + "/echo", content=b"{}")

    # off by default
    for _ in range(4):
        with orientim.record(root=root, always=True) as h:
            agent(h)
    without = len(store.survey(root))

    os.environ["ORIENTIM_MAX_RUNS"] = "2"
    try:
        for _ in range(3):
            with orientim.record(root=root, always=True) as h:
                agent(h)
        with_cap = len(store.survey(root))
        newest_survived = store.load(h.path) is not None
    finally:
        os.environ.pop("ORIENTIM_MAX_RUNS", None)
    shutil.rmtree(root, ignore_errors=True)
    return (without == 4 and with_cap == 2 and newest_survived), (
        "off by default kept %d, cap=2 left %d, the run just written survived=%s"
        % (without, with_cap, newest_survived))


# 32 --- an async server: many record() blocks, one thread ------------------
def t_async_server_isolation():
    """The shape the documentation recommends, and the one that used to break.

    Five requests in flight on one event loop. Thread-id attribution put four
    recordings' traffic into the fifth.
    """
    import asyncio
    import httpx as _httpx

    async def handler(i):
        with orientim.record(root="tests/_runs/audit", always=True) as run:
            async with _httpx.AsyncClient() as c:
                await c.post(B + "/echo", content=json.dumps({"r": i}).encode())
            await asyncio.sleep(0.01)          # yields; the tasks interleave
            async with _httpx.AsyncClient() as c:
                await c.post(B + "/echo",
                             content=json.dumps({"r": i, "b": 1}).encode())
        return i, run.path

    async def main():
        return await asyncio.gather(*[handler(i) for i in range(5)])

    wrong = []
    for i, path in asyncio.run(main()):
        _m, steps = store.load(path)
        http = [x for x in steps if x.get("t") == "http"]
        who = sorted({json.loads(x["req"]).get("r") for x in http})
        if len(http) != 2 or who != [i]:
            wrong.append((i, len(http), who))
    return (not wrong), ("each recording holds only its own 2 steps"
                         if not wrong else "mis-attributed: %r" % wrong)


# 32b --- concurrent record() never files one run's traffic under another ----
def t_concurrent_no_crosstalk():
    """Two overlapping record() blocks. Agent A's work runs on worker threads
    that build their OWN httpx clients — a worker carries no context, so
    attribution falls back. Under overlap the fallback must refuse rather than
    guess: B's recording must never contain a byte of A's traffic. This is the
    dangerous failure — one recording's bodies and secrets in another's file.
    """
    import httpx as _httpx
    a_in, b_in = threading.Event(), threading.Event()
    built = threading.Barrier(4)          # 3 A-workers + B: forces real overlap
    out = {}

    def run_a():
        def call():
            c = _httpx.Client()            # foreign client, empty context
            built.wait(5)                  # all rendezvous while B is open
            c.post(B + "/echo", content=json.dumps({"tag": "A"}).encode())
            c.close()
        with orientim.record(root="tests/_runs/audit", always=True) as h:
            a_in.set(); b_in.wait(3)       # A opens first; wait until B is open
            ts = [threading.Thread(target=call) for _ in range(3)]
            for t in ts: t.start()
            for t in ts: t.join()
        _m, steps = store.load(h.path)
        out["A"] = {json.loads(s["req"]).get("tag")
                    for s in steps if s.get("t") == "http"}

    def run_b():
        a_in.wait(3)                       # B opens second -> innermost region
        with orientim.record(root="tests/_runs/audit", always=True) as h:
            b_in.set()
            built.wait(5)                  # hold B open across A's client builds
            time.sleep(0.02)
        _m, steps = store.load(h.path)
        out["B"] = {json.loads(s["req"]).get("tag")
                    for s in steps if s.get("t") == "http"}

    ta, tb = threading.Thread(target=run_a), threading.Thread(target=run_b)
    ta.start(); tb.start(); ta.join(); tb.join()
    crosstalk = "A" in (out.get("B") or set())
    return (not crosstalk), (
        "A's traffic landed in B's recording" if crosstalk
        else "no cross-contamination (A=%s, B=%s)" % (out.get("A"), out.get("B")))


# 33 --- `orientim ci`: the whole automation surface -------------------------
def t_ci_command():
    """Replay a store, judge the build, and never write a prompt to the report."""
    import shutil
    from orientim import ci
    root = "tests/_runs/ci_audit"
    shutil.rmtree(root, ignore_errors=True)
    storage.reset_cache()

    mode = {"extra": False}

    def agent(h):
        c = h.client()
        c.post(B + "/echo", content=b'{"step":"plan"}')
        if mode["extra"]:
            c.post(B + "/echo", content=b'{"step":"extra"}')
        c.post(B + "/echo", content=b'{"step":"answer"}')

    for i in range(3):
        with orientim.record(root=root, tags={"case": "c%d" % i}, always=True) as h:
            agent(h)

    green = ci.replay_all(root, agent)
    base = ci.report(green, True, "t:agent")

    mode["extra"] = True                      # the code change
    red = ci.replay_all(root, agent)
    cmp_ = ci.compare(red, base)
    rep = ci.report(red, True, "t:agent", base)

    blob = json.dumps(rep)
    leaked = [w for w in ("plan", "answer", "extra", "echo", "127.0.0.1", "http")
              if w in blob]
    shutil.rmtree(root, ignore_errors=True)

    ok = (len(green) == 3 and all(r["ok"] for r in green)
          and len(red) == 3 and not any(r["ok"] for r in red)
          and len(cmp_["newly_changed"]) == 3 and not cmp_["still_changed"]
          and not leaked
          and ci.annotate(red) and not ci.annotate(green))
    return ok, ("3 green then 3 newly changed; report leaks %s"
                % (leaked or "nothing readable"))


if __name__ == "__main__":
    srv = labserver.start()
    time.sleep(0.4)
    print("\n  PRE-RELEASE AUDIT\n")
    check("ring buffer vs hash chain", t_ring)
    check("step index under concurrency", t_index_race)
    check("viewer with non-local locator", t_viewer_s3)
    check("secrets in the recording", t_secrets)
    check("nested record()", t_nested)
    check("corrupt recording", t_corrupt)
    check("missing entry point", t_bad_entry)
    check("python 3.9 floor", t_py_floor)
    check("captures a client we did not hand out", t_foreign_client)
    check("async agents", t_async)
    print()
    check("environment is not snapshotted", t_env_not_captured)
    check("a named env var still replays", t_env_opt_in)
    check("nothing captured is not identical", t_nothing_captured)
    check("uncaptured library is noticed", t_unseen_library)
    check("replay that raises is not identical", t_replay_raises)
    check("recorded exception replays cleanly", t_recorded_exception_replays)
    check("fixed vs still broken", t_fixed_and_still_broken)
    check("fixed via a quality check", t_fixed_via_check)
    check("a new call is not an uncaptured source", t_new_call)
    check("header-only difference is a divergence", t_header_blind_match)
    check("overlapping record() restores httpx", t_patch_leak)
    check("exception building a client leaves httpx clean", t_init_exception)
    check("response cannot inject into the viewer", t_viewer_injection)
    check("form and response credentials", t_oauth_secrets)
    check("secret in a response header is redacted", t_resp_header_secret)
    check("credential in a URL inside a JSON value", t_body_url_userinfo)
    check("secret-shaped env var is refused", t_env_secret_blocked)
    check("binary response round-trips", t_binary_roundtrip)
    check("streaming is not buffered", t_streaming_passthrough)
    check("realtime replay reproduces gaps", t_streaming_realtime)
    check("live server rejects untokened calls", t_server_csrf)
    print()
    check("always=True saves an untriggered run", t_always)
    check("on_capture fires and cannot break the agent", t_on_capture)
    check("random state stored only when used", t_random_state_conditional)
    check("counterfactual takes the other branch", t_counterfactual)
    check("counterfactual is never a reproduction", t_counterfactual_never_ok)
    check("prune keeps one per distinct failure", t_prune_by_signature)
    check("diff between two recordings", t_diff)
    check("recording is findable from its trace", t_trace_link)
    check("holder closes the clients it hands out", t_holder_closes_clients)
    check("opt-in cap, and only the opt-in cap, deletes", t_auto_cap)
    check("async server keeps recordings separate", t_async_server_isolation)
    check("concurrent record() blocks never cross-contaminate", t_concurrent_no_crosstalk)
    check("orientim ci judges a build", t_ci_command)
    srv.shutdown()
    srv.server_close()
    bugs = [f for f in FINDINGS if not f[1]]
    print("\n  %d finding(s) to fix\n" % len(bugs))
    sys.exit(1 if bugs else 0)
