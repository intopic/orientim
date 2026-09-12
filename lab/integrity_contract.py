# -*- coding: utf-8 -*-
"""The artifact-digest contract, run against its counterexamples first.

    python lab/integrity_contract.py

An experiment. Nothing in Orientim changes: no format, no chain, no field, no
replay semantics, and no existing hash is given a new meaning. The digest, the
verifier and the decision are **lab code**, written here so the table can be
attacked before any of it reaches production.

Three things review broke in the first version of this file, and each one is
now a counterexample rather than a claim:

    the anchor is an obligation
        an unknown descriptor version used to stop the anchor check and leave
        legacy releasing the file. Editing the version was a bypass that
        deleting the descriptor was not. So the anchor carries its own scheme
        and version, the check no longer reads the file's descriptor to decide
        how to compute, and when a case declares an anchor that cannot be
        verified, **both** profiles refuse.

    release is a positive condition
        `decide` used to refuse a list of states and fall through to release,
        so a snapshot nobody had anchored — `NOT_CHECKED` — was released under
        protected. Protected now releases only on `INTACT|ABSENT` + `MATCH`,
        and an unrecognised profile is an error rather than legacy.

    ambiguity is refused, not resolved
        `json.loads` silently keeps the last of a repeated key, and a `_meta`
        or a descriptor that is present and not an object slipped through as
        absent. Repeated keys are refused, and a field that is there and
        invalid is not the same answer as a field that is not there.

**What the digest covers, said precisely.** Not every byte of the file: blank
lines are dropped, and the meta line is canonicalised through `chain.digest`
rather than hashed as written. It is a digest over a *defined representation*
of the artifact — the step lines byte for byte, and the meta as a mapping.
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

KEY = "integrity"
SCHEME = "orientim-artifact-digest"
V = 1
ALGO = "sha256"
COVERS = "meta-mapping+step-lines"


# --- parsing that refuses to choose ------------------------------------------

class _Duplicate(ValueError):
    pass


def _no_duplicate_keys(pairs):
    seen = set()
    for k, _v in pairs:
        if k in seen:
            raise _Duplicate(k)
        seen.add(k)
    return dict(pairs)


def _parse(text):
    """`json.loads`, minus the part where a repeated key picks a winner."""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


# --- the digest over the defined representation -------------------------------

def artifact_digest(meta, step_lines):
    """Step lines as written; meta as a canonical mapping.

    The meta cannot be covered as written, because writing the digest into it
    would change the bytes the digest is over — so it goes through
    `chain.digest`, which already canonicalises a mapping and keeps one
    serialisation rule in the codebase. The descriptor is included **minus its
    digest value**, so the descriptor's own claims are covered.

    Nothing is excluded for being volatile: `t0`, `ms`, `started_at` and
    `runtime` are part of the artifact, and this digest is never compared
    between two recordings — only against a value taken from the same bytes.
    """
    m = copy.deepcopy(meta or {})
    if KEY in m:
        desc = dict(m.get(KEY) or {})
        desc.pop("digest", None)
        m[KEY] = desc
    head = chain.digest(m).encode("utf-8")
    return hashlib.sha256(head + b"\n" + b"\n".join(step_lines)).hexdigest()


def descriptor(meta, step_lines):
    claims = {"scheme": SCHEME, "v": V, "algo": ALGO, "covers": COVERS,
              "steps": len(step_lines)}
    body = dict(meta or {})
    body[KEY] = claims
    return dict(claims, digest=artifact_digest(body, step_lines))


def anchor_for(meta, step_lines):
    """What a case file would hold. It names its own scheme, not the file's."""
    return {"scheme": SCHEME, "v": V, "algo": ALGO,
            "digest": artifact_digest(meta, step_lines)}


# --- the verifier: two answers, never one ------------------------------------

INTACT, CHANGED, ABSENT = "INTACT", "CHANGED", "ABSENT"
UNSUPPORTED, AMBIGUOUS, CORRUPT = "UNSUPPORTED", "AMBIGUOUS", "CORRUPT"
MATCH, MISMATCH, NO_ANCHOR = "MATCH", "MISMATCH", "NO_ANCHOR"
NOT_CHECKED, ANCHOR_UNSUPPORTED = "NOT_CHECKED", "ANCHOR_UNSUPPORTED"


