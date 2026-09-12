# -*- coding: utf-8 -*-
"""The artifact-digest contract, run against its counterexamples first.

    python lab/integrity_contract.py

An experiment. Nothing in Orientim changes: no format, no chain, no field, no
replay semantics, and no existing hash is given a new meaning. The digest and
the verifier below are **lab code**, written here so the decision table can be
attacked before any of it reaches production.

The measurements in INTEGRITY.md establish what is uncovered today. This one
settles the four questions that decide the shape of the fix:

    1  self-consistency is not a match against an anchor, and one of them is
       worth much less than the other
    2  the anchor has to be checked even when the file carries no digest at
       all — otherwise deleting the descriptor is the whole attack
    3  serialisation, what the descriptor covers, and refusing a file that can
       be read two ways
    4  verifying and consuming the same bytes, rather than the same path
"""
import copy
import hashlib
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import orientim                                          # noqa: E402
from orientim import chain, session, store                # noqa: E402

PORT = 8810
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(HERE, "_runs", "contract")
OUT = os.path.join(HERE, "_runs", "_integrity_contract.json")

# --- the descriptor, and the digest over the artifact -------------------------
#
# Lab code. The shape is the proposal, not an implementation: a descriptor in
# meta under one key, carrying its own version, its algorithm, what it claims
# to cover, and how many step lines there should be. Everything in it is
# inside the digest **except the digest value itself**, so editing the
# descriptor's own claims is a mismatch rather than a way out.

KEY = "integrity"
V = 1
ALGO = "sha256"
COVERS = "meta+steps"


def artifact_digest(meta, step_lines):
    """A digest of what is stored, over bytes for the steps and a canonical
    mapping for the meta.

    Two halves for one reason. Step lines are covered **as written**, because
    that is what a reader and a replay will consume. The meta cannot be, since
    writing the digest into it would change the bytes the digest is over — so
    it goes through `chain.digest`, which already canonicalises a mapping and
    keeps one serialisation rule in the codebase.

    Nothing is excluded for being volatile. `t0`, `ms`, `started_at` and
    `runtime` are part of the artifact, and this digest is never compared
    between two recordings — only against a value taken from the same bytes.
    """
    m = copy.deepcopy(meta or {})
    desc = dict(m.get(KEY) or {})
    desc.pop("digest", None)
    if desc or KEY in m:
        m[KEY] = desc
    head = chain.digest(m).encode("utf-8")
    return hashlib.sha256(head + b"\n" + b"\n".join(step_lines)).hexdigest()


def descriptor(meta, step_lines):
    return {"v": V, "algo": ALGO, "covers": COVERS,
            "steps": len(step_lines),
            "digest": artifact_digest(dict(meta or {}, **{KEY: {
                "v": V, "algo": ALGO, "covers": COVERS,
                "steps": len(step_lines)}}), step_lines)}


# --- the verifier: two answers, never one ------------------------------------

INTACT, CHANGED, ABSENT = "INTACT", "CHANGED", "ABSENT"
UNSUPPORTED, AMBIGUOUS, CORRUPT = "UNSUPPORTED", "AMBIGUOUS", "CORRUPT"
MATCH, MISMATCH, NO_ANCHOR = "MATCH", "MISMATCH", "NO_ANCHOR"
NOT_CHECKED = "NOT_CHECKED"


class Snapshot(object):
    """One read of one artifact: the bytes, and what was parsed *from them*.

    The point of the class is that there is nothing else. A caller cannot
    verify a path and then open it again, because the only thing handed back
    is the content that was verified.
    """

    __slots__ = ("raw", "meta", "steps", "step_lines", "self_state",
                 "anchor_state", "reason")

    def __init__(self, raw):
        self.raw = raw
        self.meta, self.steps, self.step_lines = None, None, None
        self.self_state, self.anchor_state = CORRUPT, NOT_CHECKED
        self.reason = None


