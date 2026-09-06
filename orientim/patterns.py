# -*- coding: utf-8 -*-
"""The twenty documented sources of non-determinism.

Each entry is a name, a probe that deliberately triggers the source, a mutation
that changes the world between record and replay, and a declared expectation.

This is the test suite and the specification at the same time. When a customer
hits a source we have never seen, it becomes entry twenty-one.
"""
import concurrent.futures as cf
import json
import os
import random
import time
import uuid

STATE = {"indent": None, "scratch": None, "cache": {}, "version": None,
         "arrivals": []}


# --- clock -----------------------------------------------------------------

def p01(h, B):
    h.client().post(B + "/stable", content=json.dumps({"now": time.time()}).encode())


def p02(h, B):
    t0 = time.time()
    h.client().post(B + "/stable",
                    content=json.dumps({"expired": time.time() - t0 > 0.001}).encode())


def p03(h, B):
    # gmtime(time.time()) formatted down to the second. The old version asked
    # for "%Z" of a gmtime, which is the string "GMT" on every machine on every
    # day — it could not fail, so it measured nothing.
    z = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time()))
    h.client().post(B + "/stable", content=json.dumps({"stamp": z}).encode())


# --- randomness ------------------------------------------------------------

def p04(h, B):
    h.client().post(B + "/stable", content=json.dumps({"sid": str(uuid.uuid4())}).encode())


def p05(h, B):
    h.client().post(B + "/sampled", content=b'{"q":"x"}')


def p06(h, B):
    h.client().post(B + "/stable", content=json.dumps({"pick": random.random()}).encode())


# --- concurrency -----------------------------------------------------------

def p07(h, B):
    c = h.client()
    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda q: c.post(B + "/search",
                                     content=json.dumps({"q": q}).encode()),
                    ["a", "b", "c", "d"]))


def p08(h, B):
    c = h.client()
    try:
        c.get(B + "/slow", timeout=0.15)
    except Exception:
        c.post(B + "/stable", content=b'{"fallback":1}')


def p09(h, B):
    c = h.client()
    with cf.ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda i: c.post(B + "/search",
                                     content=json.dumps({"q": i}).encode()),
                    [1, 2, 3]))


# --- network ---------------------------------------------------------------

def p10(h, B):
    # Arrivals, not just the final bytes. The old probe streamed a
    # single-chunk response, so it passed even while the recorder was
    # buffering every stream into one lump — a real behaviour change that
    # nothing in the suite could see.
    with h.client().stream("GET", B + "/stream") as r:
        chunks = [c for c in r.iter_raw() if c]
    STATE["arrivals"].append(len(chunks))
    h.client().post(B + "/stable",
                    content=json.dumps({"chunks": len(chunks)}).encode())


def p11(h, B):
    c = h.client()
    for _ in range(3):
        if c.post(B + "/flaky", content=b'{"k":"try"}').status_code == 200:
            break
        time.sleep(random.uniform(0.001, 0.01))


def p12(h, B):
    h.client().post(B + "/drift", content=b"{}")


def p13(h, B):
    h.client().get(B + "/ratelimit")


# --- tools -----------------------------------------------------------------

def p14(h, B):
    body = json.dumps({"q": "same", "n": 1}, indent=STATE["indent"])
    h.client().post(B + "/stable", content=body.encode())


def p15(h, B):
    with open(STATE["scratch"], encoding="utf-8") as f:
        v = f.read().strip()
    h.client().post(B + "/stable", content=json.dumps({"from_disk": v}).encode())


def p16(h, B):
    h.client().post(B + "/send-email", content=b'{"to":"someone"}')


# --- environment -----------------------------------------------------------

def p17(h, B):
    v = os.environ.get("ORIENTIM_PROBE", "?")
    h.client().post(B + "/stable", content=json.dumps({"env": v}).encode())


def p18(h, B):
    exists = os.path.exists(STATE["scratch"])
    h.client().post(B + "/stable", content=json.dumps({"exists": exists}).encode())


# --- framework -------------------------------------------------------------

def p19(h, B):
    if "k" in STATE["cache"]:
        return
    STATE["cache"]["k"] = h.client().post(B + "/stable", content=b'{"c":1}').text


def p20(h, B):
    # A DECLARED LIMIT, not a capture. If the library version in the request
    # body changes, the request changes, and no record/replay tool can make
    # that identical — it can only report the divergence. This used to be
    # counted as captured because nothing in the test ever changed the version.
    import httpx
    h.client().post(B + "/stable",
                    content=json.dumps({"httpx": STATE["version"]
                                        or httpx.__version__}).encode())


# --- mutations applied between record and replay ---------------------------

def m14():
    STATE["indent"] = 2


def m15():
    open(STATE["scratch"], "w", encoding="utf-8").write("changed-value")


def m17():
    os.environ["ORIENTIM_PROBE"] = "changed"


def m18():
    try:
        os.remove(STATE["scratch"])
    except OSError:
        pass


def m20():
    STATE["version"] = "99.0.0-upgraded"


def reset(scratch_path):
    STATE["indent"] = None
    STATE["version"] = None
    STATE["arrivals"] = []
    STATE["cache"].clear()
    STATE["scratch"] = scratch_path
    os.environ["ORIENTIM_PROBE"] = "initial"
    with open(scratch_path, "w", encoding="utf-8") as f:
        f.write("initial-value")


CAPTURED = "captured"
LIMIT = "known limit"
# Captured only when the lookup key normalises whitespace, key order and float
# rounding. Under strict bytes it is a limit, and the report says which.
LOOSE = "loose key only"

PATTERNS = [
    ("clock",       "01", "time read into a request",     p01, None, CAPTURED),
    ("clock",       "02", "expiry / TTL branching",       p02, None, CAPTURED),
    ("clock",       "03", "clock formatted into a call",  p03, None, CAPTURED),
    ("randomness",  "04", "generated identifiers",        p04, None, CAPTURED),
    ("randomness",  "05", "model sampling",               p05, None, CAPTURED),
    ("randomness",  "06", "caller randomness",            p06, None, CAPTURED),
    ("concurrency", "07", "parallel calls out of order",  p07, None, CAPTURED),
    ("concurrency", "08", "timeout race",                 p08, None, CAPTURED),
    ("concurrency", "09", "task scheduling order",        p09, None, CAPTURED),
    ("network",     "10", "streamed response",            p10, None, CAPTURED),
    ("network",     "11", "retry with jitter",            p11, None, CAPTURED),
    ("network",     "12", "provider behaviour drift",     p12, None, CAPTURED),
    ("network",     "13", "rate limiting",                p13, None, CAPTURED),
    ("tools",       "14", "request formatting drift",     p14, m14, LOOSE),
    ("tools",       "15", "local read, never on network", p15, m15, LIMIT),
    ("tools",       "16", "side effect",                  p16, None, CAPTURED),
    ("environment", "17", "environment variables",        p17, m17, CAPTURED),
    ("environment", "18", "filesystem state",             p18, m18, LIMIT),
    ("framework",   "19", "cache inside framework",       p19, None, LIMIT),
    ("framework",   "20", "library version drift",        p20, m20,  LIMIT),
]