class Snapshot(object):
    """One read of one artifact: the bytes, and what was parsed *from them*.

    The point of the class is that there is nothing else. A caller cannot
    verify a path and then open it again, because the only thing handed back
    is the content that was verified.
    """

    __slots__ = ("raw", "meta", "steps", "step_lines", "self_state",
                 "anchor_state", "anchor_declared", "reason")

    def __init__(self, raw):
        self.raw = raw
        self.meta, self.steps, self.step_lines = None, None, None
        self.self_state, self.reason = CORRUPT, None
        # Deliberately not `NOT_CHECKED`: a snapshot nobody checked must not be
        # able to read as one that was checked and found nothing wrong.
        self.anchor_state, self.anchor_declared = NOT_CHECKED, None


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
            obj = _parse(ln.decode("utf-8"))
        except _Duplicate as e:
            snap.self_state = AMBIGUOUS
            snap.reason = "line %d repeats the key %s" % (i, e)
            return snap
        except Exception as e:
            snap.self_state = CORRUPT
            snap.reason = "line %d: %s" % (i, type(e).__name__)
            return snap
        if not isinstance(obj, dict):
            snap.self_state = CORRUPT
            snap.reason = "line %d is not an object" % i
            return snap
        if "_meta" in obj:
            metas.append((i, obj["_meta"]))
        else:
            steps.append(obj)
            step_lines.append(ln)

    # Ambiguity is refused rather than resolved. A file with two meta lines has
    # two answers to every question asked of it, and picking one is picking.
    if len(metas) != 1:
        snap.self_state = AMBIGUOUS
        snap.reason = "%d meta lines" % len(metas)
        return snap
    if metas[0][0] != 0:
        snap.self_state, snap.reason = AMBIGUOUS, "meta is not the first line"
        return snap
    if not isinstance(metas[0][1], dict):
        snap.self_state = AMBIGUOUS
        snap.reason = "_meta is present and is not an object"
        return snap

    snap.meta, snap.steps, snap.step_lines = metas[0][1], steps, step_lines

    # Present-and-invalid is not absent. A descriptor of `null` is a claim
    # nobody can read, and reading it as "there was no claim" is the mistake
    # that lets an edit look like an old recording.
    if KEY not in snap.meta:
        snap.self_state, snap.reason = ABSENT, "no descriptor"
        return snap
    desc = snap.meta.get(KEY)
    if not isinstance(desc, dict):
        snap.self_state = AMBIGUOUS
        snap.reason = "the descriptor is present and is not an object"
        return snap
    if (desc.get("scheme") != SCHEME or desc.get("v") != V
            or desc.get("algo") != ALGO):
        snap.self_state = UNSUPPORTED
        snap.reason = "descriptor scheme=%r v=%r algo=%r" % (
            desc.get("scheme"), desc.get("v"), desc.get("algo"))
        return snap
    if desc.get("steps") != len(step_lines):
        # Cheap, and it is what catches a cut at a line boundary: every line
        # left behind is valid JSON, and there are fewer of them.
        snap.self_state = CHANGED
        snap.reason = "declared %r step lines, found %d" % (
            desc.get("steps"), len(step_lines))
        return snap
    if artifact_digest(snap.meta, step_lines) != desc.get("digest"):
        snap.self_state, snap.reason = CHANGED, "digest does not match"
        return snap
    snap.self_state, snap.reason = INTACT, None
    return snap


def check_anchor(snap, anchor):
    """The second answer, and the one that survives a knowledgeable editor.

    Two things it deliberately does not do. It does not read the recording's
    own descriptor to decide how to compute — the anchor names its scheme and
    version, so a file whose descriptor was deleted, or renumbered to
    something this build has never heard of, is still checked. And it does not
    treat an anchor it cannot interpret as no anchor: an unknown scheme is its
    own refusal, with a reason.
    """
    snap.anchor_declared = bool(anchor)
    if not anchor:
        snap.anchor_state = NO_ANCHOR
        return snap
    if not isinstance(anchor, dict) or (
            anchor.get("scheme") != SCHEME or anchor.get("v") != V
            or anchor.get("algo") != ALGO or not anchor.get("digest")):
        snap.anchor_state = ANCHOR_UNSUPPORTED
        snap.reason = snap.reason or "the anchor names a scheme this build cannot compute"
        return snap
    if snap.self_state in (CORRUPT, AMBIGUOUS):
        snap.anchor_state = NOT_CHECKED       # there is nothing to hash
        return snap
    snap.anchor_state = (
        MATCH if artifact_digest(snap.meta, snap.step_lines) == anchor["digest"]
        else MISMATCH)
    return snap


