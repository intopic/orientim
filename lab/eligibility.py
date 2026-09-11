# -*- coding: utf-8 -*-
"""Response eligibility: can a recorded response be served to *this* caller?

    python lab/eligibility.py

An experiment, not a feature. Nothing in Orientim changes: no matcher, no
serving, no recording format. The question is whether an abstraction from the
research report survives contact with this codebase, and the way to find out is
to try to break it.

**The claim under test.** Finding a recorded response is a lookup; deciding
that it may be served is a separate judgement about who is asking. Orientim
today has only the first: the key is method + URL + body, and every header that
carries principal identity is excluded from the header fingerprint as well, so
a replay driven as a different user is served the first user's response and the
verdict is IDENTICAL. That is P0-3, pinned in docs/limits.md.

**The vocabulary**, fixed in advance so a result cannot be talked into being
better than it is:

    ELIGIBLE      this response may be served to this caller
    INELIGIBLE    it may not
    UNKNOWN       the evidence does not decide it
    ANALYSIS_ERROR the evidence is malformed; not a verdict about the caller

**The rules**, also fixed in advance. No plaintext Authorization, no plaintext
Cookie, no plaintext tokens or secrets in anything this writes. No replayed
`/me` counted as proof of identity — a recorded response is not evidence about
the caller replaying it. No live calls at replay time.

Two decision models are graded side by side against ground truth that is known
by construction, because the interesting question is not whether a model
answers but whether it is ever *wrong*:

    R  the research model      eligibility is decided by the principal
    O  the asymmetric model    only INELIGIBLE is provable; ELIGIBLE never is

and four evidence sources, from what exists today to what would have to be
built.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import orientim                                    # noqa: E402
from orientim import session, store                # noqa: E402

PORT = 8797
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(HERE, "_runs", "eligibility")
OUT = os.path.join(HERE, "_runs", "_eligibility.json")

# The key that makes a credential witness a witness rather than a stored
# secret. It lives outside every artefact this writes, which is the whole
# point: with the recording alone, the digests are not attackable by guessing
# candidate tokens.
_KEY = os.environ.get("ORIENTIM_LAB_WITNESS_KEY", "lab-key-not-in-any-file")

ELIGIBLE = "ELIGIBLE"
INELIGIBLE = "INELIGIBLE"
UNKNOWN = "UNKNOWN"
ANALYSIS_ERROR = "ANALYSIS_ERROR"


def witness(credential):
    """A keyed digest of the credential that was actually sent.

    Not a stored credential and not reversible without the key, which is not
    in the recording. It answers exactly one question — is this the same
    credential as last time — and deliberately no others.
    """
    if credential is None:
        return None
    return hmac.new(_KEY.encode(), credential.encode(),
                    hashlib.sha256).hexdigest()[:32]


# --- part 1: what a recording holds today -------------------------------------

TOKENS = {
    # principal, tenant, scopes, and the opaque string actually sent
    "alice_a1": ("alice", "acme", ("orders:read",), "tok_alice_1111"),
    "alice_a2": ("alice", "acme", ("orders:read",), "tok_alice_2222"),
    "bob":      ("bob", "acme", ("orders:read",), "tok_bob_3333"),
    "alice_rw": ("alice", "acme", ("orders:read", "orders:write"),
                 "tok_alice_rw44"),
    "alice_other_tenant": ("alice", "globex", ("orders:read",),
                           "tok_alice_gx55"),
    "opaque":   (None, None, None, "tok_opaque_6666"),
    "opaque2":  (None, None, None, "tok_opaque_7777"),
}


class _Orders(BaseHTTPRequestHandler):
    """A server that authorizes. The response depends on who asked."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        sent = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        who = next((k for k, v in TOKENS.items() if v[3] == sent), None)
        if who is None:
            body = json.dumps({"error": "unauthorized"}).encode()
            code = 401
        else:
            principal, tenant, _scopes, _tok = TOKENS[who]
            body = json.dumps({"order": "4471", "owner": principal,
                               "tenant": tenant}).encode()
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Orders)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def measure_today():
    """What is in the file, and what a replay by another principal costs.

    Real recordings, real replay, no simulation: this is the evidence source
    called `none` in the matrix below, measured rather than assumed.
    """
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    srv = _serve()
    seen = {}
    try:
        def as_(who):
            def fn(h):
                r = h.client().get(
                    BASE + "/orders/4471",
                    headers={"Authorization": "Bearer " + TOKENS[who][3]})
                seen[who] = r.text
                h.output = r.text
            return fn

        with orientim.record(root=ROOT, always=True) as h:
            as_("alice_a1")(h)
        recorded = h.path
        d = session.replay(recorded, as_("bob"), strict=True)

        raw = open(recorded, encoding="utf-8").read()
        leaked = sorted({t for _p, _t, _s, t in TOKENS.values() if t in raw})
        _meta, steps = store.load(recorded)
        http = [s for s in steps if s.get("t") == "http"]
        return {
            "verdict": d.diagnosis[0],
            "bob_was_served_alices_response": seen["bob"] == seen["alice_a1"],
            "credentials_in_the_file": leaked,
            "identity_fields_in_a_step": sorted(
                k for k in (http[0] if http else {})
                if re.search(r"auth|princip|ident|cred|token|user", k, re.I)),
            "hdr_fp": (http[0].get("hdr_fp") if http else None),
        }
    finally:
        srv.shutdown()
        srv.server_close()


