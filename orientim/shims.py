"""Clock, randomness and identifiers.

What these returned during recording is what they return during replay.
Covers sources 01-04 and 06 of the conformance suite.

Two limits, stated here because they are invisible otherwise:

* Only call sites that look the function up on the module are shimmed. A
  library that did `from time import time` before record() started holds the
  original and is not covered. A replay hits that as a divergence rather than
  silently passing, but the diagnosis will point elsewhere.
* `secrets`, `os.urandom` and `numpy.random` are not shimmed at all.

The `random` module is covered a second way: the generator's state at the start
of the run is written down, and a replay puts the generator back there before
the agent starts. That makes `randint`, `choice`, `uniform` and `shuffle` replay
correctly as long as the code path consumes them in the same order. Recording
only reads the state — it never puts it back, which would rewind the caller's
own random stream.
"""
import base64
import contextlib
import random as _random
import struct
import threading
import time as _time
import uuid as _uuid

from . import scope

# A stack, not a single slot: record() inside record() used to switch the outer
# one off on the inner one's way out, and neither of them said so.
_stack = []
_lock = threading.RLock()


def _current():
    """The frame this clock read belongs to.

    Same reason as the httpx patch: on an event loop every coroutine shares a
    thread, so the innermost frame used to swallow time and randomness that
    belonged to a different request.
    """
    region = scope.current()
    if region is not None:
        frame = region.data.get("shims")
        if frame is not None:
            return frame
    # A worker thread with no context. One frame open → attribute to it. Several
    # open at once → refuse rather than guess, for the same reason as the httpx
    # patch: a worker's clock reading recorded against a different run corrupts
    # that recording. A missing shim surfaces on replay as a shim-miss, never as
    # a false IDENTICAL. See scope.current().
    return _stack[-1] if len(_stack) == 1 else None


def _consume(kind, produce):
    with _lock:
        st = _current()
        if st is None:
            return produce()
        if st["mode"] == "record":
            v = produce()
            st["rec"].add({"t": "shim", "kind": kind, "value": str(v)})
            return v
        if st["mode"] == "replay":
            q = st["queue"]
            for idx, s in enumerate(q):
                if s.get("t") == "shim" and s.get("kind") == kind:
                    q.pop(idx)
                    return s["value"]
            # Nothing recorded for this: the agent asked for more randomness
            # than the recording holds, which is itself a divergence worth
            # reporting.
            st["rec"].note_uncaptured("shim-miss", kind)
            return produce()
    return produce()


class _Patched:
    """Installed once for the outermost scope, removed when it leaves."""

    def __init__(self):
        self.orig = {}

    def install(self):
        self.orig["time"] = _time.time
        self.orig["time_ns"] = _time.time_ns
        self.orig["uuid4"] = _uuid.uuid4
        self.orig["rand"] = _random.random

        _time.time = lambda: float(_consume("time", self.orig["time"]))
        _time.time_ns = lambda: int(_consume("time_ns", self.orig["time_ns"]))
        _uuid.uuid4 = lambda: _uuid.UUID(
            str(_consume("uuid4", lambda: self.orig["uuid4"]())))
        _random.random = lambda: float(_consume("random", self.orig["rand"]))

    def remove(self):
        _time.time = self.orig["time"]
        _time.time_ns = self.orig["time_ns"]
        _uuid.uuid4 = self.orig["uuid4"]
        _random.random = self.orig["rand"]


_patched = None


@contextlib.contextmanager
def active(mode, rec, steps=None, region=None):
    global _patched
    frame = {"mode": mode, "rec": rec, "queue": list(steps or [])}
    if region is not None:
        region.data["shims"] = frame
    with _lock:
        _stack.append(frame)
        first = len(_stack) == 1
        if first:
            _patched = _Patched()
            _patched.install()

    # Everything random.* draws from one shared generator. Writing its state
    # down covers randint, choice, uniform and shuffle, which the per-call shim
    # above does not reach.
    #
    # Recording only reads the state. It must not put it back afterwards: that
    # would rewind the caller's global random stream on every exit, so thirty
    # runs of the same agent in one process would draw the same "random"
    # numbers thirty times. Only replay, which deliberately hijacked the
    # generator, restores what it found.
    saved_state = None
    if mode == "record":
        # Held on the recording, not written yet. Most runs never touch the
        # shared generator, and 625 integers of Mersenne Twister state was
        # 7.3 KB — three quarters of a small recording — spent on a field
        # nothing would ever read back. settle() decides, before the save.
        rec._random_start = _random.getstate()
    elif mode == "replay":
        recorded = getattr(rec, "replay_random_state", None)
        if recorded:
            saved_state = _random.getstate()
            try:
                _random.setstate(_decode_state(recorded))
            except (TypeError, ValueError):
                saved_state = None
    try:
        yield
    finally:
        if saved_state is not None:
            _random.setstate(saved_state)
        with _lock:
            _stack.pop()
            if not _stack and _patched is not None:
                _patched.remove()
                _patched = None


def settle(rec):
    """Decide whether this run needs the generator state written down.

    Called from record() before the file is serialised — not from active()'s
    exit, which unwinds after the save and would have set the field too late to
    be stored. If the generator is where we found it, nothing drew from it and
    a replay has nothing to restore.
    """
    start = getattr(rec, "_random_start", None)
    if start is not None and _random.getstate() != start:
        # Process-wide, not agent-wide: another thread drawing from the shared
        # generator also counts. That errs towards storing 3 KB nobody needs,
        # which is the right direction — the other error is a replay that
        # cannot reproduce the numbers the run actually saw.
        rec.random_state = _encode_state(start)


def _encode_state(state):
    """Pack the generator state small enough to stop mattering.

    Mersenne Twister keeps 625 32-bit words. As a JSON array that is 7.3 KB;
    packed and base64-encoded it is 3.3 KB, and it stays one short string
    instead of a wall of digits in the middle of a file people read by hand.
    """
    version, keys, gauss = state
    try:
        packed = struct.pack("<%dI" % len(keys), *keys)
        return [version, "b64:" + base64.b64encode(packed).decode("ascii"), gauss]
    except (struct.error, OverflowError, TypeError):
        return [version, list(keys), gauss]


def _decode_state(raw):
    version, keys, gauss = raw
    if isinstance(keys, str) and keys.startswith("b64:"):
        packed = base64.b64decode(keys[4:])
        keys = struct.unpack("<%dI" % (len(packed) // 4), packed)
    return (version, tuple(int(k) for k in keys), gauss)