# --- the decision -------------------------------------------------------------

RELEASE, REFUSE, SUITE_ERROR = "release", "refuse", "suite_error"
PROFILES = ("protected", "legacy")


def decide(snap, profile="protected"):
    """What a profile does with the pair, as a positive condition.

    Written the other way round the first time — a list of states to refuse,
    then `release` — which released a snapshot nobody had checked. Release is
    now something a caller has to earn.
    """
    if profile not in PROFILES:
        return SUITE_ERROR, "unknown profile %r" % (profile,)
    if snap.self_state == CORRUPT:
        return SUITE_ERROR, "the artifact cannot be read"
    if snap.self_state == AMBIGUOUS:
        return SUITE_ERROR, "the artifact can be read two ways"
    if snap.anchor_declared is None:
        # Nobody asked. That is distinguishable from "asked, none declared",
        # and it is a fault in the caller rather than a state of the file —
        # so it is an error and not a release, in either profile.
        return SUITE_ERROR, "the anchor was never checked"

    if snap.anchor_declared:
        # An anchor is an obligation. Unable to verify is not permission,
        # whichever profile is asking.
        if snap.anchor_state != MATCH:
            return REFUSE, ("this case declares an anchor and the artifact "
                            "cannot be verified against it")
        if snap.self_state not in (INTACT, ABSENT):
            return REFUSE, ("the anchor matches and the artifact's own claim "
                            "does not: two readings of one file")
        return RELEASE, None

    # No anchor declared, so nothing was promised about this artifact.
    if snap.self_state == CHANGED:
        return REFUSE, "the artifact disagrees with its own descriptor"
    if profile == "protected":
        return REFUSE, ("no anchor: self-consistency is not tamper evidence"
                        if snap.self_state == INTACT else
                        "no anchor and nothing to check")
    return RELEASE, "legacy: unverified, reported, and no anchor required"


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


def _split(raw):
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    meta = json.loads(lines[0].decode("utf-8"))["_meta"]
    return meta, lines[1:]


def _join(meta, step_lines):
    head = json.dumps({"_meta": meta}, default=str).encode("utf-8")
    return b"\n".join([head] + list(step_lines)) + b"\n"


def _sealed(path):
    """The recording as a recorder that wrote a descriptor would have left it."""
    meta, steps = _split(open(path, "rb").read())
    meta[KEY] = descriptor(meta, steps)
    return _join(meta, steps)


def _edit(raw, fn):
    """Edit the parsed form, then re-serialise it."""
    meta, lines = _split(raw)
    steps = [json.loads(ln.decode("utf-8")) for ln in lines]
    meta, steps = fn(meta, steps)
    return _join(meta, [json.dumps(s, default=str).encode("utf-8")
                        for s in steps])


# --- the mutations ------------------------------------------------------------

def _swap_tool(steps):
    for s in steps:
        if s.get("t") == "http":
            s["body"] = s["body"].replace("send_email", "wire_transfer")
            s["body_sha"] = hashlib.sha256(s["body"].encode()).hexdigest()[:32]
            return s
    return None


def _body_and_reseal(meta, steps):
    """The editor who knows how the digest works and recomputes it."""
    _swap_tool(steps)
    lines = [json.dumps(s, default=str).encode("utf-8") for s in steps]
    meta[KEY] = descriptor({k: v for k, v in meta.items() if k != KEY}, lines)
    return meta, steps


def _drop_descriptor(meta, steps):
    meta.pop(KEY, None)
    return meta, steps


def _body_and_unknown_version(meta, steps):
    """The bypass review found: change the content *and* the version."""
    _swap_tool(steps)
    meta[KEY] = dict(meta[KEY], v=2)
    return meta, steps


def _null_descriptor(meta, steps):
    meta[KEY] = None
    return meta, steps


def _edit_the_descriptors_claims(meta, steps):
    meta[KEY] = dict(meta[KEY], covers="steps")
    return meta, steps


def _duplicate_meta(raw):
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    return b"\n".join([lines[0], lines[0]] + lines[1:]) + b"\n"


def _cut_at_a_line_boundary(raw):
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    return b"\n".join(lines[:-1]) + b"\n"      # every line left is valid JSON


def _null_meta(raw):
    _meta, lines = _split(raw)
    return b"\n".join([b'{"_meta": null}'] + list(lines)) + b"\n"