# --- part 2: the evidence sources ---------------------------------------------
#
# What each of them can put beside a request, and what it costs to have it.
# `bound` is the one that matters: whether the principal claim is tied to the
# credential that actually went out, or is only an assertion made alongside it.

def ev_none(_side):
    return {}


def ev_declared(side):
    """What the harness says it is acting as. An assertion, not evidence: it
    is written by the same code that could be wrong about it."""
    return {"principal": side.get("declared_principal"),
            "tenant": side.get("declared_tenant"),
            "scope": side.get("declared_scope"), "bound": False}


def ev_witness(side):
    """A keyed digest of the credential actually sent. Evidence about the
    credential, and about nothing else."""
    return {"witness": witness(side.get("credential")), "bound": True}


def ev_bound(side):
    """Both, tied together: the principal as resolved *at record time* from
    the credential that was sent, plus the witness that binds the claim to it.

    Resolution happens while the run is live, because a replay makes no
    network calls and a recorded `/me` response is not proof of who is
    replaying it.
    """
    e = ev_witness(side)
    e.update({"principal": side.get("true_principal"),
              "tenant": side.get("true_tenant"),
              "scope": side.get("true_scope")})
    return e


SOURCES = [
    ("none", ev_none, "nothing to write; nothing to compare"),
    ("declared", ev_declared, "one argument per run"),
    ("witness", ev_witness, "a capture-time hook over request headers, and a "
                            "key kept outside the recording"),
    ("bound", ev_bound, "that hook, plus resolving the credential once while "
                        "the run is live"),
]


# --- the two decision models --------------------------------------------------

def model_R(rec, live):
    """The research model, implemented as stated: a lookup is not an
    eligibility decision, and eligibility is decided by the principal."""
    for e in (rec, live):
        if e.get("scope") is not None and not isinstance(e["scope"], tuple):
            return ANALYSIS_ERROR, "scope is not a set of scopes"
    if rec.get("principal") and live.get("principal"):
        if rec["principal"] != live["principal"]:
            return INELIGIBLE, "a different principal"
        if rec.get("tenant") != live.get("tenant"):
            return INELIGIBLE, "a different tenant"
        if rec.get("scope") != live.get("scope"):
            return INELIGIBLE, "a different scope"
        return ELIGIBLE, "the same principal, tenant and scope"
    if rec.get("witness") and live.get("witness"):
        if rec["witness"] != live["witness"]:
            return UNKNOWN, "a different credential, and nothing resolves it"
        return ELIGIBLE, "the same credential"
    return UNKNOWN, "no identity evidence on one side or the other"


