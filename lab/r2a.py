# -*- coding: utf-8 -*-
"""R2-A: what replay-dependency evidence can Orientim actually get?

    python lab/r2a.py

Before designing an eligibility monitor, find out what it would have to
mediate with. This measures, against real recordings and real replays, which
replay dependencies Orientim can observe today, which it could observe with
the smallest recorder change, and which are not on the wire at all.

Nothing in Orientim changes here. No matcher, no serving, no recording format,
no new stored field.

**Strength, recorded for every relation**, because a relation is only as good
as what establishes it:

    OBSERVED   derived from bytes that actually went out or came back
    VERIFIED   those bytes, checked against a trust anchor available live
    RESOLVED   an authority answered about the credential that went out
    BOUND      an assertion tied to the outgoing bytes by a witness
    ASSERTED   stated alongside the run, not tied to the bytes
    ABSENT     no evidence is available at all

The lab that came before this one showed why the distinction matters: an
ASSERTED principal is wrong whenever the harness is wrong about itself, and a
run-level assertion is wrong by construction when two principals run in
parallel. See ELIGIBILITY.md.

**The rules**, fixed in advance: no plaintext Authorization, no plaintext
Cookie, no plaintext tokens, no replayed `/me` as identity proof, no live
calls at replay time, no production change.
"""
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import orientim                                    # noqa: E402
from orientim import session, store, transport     # noqa: E402

PORT = 8798
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(HERE, "_runs", "r2a")
OUT = os.path.join(HERE, "_runs", "_r2a.json")

OBSERVED = "OBSERVED"
VERIFIED = "VERIFIED"
RESOLVED = "RESOLVED"
BOUND = "BOUND"
ASSERTED = "ASSERTED"
ABSENT = "ABSENT"

# The world the server answers from. Changing this between a recording and a
# replay is the falsification case: nothing about the caller moves, and the
# recorded answer stops being true.
WORLD = {"balance": 100, "version": 1}


