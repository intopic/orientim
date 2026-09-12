# -*- coding: utf-8 -*-
"""Recordings: ring buffer, trigger capture, and where they are written.

We do not write every run. Steps are held in memory and flushed only when
something fires — an error, a declared trigger, an uncaptured source. The
destination is decided by the store URL, so a free user on local disk and a
team on their own S3 bucket run identical code.
"""
import collections
import json
import os
import secrets
import threading
import time
import uuid

from . import integrity, model
from .storage import open_store

# Bumped when the meaning of a stored field changes. A recording written before
# the step digest was recomputed at comparison time cannot be judged by the
# current rule, and saying so beats replaying it under the wrong one.
#
# 4 adds the execution model: typed steps, model metadata, the final output and
# the agent identity. None of it touches chain.DIGEST_FIELDS, so a format 3
# recording migrated to 4 produces byte-identical chain hashes — which is what
# makes the migration below safe rather than merely convenient.
FORMAT = 4

# The oldest format `migrate` can carry forward. Below this the *meaning* of a
# stored field changed, not just the set of them, so there is nothing honest to
# migrate: a format 2 recording stored a digest that is no longer computed the
# same way, and replaying it under today's rule would answer a question it was
# never asked. Those stay stale, on purpose.
MIGRATABLE_FROM = 3


SESSION_HEADER = "mcp-session-id"
TRANSFORM = "session_pseudonym"
TRANSFORM_V = 1