def model_O(rec, live):
    """The asymmetric model, which is how this codebase already treats every
    other claim: a violation observed is admissible, and an absence needs the
    enumeration to have closed.

    Applied here, the asymmetry is severe and the reason is not a limitation
    of the recorder. Eligibility is a property of the authorization state on
    the server at the moment the response was produced, and that state is not
    on the wire. A recording can observe that the credential changed. It can
    never observe that the authority behind an unchanged credential did not.
    So INELIGIBLE is provable and ELIGIBLE is not.
    """
    for e in (rec, live):
        if e.get("scope") is not None and not isinstance(e["scope"], tuple):
            return ANALYSIS_ERROR, "scope is not a set of scopes"
    if rec.get("witness") and live.get("witness") \
            and rec["witness"] != live["witness"]:
        if rec.get("principal") and live.get("principal"):
            if rec["principal"] == live["principal"] \
                    and rec.get("tenant") == live.get("tenant") \
                    and rec.get("scope") == live.get("scope"):
                return UNKNOWN, "a rotated credential for the same principal"
            return INELIGIBLE, "a different principal, tenant or scope"
        return UNKNOWN, "a different credential, and nothing resolves it"
    if rec.get("bound") and live.get("bound") \
            and rec.get("principal") and live.get("principal") \
            and (rec["principal"] != live["principal"]
                 or rec.get("tenant") != live.get("tenant")
                 or rec.get("scope") != live.get("scope")):
        return INELIGIBLE, "a different principal, tenant or scope"
    return UNKNOWN, "not decided by anything on the wire"


MODELS = [("R", model_R), ("O", model_O)]


# --- the adversarial matrix ---------------------------------------------------
#
# `truth` is what is actually the case, known by construction. A verdict that
# contradicts it is unsound, and the direction matters: a false ELIGIBLE
# serves one caller's data to another, a false INELIGIBLE blocks a replay that
# was fine.

def _side(token_key, declared=None, true_identity=True):
    principal, tenant, scope, credential = TOKENS[token_key]
    return {
        "credential": credential,
        "declared_principal": (declared or principal),
        "declared_tenant": tenant,
        "declared_scope": scope,
        "true_principal": principal if true_identity else None,
        "true_tenant": tenant if true_identity else None,
        "true_scope": scope if true_identity else None,
    }


def _legacy(token_key):
    """A recording made before any of this existed: the request went out with
    a credential and the file kept nothing about it."""
    return {"credential": None, "declared_principal": None,
            "declared_tenant": None, "declared_scope": None,
            "true_principal": None, "true_tenant": None, "true_scope": None}


SCENARIOS = [
    ("1 same principal, rotated token",
     _side("alice_a1"), _side("alice_a2"), ELIGIBLE,
     "alice's token was rotated; the response is still hers"),
    ("2 a different principal, same request",
     _side("alice_a1"), _side("bob"), INELIGIBLE,
     "method, url and body are identical; the caller is not"),
    ("3 same principal, wider scope",
     _side("alice_a1"), _side("alice_rw"), INELIGIBLE,
     "the recorded response was produced under a narrower scope"),
    ("4 same principal, another tenant",
     _side("alice_a1"), _side("alice_other_tenant"), INELIGIBLE,
     "the same human, a different customer's data"),
    ("5 same token, authority revoked server-side",
     _side("alice_a1"), _side("alice_a1"), INELIGIBLE,
     "the bytes on the wire did not change and the authority behind them did"),
    ("6 an opaque token nothing resolves",
     _side("opaque", true_identity=False),
     _side("opaque2", true_identity=False), UNKNOWN,
     "two credentials, and nothing on either side says whose they are"),
    ("7 the declaration disagrees with the credential",
     _side("alice_a1"), _side("bob", declared="alice"), INELIGIBLE,
     "the harness says alice; bob's credential went out"),
    ("8 two principals in one run, in parallel",
     _side("alice_a1"), _side("bob", declared="alice"), INELIGIBLE,
     "a run-level declaration cannot be right for both steps"),
    ("9 a legacy recording with no identity evidence",
     _legacy("alice_a1"), _side("bob"), INELIGIBLE,
     "the file predates all of this"),
]


