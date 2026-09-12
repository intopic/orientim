# MCP over HTTP — the profile contract

A contract, and the measurements behind it. **No production implementation.**
Nothing in Orientim changes: no recording format, no chain, no field, no
matcher, no cursor, no lookup key, no evaluator and no gate. The reader used
throughout is `orientim.rpc` at `SCHEMA 2`, unmodified.

Reproduce every number here with:

```
python lab/mcp_profile.py
```

## 0. The revision this profile reads

A profile that does not name a revision will silently read the wrong one, so
the revision is pinned and it is **measured, not assumed**:

```
spec revision   2025-11-25   (mcp sdk 1.26.0, LATEST_PROTOCOL_VERSION)
no revision     2025-03-26   (DEFAULT_NEGOTIATED_VERSION)
```

Two things that statement is careful not to say. It does not say 2025-11-25 is
the newest revision that exists — it says it is the newest the installed SDK
names, and that is all this machine can establish. And the second line is not
a detail: a peer that declares no revision is treated as speaking a *different*
string, so "no version was declared" and "version 2025-11-25" are two readings
and a profile may not merge them.

**The rule that follows.** The profile reads a recording under the revision the
recording itself declares — `protocolVersion` in the `InitializeResult`, or the
`MCP-Protocol-Version` header. When neither is present in scope, the answer is
`version_undeclared`, and every fact whose meaning depends on the revision is
NOT DETERMINED. The profile never falls back to the revision it was written
against: that would be reading this machine's SDK into someone else's
recording.

## 1. Three reading paths, and they are not one

MCP's Streamable HTTP transport answers a POST in either of two ways, and
opens a third channel of its own. They fail separately, and from a distance the
failures look alike — so the profile keeps them apart by construction.

```
  path                   content-type                sent    recv    framed pairing
  a JSON response        application/json               1       1         - transport, then the id
  an SSE response        text/event-stream              1       0         1 the id alone
  an SSE stream (GET)    text/event-stream              0       0         2 none in scope
  an SSE stream, cut     text/event-stream              1       0         0 none: a loss
```

`recv` is what `rpc` reads out of the response body today. `framed` is what SSE
framing finds in the same bytes. The gap between those two columns is the whole
of what an MCP profile would have to build.

**A JSON response.** One exchange, one body, and the transport has already
paired the request with the response before any `id` is read. The id is then a
*second, independent* claim about a link that already exists — which is why
these come out `CORROBORATED` and not merely `BY_ID`.

**An SSE response.** One exchange, and the transport's pairing is gone: the
body is a stream of records, each of which may be a message, and a response
inside it is attached to the request by its `id` alone. `rpc` reads **zero**
messages from it today, because it reads a body as one JSON document and an
event stream is not one.

**Links beyond one exchange.** A server request arrives on the GET stream and
the client answers it in a *separate* POST. Nothing inside either exchange
pairs them. This is the one place a profile is tempted to guess, and the
contract forbids it: see §4.

## 2. The table

fact → evidence needed → evidence that exists → limit → measured result.

| fact | needs | has | limit | measured |
|---|---|---|---|---|
| a session was initialized | an `initialize` request linked to its `InitializeResult` | both bodies, and an rpc link inside one exchange | the link holds inside one HTTP exchange and nowhere else | **CLOSED** — `CORROBORATED` |
| the protocol revision in force | `protocolVersion` in the `InitializeResult` | the response body, and the client's ask in the request body | what the server *stated*, not what either side then did | **CLOSED** — asked `2025-11-25`, answered `2025-11-25` |
| the session identifier the server issued | `Mcp-Session-Id` on the initialize response | stored response headers | plaintext unless session pseudonymisation is on | **CLOSED** — `'mcp-sess-0f1e2d3c4b5a6978'` |
| a later request belonged to that session | `Mcp-Session-Id` on the **request** | `hdr_fp` only: a 16-character truncated digest over all request headers | not extractable, and not comparable on its own | **NOT DETERMINED** — recoverable=False |
| the client declared itself initialized | a `notifications/initialized` message in a request body | the request body, read as a notification | `202` is the POST accepted, not the notification processed | **CLOSED** — `notification`, `NOT_CONFIRMABLE`, answered 0 |
| a tool invocation was requested | a `tools/call` request carrying `params.name` | the request body | a request was *sent*; not that a tool ran | **CLOSED** — 4 of 4 answered |
| an invocation returned a result | a linked response carrying `result` | an rpc link in `CONFIRMED` | the result is the server's text, not the world's state | **CLOSED** — `CORROBORATED` ×4 |
| a tool error, apart from a protocol error | `result.isError` true, versus a JSON-RPC `error` member | both response bodies | two different facts; merging them loses which one failed | **CLOSED** — `isError=True`, `rpc_error=False` |
| a message carried in an event stream | SSE framing, then each `data` payload read as a message | the raw body is stored; framing lives in `model`, not in `rpc` | `rpc` reads a whole SSE body as one JSON document | **OPEN** — rpc reads 0, envelope `malformed`; framing finds 1 → `response_result` |
| the stream carried every message it was going to | a framing terminator, or a declared loss | `model._sse_frames` losses | MCP has no `[DONE]`; a stream ends when the response ends | **CLOSED as a loss** — `unterminated: 1, first_at: 0` |
| the server sent a request of its own | a request message in the received direction | `rpc` keeps it, and refuses to pair it | nothing inside that exchange can answer it | **CLOSED as unlinked** — rpc reads 0; framing finds `notifications/tools/list_changed`, `sampling/createMessage` → `notification`, `request` |
| the client answered that request | the request and the response joined across two exchanges | both bodies exist; the reader's scope is one exchange | needs a run-level linker, and stream identity to scope it | **NOT DETERMINED** — `response_result`, `UNLINKED`, answered 0, `out_of_scope_direction` |
| the stream was resumed where it stopped | `Last-Event-ID` on the request, and `id:` fields in the stream | the stream ids are in the body; the request header is not | `hdr_fp` changes, and never says which header changed | **NOT DETERMINED** — ids `e1`, `e2` readable; header readable=False; hdr_fp differs=True |
| the order of messages within a session | stream identity, and a sequence inside it | the step index `i`, which is per recording | two streams in one recording interleave by step, not by stream | **NOT DETERMINED** — no stream identity is stored |

