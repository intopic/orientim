# -*- coding: utf-8 -*-
"""Traffic we cannot capture, noticed while it happens.

Orientim intercepts httpx, httpx2 and requests. An agent whose search tool calls
out through `aiohttp` produces a recording that is silently incomplete — and the whole
proposition of this package is that a replay which says "identical" means it.
A recording missing half the run cannot honestly say that.

So instead of pretending the gap does not exist, we watch for it. These patches
capture nothing and change nothing: they count the call, write down where it
went, and hand it straight to the original. The recording then carries the
count, and a replay of that recording can never report IDENTICAL — it reports
UNCAPTURED_LIBRARY and names the URLs.

Only libraries already imported when record() starts are watched. One imported
later is missed, which is stated rather than worked around.
"""
import contextlib
import sys
import threading

from . import scope

_lock = threading.RLock()
_scopes = []
_orig = {}


def _note(kind, method, url):
    """Charge the call to the recording that made it, not to all of them.

    Noting it against every open recording would mark runs as incomplete that
    were nothing of the sort — and a false UNCAPTURED_LIBRARY is exactly as
    damaging as a false IDENTICAL, in the other direction.
    """
    from .transport import redact
    detail = "%s %s" % (method, redact(str(url)))
    region = scope.current()
    if region is not None and region.data.get("detect"):
        region.rec.note_unseen(kind, detail)
        return
    with _lock:
        mine = list(_scopes)
    for rec in mine:
        rec.note_unseen(kind, detail)


# `requests` is not here any more: it is captured, not merely counted. See
# orientim/reqs.py.


# --- aiohttp ----------------------------------------------------------------

def _patch_aiohttp(mod):
    session = mod.ClientSession
    _orig["aiohttp"] = (session, session._request)
    original = session._request

    async def _request(self, method, url, *a, **kw):
        _note("aiohttp", method, url)
        return await original(self, method, url, *a, **kw)

    session._request = _request


# --- urllib -----------------------------------------------------------------

def _patch_urllib(mod):
    _orig["urllib"] = (mod, mod.urlopen)
    original = mod.urlopen

    def urlopen(url, *a, **kw):
        method = getattr(url, "get_method", lambda: "GET")()
        target = getattr(url, "full_url", url)
        _note("urllib", method, target)
        return original(url, *a, **kw)

    mod.urlopen = urlopen


_WATCHED = (
    ("aiohttp", "aiohttp", _patch_aiohttp),
    ("urllib", "urllib.request", _patch_urllib),
)


def _restore():
    for name, (holder, fn) in list(_orig.items()):
        if name == "aiohttp":
            holder._request = fn
        else:
            holder.urlopen = fn
    _orig.clear()


@contextlib.contextmanager
def watching(rec, region=None):
    """Count calls made through libraries Orientim does not capture."""
    if region is not None:
        region.data["detect"] = True
    with _lock:
        _scopes.append(rec)
        first = len(_scopes) == 1
        if first:
            for name, module, patch in _WATCHED:
                mod = sys.modules.get(module)
                if mod is None:
                    continue
                try:
                    patch(mod)
                except Exception:
                    # A library shaped differently from what we expect is not
                    # worth breaking someone's recording over.
                    pass
    try:
        yield
    finally:
        with _lock:
            _scopes.remove(rec)
            if not _scopes:
                _restore()
