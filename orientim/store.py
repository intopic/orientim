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
import threading
import time
import uuid

from . import model
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
        return {
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
            "runtime": model.runtime_info(),
        }

    def serialize(self):
        lines = [json.dumps({"_meta": self.meta()}, default=str)]
        lines += [json.dumps(s, default=str) for s in self.steps]
        return ("\n".join(lines) + "\n").encode("utf-8")

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
