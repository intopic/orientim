# -*- coding: utf-8 -*-
"""Capture at the `requests` boundary.

Model traffic goes through httpx; tools are very often written with `requests`,
and a recording that holds half a run cannot honestly say IDENTICAL. Until this
existed, those calls were counted and named — `UNCAPTURED_LIBRARY` — but never
reproduced.

The seam is `HTTPAdapter.send`: it takes a PreparedRequest and returns a
Response, the same shape as the httpx transport one layer down. Redirects and
retries are handled above it by `Session.send`, so each hop arrives here on its
own, exactly as each httpx hop does.

The recorded queue is shared with the httpx transports, so an agent whose model
calls go through httpx and whose tools use requests still replays in one order.
"""
import json
import time as _rt

from . import transport

_orig = {}
_pick = None


def _body(request):
    """The bytes this request carries, for the lookup key.

    A file or generator body is consumed by sending it, and reading it here
    would empty it before it is sent. Those uploads are matched on method and
    URL alone — stated here rather than silently mismatched.
    """
    b = getattr(request, "body", None)
    if b is None:
        return b""
    if isinstance(b, bytes):
        return b
    if isinstance(b, str):
        return b.encode("utf-8", "replace")
    return b""


class _RecordRaw:
    """Notes each chunk as the caller reads it, and passes it straight on.

    Reading the body here instead would hand a streaming caller everything at
    once — a behaviour change caused by the observer, which is the one thing a
    recorder must not do.
    """

    def __init__(self, inner, step, t0, rec):
        self.inner = inner
        self.step = step
        self.t0 = t0
        self.rec = rec
        self.buf = bytearray()
        self.marks = []
        self.done = False

    def _note(self, chunk):
        self.marks.append([round((_rt.monotonic() - self.t0) * 1000.0, 3),
                           len(chunk)])
        self.buf += chunk

    def stream(self, amt=None, decode_content=True):
        try:
            for chunk in self.inner.stream(amt, decode_content=decode_content):
                self._note(chunk)
                yield chunk
        except Exception as exc:
            self._finish(error=type(exc).__name__)
            raise
        finally:
            self._finish()

    def read(self, amt=None, decode_content=True, **kw):
        data = self.inner.read(amt, decode_content=decode_content, **kw)
        if data:
            self._note(data)
        if not data or amt is None:
            self._finish()
        return data

    def _finish(self, error=None):
        if self.done:
            return
        self.done = True
        if error:
            self.step["error"] = error
        transport._close_step(self.step, bytes(self.buf), self.marks, self.t0)
        if self.step["status"] >= 500:
            self.rec.trigger("http %d" % self.step["status"])
        if error:
            self.rec.trigger("exception:" + error)

    def close(self):
        try:
            self.inner.close()
        finally:
            self._finish()

    def __getattr__(self, name):
        try:
            inner = self.__dict__["inner"]
        except KeyError:
            raise AttributeError(name)
        return getattr(inner, name)


class _ReplayRaw:
    """Hands back the recorded chunks, in the recorded shape."""

    def __init__(self, pieces, realtime=False):
        self.pieces = list(pieces)
        self.realtime = realtime
        self._left = list(pieces)
        self.decode_content = True
        self.closed = False

    def stream(self, amt=None, decode_content=True):
        prev = 0.0
        for offset, chunk in self.pieces:
            if self.realtime and offset > prev:
                _rt.sleep((offset - prev) / 1000.0)
                prev = offset
            yield chunk
        self._left = []

    def read(self, amt=None, decode_content=True, **kw):
        data = b"".join(c for _, c in self._left)
        self._left = []
        return data

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


def _response(status, headers, request, pieces, realtime=False):
    import requests
    from requests.structures import CaseInsensitiveDict

    r = requests.Response()
    r.status_code = int(status or 0)
    r.headers = CaseInsensitiveDict(headers or {})
    r.url = str(request.url)
    r.request = request
    r.reason = ""
    r.raw = _ReplayRaw(pieces, realtime)
    try:
        from requests.utils import get_encoding_from_headers
        r.encoding = get_encoding_from_headers(r.headers)
    except Exception:
        pass
    return r


def _record(orig, adapter, request, kwargs, rec):
    body = _body(request)
    t0 = _rt.monotonic()
    url = str(request.url)
    try:
        resp = orig(adapter, request, **kwargs)
    except Exception as exc:
        step = transport._open_step(request, url, body, 0, None, t0, rec,
                                    error=type(exc).__name__)
        transport._close_step(step, b"", [], t0)
        rec.add(step)
        rec.trigger("exception:" + type(exc).__name__)
        raise
    step = transport._open_step(request, url, body, resp.status_code,
                                resp.headers, t0, rec)
    rec.add(step)
    resp.raw = _RecordRaw(resp.raw, step, t0, rec)
    return resp


def _replay(tr, request):
    import requests

    body = _body(request)
    url = str(request.url)
    step = tr.claim(request.method, url, body)
    if step is not None:
        tr.note_hit(request, url, body, step)
        if step.get("error"):
            raise requests.exceptions.ReadTimeout(step["error"])
        return _response(step.get("status"), step.get("headers"), request,
                         transport.split_chunks(step), tr.realtime)
    tr.note_miss(request, url)
    payload = json.dumps({"Orientim": "divergence",
                          "url": transport.redact(url)}).encode()
    return _response(599, {"content-type": "application/json"}, request,
                     [(0.0, payload)])


def _send(self, request, **kwargs):
    info = _pick() if _pick is not None else None
    orig = _orig["send"]
    if info is None:
        return orig(self, request, **kwargs)
    if info["mode"] == "record":
        return _record(orig, self, request, kwargs, info["rec"])
    return _replay(info["sync"], request)


def install(pick):
    """Patch HTTPAdapter.send, if requests is installed. Returns whether it was."""
    global _pick
    try:
        from requests.adapters import HTTPAdapter
    except ImportError:
        return False
    if "send" in _orig:
        return True
    _pick = pick
    _orig["adapter"] = HTTPAdapter
    _orig["send"] = HTTPAdapter.send
    HTTPAdapter.send = _send
    return True


def uninstall():
    global _pick
    if "send" not in _orig:
        return
    _orig["adapter"].send = _orig["send"]
    _orig.clear()
    _pick = None