class SessionTokens:
    """Opaque tokens standing in for session identifiers, for one recording.

    An MCP server mints a session identifier and returns it in a response
    header; a client reads it there and echoes it on every later request. The
    identifier is then in two places at once — the stored response header, in
    the clear, and inside `hdr_fp`, where it is a hash. Removing it from the
    first breaks the second: the replayed client echoes whatever the recording
    gave it, and a fingerprint taken over the real value no longer matches.

    So both sides are written in token space instead. A replay records nothing,
    so this object does not exist then: the stored token is served, the client
    echoes the token, and the fingerprint is taken over the token. Nothing in
    the replay path changes.

    **What is checked, and what is only asserted.** The rule here is an
    observation — this value was not carried by an earlier request — and it is
    not proof that the client took the value from the response. A client
    configured with a session it keeps using regardless of what a response says
    looks identical from here. That condition is the operator's to assert, by
    turning the feature on, and `docs/recordings.md` states it. Where the
    condition does not hold and the client's value reaches a captured request
    header, the first replay diverges; where it does not reach one — a value
    used only in the agent's own logic, or never sent again — nothing here
    notices.

    **Sequential runs only.** A step is opened when the response headers
    arrive, not when the request is sent, so with requests in flight at once a
    response can be examined before an earlier request has been seen. The rule
    would then call a value response-derived when it was not. v1 claims
    nothing about concurrent sessions.

    The map is held here and never written. What reaches the file is a count,
    the step indices it touched, and what it left alone.
    """

    def __init__(self, header=SESSION_HEADER):
        self.header = header
        self._map = {}            # identifier -> token
        self._taken = set()
        self._on_requests = set()  # values a request carried
        # Steps whose fingerprint was taken over a token, as indices. A step
        # does not know its own index until it is added, so it is noted by
        # identity for that one moment and resolved to an integer the instant
        # the index exists. No step object is held: the ring is free to evict
        # whatever it likes, and `block` reports only what is still in the
        # file.
        self._pending = {}
        self._fp_index = []
        self._lock = threading.Lock()

    def _candidate(self):
        """One documented shape, 128 bits. A seam, so a test can force one."""
        return "sess-" + secrets.token_hex(16)

    def _mint(self, current=None):
        """A token that is not any token, and not any identifier we have seen.

        Not the length of the value it replaces: a token truncated to fit a
        short identifier loses the random part that makes it a different
        token, and two sessions become one.

        Three things a candidate must not collide with, and each for its own
        reason. A token already minted, or two sessions would read as one. An
        identifier already observed — on a request or as a key in the map —
        or a value left in the clear elsewhere in this file would be
        indistinguishable from a token, and `block` would report it as
        transformed when it is not. And the value being replaced right now,
        which is the same problem one moment earlier.
        """
        while True:
            token = self._candidate()
            if (token not in self._taken and token not in self._map
                    and token not in self._on_requests and token != current):
                self._taken.add(token)
                return token

    def for_request(self, headers):
        """The headers a request fingerprint should be taken over.

        Also the only place a value is seen arriving *from* the client, which
        is what the rule below reads.
        """
        out, hit = {}, False
        with self._lock:
            for k, v in headers.items():
                if k.lower() == self.header and v:
                    self._on_requests.add(v)
                    if v in self._map:
                        v, hit = self._map[v], True
                out[k] = v
        return out if hit else headers

    def _value(self, headers):
        """The session header of a stored response, whatever its casing.

        `for_response` already matches case-insensitively; reading it back by
        one exact spelling would let a transformed header go uncounted, and a
        count that misses what it did is worse than no count.
        """
        for k, v in (headers or {}).items():
            if k.lower() == self.header and v:
                return v
        return None

    def note_fingerprint(self, step):
        """This step's fingerprint was taken over a token, not the value.

        Keyed by identity rather than by holding the object: `resolve` runs a
        moment later, while the caller still has the step, and nothing here
        outlives that.
        """
        with self._lock:
            self._pending[id(step)] = True

    def resolve(self, step):
        """Called once the step has an index. Turns the note into an integer."""
        with self._lock:
            if self._pending.pop(id(step), False):
                self._fp_index.append(step.get("i"))

    def for_response(self, stored):
        """Replace the session header in a stored response, in place.

        Only a value no earlier request carried: one the client already had is
        not this recording's to rename, and renaming it would leave the file
        disagreeing with the client on the next replay.
        """
        with self._lock:
            for k in list(stored):
                if k.lower() != self.header or not stored[k]:
                    continue
                v = stored[k]
                if v in self._map:
                    stored[k] = self._map[v]
                elif v not in self._on_requests:
                    self._map[v] = self._mint(current=v)
                    stored[k] = self._map[v]

    def block(self, steps):
        """What the file says about all this. No values, no map.

        The two sides are listed apart because they are read for different
        reasons. `stored_headers` is where an identifier would have been; a
        reader checking what was written looks there. `hdr_fp` is where a
        token is hashed into a fingerprint, which is what makes two
        recordings incomparable on those steps and nowhere else — and it is a
        different set, because the response that carries the identifier is
        rarely the request that echoes it.

        The stored side is derived from the steps themselves: a stored session
        header is either one of the tokens minted here or a value left alone.
        A count of zero on its own would not say whether there was no session
        at all or a session left in the clear, so both are named.
        """
        present = {s.get("i") for s in steps}
        with self._lock:
            tokens = set(self._map.values())
            seen = set(self._on_requests)
            # Only what the file still holds: the ring may have evicted a step
            # this touched, and an index nobody can look up explains nothing.
            fps = sorted(i for i in self._fp_index if i in present)
        done, left, left_values = [], [], {}
        for s in steps:
            v = self._value(s.get("headers"))
            if not v:
                continue
            if v in tokens:
                done.append(s.get("i"))
            else:
                reason = ("carried_by_an_earlier_request" if v in seen
                          else "not_transformed")
                left_values.setdefault(reason, set()).add(v)
                left.append((s.get("i"), reason))
        return {
            "id": TRANSFORM, "v": TRANSFORM_V, "activation": "explicit",
            "scope": {"header": self.header,
                      "rule": "first_observed_in_response",
                      "condition": "client_reuses_response_value",
                      "applies_to": ["stored_headers", "hdr_fp"],
                      "supported_flow": "sequential"},
            "transformed": {"values": len(tokens),
                            "stored_headers": done, "hdr_fp": fps},
            "untransformed": [
                {"values": len(left_values[r]),
                 "steps": [i for i, why in left if why == r], "reason": r}
                for r in sorted(left_values)],
            # Two recordings' fingerprints are not comparable on the steps
            # above: each carries its own tokens. It explains a difference; it
            # never excuses one.
            "comparable_across_recordings": False,
        }


