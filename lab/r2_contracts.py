# -*- coding: utf-8 -*-
"""Two falsification tests before any R2 implementation.

    python lab/r2_contracts.py

Nothing in Orientim changes here. No matcher, no serving, no recording format.

The previous lab reported two things too confidently, and both are corrected
by the tests below.

**It read `HEADERS_CHANGED` as mediation.** A verdict that names a mismatch
and a decision that refuses to serve one are not the same event, and the
difference is whether the agent already acted on somebody else's data. TEST 1
finds out which one happens, per dimension.

**It read "the world moved" as proof that a historical replay was unsound.**
That was wrong. A controlled historical replay *wants* the recorded world:
new code, same frozen conditions, compare behaviour. The recorded balance
being stale today says nothing about whether serving it was correct. It says
something about a different question, and TEST 2 separates the two:

    A. Historical Replay Contract
       may this fixture be served as part of reproducing the recorded world?

    B. Current-World Applicability
       can this recording still validly test a claim against today's world?

**And it froze a conclusion too early.** "ELIGIBLE can never be proven" holds
only if ELIGIBLE means *the live system would answer the same now*. Under a
bounded contract it means something local and provable: every mandatory
obligation this replay profile declares was discharged. The prototype at the
end tries that narrower semantics.
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
from orientim import evaluate as ev, session, store   # noqa: E402

PORT = 8799
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(HERE, "_runs", "r2c")
OUT = os.path.join(HERE, "_runs", "_r2_contracts.json")

WORLD = {"balance": 100, "version": 1}


class _Accounts(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        body = json.dumps({
            "balance": WORLD["balance"],
            "account": sent.get("account", "4471"),
            "tenant": self.headers.get("X-Tenant"),
            "actor": self.headers.get("X-Actor"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"w%d"' % WORLD["version"])
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Accounts)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)


# --- TEST 1: is a context mismatch mediated, or reported afterwards? ----------

def _reader(tenant, account="4471", saw=None):
    """An agent that reads, then *acts on what it read*.

    The action is in-process on purpose. Replay forwards nothing anywhere, so
    a second HTTP call would prove less than this does: the agent's control
    flow took a branch chosen by the body it was handed.
    """
    def fn(h):
        r = h.client().post(BASE + "/accounts/" + account,
                            headers={"X-Tenant": tenant},
                            content=json.dumps({"account": account}).encode())
        body = r.json()
        if saw is not None:
            saw["status"] = r.status_code
            saw["tenant_in_body"] = body.get("tenant")
            saw["balance"] = body.get("balance")
            # The branch. If this runs, the response was not merely delivered
            # — it was used.
            saw["acted_on"] = ("released %s to %s"
                               % (body.get("balance"), tenant)
                               if body.get("balance") is not None else None)
        h.output = json.dumps(body)
    return fn


def test_1_pre_serve_or_post_hoc():
    """Alice records; Bob replays the same operation shape."""
    srv = _serve()
    out = {}
    try:
        # Recorded: tenant acme.
        _fresh()
        with orientim.record(root=ROOT, always=True) as h:
            _reader("acme")(h)
        path = h.path

        # Replayed: tenant globex. Same method, same URL, same body — the
        # whole difference is the caller-context header.
        saw = {}
        d = session.replay(path, _reader("globex", saw=saw), strict=True)
        out["header_context"] = {
            "candidate_found": saw.get("status") == 200,
            "fixture_served": saw.get("status") == 200,
            "agent_consumed": saw.get("tenant_in_body"),
            "agent_acted": saw.get("acted_on"),
            "verdict": d.diagnosis[0],
            "detected_at": ("after the replay function returned"
                            if d.diagnosis[0] != "IDENTICAL" else "not at all"),
            "terminated_before_serving": False,
        }

        # The control: move the same distinction into the request body, which
        # *is* part of the lookup key.
        saw2 = {}
        d2 = session.replay(path, _reader("acme", account="9999", saw=saw2),
                            strict=True)
        out["key_context"] = {
            "candidate_found": False,
            "fixture_served": saw2.get("status") == 200,
            "agent_consumed": saw2.get("tenant_in_body"),
            "agent_acted": saw2.get("acted_on"),
            "verdict": d2.diagnosis[0],
            "detected_at": "before anything was served",
            "terminated_before_serving": saw2.get("status") != 200,
        }

        # The third tier: a credential, which is in neither the key nor the
        # fingerprint.
        def as_(token, saw):
            def fn(h):
                r = h.client().post(
                    BASE + "/accounts/4471",
                    headers={"X-Tenant": "acme",
                             "Authorization": "Bearer " + token},
                    content=json.dumps({"account": "4471"}).encode())
                saw["status"] = r.status_code
                saw["tenant_in_body"] = r.json().get("tenant")
                h.output = r.text
            return fn

        _fresh()
        with orientim.record(root=ROOT, always=True) as h2:
            as_("tok_alice", {})(h2)
        saw3 = {}
        d3 = session.replay(h2.path, as_("tok_bob", saw3), strict=True)
        out["credential_context"] = {
            "candidate_found": True,
            "fixture_served": saw3.get("status") == 200,
            "agent_consumed": saw3.get("tenant_in_body"),
            "agent_acted": None,
            "verdict": d3.diagnosis[0],
            "detected_at": "never",
            "terminated_before_serving": False,
        }
    finally:
        srv.shutdown()
        srv.server_close()
    return out


# --- TEST 2: historical replay vs current-world applicability -----------------

def _teller(threshold=50):
    """An agent whose answer depends on the balance it was given."""
    def fn(h):
        r = h.client().post(BASE + "/accounts/4471",
                            headers={"X-Tenant": "acme"},
                            content=json.dumps({"account": "4471"}).encode())
        bal = r.json()["balance"]
        h.output = "low balance" if bal < threshold else "balance ok"
    return fn


def test_2_two_contracts():
    """One recording, two questions, and they do not have the same answer."""
    srv = _serve()
    out = {}
    try:
        WORLD.update(balance=100, version=1)
        _fresh()
        with orientim.record(root=ROOT, always=True) as h:
            _teller()(h)
        path = h.path
        recorded_output = h.output

        WORLD.update(balance=25, version=2)      # the world moves

        # --- Contract A: historical replay. The world is frozen on purpose.
        same = session.replay(path, _teller(), strict=True)
        # The same agent, with a real code change: the threshold moves.
        changed = session.replay(path, _teller(threshold=200), strict=True)

        ex = ev.Execution.load(path)
        claim = ev.output_matches("balance ok")(ex)
        out["historical_replay"] = {
            "served": recorded_output,
            "unchanged_code": same.diagnosis[0],
            "changed_code": changed.diagnosis[0],
            "claim_about_recorded_conditions": claim.status,
            "regression_still_caught": changed.diagnosis[0] != "IDENTICAL",
            "world_moved_underneath": True,
        }

        # --- Contract B: is this recording still applicable to today?
        import httpx
        live = httpx.post(BASE + "/accounts/4471",
                          headers={"X-Tenant": "acme"},
                          content=json.dumps({"account": "4471"}).encode())
        _meta, steps = store.load(path)
        step = [s for s in steps if s.get("t") == "http"][0]
        headers = {k.lower(): v for k, v in (step.get("headers") or {}).items()}
        recorded_etag = headers.get("etag")
        live_etag = live.headers.get("etag")
        out["current_applicability"] = {
            "recorded_validator": recorded_etag,
            "live_validator": live_etag,
            "world_version_equal": recorded_etag == live_etag,
            "recorded_condition": "balance 100 (>= threshold)",
            "todays_condition": "balance %s (< threshold)" % live.json()["balance"],
            "claim_needing_todays_world": "the agent warns when the balance "
                                          "is low",
            "exercised_by_this_recording": False,
            "how_it_could_be_known": "ask the server, which is outside replay "
                                     "by design",
        }
    finally:
        srv.shutdown()
        srv.server_close()
    return out


# --- the narrower semantics, prototyped ---------------------------------------
#
# ELIGIBLE_UNDER_CONTRACT is a local statement: every mandatory obligation this
# replay profile declares was discharged against the replay context. It is not
# a claim that the live system would answer the same, and it must never be read
# as one.

ELIGIBLE = "ELIGIBLE_UNDER_CONTRACT"
INELIGIBLE = "INELIGIBLE"
UNKNOWN = "UNKNOWN"
ANALYSIS_ERROR = "ANALYSIS_ERROR"

# Only what a contract can actually ask about. Deliberately not `principal`:
# the previous lab showed one name cannot carry subject, actor and tenant at
# once, and a run-level one is wrong by construction under concurrency.
CONTEXT_FIELDS = ("credential_relation", "subject", "actor", "tenant",
                  "session", "workload")

CONTRACTS = {
    # What runs today, named rather than changed: the obligations are the
    # lookup key, and the key is already mediated.
    "legacy": (),
    "tenant": ("tenant",),
    "delegated": ("tenant", "actor", "subject"),
    "session": ("tenant", "session"),
}


def mediate(contract, recorded, current):
    """Would this fixture be served, under this contract?"""
    obligations = CONTRACTS.get(contract)
    if obligations is None:
        return ANALYSIS_ERROR, "no contract named %r" % (contract,)
    unknown, broken = [], []
    for name in obligations:
        if name not in CONTEXT_FIELDS:
            return ANALYSIS_ERROR, "no such obligation: %s" % name
        was, now = recorded.get(name), current.get(name)
        if was is None or now is None:
            # A declared dependency with no value on one side is not a
            # dependency that matched. It is one nobody can speak for.
            unknown.append(name)
        elif was != now:
            broken.append(name)
    if broken:
        return INELIGIBLE, "different " + ", ".join(broken)
    if unknown:
        return UNKNOWN, "no value for " + ", ".join(unknown)
    return ELIGIBLE, ("every obligation discharged"
                      if obligations else
                      "this contract declares none beyond the lookup key")


ALICE = {"tenant": "acme", "subject": "alice", "actor": "svc-support",
         "session": "s-1", "credential_relation": "same_credential_bytes"}
BOB = dict(ALICE, tenant="globex", subject="bob")
NO_CONTEXT = {}


def prototype():
    rows = []
    for contract in ("legacy", "tenant", "delegated", "session"):
        for label, current in (("same context", ALICE),
                               ("another tenant and subject", BOB),
                               ("no replay context at all", NO_CONTEXT)):
            verdict, why = mediate(contract, ALICE, current)
            rows.append({"contract": contract, "replay_context": label,
                         "verdict": verdict, "why": why})
    return rows


def main():
    print("TEST 1: is a context mismatch mediated, or reported afterwards?")
    print()
    t1 = test_1_pre_serve_or_post_hoc()
    print("  %-22s %-9s %-9s %-14s %-18s %s"
          % ("dimension", "served", "consumed", "verdict", "detected", "stopped"))
    print("  " + "-" * 94)
    for key, label in (("key_context", "body (in the key)"),
                       ("header_context", "X-Tenant header"),
                       ("credential_context", "Authorization")):
        r = t1[key]
        print("  %-22s %-9s %-9s %-14s %-18s %s"
              % (label,
                 "yes" if r["fixture_served"] else "no",
                 r["agent_consumed"] or "-",
                 r["verdict"],
                 r["detected_at"][:18],
                 "yes" if r["terminated_before_serving"] else "NO"))
    print()
    print("  what the agent did with the header mismatch: %r"
          % (t1["header_context"]["agent_acted"],))
    print()

    print("TEST 2: historical replay vs current-world applicability")
    print()
    t2 = test_2_two_contracts()
    print("  A. Historical Replay Contract (the world is frozen on purpose)")
    for k in ("served", "unchanged_code", "changed_code",
              "claim_about_recorded_conditions", "regression_still_caught",
              "world_moved_underneath"):
        print("       %-34s %r" % (k, t2["historical_replay"][k]))
    print()
    print("  B. Current-World Applicability")
    for k in ("recorded_validator", "live_validator", "world_version_equal",
              "recorded_condition", "todays_condition",
              "claim_needing_todays_world", "exercised_by_this_recording",
              "how_it_could_be_known"):
        print("       %-34s %r" % (k, t2["current_applicability"][k]))
    print()

    print("PROTOTYPE: ELIGIBLE under a bounded contract")
    print()
    rows = prototype()
    print("  %-12s %-28s %-24s %s"
          % ("contract", "replay context", "verdict", "why"))
    print("  " + "-" * 96)
    for r in rows:
        print("  %-12s %-28s %-24s %s"
              % (r["contract"], r["replay_context"], r["verdict"], r["why"]))
    print()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"test_1": t1, "test_2": t2, "prototype": rows}, f,
                  indent=2, default=str)
    raw = open(OUT, encoding="utf-8").read()
    leaked = [t for t in ("tok_alice", "tok_bob") if t in raw]
    print("  written: %s" % OUT)
    print("  credentials in it: %r" % (leaked,))
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