def read(raw):
    """Parse and check one byte string. Never a path."""
    snap = Snapshot(raw)
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    if not lines:
        snap.reason = "empty"
        return snap

    metas, steps, step_lines = [], [], []
    for i, ln in enumerate(lines):
        try:
            obj = json.loads(ln.decode("utf-8"))
        except Exception as e:
            snap.self_state, snap.reason = CORRUPT, "line %d: %s" % (
                i, type(e).__name__)
            return snap
        if not isinstance(obj, dict):
            snap.self_state, snap.reason = CORRUPT, "line %d is not an object" % i
            return snap
        if "_meta" in obj:
            metas.append((i, obj["_meta"]))
        else:
            steps.append(obj)
            step_lines.append(ln)

    # Ambiguity is refused rather than resolved. A file with two meta lines has
    # two answers to every question asked of it, and picking one is picking.
    if len(metas) != 1:
        snap.self_state, snap.reason = AMBIGUOUS, "%d meta lines" % len(metas)
        return snap
    if metas[0][0] != 0:
        snap.self_state, snap.reason = AMBIGUOUS, "meta is not the first line"
        return snap

    snap.meta, snap.steps, snap.step_lines = metas[0][1], steps, step_lines
    desc = snap.meta.get(KEY)
    if desc is None:
        snap.self_state, snap.reason = ABSENT, "no descriptor"
        return snap
    if not isinstance(desc, dict):
        snap.self_state, snap.reason = AMBIGUOUS, "descriptor is not an object"
        return snap
    if desc.get("v") != V or desc.get("algo") != ALGO:
        snap.self_state = UNSUPPORTED
        snap.reason = "descriptor v=%r algo=%r" % (desc.get("v"),
                                                   desc.get("algo"))
        return snap
    if desc.get("steps") != len(step_lines):
        # Cheap, and it is what catches a cut at a line boundary: every line
        # left behind is valid JSON, and there are fewer of them.
        snap.self_state = CHANGED
        snap.reason = "declared %r step lines, found %d" % (desc.get("steps"),
                                                            len(step_lines))
        return snap
    recomputed = artifact_digest(snap.meta, step_lines)
    if recomputed != desc.get("digest"):
        snap.self_state, snap.reason = CHANGED, "digest does not match"
        return snap
    snap.self_state, snap.reason = INTACT, None
    return snap


def check_anchor(snap, anchor):
    """The second answer, and the one that survives a knowledgeable editor.

    Computed from the bytes rather than read out of the descriptor, so a file
    whose descriptor was deleted is still checked — deleting it is otherwise
    the whole attack.
    """
    if snap.self_state in (CORRUPT, AMBIGUOUS, UNSUPPORTED):
        # UNSUPPORTED belongs here too: the digest definition belongs to the
        # descriptor version, so a v2 file cannot be given a v1 verdict. Not
        # checked is the honest answer, and protected refuses on it anyway.
        snap.anchor_state = NOT_CHECKED
        return snap
    if not anchor:
        snap.anchor_state = NO_ANCHOR
        return snap
    snap.anchor_state = (MATCH if artifact_digest(snap.meta, snap.step_lines)
                         == anchor else MISMATCH)
    return snap


# --- the decision ------------------------------------------------------------

RELEASE, REFUSE, SUITE_ERROR = "release", "refuse", "suite_error"


def decide(snap, profile="protected"):
    """What a profile does with the pair. Never one answer standing in for two."""
    if snap.self_state == CORRUPT:
        return SUITE_ERROR, "the artifact cannot be read"
    if snap.self_state == AMBIGUOUS:
        return SUITE_ERROR, "the artifact can be read two ways"
    if snap.self_state == CHANGED or snap.anchor_state == MISMATCH:
        return REFUSE, "the artifact is not the one this case was frozen against"
    if profile != "protected":
        return RELEASE, "legacy: reported, not enforced"
    if snap.self_state == UNSUPPORTED:
        return REFUSE, "the descriptor is a version this build cannot check"
    if snap.anchor_state == NO_ANCHOR:
        return REFUSE, "no anchor: self-consistency is not tamper evidence"
    return RELEASE, None


# --- a recording to attack ----------------------------------------------------