class Recording:
    def __init__(self, run_id=None, ring=2000, tags=None):
        self.run_id = run_id or "run_" + uuid.uuid4().hex[:8]
        self.steps = collections.deque(maxlen=ring)
        self.tags = dict(tags or {})
        self.started_at = time.time()
        self.t0 = time.monotonic()
        self.ended_at = None
        self.status = "ok"
        self.triggered = False
        self.trigger_reason = None
        self.uncaptured = []      # sources we could not capture
        self.blocked = []         # side effects blocked during replay
        self.env = {}             # environment snapshot (source 17), opt-in
        self.random_state = None  # random module state, only if the run used it
        self.trace = {}           # trace/span id of the host application
        self.unseen = []          # calls through libraries we do not capture
        self.attempts = 0         # requests tried during replay
        self.dropped = 0          # steps the ring buffer evicted
        self.outcome = None       # what the agent finally returned, if declared
        self.input = None         # what it was asked to do, if declared
        self.agent = None         # who the agent is, if declared
        self.context = None       # who it was acting as, if declared
        # Session pseudonymisation, off unless the run asked for it. The map
        # lives in this object and is never written; see SessionTokens.
        self.sessions = None
        # Requests a replay could not match. Deliberately NOT steps: the chain
        # is built from steps, so putting these there would change verdicts.
        # Never written to the file — meta() does not mention them.
        self.unmatched = []
        self._seq = 0             # step counter, unaffected by eviction
        # Which worker issued each step, numbered per run in the order the
        # threads first appear. NOT an OS id — that would leak and would not
        # mean anything to a reader — and NOT comparable between runs, because
        # the numbering depends on which thread got there first, which is the
        # very thing concurrency analysis is asking about. Useful within a run,
        # for grouping; useless across them, and concurrency.py treats it that
        # way.
        self._workers = {}
        self._lock = threading.Lock()

    def add(self, step: dict):
        # An agent with calls in flight on four threads reaches this at once,
        # and `self._seq += 1` is a read-modify-write. Cheap lock, no races.
        with self._lock:
            # A full ring evicts the oldest step. That breaks replay in a way
            # the user cannot see, so count it and say so rather than let them
            # find out from a confusing divergence three days later.
            if self.steps.maxlen is not None and len(self.steps) == self.steps.maxlen:
                self.dropped += 1
            # Counted, not len(steps): once the ring is full the length stops
            # growing and every remaining step would carry the same index.
            step["i"] = self._seq
            self._seq += 1
            tid = threading.get_ident()
            if tid not in self._workers:
                self._workers[tid] = len(self._workers)
            step["worker"] = self._workers[tid]
            self.steps.append(step)
            if self.sessions is not None:
                self.sessions.resolve(step)

    def trigger(self, reason: str):
        self.triggered = True
        self.trigger_reason = reason

    def note_uncaptured(self, kind: str, detail: str):
        self.uncaptured.append({"kind": kind, "detail": detail})
        self.trigger(f"uncaptured:{kind}")

    def note_unseen(self, kind: str, detail: str):
        """A call that left the process through a library we do not intercept.

        Not a trigger: a run is not worth saving just because it used requests.
        But it is written into the file, and a replay of a recording that holds
        one can never report IDENTICAL — part of the run was never captured.
        """
        with self._lock:
            if len(self.unseen) < 50:
                self.unseen.append({"kind": kind, "detail": detail})
            self.unseen_n = getattr(self, "unseen_n", 0) + 1

    def meta(self):
        out = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "steps": len(self.steps),
            "tags": self.tags,
            "trigger": self.trigger_reason,
            "uncaptured": self.uncaptured,
            "blocked": self.blocked,
            "env": self.env,
            "unseen": self.unseen,
            "unseen_n": getattr(self, "unseen_n", 0),
            "random_state": self.random_state,
            "trace": self.trace,
            "dropped": self.dropped,
            "format": FORMAT,
            "outcome": self.outcome,
            "input": self.input,
            "agent": self.agent,
            # Read by `context.mediate` when a later replay runs under a
            # contract. Absent on every recording written before contracts
            # existed, which is why a contract that needs it answers UNKNOWN
            # rather than assuming anything.
            "context": self.context,
            "runtime": model.runtime_info(),
        }
        if self.sessions is not None:
            # What was done to this file, said by the file. Absent means
            # nothing was done, which is what every recording written before
            # this says by saying nothing.
            out["transforms"] = [self.sessions.block(self.steps)]
        return out

    def serialize(self):
        # Steps first, because the descriptor is a digest over exactly these
        # bytes. The meta line cannot be covered as written — writing the
        # digest into it would change what the digest is over — so it is
        # covered as a canonical mapping instead. See orientim/integrity.py.
        steps = [json.dumps(s, default=str).encode("utf-8")
                 for s in self.steps]
        meta = self.meta()
        meta[integrity.KEY] = integrity.descriptor(meta, steps)
        head = json.dumps({"_meta": meta}, default=str).encode("utf-8")
        return b"\n".join([head] + steps) + b"\n"

    def save(self, root=None, force=False):
        """Write only if the trigger fired. Returns a locator, or None."""
        if not (self.triggered or force):
            return None
        backend = open_store(root)
        key = f"{self.run_id}.jsonl"
        backend.write(key, self.serialize())
        locator = locator_for(root, key)
        _auto_cap(root)
        return locator