def grade(verdict, truth):
    if verdict == ANALYSIS_ERROR:
        return "error"
    if verdict == UNKNOWN:
        return "silent" if truth != UNKNOWN else "right"
    if truth == UNKNOWN:
        return "unsound"          # claiming a decision nothing supports
    return "right" if verdict == truth else "unsound"


def run_matrix():
    rows = []
    for name, rec_side, live_side, truth, why in SCENARIOS:
        for source, build, _cost in SOURCES:
            rec, live = build(rec_side), build(live_side)
            for model, fn in MODELS:
                verdict, reason = fn(rec, live)
                rows.append({"scenario": name, "source": source,
                             "model": model, "truth": truth,
                             "verdict": verdict, "reason": reason,
                             "grade": grade(verdict, truth), "why": why})
    return rows


def _table(rows):
    L = []
    order = [s[0] for s in SOURCES]
    head = "  %-46s %s" % ("scenario", "  ".join(
        "%-9s" % ("%s/%s" % (s[:4], m)) for s in order for m, _f in MODELS))
    L.append(head)
    L.append("  " + "-" * (len(head) - 2))
    for name, _r, _l, truth, _why in SCENARIOS:
        cells = []
        for source in order:
            for model, _fn in MODELS:
                r = next(x for x in rows if x["scenario"] == name
                         and x["source"] == source and x["model"] == model)
                mark = {"unsound": "!", "right": "+", "silent": " ",
                        "error": "e"}[r["grade"]]
                cells.append("%-8s%s" % (r["verdict"][:8], mark))
        L.append("  %-46s %s" % (name[:46], "  ".join(cells)))
        L.append("  %-46s truth: %s" % ("", truth))
    return "\n".join(L)


def main():
    print(__doc__.strip().splitlines()[0])
    print()
    today = measure_today()
    print("what a recording holds today")
    for k, v in today.items():
        print("    %-32s %r" % (k, v))
    print()

    rows = run_matrix()
    print(_table(rows))
    print()
    print("  + sound and useful    ! unsound    (blank) sound but silent")
    print()

    print("by model and evidence source")
    print("    %-10s %-6s %8s %8s %8s   %s"
          % ("source", "model", "right", "silent", "unsound", "what it costs"))
    for source, _b, cost in SOURCES:
        for model, _fn in MODELS:
            mine = [r for r in rows
                    if r["source"] == source and r["model"] == model]
            counts = {g: sum(1 for r in mine if r["grade"] == g)
                      for g in ("right", "silent", "unsound", "error")}
            print("    %-10s %-6s %8d %8d %8d   %s"
                  % (source, model, counts["right"], counts["silent"],
                     counts["unsound"], cost if model == "R" else ""))
    print()

    unsound = [r for r in rows if r["grade"] == "unsound"]
    print("every unsound claim, in full")
    for r in unsound:
        print("    %-46s %-9s %-2s says %-10s (truth %s)"
              % (r["scenario"][:46], r["source"], r["model"], r["verdict"],
                 r["truth"]))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"today": today, "rows": rows}, f, indent=2, default=str)

    # The rule this experiment was given, checked rather than asserted: no
    # credential, in plaintext or as a digest, in anything it wrote.
    written = open(OUT, encoding="utf-8").read()
    creds = sorted({t for _p, _t, _s, t in TOKENS.values() if t in written})
    digests = [w for w in (witness(t) for _p, _t, _s, t in TOKENS.values())
               if w and w in written]
    print()
    print("  written: %s" % OUT)
    print("  credentials in it: %r   witness digests in it: %d"
          % (creds, len(digests)))
    return 0 if not creds and not digests else 1


if __name__ == "__main__":
    sys.exit(main())