class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"model": "m", "choices": [
            {"index": 0, "finish_reason": "tool_calls",
             "message": {"role": "assistant", "tool_calls": [
                 {"id": "c0", "type": "function",
                  "function": {"name": "send_email",
                               "arguments": "{}"}}]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _agent(h):
    c = h.client()
    for _ in (1, 2, 3):
        c.post(BASE + "/v1/chat/completions",
               content=json.dumps({"model": "m",
                                   "messages": [{"role": "user",
                                                 "content": "go"}]}).encode())
    h.output = "done"


def _sealed(path):
    """The recording as a recorder that wrote a descriptor would have left it."""
    raw = open(path, "rb").read()
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    meta = json.loads(lines[0].decode("utf-8"))["_meta"]
    steps = lines[1:]
    meta[KEY] = descriptor(meta, steps)
    head = json.dumps({"_meta": meta}, default=str).encode("utf-8")
    return b"\n".join([head] + steps) + b"\n"


# --- the counterexamples ------------------------------------------------------

def _edit(raw, fn):
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    meta = json.loads(lines[0].decode("utf-8"))["_meta"]
    steps = [json.loads(ln.decode("utf-8")) for ln in lines[1:]]
    meta, steps = fn(meta, steps)
    head = json.dumps({"_meta": meta}, default=str).encode("utf-8")
    body = [json.dumps(s, default=str).encode("utf-8") for s in steps]
    return b"\n".join([head] + body) + b"\n"


def _body_and_reseal(meta, steps):
    """The editor who knows how the digest works and recomputes it."""
    for s in steps:
        if s.get("t") == "http":
            s["body"] = s["body"].replace("send_email", "wire_transfer")
            s["body_sha"] = hashlib.sha256(s["body"].encode()).hexdigest()[:32]
            break
    lines = [json.dumps(s, default=str).encode("utf-8") for s in steps]
    meta[KEY] = descriptor({k: v for k, v in meta.items() if k != KEY}, lines)
    return meta, steps


def _drop_descriptor(meta, steps):
    meta.pop(KEY, None)
    return meta, steps


def _duplicate_meta(raw):
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    return b"\n".join([lines[0], lines[0]] + lines[1:]) + b"\n"


def _cut_at_a_line_boundary(raw):
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    return b"\n".join(lines[:-1]) + b"\n"      # every line left is valid JSON


def _unsupported_version(meta, steps):
    meta[KEY] = dict(meta[KEY], v=2)
    return meta, steps


def _edit_the_descriptors_claims(meta, steps):
    """Change what the descriptor says it covers, and nothing else."""
    meta[KEY] = dict(meta[KEY], covers="steps")
    return meta, steps


def measure():
    srv = _serve()
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)
        with orientim.record(root=ROOT, always=True) as h:
            _agent(h)
        path = h.path
        sealed = _sealed(path)
        anchor = read(sealed)
        anchor_digest = artifact_digest(anchor.meta, anchor.step_lines)

        cases = [
            ("0 untouched (control)", sealed),
            ("1 body edited, digest recomputed", _edit(sealed, _body_and_reseal)),
            ("2 integrity block deleted", _edit(sealed, _drop_descriptor)),
            ("3 cut at a line boundary", _cut_at_a_line_boundary(sealed)),
            ("4 two meta lines", _duplicate_meta(sealed)),
            ("5 unsupported descriptor version",
             _edit(sealed, _unsupported_version)),
            ("6 the descriptor's own claims edited",
             _edit(sealed, _edit_the_descriptors_claims)),
            ("7 a byte flipped mid-line",
             sealed.replace(b'"status": 200', b'"status": 2q0', 1)),
        ]

        rows = []
        for label, raw in cases:
            snap = check_anchor(read(raw), anchor_digest)
            prot = decide(snap, "protected")
            leg = decide(snap, "legacy")
            # And what Orientim does with the same file today.
            p = os.path.join(ROOT, "x.jsonl")
            open(p, "wb").write(raw)
            try:
                _m, st = store.load(p)
                today = "loads, %d steps" % len(
                    [s for s in st if s.get("t") == "http"])
            except Exception as e:
                today = "%s at load" % type(e).__name__
            try:
                d = session.replay(p, _agent, strict=True)
                today += " / replay %s" % (d.diagnosis[0] if d.diagnosis
                                           else "?")
            except Exception as e:
                today += " / replay raised %s" % type(e).__name__
            rows.append({"case": label, "self": snap.self_state,
                         "anchor": snap.anchor_state, "reason": snap.reason,
                         "protected": prot[0], "why": prot[1],
                         "legacy": leg[0], "today": today})

        # 4 — verifying a path is not verifying what you then read.
        good = os.path.join(ROOT, "toctou.jsonl")
        open(good, "wb").write(sealed)
        verified = check_anchor(read(open(good, "rb").read()), anchor_digest)
        open(good, "wb").write(_edit(sealed, _body_and_reseal))   # swapped
        reread = check_anchor(read(open(good, "rb").read()), anchor_digest)
        snapshot = {
            "verified_the_first_read": [verified.self_state,
                                        verified.anchor_state],
            "the_path_now_holds": [reread.self_state, reread.anchor_state],
            "wire_transfer_in_the_verified_snapshot": (
                b"wire_transfer" in verified.raw),
            "wire_transfer_at_the_path_now": (
                b"wire_transfer" in open(good, "rb").read()),
        }
        return {"anchor": anchor_digest[:16], "rows": rows,
                "same_snapshot": snapshot}
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    m = measure()
    print("the artifact-digest contract, against its counterexamples")
    print()
    print("  anchor in the case file: %s..." % m["anchor"])
    print()
    print("  %-38s %-12s %-11s %-11s %-8s %s"
          % ("case", "self", "anchor", "protected", "legacy", "today"))
    print("  " + "-" * 118)
    for r in m["rows"]:
        print("  %-38s %-12s %-11s %-11s %-8s %s"
              % (r["case"], r["self"], r["anchor"], r["protected"],
                 r["legacy"], r["today"]))
    print()
    for r in m["rows"]:
        if r["reason"] or r["why"]:
            print("     %-38s %s" % (r["case"], r["reason"] or r["why"]))
    print()
    print("  verifying bytes, not a path")
    for k, v in m["same_snapshot"].items():
        print("     %-38s %s" % (k, v))
    print()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
