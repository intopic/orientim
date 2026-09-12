# -*- coding: utf-8 -*-
"""Is this the recording the case was frozen against?

A separate question from every hash already in a recording, and a separate
value. `body_sha` fingerprints the bytes that crossed the wire and cannot be
recomputed from the file; the chain covers seven fields of a step and nothing
in the metadata. Measured, in `lab/artifact_integrity.py`: editing the stored
body turns `did_not_call("send_email")` from fail into PASS with the chain root
unmoved and the replay reporting IDENTICAL; editing a stored response header
hands the agent somebody else's value under the same IDENTICAL; editing the
recorded context flips the R2 gate. None of those is covered, and none of them
is what `body_sha` is about.

So this module adds one value of its own and gives it its own name. It changes
nothing about the chain, the lookup key, matching or replay.

**Two answers, never one.**

    self-consistency   is this file the file it says it is
    anchor match       is this the file the case was frozen against

The first detects corruption, including the kind that leaves the JSON valid.
Only the second detects substitution, and the difference is not academic: an
editor who recomputes the descriptor produces a file that is perfectly
self-consistent. Self-consistency must never be spent as if it were tamper
evidence.

**An anchor is an obligation.** Where a case declares one and the artifact
cannot be verified against it, every profile refuses — unable to verify is not
permission. The anchor carries its own scheme, version and algorithm, so how
to compute the digest is decided by the anchor and never by the descriptor
inside the file being checked; otherwise renumbering the descriptor is a
bypass that deleting it is not.

**Presence is not truth.** `MISSING` says the field was not there. Every other
value — including `None`, `{}` and `""` — is a field that is there, and a
field that is there and unusable is refused rather than read as absent.

**What the digest covers, precisely.** Not every byte of the file: blank lines
are dropped and the meta line is canonicalised, because writing the digest
into the meta would change the bytes the digest is over. It is a digest over a
defined representation — the step lines byte for byte, the meta as a mapping.

Not in v1: per-step digests, keys, MACs, signatures, authenticity, and any
rewriting of recordings that already exist. Authenticity is out of reach for a
reason that is not cryptographic: whoever can edit a recording in a repository
can edit the case and the baseline too.
"""
import copy
import hashlib
import json

from . import chain

KEY = "integrity"                       # where the descriptor sits in meta
SCHEME = "orientim-artifact-digest"
V = 1
ALGO = "sha256"
COVERS = "meta-mapping+step-lines"

# Self-consistency: what the file says about itself.
INTACT = "INTACT"                       # a descriptor is present and matches
CHANGED = "CHANGED"                     # present and does not match
ABSENT = "ABSENT"                       # no descriptor at all
UNSUPPORTED = "UNSUPPORTED"             # a descriptor this build cannot check
AMBIGUOUS = "AMBIGUOUS"                 # a file with two readings
CORRUPT = "CORRUPT"                     # a file with none

# The anchor: what something outside the file says about it.
MATCH = "MATCH"
MISMATCH = "MISMATCH"
NO_ANCHOR = "NO_ANCHOR"                 # the field was not there
ANCHOR_INVALID = "ANCHOR_INVALID"       # there, and not an anchor
ANCHOR_UNSUPPORTED = "ANCHOR_UNSUPPORTED"   # there, and names a scheme we lack
NOT_CHECKED = "NOT_CHECKED"             # nobody asked, or nothing to hash

RELEASE, REFUSE, SUITE_ERROR = "release", "refuse", "suite_error"
PROTECTED, LEGACY = "protected", "legacy"
PROFILES = (PROTECTED, LEGACY)


class _Missing(object):
    __slots__ = ()

    def __repr__(self):
        return "MISSING"


#: The field was not there. Distinct from every value it could have held.
MISSING = _Missing()


class Refused(Exception):
    """A recording that may not be used, with the pair that decided it."""

    def __init__(self, action, reason, snapshot):
        Exception.__init__(self, reason)
        self.action = action
        self.reason = reason
        self.snapshot = snapshot


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


def _parse_line(text):
    """`json.loads`, minus the part where a repeated key picks a winner."""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


