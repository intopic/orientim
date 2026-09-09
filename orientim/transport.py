# -*- coding: utf-8 -*-
"""Capture at the HTTP boundary — the central architectural decision.

Not inside the model, and not at the instruction level the way rr does it.
One interception point covers the model, HTTP tools and MCP servers, which is
what makes this weeks of work rather than a research project.

Responses pass through as they arrive. Nothing here waits for a stream to
finish before handing bytes to the caller, because an agent that renders tokens
as they land must behave the same whether or not it is being recorded.
"""
import base64
import hashlib
import json
import re
import threading
import time as _rt
import urllib.parse
import httpx

from . import model

# Names that carry credentials, matched against query parameters, JSON keys and
# form fields. Their values are replaced before anything is written down. The
# lookup key is still computed from the real bytes, so redaction costs nothing
# at replay time — the hash never contained a readable secret in the first
# place.
SECRET_PARAMS = (
    "api_key", "apikey", "api-key", "key", "token", "access_token",
    "refresh_token", "id_token", "auth", "authorization", "password", "passwd",
    "secret", "client_secret", "private_key", "session_key", "sig",
    "signature", "credential", "credentials", "assertion", "code_verifier",
)
_SECRET_RE = re.compile(
    r"([?&](?:%s)=)([^&#]*)" % "|".join(re.escape(p) for p in SECRET_PARAMS),
    re.IGNORECASE)
_USERINFO_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]*@")
# The same, but not anchored: catches a `scheme://user:pass@host` sitting inside
# a JSON value or a response header, not only at the start of the request URL.
# It needs a literal `scheme://…@`, which prose does not contain, so it is safe
# to run over free text — unlike the query-parameter rule, which would rewrite an
# ordinary sentence that happened to contain "?token= ".
_USERINFO_ANY_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]*@")

# Request headers that say nothing about which response comes back, or that
# carry a credential. They are left out of the header fingerprint so a rotated
# token or a new httpx version does not read as a behaviour change.
HEADER_DENY = {
    "authorization", "proxy-authorization", "cookie", "x-api-key", "api-key",
    "x-goog-api-key", "openai-organization",
    "host", "content-length", "user-agent", "accept-encoding", "connection",
    "date", "transfer-encoding", "te", "expect",
}
# Response headers that must not be written down, or that describe an encoding
# already undone by the time the body is stored.
_RESP_HEADER_DENY = {
    "set-cookie", "authorization", "www-authenticate", "proxy-authenticate",
    "content-encoding", "content-length", "transfer-encoding", "connection",
}


def redact(url: str) -> str:
    """Strip credential-shaped query parameters and any user:password@ prefix.

    A secret inside a path segment — a Slack webhook, a Telegram bot token — is
    NOT redacted: nothing distinguishes it from an ordinary path. That is stated
    in docs/recordings.md rather than half-solved here.
    """
    return _SECRET_RE.sub(r"\1<redacted>", _USERINFO_RE.sub(r"\1<redacted>@", url))


def _redact_url_creds(s: str) -> str:
    """Strip user:pass@ from any URL sitting inside a string.

    Only the userinfo, because it is the one credential pattern URLs carry that
    has no false positive in prose. A secret in a query parameter or a path
    segment inside a body value is left alone — the same limit stated for URL
    paths in docs/recordings.md — rather than risk rewriting a prompt.
    """
    return _USERINFO_ANY_RE.sub(r"\1<redacted>@", s) if s else s


def _walk_redact(o):
    if isinstance(o, dict):
        return {k: ("<redacted>" if str(k).lower().replace("-", "_") in SECRET_PARAMS
                    else _walk_redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_walk_redact(v) for v in o]
    if isinstance(o, str):
        # A credential can hide in a URL used as a value — a callback, a webhook,
        # a next-page link — where no key name gives it away.
        return _redact_url_creds(o)
    return o


def _redact_form(text):
    """Redact credential-shaped fields of an x-www-form-urlencoded body.

    OAuth token exchange is form-encoded, so this is the single most likely
    place for a client_secret to arrive.
    """
    if "=" not in text or "\n" in text:
        return None
    # Only touch it if a credential-shaped name is actually present. Otherwise
    # an ordinary answer that happens to contain "a=1" would be parsed as a form
    # and re-encoded, quietly rewriting the text a person came here to read.
    low = text.lower()
    if not any(name in low for name in SECRET_PARAMS):
        return None
    try:
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True,
                                       strict_parsing=True)
    except ValueError:
        return None
    if not pairs:
        return None
    return urllib.parse.urlencode(
        [(k, "<redacted>" if k.lower().replace("-", "_") in SECRET_PARAMS else v)
         for k, v in pairs])