# --- locators ---------------------------------------------------------------
# A locator names one recording wherever it lives. Local recordings keep plain
# filesystem paths so nothing about the free path changes.

def locator_for(root, key):
    root = root or os.environ.get("ORIENTIM_STORE") or "runs"
    if root.startswith(("s3://", "memory://")):
        return root.rstrip("/") + "/" + key
    return os.path.join(root, key)


def split_locator(locator):
    """Return (root, key) for any locator form."""
    for scheme in ("s3://", "memory://"):
        if locator.startswith(scheme):
            root, _, key = locator.rpartition("/")
            return root, key
    return os.path.dirname(locator) or ".", os.path.basename(locator)


# --- migration ----------------------------------------------------------------
# A format change that invalidates history is not an upgrade, it is a data loss
# with a version number on it. `stale` is not a refusal here: session.py excludes
# it from `ok`, so bumping FORMAT without this would leave every recording
# already on disk permanently unable to report IDENTICAL. Recordings that were
# fine would start reporting as failures, which is the worst kind of wrong — a
# silent false alarm on history nobody has a reason to re-examine.
#
# So the upgrade happens on read, in memory, and the file on disk is never
# rewritten. A recording is a record of something that happened; editing it in
# place to suit a newer version of the tool would be the wrong instinct.