# --- the digest ---------------------------------------------------------------

def digest(meta, step_lines):
    """The digest over the defined representation of one artifact.

    The descriptor is included minus its own digest value, so the descriptor's
    claims — its version, its algorithm, its step count — are covered and
    editing them is a mismatch rather than a way out.

    Nothing is excluded for being volatile. `t0`, `started_at` and `runtime`
    are part of the artifact, and this value is never compared between two
    recordings, only against a value taken from the same bytes.
    """
    m = copy.deepcopy(meta or {})
    if KEY in m:
        desc = dict(m.get(KEY) or {})
        desc.pop("digest", None)
        m[KEY] = desc
    head = chain.digest(m).encode("utf-8")
    return hashlib.sha256(head + b"\n" + b"\n".join(step_lines)).hexdigest()


def descriptor(meta, step_lines):
    """What a recorder writes into the file. The claims, and the digest."""
    claims = {"scheme": SCHEME, "v": V, "algo": ALGO, "covers": COVERS,
              "steps": len(step_lines)}
    return dict(claims, digest=digest(dict(meta or {}, **{KEY: claims}),
                                      step_lines))


def anchor_of(snapshot):
    """The anchor a case should store for this artifact.

    Taken from a snapshot rather than from a path, so what is anchored is what
    was read.
    """
    return {"scheme": SCHEME, "v": V, "algo": ALGO,
            "digest": digest(snapshot.meta, snapshot.step_lines)}


# --- the snapshot -------------------------------------------------------------

class Snapshot(object):
    """One read of one artifact: the bytes, and what was parsed from them.

    The point is that there is nothing else to hand on. A caller cannot verify
    a path and then open it again, because what comes back is the content that
    was verified — measured in `lab/integrity_contract.py`, where a path that
    verified INTACT/MATCH held an internally consistent forgery a moment later.
    """

    __slots__ = ("raw", "meta", "step_lines", "self_state", "anchor_state",
                 "anchor_declared", "reason")

    def __init__(self, raw):
        self.raw = raw
        self.meta, self.step_lines = None, None
        self.self_state, self.reason = CORRUPT, None
        # None, not NOT_CHECKED: a snapshot nobody checked must not be able to
        # read as one that was checked and found nothing wrong.
        self.anchor_state, self.anchor_declared = NOT_CHECKED, None

    def as_dict(self):
        return {"self": self.self_state, "anchor": self.anchor_state,
                "anchor_declared": self.anchor_declared,
                "reason": self.reason}


def read(raw):
    """Check one byte string. Never a path."""
    snap = Snapshot(raw)
    lines = [ln for ln in (raw or b"").split(b"\n") if ln.strip()]
    if not lines:
        snap.reason = "the artifact is empty"
        return snap

    metas, step_lines = [], []
    for i, ln in enumerate(lines):
        try:
            obj = _parse_line(ln.decode("utf-8"))
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
            step_lines.append(ln)

    if len(metas) != 1:
        snap.self_state = AMBIGUOUS
        snap.reason = "%d metadata lines" % len(metas)
        return snap
    if metas[0][0] != 0:
        snap.self_state = AMBIGUOUS
        snap.reason = "the metadata is not the first line"
        return snap
    if not isinstance(metas[0][1], dict):
        snap.self_state = AMBIGUOUS
        snap.reason = "the metadata is present and is not an object"
        return snap

    snap.meta, snap.step_lines = metas[0][1], step_lines

    # Present-and-invalid is not absent. Reading a null descriptor as "there
    # was no descriptor" is how an edited file comes to look like an old one.
    if KEY not in snap.meta:
        snap.self_state = ABSENT
        snap.reason = "no integrity descriptor"
        return snap
    desc = snap.meta.get(KEY)
    if not isinstance(desc, dict):
        snap.self_state = AMBIGUOUS
        snap.reason = "the integrity descriptor is present and is not an object"
        return snap
    if (desc.get("scheme") != SCHEME or desc.get("v") != V
            or desc.get("algo") != ALGO):
        snap.self_state = UNSUPPORTED
        snap.reason = "descriptor scheme=%r v=%r algo=%r" % (
            desc.get("scheme"), desc.get("v"), desc.get("algo"))
        return snap
    if desc.get("steps") != len(step_lines):
        # What catches a cut at a line boundary: every line left behind parses,
        # and there are fewer of them.
        snap.self_state = CHANGED
        snap.reason = "the descriptor declares %r step lines and there are %d" % (
            desc.get("steps"), len(step_lines))
        return snap
    if digest(snap.meta, step_lines) != desc.get("digest"):
        snap.self_state = CHANGED
        snap.reason = "the digest does not match the artifact"
        return snap
    snap.self_state, snap.reason = INTACT, None
    return snap