def redact_body(raw) -> str:
    """Redact credential-shaped fields, keep everything else.

    Prompts are the point of a recording and must survive intact. A field
    literally named "password" or "client_secret" is never something anyone
    needs to read back during a replay, so it is not written down. The lookup
    key is computed from the real bytes, so redaction costs nothing at replay.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")
    if not text:
        return text
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        form = _redact_form(text)
        return form if form is not None else _redact_url_creds(text)
    try:
        return json.dumps(_walk_redact(obj), ensure_ascii=False)
    except (TypeError, ValueError):
        return _redact_url_creds(text)


def redact_response(text: str) -> str:
    """The same treatment for what comes back.

    A token endpoint answers with access_token and refresh_token in the body,
    and it may answer form-encoded. Redacting only the request left both in the
    file; redacting only JSON left the form-encoded answer in the file.
    """
    return redact_body(text)


def _hdr_fp(headers) -> str:
    """A fingerprint of the request headers that can change the response.

    Stored as a hash, so no header value ever lands in the file. It goes into
    the step digest but NOT into the lookup key: a request still matches its
    recorded twin, and a request that differs only by header is reported as a
    divergence instead of silently being handed someone else's response.
    """
    items = sorted((k.lower(), v) for k, v in headers.items()
                   if k.lower() not in HEADER_DENY)
    raw = "\n".join("%s:%s" % kv for kv in items)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


# Paths whose effects are real. Replay forwards nothing at all — every response
# comes out of the file and an unmatched request gets a synthetic 599 — so this
# list only decides what the timeline *marks*. It is a label on a mechanism that
# is already closed, not the mechanism. Edit it for your own endpoints.
#
# Marking every non-idempotent method instead was tried and is worse: an LLM
# chat completion is a POST, so the warning would appear on nearly every step
# and stop meaning anything. Set MARK_ALL_WRITES if you want it anyway.
SIDE_EFFECTING = ["/send-email", "/charge", "/notify", "/webhook"]
SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")
MARK_ALL_WRITES = False


def _canon(method, url, body, strict=True):
    """The lookup key. strict=True demands identical bytes.

    strict=False normalises key order, whitespace and float rounding. This is
    the trade-off in source 14, and the only source that moves between the two
    columns. The conformance suite measures the difference instead of assuming
    one is right: 15/20 strict, 16/20 loose.
    """
    b = body or b""
    if not strict:
        try:
            obj = json.loads(b)
            b = json.dumps(_round(obj), sort_keys=True, separators=(",", ":")).encode()
        except Exception:
            b = b" ".join(b.split())
    raw = f"{method}|{url}|".encode() + b
    return hashlib.sha256(raw).hexdigest()[:32]


def _round(o, nd=6):
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: _round(v, nd) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v, nd) for v in o]
    return o


def is_side_effecting(url: str, method: str = "POST") -> bool:
    if any(p in url for p in SIDE_EFFECTING):
        return True
    return MARK_ALL_WRITES and (method or "").upper() not in SAFE_METHODS


def _body_fields(content: bytes):
    """How a response body is written down.

    Text is stored as text so a person can read the recording. Anything that is
    not clean UTF-8 is stored as base64 rather than mangled by
    errors="replace", which used to make a replayed image a different image.
    """
    try:
        return {"body": redact_response(content.decode("utf-8")), "b64": False}
    except UnicodeDecodeError:
        return {"body": base64.b64encode(content).decode("ascii"), "b64": True}


def body_bytes(step) -> bytes:
    raw = step.get("body") or ""
    if step.get("b64"):
        try:
            return base64.b64decode(raw)
        except Exception:
            return b""
    return raw.encode("utf-8")


def split_chunks(step):
    """Rebuild the recorded chunk boundaries from the stored body.

    Chunks are written down as [offset_ms, n_bytes], not as their own copies of
    the bytes: a chunk can split a UTF-8 character in half, so storing the
    pieces would force base64 and make every streamed recording unreadable.
    Sizes cost nothing and rebuild the split exactly.
    """
    raw = body_bytes(step)
    marks = step.get("chunks") or []
    if not marks:
        return [(0.0, raw)] if raw else []
    out, pos = [], 0
    for offset, size in marks:
        piece = raw[pos:pos + size]
        pos += size
        if piece:
            out.append((float(offset), piece))
    if pos != len(raw):
        # Sizes and body disagree — an edited or truncated file. One chunk is
        # wrong about timing and right about content, the safer of the two.
        return [(0.0, raw)] if raw else []
    return out


def _resp_headers(headers):
    # A redirect answers with the next URL in Location, and that URL can carry a
    # credential-shaped query parameter or a user:pass@ prefix. The step's own
    # `url` field is redacted; the response header holding the same URL was not,
    # so it is run through the same rule here. redact() is a no-op on a header
    # that is not URL-shaped.
    #
    # Trade-off, on purpose: when a client *follows* such a redirect
    # (follow_redirects=True, or code that reads response.headers["location"]),
    # a replay hands back this redacted Location, the follow-up request goes to
    # the redacted URL, and its lookup key — computed from real bytes — no longer
    # matches the recorded hop. That replays as a visible divergence rather than
    # IDENTICAL. Not leaking the secret is worth a rare, honest divergence; the
    # alternative is a live credential sitting in a file people share. A recorded
    # secret can never be un-shared; a divergence is just re-recorded.
    return {k: _redact_url_creds(redact(v)) for k, v in headers.items()
            if k.lower() not in _RESP_HEADER_DENY}


def _open_step(request, url, body, status, headers, t0, rec, error=None):
    """The half of a step that is known when the response headers arrive.

    The body half is filled in by the stream as it drains. Opening the step
    here rather than after the last byte keeps the recorded order equal to the
    order the responses actually started in, even with four calls in flight.
    """
    step = {
        "t": "http",
        "method": request.method,
        "url": redact(url),
        "key_strict": _canon(request.method, url, body, True),
        "key_loose": _canon(request.method, url, body, False),
        "hdr_fp": _hdr_fp(request.headers),
        "status": status,
        "body": "",
        "b64": False,
        "body_sha": "",
        "chunks": [],
        "complete": False,
        "headers": _resp_headers(headers) if headers is not None else {},
        "req": redact_body(body),
        "t0": t0 - rec.t0,
        "ms": 0.0,
        "side_effect": is_side_effecting(url, request.method),
        # The execution model. A hint about what this call *was*, written beside
        # the step and deliberately outside chain.DIGEST_FIELDS, so it can never
        # change what a replay decides. Derived from the real body rather than
        # the redacted one: the fields read out of it are an allowlist of
        # scalars (model, temperature, tool names) that no redaction rule
        # targets, and reading the redacted copy would only add a way to be
        # wrong.
        # Named `role`, not `kind`: a shim step already stores `kind` ("time",
        # "random"), and one field name meaning two different things in one file
        # is how a reader — or a loop over every step — gets it wrong.
        "role": model.classify(url, body, request.method),
    }
    if step["role"] == model.MODEL:
        call = model.describe_model_call(url, body)
        if call:
            step["model"] = call
    if error:
        step["error"] = error
    return step


def _close_step(step, content, marks, t0):
    fields = _body_fields(content)
    step["body"] = fields["body"]
    step["b64"] = fields["b64"]
    step["body_sha"] = hashlib.sha256(content).hexdigest()[:32]
    step["chunks"] = marks
    step["ms"] = (_rt.monotonic() - t0) * 1000.0
    step["complete"] = True
    # Token counts and the model that actually answered, which is not always the
    # one that was asked for. Only for steps already classified as model calls,
    # so the ordinary case — a tool call returning some JSON — never pays for a
    # parse it has no use for.
    if step.get("role") == model.MODEL and not fields["b64"]:
        served = model.describe_model_response(fields["body"])
        if served:
            step["served"] = served
    return step


# Stream classes are built per HTTP library, not once. The base class matters:
# httpx2.Response inspects a stream's type to tell sync from async, and refuses
# an httpx.SyncByteStream handed to an httpx2 response. openai 3.x and
# anthropic 1.x moved onto httpx2, so each library gets its own set, built
# lazily and cached.
_STREAMS = {}


class _StreamSet:
    __slots__ = ("RecordStream", "AsyncRecordStream", "ReplayStream",
                 "AsyncReplayStream")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _streams(hx):
    ns = _STREAMS.get(hx)
    if ns is not None:
        return ns

    class _RecordStream(hx.SyncByteStream):
        """Hands each chunk to the caller the moment it arrives, and notes it.

        The previous version read the whole response before returning, so an agent
        streaming tokens got all of them at once. That is a behaviour change caused
        by the observer, which is the one thing a recorder must not do.
        """

        def __init__(self, inner, step, t0, rec):
            self.inner = inner
            self.step = step
            self.t0 = t0
            self.rec = rec
            self.buf = bytearray()
            self.marks = []
            self.done = False

        def __iter__(self):
            try:
                for chunk in self.inner:
                    self.marks.append([round((_rt.monotonic() - self.t0) * 1000.0, 3),
                                       len(chunk)])
                    self.buf += chunk
                    yield chunk
            except Exception as exc:
                # A read timeout halfway through a body is a failure, not a short
                # response. Without this the step closed as a complete 200 holding
                # the bytes that had arrived, and the replay handed that back
                # successfully — the recorded run raised and the replay did not.
                self._finish(error=type(exc).__name__)
                raise
            finally:
                self._finish()

        def _finish(self, error=None):
            if self.done:
                return
            self.done = True
            if error:
                self.step["error"] = error
            _close_step(self.step, bytes(self.buf), self.marks, self.t0)
            if self.step["status"] >= 500:
                self.rec.trigger("http %d" % self.step["status"])
            if error:
                self.rec.trigger("exception:" + error)

        def close(self):
            try:
                self.inner.close()
            finally:
                self._finish()


    class _AsyncRecordStream(hx.AsyncByteStream):
        def __init__(self, inner, step, t0, rec):
            self.inner = inner
            self.step = step
            self.t0 = t0
            self.rec = rec
            self.buf = bytearray()
            self.marks = []
            self.done = False

        async def __aiter__(self):
            try:
                async for chunk in self.inner:
                    self.marks.append([round((_rt.monotonic() - self.t0) * 1000.0, 3),
                                       len(chunk)])
                    self.buf += chunk
                    yield chunk
            except Exception as exc:
                self._finish(error=type(exc).__name__)
                raise
            finally:
                self._finish()

        def _finish(self, error=None):
            if self.done:
                return
            self.done = True
            if error:
                self.step["error"] = error
            _close_step(self.step, bytes(self.buf), self.marks, self.t0)
            if self.step["status"] >= 500:
                self.rec.trigger("http %d" % self.step["status"])
            if error:
                self.rec.trigger("exception:" + error)

        async def aclose(self):
            try:
                await self.inner.aclose()
            finally:
                self._finish()


    class _ReplayStream(hx.SyncByteStream):
        """Hands back the recorded chunks, in the recorded shape.

        realtime=True also reproduces the gaps between them, for a bug that depends
        on how long a stream went quiet. Off by default: replay being fast is half
        the point of replay.
        """

        def __init__(self, pieces, realtime=False):
            self.pieces = pieces
            self.realtime = realtime

        def __iter__(self):
            prev = 0.0
            for offset, chunk in self.pieces:
                if self.realtime and offset > prev:
                    _rt.sleep((offset - prev) / 1000.0)
                    prev = offset
                yield chunk

        def close(self):
            pass


    class _AsyncReplayStream(hx.AsyncByteStream):
        def __init__(self, pieces, realtime=False):
            self.pieces = pieces
            self.realtime = realtime

        async def __aiter__(self):
            import asyncio
            prev = 0.0
            for offset, chunk in self.pieces:
                if self.realtime and offset > prev:
                    await asyncio.sleep((offset - prev) / 1000.0)
                    prev = offset
                yield chunk

        async def aclose(self):
            pass


    ns = _StreamSet(RecordStream=_RecordStream,
                    AsyncRecordStream=_AsyncRecordStream,
                    ReplayStream=_ReplayStream,
                    AsyncReplayStream=_AsyncReplayStream)
    _STREAMS[hx] = ns
    return ns


class RecordTransport(httpx.BaseTransport):
    orientim_wrapped = True

    def __init__(self, inner, rec, hx=httpx):
        self.inner = inner
        self.rec = rec
        self.hx = hx        # which HTTP library this request came through

    def handle_request(self, request):
        body = request.read()
        t0 = _rt.monotonic()
        url = str(request.url)
        try:
            resp = self.inner.handle_request(request)
        except Exception as exc:
            step = _open_step(request, url, body, 0, None, t0, self.rec,
                              error=type(exc).__name__)
            _close_step(step, b"", [], t0)
            self.rec.add(step)
            self.rec.trigger("exception:" + type(exc).__name__)
            raise
        step = _open_step(request, url, body, resp.status_code, resp.headers,
                          t0, self.rec)
        self.rec.add(step)
        return self.hx.Response(
            resp.status_code, headers=resp.headers,
            stream=_streams(self.hx).RecordStream(resp.stream, step, t0, self.rec),
            request=request, extensions=resp.extensions)


class AsyncRecordTransport(httpx.AsyncBaseTransport):
    orientim_wrapped = True

    def __init__(self, inner, rec, hx=httpx):
        self.inner = inner
        self.rec = rec
        self.hx = hx

    async def handle_async_request(self, request):
        body = await request.aread()
        t0 = _rt.monotonic()
        url = str(request.url)
        try:
            resp = await self.inner.handle_async_request(request)
        except Exception as exc:
            step = _open_step(request, url, body, 0, None, t0, self.rec,
                              error=type(exc).__name__)
            _close_step(step, b"", [], t0)
            self.rec.add(step)
            self.rec.trigger("exception:" + type(exc).__name__)
            raise
        step = _open_step(request, url, body, resp.status_code, resp.headers,
                          t0, self.rec)
        self.rec.add(step)
        return self.hx.Response(
            resp.status_code, headers=resp.headers,
            stream=_streams(self.hx).AsyncRecordStream(resp.stream, step, t0,
                                                       self.rec),
            request=request, extensions=resp.extensions)


# --- replay -----------------------------------------------------------------

def _respond(step, request, hx=httpx, realtime=False, is_async=False):
    """Rebuild the recorded response, in the library the caller asked with."""
    if step.get("error"):
        raise hx.ReadTimeout(step["error"], request=request)
    pieces = split_chunks(step)
    S = _streams(hx)
    stream = (S.AsyncReplayStream(pieces, realtime) if is_async
              else S.ReplayStream(pieces, realtime))
    return hx.Response(step["status"], headers=step.get("headers") or {},
                       stream=stream, request=request)


class ReplayTransport(httpx.BaseTransport):
    """Never touches the network. Feeds the agent its own past.

    Order is forced: a request blocks until it is next in the recorded
    sequence. This is what makes parallel calls deterministic — source 07,
    which moved three patterns from failing to passing in about forty lines.
    """

    orientim_wrapped = True

    def __init__(self, recorded, rec, strict=True, ordered=True, timeout=3.0,
                 on_step=None, realtime=False):
        self.pool = list(recorded)
        self.rec = rec
        self.strict = strict
        self.ordered = ordered
        self.timeout = timeout
        self.realtime = realtime
        self.field = "key_strict" if strict else "key_loose"
        self.cv = threading.Condition()
        self.cursor = 0
        self.on_step = on_step

    def observed(self, request, url, body, step):
        """The step the replay itself produced.

        Writing the matched step down verbatim would make the hash chain a
        tautology: a matched step carries the recorded digest, so the chain
        could only ever notice a difference in length. This records the request
        the agent made *now*, so a request that differs from the recorded one in
        a way the lookup key ignores — a header, above all — still surfaces as a
        divergence.
        """
        out = {
            "t": "http",
            "method": request.method,
            "url": redact(url),
            "key_strict": _canon(request.method, url, body, True),
            "key_loose": _canon(request.method, url, body, False),
            "hdr_fp": _hdr_fp(request.headers),
            "status": step.get("status", 0),
            "body": step.get("body", ""),
            "b64": step.get("b64", False),
            "body_sha": step.get("body_sha", ""),
            "chunks": step.get("chunks", []),
            "complete": step.get("complete", True),
            "req": redact_body(body),
            "t0": step.get("t0", 0.0),
            "ms": step.get("ms", 0.0),
            "side_effect": is_side_effecting(url, request.method),
            "orig_i": step.get("i"),
        }
        if step.get("error"):
            out["error"] = step["error"]
        return out

    def _take_ordered_nowait(self, key):
        """One attempt at claiming the head of the queue. Never blocks."""
        with self.cv:
            if self.cursor >= len(self.pool):
                return None
            head = self.pool[self.cursor]
            if head.get(self.field) == key:
                self.cursor += 1
                self.cv.notify_all()
                return head
            return None

    def _pending(self, key):
        """Is this key still somewhere ahead of the cursor?"""
        with self.cv:
            return any(s.get(self.field) == key for s in self.pool[self.cursor:])

    def _take_ordered(self, key):
        deadline = _rt.monotonic() + self.timeout
        with self.cv:
            while True:
                if self.cursor >= len(self.pool):
                    return None
                head = self.pool[self.cursor]
                if head.get(self.field) == key:
                    self.cursor += 1
                    step = head
                    self.cv.notify_all()
                    return step
                # Is it anywhere further down? If not, waiting is pointless.
                if not any(s.get(self.field) == key for s in self.pool[self.cursor:]):
                    return None
                remaining = deadline - _rt.monotonic()
                if remaining <= 0:
                    return None
                self.cv.wait(timeout=min(remaining, 0.05))

    def _take_any(self, key):
        with self.cv:
            for idx in range(self.cursor, len(self.pool)):
                if self.pool[idx].get(self.field) == key:
                    return self.pool.pop(idx)
        return None

    def claim(self, method, url, body):
        """Match one request against the recorded queue, and account for it.

        Library-agnostic on purpose. httpx, httpx2 and requests all arrive here,
        and they have to draw from the same queue or a run that mixes libraries
        replays out of order.
        """
        key = _canon(method, url, body, self.strict)
        self.rec.attempts += 1
        if is_side_effecting(url, method):
            # Source 16: the recorded run was allowed to send this once. A
            # replay must never send it again — and never does, because nothing
            # at all is forwarded.
            self.rec.blocked.append(redact(url))
        return self._take_ordered(key) if self.ordered else self._take_any(key)

    def note_hit(self, request, url, body, step):
        """Write down what the replay itself asked for, whatever library asked."""
        self.rec.add(self.observed(request, url, body, step))
        if self.on_step:
            self.on_step({"i": len(self.rec.steps) - 1, "kind": "match",
                          "url": redact(url), "method": request.method,
                          "status": step.get("status", 0),
                          "side": is_side_effecting(url, request.method),
                          "orig_i": step.get("i")})

    def note_miss(self, request, url):
        self.rec.note_uncaptured("no-match", f"{request.method} {redact(url)}")
        if self.on_step:
            self.on_step({"i": len(self.rec.steps), "kind": "divergence",
                          "url": redact(url), "method": request.method,
                          "status": 599,
                          "side": is_side_effecting(url, request.method)})

    def hit(self, request, url, body, step, hx=httpx, is_async=False):
        self.note_hit(request, url, body, step)
        return _respond(step, request, hx, self.realtime, is_async)

    def miss(self, request, url, hx=httpx):
        self.note_miss(request, url)
        return hx.Response(
            599,
            content=json.dumps({"Orientim": "divergence",
                                "url": redact(url)}).encode(),
            request=request)

    def handle_request(self, request, hx=httpx):
        body = request.read()
        url = str(request.url)
        step = self.claim(request.method, url, body)
        if step is not None:
            return self.hit(request, url, body, step, hx)
        return self.miss(request, url, hx)


class AsyncReplayTransport(httpx.AsyncBaseTransport):
    """The async twin. Ordering waits without blocking the event loop."""

    orientim_wrapped = True

    def __init__(self, recorded, rec, strict=True, ordered=True, timeout=3.0,
                 on_step=None, realtime=False):
        self.sync = ReplayTransport(recorded, rec, strict=strict,
                                    ordered=ordered, timeout=timeout,
                                    on_step=on_step, realtime=realtime)

    async def handle_async_request(self, request, hx=httpx):
        import asyncio
        body = await request.aread()
        url = str(request.url)
        key = _canon(request.method, url, body, self.sync.strict)
        rec = self.sync.rec
        rec.attempts += 1

        if is_side_effecting(url, request.method):
            rec.blocked.append(redact(url))

        deadline = _rt.monotonic() + self.sync.timeout
        while True:
            step = self.sync._take_ordered_nowait(key)
            if step is not None:
                return self.sync.hit(request, url, body, step, hx,
                                     is_async=True)
            if not self.sync._pending(key) or _rt.monotonic() > deadline:
                break
            await asyncio.sleep(0.003)

        return self.sync.miss(request, url, hx)