def migrate(meta, steps, enrich_steps=True):
    """Carry a recording forward to the current format, in memory.

    Returns (meta, steps). Both may be the objects passed in, when nothing
    needed doing. Never raises: a recording that cannot be migrated is returned
    untouched and will be reported as stale, which is a worse answer than a
    migration and a much better one than a crash.

    enrich_steps=False migrates the metadata and leaves the steps alone. For a
    caller that only reads metadata — `ls`, retention — the per-step work is
    pure cost: it roughly doubles the time to read an old recording, and over a
    store of a few thousand it is the difference between `ls` feeling instant
    and not. Nothing the skipped fields carry can change a chain digest, a
    signature or a retention decision.
    """
    if not meta:
        return meta, steps
    try:
        found = int(meta.get("format", 1))
    except (TypeError, ValueError):
        return meta, steps
    if found >= FORMAT or found < MIGRATABLE_FROM:
        return meta, steps

    try:
        meta = dict(meta)
        meta["format"] = FORMAT
        meta["migrated_from"] = found
        # Never recorded before format 4 and not recoverable after the fact.
        # Explicit nulls rather than absent keys, so a reader can tell "this run
        # returned nothing" from "this run predates us asking".
        for key in ("outcome", "input", "agent", "runtime"):
            meta.setdefault(key, None)

        if not enrich_steps:
            return meta, steps

        # The rest *is* recoverable. Classification and model metadata are pure
        # functions of the request, and the request was already stored, so an
        # old recording gets the same typed steps a new one would. This is the
        # part that makes the execution model worth having on day one instead
        # of only for runs recorded from here on.
        out = []
        for step in steps:
            out.append(enrich(step) if step.get("t") == "http" else step)
        return meta, out
    except Exception:
        return meta, steps


def enrich(step):
    """Add the execution-model fields to one http step, without altering it.

    Returns a new dict. The fields it writes are outside chain.DIGEST_FIELDS by
    construction, so an enriched step hashes exactly as it did before — the
    property the migration rests on, and the one
    tests/test_execution.py::t_migration_preserves_chain checks.
    """
    if "role" in step:
        return step
    try:
        s = dict(step)
        url, req = s.get("url") or "", s.get("req")
        s["role"] = model.classify(url, req, s.get("method"))
        if s["role"] == model.MODEL:
            call = model.describe_model_call(url, req)
            if call:
                s["model"] = call
            if not s.get("b64"):
                served = model.describe_model_response(s.get("body"))
                if served:
                    s["served"] = served
        return s
    except Exception:
        return step


def parse(raw, upgrade=True, enrich_steps=True):
    steps, meta = [], None
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "_meta" in obj:
            meta = obj["_meta"]
        else:
            steps.append(obj)
    if upgrade:
        meta, steps = migrate(meta, steps, enrich_steps=enrich_steps)
    return meta, steps


def load(locator):
    root, key = split_locator(locator)
    return parse(open_store(root).read(key))


def read_snapshot(locator):
    """One read: the integrity snapshot, and the parsed form of those bytes.

    The pair exists so that nothing can verify a path and then open it again.
    A caller checks the snapshot and consumes the `(meta, steps)` that came out
    of the same read, rather than calling `load(locator)` a second time — a
    path that verified is not a path that can be read again.
    """
    root, key = split_locator(locator)
    raw = open_store(root).read(key)
    snap = integrity.read(raw)
    if snap.self_state in (integrity.CORRUPT, integrity.AMBIGUOUS):
        return snap, None               # there is nothing safe to parse
    return snap, parse(raw)


def list_runs(root=None):
    backend = open_store(root)
    out = []
    for name in backend.list():
        try:
            meta, _ = parse(backend.read(name), enrich_steps=False)
            if meta:
                out.append(meta)
        except Exception:
            continue
    return out


def url_for(locator, expires=3600):
    """A URL the browser can read directly, without passing through anyone."""
    root, key = split_locator(locator)
    return open_store(root).url(key, expires=expires)


def describe(root=None):
    return open_store(root).describe()


# --- retention ---------------------------------------------------------------
# Recordings accumulate. A production agent that fails one request in a hundred
# writes a file a hundred times a day, forever, and eventually fills a disk that
# somebody else's service also lives on.
#
# Nothing here runs on its own. Deleting a user's recordings as a side effect of
# writing one is the kind of surprise that is never worth the disk it saves, so
# pruning is an explicit command — or an explicitly opted-into cap.