def check(snapshot, anchor=MISSING):
    """The second answer. `MISSING` means the field was not there.

    Computed from the bytes rather than read out of the descriptor, so a file
    whose descriptor was deleted — or renumbered to a version this build has
    never heard of — is still checked against what the case declared.
    """
    snapshot.anchor_declared = anchor is not MISSING
    if not snapshot.anchor_declared:
        snapshot.anchor_state = NO_ANCHOR
        return snapshot
    if not isinstance(anchor, dict) or not anchor.get("digest"):
        snapshot.anchor_state = ANCHOR_INVALID
        snapshot.reason = snapshot.reason or (
            "the anchor is present and is not an anchor")
        return snapshot
    if (anchor.get("scheme") != SCHEME or anchor.get("v") != V
            or anchor.get("algo") != ALGO):
        snapshot.anchor_state = ANCHOR_UNSUPPORTED
        snapshot.reason = snapshot.reason or (
            "the anchor names scheme=%r v=%r algo=%r, which this build cannot "
            "compute" % (anchor.get("scheme"), anchor.get("v"),
                         anchor.get("algo")))
        return snapshot
    if snapshot.self_state in (CORRUPT, AMBIGUOUS):
        snapshot.anchor_state = NOT_CHECKED     # there is nothing to hash
        return snapshot
    snapshot.anchor_state = (
        MATCH if digest(snapshot.meta, snapshot.step_lines) == anchor["digest"]
        else MISMATCH)
    return snapshot


def decide(snapshot, profile):
    """What a profile does with the pair, as a positive condition.

    Written as a list of refusals with a fall-through the first time, which
    released a snapshot nobody had checked. Release is something a caller has
    to earn.
    """
    if profile not in PROFILES:
        return SUITE_ERROR, "unknown integrity profile %r" % (profile,)
    if snapshot.self_state == CORRUPT:
        return SUITE_ERROR, snapshot.reason or "the artifact cannot be read"
    if snapshot.self_state == AMBIGUOUS:
        return SUITE_ERROR, snapshot.reason or "the artifact has two readings"
    if snapshot.anchor_declared is None:
        # Nobody asked. A fault in the caller, not a state of the file, and
        # distinguishable from "asked, and none was declared".
        return SUITE_ERROR, "the anchor was never checked"

    if snapshot.anchor_declared:
        if snapshot.anchor_state != MATCH:
            return REFUSE, (snapshot.reason if snapshot.anchor_state in (
                ANCHOR_INVALID, ANCHOR_UNSUPPORTED) else
                "this case declares an anchor and the recording cannot be "
                "verified against it")
        if snapshot.self_state not in (INTACT, ABSENT):
            return REFUSE, ("the anchor matches and the recording's own "
                            "descriptor does not: two readings of one file")
        return RELEASE, None

    if snapshot.self_state == CHANGED:
        return REFUSE, snapshot.reason
    if profile == PROTECTED:
        return REFUSE, ("no anchor: self-consistency is not tamper evidence"
                        if snapshot.self_state == INTACT
                        else "no anchor, and nothing in the file to check")
    return RELEASE, "unverified: no anchor was declared for this recording"


def describe(snapshot, action, reason):
    """One line for a person, and the pair that produced it."""
    return {"action": action, "reason": reason,
            "integrity": snapshot.as_dict()}