def _repeated_key(raw):
    """One `_meta`, two `context` keys. `json.loads` keeps the last."""
    meta, lines = _split(raw)
    head = json.dumps({"_meta": meta}, default=str)
    injected = head.replace(
        '"context":', '"context": {"tenant": "A"}, "context":', 1)
    if injected == head:                       # no context key in this file
        injected = head.replace('{"_meta": {', '{"_meta": {"tags": {}, ', 1)
        injected = injected.replace('"tags": {}, ', '"tags": {}, "tags": {}, ', 1)
    return b"\n".join([injected.encode("utf-8")] + list(lines)) + b"\n"


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
        clean = read(sealed)
        anchor = anchor_for(clean.meta, clean.step_lines)

        cases = [
            ("0 untouched (control)", sealed, anchor),
            ("1 body edited, digest recomputed",
             _edit(sealed, _body_and_reseal), anchor),
            ("2 integrity block deleted", _edit(sealed, _drop_descriptor),
             anchor),
            ("3 body edited + unknown version",
             _edit(sealed, _body_and_unknown_version), anchor),
            ("4 cut at a line boundary", _cut_at_a_line_boundary(sealed),
             anchor),
            ("5 two meta lines", _duplicate_meta(sealed), anchor),
            ("6 descriptor's own claims edited",
             _edit(sealed, _edit_the_descriptors_claims), anchor),
            ("7 a byte flipped mid-line",
             sealed.replace(b'"status": 200', b'"status": 2q0', 1), anchor),
            ("8 a repeated JSON key in meta", _repeated_key(sealed), anchor),
            ("9 _meta present and null", _null_meta(sealed), anchor),
            ("10 descriptor present and null", _edit(sealed, _null_descriptor),
             anchor),
            ("11 the anchor's own scheme is unknown", sealed,
             dict(anchor, v=2)),
            ("12 untouched, and no anchor declared", sealed, None),
        ]

        rows = []
        for label, raw, anch in cases:
            snap = check_anchor(read(raw), anch)
            prot, why_p = decide(snap, "protected")
            leg, why_l = decide(snap, "legacy")
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
                         "protected": prot, "why": why_p, "legacy": leg,
                         "legacy_why": why_l, "today": today})

        # The decision function on its own, without the anchor check having run.
        bare = read(sealed)
        unchecked = {
            "self_state": bare.self_state,
            "anchor_state": bare.anchor_state,
            "anchor_declared": bare.anchor_declared,
            "protected": decide(bare, "protected")[0],
            "legacy": decide(bare, "legacy")[0],
            "unknown profile": decide(
                check_anchor(read(sealed), anchor), "prod")[0],
        }

        # Verifying a path is not verifying what you then read.
        good = os.path.join(ROOT, "toctou.jsonl")
        open(good, "wb").write(sealed)
        verified = check_anchor(read(open(good, "rb").read()), anchor)
        open(good, "wb").write(_edit(sealed, _body_and_reseal))
        reread = check_anchor(read(open(good, "rb").read()), anchor)
        snapshot = {
            "verified_the_first_read": [verified.self_state,
                                        verified.anchor_state],
            "the_path_now_holds": [reread.self_state, reread.anchor_state],
            "wire_transfer_in_the_verified_snapshot": (
                b"wire_transfer" in verified.raw),
            "wire_transfer_at_the_path_now": (
                b"wire_transfer" in open(good, "rb").read()),
        }
        return {"anchor": anchor["digest"][:16], "rows": rows,
                "decision_without_a_check": unchecked,
                "same_snapshot": snapshot}
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    m = measure()
    print("the artifact-digest contract, against its counterexamples")
    print()
    print("  anchor in the case file: %s v%d %s, digest %s..."
          % (SCHEME, V, ALGO, m["anchor"]))
    print()
    print("  %-38s %-12s %-18s %-11s %-11s %s"
          % ("case", "self", "anchor", "protected", "legacy", "today"))
    print("  " + "-" * 124)
    for r in m["rows"]:
        print("  %-38s %-12s %-18s %-11s %-11s %s"
              % (r["case"], r["self"], r["anchor"], r["protected"],
                 r["legacy"], r["today"]))
    print()
    for r in m["rows"]:
        if r["reason"]:
            print("     %-38s %s" % (r["case"], r["reason"]))
    print()
    print("  the decision function on its own, no anchor check run")
    for k, v in m["decision_without_a_check"].items():
        print("     %-38s %s" % (k, v))
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
