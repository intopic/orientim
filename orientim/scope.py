# -*- coding: utf-8 -*-
"""Which recording the code running right now belongs to.

Three subsystems need this answer — the httpx patch, the clock and randomness
shims, and the uncaptured-library detector — and all three used to guess it
from the thread id. That is correct for a thread-per-request server and wrong
for an async one, where every coroutine shares a single thread: with five
requests in flight, four recordings came out empty and the fifth held
everybody's traffic.

A ContextVar is the right instrument. asyncio gives each task its own context,
threads get their own by default, and a value set inside a `with` block is
visible to everything it awaits. The thread-id and innermost fallbacks stay for
the one case a ContextVar cannot reach: a client built in a worker thread
spawned inside the block, which starts with a fresh, empty context.
"""
import contextlib
import contextvars
import threading

_active = contextvars.ContextVar("orientim_active", default=None)
_lock = threading.RLock()
_open = []          # every live region, innermost last — the fallback


class Region:
    """One live record() or replay(), and what each subsystem hung on it."""

    __slots__ = ("rec", "mode", "thread", "data")

    def __init__(self, rec, mode):
        self.rec = rec
        self.mode = mode
        self.thread = threading.get_ident()
        self.data = {}


@contextlib.contextmanager
def bound(rec, mode):
    region = Region(rec, mode)
    token = _active.set(region)
    with _lock:
        _open.append(region)
    try:
        yield region
    finally:
        _active.reset(token)
        with _lock:
            try:
                _open.remove(region)
            except ValueError:
                pass


def current():
    """The region this code belongs to, or None.

    The ContextVar answers for the caller's own task or thread. A worker thread
    spawned inside a region carries no context, so we look for a region opened
    on this thread, and failing that take the innermost open one — which is what
    keeps capture working for a thread pool inside an agent.
    """
    region = _active.get()
    if region is not None:
        return region
    with _lock:
        if not _open:
            return None
        tid = threading.get_ident()
        for r in reversed(_open):
            if r.thread == tid:
                return r
        # No context, and this thread opened nothing — a worker thread spawned
        # inside a region, which starts with an empty context (raw threads and
        # ThreadPoolExecutor do not copy it; asyncio.to_thread does, and reaches
        # us through _active above). If exactly one region is open we attribute
        # to it: the thread-pool-inside-one-agent case, which must keep working.
        # If several are open we cannot tell which this worker belongs to, and
        # guessing the innermost silently files one recording's traffic — bodies
        # and secrets included — under another concurrent recording. Refuse
        # instead: not capturing surfaces as a divergence on replay, while
        # misattributing corrupts two recordings and leaks one's data into the
        # other's file. See docs/limits.md.
        return _open[-1] if len(_open) == 1 else None


def all_open():
    with _lock:
        return list(_open)