class _Bank(BaseHTTPRequestHandler):
    """An endpoint whose answer depends on who asks *and* on the world."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        body = json.dumps({
            "balance": WORLD["balance"],
            "account": sent.get("account", "4471"),
            "tenant": self.headers.get("X-Tenant"),
            "session": self.headers.get("X-Session-Id"),
            "actor": self.headers.get("X-Actor"),
            "on_behalf_of": self.headers.get("X-On-Behalf-Of"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # A validator: the server telling us which version of its world this
        # answer came from. Orientim stores response headers, so this one is
        # already in every recording of an endpoint that sends it.
        self.send_header("ETag", '"w%d"' % WORLD["version"])
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Bank)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


def _caller(headers=None, body=None, tags=None):
    """One request, with whatever context the experiment puts on it."""
    def fn(h):
        r = h.client().post(BASE + "/accounts/4471",
                            headers=headers or {},
                            content=json.dumps(body or {"account": "4471"})
                            .encode())
        h.output = r.text
    return fn


def _record_then_replay(a, b, tags=None):
    """Record with context `a`, replay with context `b`. Returns the verdict."""
    _fresh()
    with orientim.record(root=ROOT, always=True, tags=tags or {}) as h:
        a(h)
    d = session.replay(h.path, b, strict=True)
    return h.path, d


# --- part 1: every relation, put on the wire and measured ---------------------
#
# `carrier` is where a real application would put this dependency. `moved`
# changes it the way a different caller or a different world would.

def _hdr(name, value, other):
    return (_caller(headers={name: value}), _caller(headers={name: other}))


def _cred(value, other):
    return (_caller(headers={"Authorization": "Bearer " + value}),
            _caller(headers={"Authorization": "Bearer " + other}))


RELATIONS = [
    ("exact credential recurrence", "Authorization header",
     _cred("tok_alice_1111", "tok_alice_2222")),
    ("verified/bound subject", "a JWT in Authorization",
     _cred("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.sig",
           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib2IifQ.sig")),
    ("tenant, in a header", "X-Tenant",
     _hdr("X-Tenant", "acme", "globex")),
    ("tenant, in the body", "the request body",
     (_caller(body={"account": "4471", "tenant": "acme"}),
      _caller(body={"account": "4471", "tenant": "globex"}))),
    ("actor", "X-Actor",
     _hdr("X-Actor", "svc-support", "svc-billing")),
    ("represented subject", "X-On-Behalf-Of",
     _hdr("X-On-Behalf-Of", "alice", "bob")),
    ("session, in a cookie", "Cookie",
     _hdr("Cookie", "sid=alice-1", "sid=bob-9")),
    ("session, in a header", "X-Session-Id",
     _hdr("X-Session-Id", "s-alice-1", "s-bob-9")),
    ("application context", "record(tags=...)",
     (_caller(), _caller())),
]

# Two relations from the R2-A list that no request carries, measured by
# looking for the channel rather than by moving a value through one.
OFF_THE_WIRE = [
    ("endpoint policy", "not on the wire: configuration"),
    ("resource/world version", "an ETag on the response"),
]


def _strength(verdict, stored, denied):
    """What the measurement licenses, not what we hoped for."""
    if verdict != "IDENTICAL":
        return OBSERVED       # a change in it moves a verdict: it is on record
    if denied:
        return ABSENT         # deliberately not kept, so nothing can compare
    return ASSERTED if stored else ABSENT


def measure_relations():
    rows = []
    srv = _serve()
    try:
        for name, carrier, (a, b) in RELATIONS:
            path, d = _record_then_replay(a, b)
            raw = open(path, encoding="utf-8").read()
            denied = any(k in carrier.lower()
                         for k in transport.HEADER_DENY)
            _meta, steps = store.load(path)
            step = [s for s in steps if s.get("t") == "http"][0]
            rows.append({
                "relation": name, "carrier": carrier,
                "verdict": d.diagnosis[0],
                "detected": d.diagnosis[0] != "IDENTICAL",
                "in_the_file": ("acme" in raw or "alice" in raw
                                or "svc-support" in raw),
                "denied_by_policy": denied,
                "hdr_fp": step.get("hdr_fp"),
                "strength": _strength(d.diagnosis[0], "acme" in raw, denied),
            })
    finally:
        srv.shutdown()
        srv.server_close()
    return rows


# --- part 2: the falsification case -------------------------------------------

def off_the_wire():
    """The relations no request carries, and what that leaves.

    A run-level `tags` declaration has no replay-side counterpart at all:
    `session.replay` takes a function and a recording, and there is nowhere to
    say "this replay is a different tenant". So even an assertion cannot be
    compared — there is only one side of it.
    """
    import inspect
    replay_args = list(inspect.signature(session.replay).parameters)
    return [
        {"relation": "endpoint policy", "carrier": "configuration",
         "verdict": "n/a", "detected": False, "in_the_file": False,
         "denied_by_policy": False, "hdr_fp": None,
         "strength": ABSENT,
         "note": "no case or recording field declares what a response "
                 "depends on"},
        {"relation": "resource/world version", "carrier": "an ETag, in the "
                                                          "response",
         "verdict": "n/a", "detected": False, "in_the_file": True,
         "denied_by_policy": False, "hdr_fp": None,
         "strength": OBSERVED,
         "note": "stored at record time and unusable at replay time: see "
                 "the falsification case"},
        {"relation": "a replay-side context channel", "carrier": "replay(%s)"
                                                                 % ", ".join(replay_args),
         "verdict": "n/a", "detected": False, "in_the_file": False,
         "denied_by_policy": False, "hdr_fp": None,
         "strength": ABSENT,
         "note": "nothing on this signature says who the replay is"},
    ]


def changed_world():
    """Same identity, same credential, same request. A different world.

    The report's claim, put to the test: if eligibility were primarily an
    identity problem, a perfect identity answer would make this replay safe.
    """
    srv = _serve()
    try:
        WORLD.update(balance=100, version=1)
        same = _caller(headers={"Authorization": "Bearer tok_alice_1111",
                                "X-Tenant": "acme"})
        _fresh()
        with orientim.record(root=ROOT, always=True) as h:
            same(h)

        WORLD.update(balance=25, version=2)     # the world moves
        d = session.replay(h.path, same, strict=True)

        _meta, steps = store.load(h.path)
        step = [s for s in steps if s.get("t") == "http"][0]
        etag = (step.get("headers") or {}).get("etag") \
            or (step.get("headers") or {}).get("ETag")

        # What the server would say now, asked directly. Not part of replay —
        # replay makes no network calls, which is the whole point — measured
        # here only to show what the recorded answer is being compared against.
        import httpx
        live = httpx.post(BASE + "/accounts/4471",
                          headers={"Authorization": "Bearer tok_alice_1111",
                                   "X-Tenant": "acme"},
                          content=json.dumps({"account": "4471"}).encode())
        # What the replay actually handed the agent: the body out of the
        # file, not anything the server said just now.
        body = (d.replay_steps or [{}])[0].get("body") or "{}"
        served = json.loads(body)
        return {
            "verdict": d.diagnosis[0],
            "replay_served": served.get("balance"),
            "the_world_now": live.json()["balance"],
            "recorded_validator": etag,
            "live_validator": live.headers.get("etag"),
            "identity_moved": False,
            "detected_by_anything": d.diagnosis[0] != "IDENTICAL",
        }
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    print("R2-A: what replay-dependency evidence can Orientim actually get?")
    print()
    rows = measure_relations()
    rows += off_the_wire()
    print("  %-28s %-22s %-16s %-9s %s"
          % ("relation", "carried as", "verdict", "detected", "strength"))
    print("  " + "-" * 96)
    for r in rows:
        print("  %-28s %-22s %-16s %-9s %s"
              % (r["relation"][:28], r["carrier"][:22], r["verdict"],
                 "yes" if r["detected"] else "NO", r["strength"]))
    print()

    cw = changed_world()
    print("  the falsification case: same identity, same credential, same")
    print("  method, url and body, and a world that moved underneath")
    for k in ("verdict", "replay_served", "the_world_now",
              "recorded_validator", "live_validator", "detected_by_anything"):
        print("    %-22s %r" % (k, cw[k]))
    print()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"relations": rows, "changed_world": cw}, f, indent=2,
                  default=str)
    raw = open(OUT, encoding="utf-8").read()
    leaked = [t for t in ("tok_alice_1111", "tok_alice_2222", "sid=alice-1")
              if t in raw]
    print("  written: %s" % OUT)
    print("  credentials in it: %r" % (leaked,))
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