Five closed, one closed as a loss, one closed as unlinked, one open with the
missing piece named, and four NOT DETERMINED with the reason in each row.

## 3. The evidence gap, named once

Three of the four NOT DETERMINED rows have the same cause, and it is worth
stating on its own because it decides what is buildable without touching the
recording format:

> **Request headers are not stored.** A step keeps `hdr_fp`, a truncated
> sha256 over the request headers minus `HEADER_DENY`. The response headers
> *are* stored, minus the deny list.

So the session id the **server issued** is readable, and the session id a
**later request claimed** is not. Neither is `Last-Event-ID`, nor
`MCP-Protocol-Version` as the client sent it. And `hdr_fp` cannot substitute:
it is one digest over all headers together, so a change in it says *some*
header changed and never which, and equality of it requires every other header
to be equal too. Using `hdr_fp` as a session-equality test would be exactly the
mistake the JSON-RPC contract already refuses for credentials — a generic
digest pressed into service as an identity mechanism.

That gap is not closed here, and closing it would be a recording-format change,
which is out of this scope.

## 4. What the profile must refuse

- **No link without an id on both sides.** `CONFIRMED` stays `CORROBORATED` or
  `BY_ID`, exactly as the JSON-RPC contract defines it. An MCP method name is
  never a link.
- **No link across exchanges by proximity.** "The next POST after the stream"
  is not evidence. Without stream identity, a response in a later request body
  stays `UNLINKED` with `out_of_scope_direction`, which is what it measures as
  today.
- **No session correlation from `hdr_fp`.** See §3.
- **No revision fallback.** Undeclared is `version_undeclared`, never the
  revision this profile was written against.
- **`tools/call` is not a tool run.** The wire shows an invocation was
  *requested* and a result was *returned*. That the tool executed, that it
  changed anything, or that its answer is true, is not in the bytes.
- **A `[DONE]`-style terminator does not exist here.** A stream that ends
  without one is not thereby truncated, and a stream that ends is not thereby
  complete. Completion is the HTTP response ending; anything more is a claim
  the transport cannot support.
- **A loss is never erased by a later message.** The `unterminated: 1` above is
  a hole, and nothing read after a hole enumerates over it.

## 5. Counterexamples, each recorded rather than assembled

Four were asked for, and all four are in the run:

**A notification with no response.** `notifications/initialized` → `202`, and
an empty stored body. Read as `notification` → `NOT_CONFIRMABLE`, answered 0.
The profile must not read `202` as *the notification was processed*: it is the
POST being accepted, and nothing else.

**An interrupted SSE.** The server stops inside a record — no blank line, so
the event never ends. Framing reports `unterminated: 1, first_at: 0`: zero
events dispatched, one payload arrived and unread. The response still carried
HTTP 200.

**A message in the reverse direction.** The GET stream carries
`notifications/tools/list_changed` and a `sampling/createMessage` **request
from the server**. `rpc` reads 0 messages from the raw SSE body; after framing,
both are readable, and the request is kept as a request that nothing in this
exchange can answer.

**A response arriving in a different exchange.** The client POSTs
`{"id":"srv-1","result":{…}}`. Read on its own it is a `response_result` in the
*sent* direction: `UNLINKED`, answered 0, finding `out_of_scope_direction`.
That is the correct reading, and it is also the ceiling — the request it
answers is in another exchange, and joining them needs §3.

## 6. Decisions this contract does not take

Each has more than one viable design, so each is left for a separate order.

1. **Where SSE→messages lives.** A framing step inside `rpc`, a separate `mcp`
   module composing `model._sse_frames` with `rpc.read_message`, or a shared
   framing primitive both call. The measurement says the pieces exist; it does
   not say which of the three should own them.
2. **Whether a run-level linker exists at all,** and if so what scopes it.
   Without stream identity, its only honest scope is *the whole recording*,
   with duplicate ids inside it reported as `AMBIGUOUS` rather than resolved —
   which may be worth less than not having it.
3. **Whether the recording should store selected request headers.** It is the
   one change that would close three rows of the table at once, and it is a
   format change with privacy consequences (`Mcp-Session-Id` in plaintext is
   already an open finding).
4. **Whether MCP evidence gets a claim vocabulary at all** — the same question
   left open for `rpc_*` — or stays read-only evidence beside the verdicts.

## 7. Untouched, and verified untouched

`transport.py`, `chain.py`, `diff.py`, `evaluate.py`, `ci.py`, `cases.py`,
`store.py` and `rpc.py` are unchanged by this work. The lab adds one file and
reads recordings it makes itself.