def signature(meta, steps):
    """What makes two failures the same failure.

    The chain root plus the trigger reason. Five hundred runs that failed in
    exactly the same way are one thing you need to look at, not five hundred —
    and the root hash already tells us they are the same run.
    """
    from . import chain
    http = [s for s in steps if s.get("t") == "http"]
    _, root = chain.build_steps(http)
    return "%s:%s" % (meta.get("trigger") or "-", root[:16])


def survey(root=None):
    """Every recording in a store, with what pruning needs to decide."""
    backend = open_store(root)
    out = []
    for name in backend.list():
        try:
            raw = backend.read(name)
            # The signature is a chain root plus the trigger, and the chain is
            # computed from an allowlist the execution model is not in, so the
            # enriched fields cannot change what this decides.
            meta, steps = parse(raw, enrich_steps=False)
            if not meta:
                continue
            size, mtime = backend.stat(name)
            out.append({
                "key": name,
                "run_id": meta.get("run_id", name),
                "started_at": meta.get("started_at") or mtime,
                "status": meta.get("status"),
                "trigger": meta.get("trigger"),
                "bytes": size,
                "signature": signature(meta, steps),
            })
        except Exception:
            # A file we cannot read is a file we will not delete.
            continue
    out.sort(key=lambda r: r["started_at"], reverse=True)
    return out


def plan_prune(rows, keep=None, older_than_days=None, max_bytes=None,
               per_signature=None, now=None):
    """Decide what goes, without touching anything.

    Rules are applied together: a recording is removed if ANY rule condemns it.
    Newest first throughout, so 'keep' always keeps the most recent.
    """
    now = time.time() if now is None else now
    doomed, seen, running = {}, {}, 0

    for pos, r in enumerate(rows):
        why = []
        if keep is not None and pos >= keep:
            why.append("beyond the newest %d" % keep)
        if older_than_days is not None:
            age = (now - r["started_at"]) / 86400.0
            if age > older_than_days:
                why.append("%.0f days old" % age)
        if per_signature is not None:
            n = seen.get(r["signature"], 0) + 1
            seen[r["signature"]] = n
            if n > per_signature:
                why.append("copy %d of the same failure" % n)
        if max_bytes is not None and not why:
            # Only what survives the other rules counts against the budget.
            # Charging condemned files to it would evict recordings to make
            # room for recordings that are themselves about to be deleted.
            if running + r["bytes"] > max_bytes:
                why.append("over the %d byte budget" % max_bytes)
            else:
                running += r["bytes"]
        if why:
            doomed[r["key"]] = (r, why)
    return [doomed[k] for k in doomed]


def prune(root=None, keep=None, older_than_days=None, max_bytes=None,
          per_signature=None, dry_run=False):
    """Apply a retention policy. Returns the rows it removed (or would).

    With no rules given, nothing is removed — an empty policy deletes nothing
    rather than everything, because the other way round is unrecoverable.
    """
    if keep is None and older_than_days is None and max_bytes is None             and per_signature is None:
        return []
    rows = survey(root)
    victims = plan_prune(rows, keep, older_than_days, max_bytes, per_signature)
    if not dry_run:
        backend = open_store(root)
        for r, _why in victims:
            backend.delete(r["key"])
    return victims


def _auto_cap(root):
    """The opt-in cap, applied after a save. Off unless ORIENTIM_MAX_RUNS is set.

    Local stores only: listing and stat-ing a bucket after every save would put
    a network round trip on the hot path of a tool whose whole point is that it
    stays out of the way.
    """
    cap = os.environ.get("ORIENTIM_MAX_RUNS")
    if not cap:
        return
    try:
        cap = int(cap)
    except ValueError:
        return
    if cap <= 0:
        return
    target = root or os.environ.get("ORIENTIM_STORE") or "runs"
    if target.startswith(("s3://", "memory://")):
        return
    try:
        prune(root, keep=cap)
    except Exception:
        pass
