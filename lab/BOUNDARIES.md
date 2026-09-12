# Two measurements before a protocol layer

```
python lab/mcp_identity.py
python lab/sse_close.py
```

Nothing in Orientim changed to produce either one. No format, no chain, no
field, no matcher, no evaluator.

Both were run against `d9a823e` plus the integrity experiment (`653916c`),
with MCP protocol **2025-11-25** and the installed **mcp 1.26.0** SDK, whose
own types serialized every JSON-RPC message on the wire.

Neither experiment claims anything about tool *execution*. `tools/call` on the
wire is evidence that an invocation request was sent. It is not evidence that
the server started the tool, that the tool ran, or that anything outside
changed.

---

## Measurement 1 — what identifies a JSON-RPC call over HTTP

> **Labelling, corrected.** This measurement drives **MCP-shaped** traffic —
> the wire form and the pinned protocol version come from the MCP SDK's own
> types — and what it measures is JSON-RPC identity and Orientim's matching.
> It establishes nothing about MCP *semantics*: that `tools/call` means an
> invocation request was sent, and not that a tool ran, belongs to the profile
> above JSON-RPC and is not in evidence here. Read every "MCP call" below as
> "a JSON-RPC call in MCP's wire form".

A local MCP-over-HTTP server, `POST /mcp`, answering JSON-RPC. Not the SDK's
server: that one opens a long-lived GET stream whose lifetime would become the
experiment rather than its subject. The bodies are built by the SDK's own
`JSONRPCRequest` / `JSONRPCResponse`, so the wire shape and the pinned version
are the SDK's.

Every case has its control first.

### A — what the recording already holds

```
request_semantically_lossless     True     method, params and id all readable
request_bytes_preserved           False    redaction re-serializes JSON
response_body_recorded            True
response_semantically_lossless    True
response_bytes_preserved          False    same re-serialization
body_sha_over_stored_body         False    the digest is over the wire bytes
id_in_request / id_in_response    17 / 17
role_assigned                     tool
http_steps_recorded               4        initialize, notification, list, call
notification_recorded             True     a 202 with no id, kept as a step
```

An MCP call is already recorded well enough to interpret. Two things are not
what a reader might assume: the stored body is the *redacted re-encoding* of
the wire body, not the wire bytes; and `body_sha` is taken over the original
content, so it does not hash the text on disk.

> This refines design D in [INTEGRITY.md](INTEGRITY.md). A self-consistency
> check of the form `sha256(body) == body_sha` would fail on every response
> redaction touched. Whatever integrity design is chosen has to hash what is
> actually stored, or store what it hashes.

### B — the same call, a different request id

```
control_strict    IDENTICAL           matched 4/4
control_loose     IDENTICAL           matched 4/4
mutated_strict    UNCAPTURED_SOURCE   matched 3/4    ledger: no-match
mutated_loose     UNCAPTURED_SOURCE   matched 3/4    ledger: no-match

key strict_equal            False
key loose_equal             False
key equal_once_id_removed   True
```

Same method, same params, `id` 17 → 83, and the fixture is unreachable under
both strictness settings. Attributed rather than asserted: the two keys become
equal the moment `id` is removed from the body, so the `id` is the field doing
it. The agent got a synthetic 599 and the escape ledger recorded `no-match` —
nothing went live.

### C — can a real MCP client consume a response carrying another id

The installed SDK's own `ClientSession`, over memory streams.

```
client_asked_with_id   1
control_same_id        consumed: lookup_order -> 4471 #1
different_id           never delivered (client timed out)
correlates_by          request id (shared/session.py _response_streams)
```

Not "it errors" — it never arrives. The response is routed by id, finds no
waiting stream, and the call blocks until its deadline.

One thing this loop found along the way: `call_tool` in SDK 1.26 issues a
`tools/list` *during* the call, to validate the result against the tool's
output schema. A server that does not answer it blocks the call.

### D — a different Mcp-Session-Id

```
control_same_session     IDENTICAL         matched 5/5   headers_changed False
replayed_other_session   HEADERS_CHANGED   matched 5/5   headers_changed True

served same bytes   [False, False]     (the stored body is re-encoded)
served same json    [True,  True]      (the agent got the recorded result)
refused             False
session id in hdr_fp True
```

Three separate answers in one row. The session identity is **not** in the
lookup key, so the fixture is found. It **is** in `hdr_fp`, which is in
`chain.DIGEST_FIELDS`, so the run is reported as `HEADERS_CHANGED`. And there
is no pre-serve question about it at all: the cursor advanced through all five
steps, the agent consumed another session's recorded result, and the mismatch
was reported after the run had finished.

That is the same three-tier shape R2 was built for — a verdict that names a
mismatch is not a decision that refuses to serve one.

> **Separate finding, not part of the identity question.** The server's
> `Mcp-Session-Id` is written into the recording in plaintext, in the stored
> response headers. `_RESP_HEADER_DENY` covers `set-cookie` and
> `authorization` and does not cover it, and `redact()` is a no-op on a value
> that is not URL-shaped. Under the MCP spec a session id is what authorizes
> continued access to a session, which puts it in the same class as a cookie.
> Measured, not fixed.

### E — two calls, same method and params, different results

Recorded twice: once sequentially, once from two threads. The server returns a
different result each time, so the two calls are genuinely not interchangeable.

```
                       sequential                    concurrent
results                #4 , #5                       #6 , #7
results differ         True                          True
keys differ with id    True                          True
collide without id     True                          True
occurrence (i)         10, 13                        10, 13
actor (worker)         1, 1                          3, 5
operation field        False                         False
causal edge field      False                         False
```

Removing `id` from the fingerprint collapses two calls with different results
into one key. What is available to tell them apart: the occurrence index, the
actor, the start offset, and the recorded order. What is not available: any
operation identity, and any causal edge.

And the recorded order means two different things. Sequential, it is program
order — one worker, one after the other. Concurrent, it is the order the
responses started arriving, across two workers, and nothing in the step says
which of the two situations produced it.

---

## Measurement 2 — a stream that stopped early

Reading the recorder suggested a conflation: `_close_step` writes
`complete = True` whether the stream ended or the consumer walked away. Whether
that is also an *evidence* bug is a different question, and only a claim can
answer it.

```
row                          complete  frames  evidence  issues          verdicts
A0 control: read it all      True      4       True      -               did_not_call FAIL / used_tool PASS
A  read one chunk            True      1       False     channel_open    did_not_call UNKNOWN / used_tool UNKNOWN
B0 control: read it all      True      3       False     channel_open    used_tool PASS / did_not_call(other) UNKNOWN
B  abandon after witness     True      1       False     channel_open    used_tool PASS / did_not_call(other) UNKNOWN
C0 control: nothing torn     True      2       True      -               did_not_call PASS      <- correct
C  torn event, then [DONE]   True      3       True      -               did_not_call PASS      <- FALSE
```

**A and B: the claims are sound.** `complete = True` is transport bookkeeping
and it is misleading as a name, but the semantic layer does not read it: it
reports `channel_open`, the tool domain is incomplete, and the prohibition
answers UNKNOWN where the fully-consumed control answers FAIL. A positive
witness survives the later loss of closure — `used_tool` stays PASS on an
abandoned stream while `did_not_call` on a *different* tool is UNKNOWN on the
same recording. That is the asymmetry the evidence model is supposed to have,
and it holds.

**C is a false PASS.** A `data:` frame torn mid-JSON — one that would have
carried a `send_email` request — followed by a syntactically valid
`data: [DONE]`, produces a recording that is indistinguishable from the clean
control in every field measured: `evidence_complete = True`, no issues, no
names, and `did_not_call("send_email")` returns **PASS, was never requested**.

`_sse_frames` skips a frame it cannot parse and writes nothing down about
having skipped it, so the terminator then certifies a closure over a stream
with a hole in it. The issue vocabulary already has the words for this
(`SCHEMA_MISMATCH`, `EVENTS_TRUNCATED`, `PARTIAL_CALL`); the frame loop is
simply not reporting one.

Measured, not fixed.

---

## The twelve questions

**1. Can JSON-RPC interpretation be added read-side without a recording-format
change?** Yes. `method`, `params`, `id`, `result` are all on disk and readable
for both directions. Two qualifications, both measured: the stored body is a
redacted re-encoding rather than the wire bytes, and `body_sha` hashes the wire
bytes rather than the stored text. Neither blocks interpretation; both break any
design that assumes the stored body *is* the wire body.

**2. Does the JSON-RPC id currently break replay matching?** Yes. Control
IDENTICAL 4/4; the same call with a different id is `UNCAPTURED_SOURCE`, 3/4,
under strict and loose alike, with `no-match` in the escape ledger and a
synthetic 599 to the agent. The keys become equal once the `id` is removed.

**3. Must replay rewrite response correlation IDs?** Only if we choose to
support differing ids — this is a consequence of that choice, not an
independent obligation. Measured: the real SDK correlates strictly by request
id, and a response carrying another id is never delivered. So semantic matching
without id projection produces a client that hangs rather than a client that
diverges. Declining to support differing ids is a coherent alternative, priced
as: a replay must reproduce the recorded id sequence.

**4. Does Mcp-Session-Id affect matching, post-hoc diff, or pre-serve
mediation?** Matching: no. Post-hoc diff: yes, `HEADERS_CHANGED`, because it
is inside `hdr_fp` and `hdr_fp` is in `DIGEST_FIELDS`. Pre-serve mediation:
no — the fixture is served, the cursor advances, and the mismatch is reported
after the agent has already consumed another session's result.

**5. If id is excluded from semantic matching, what disambiguates duplicate
operations today?** Occurrence index, actor, start offset and recorded order —
and nothing else: no operation identity and no causal edges. That is enough for
the sequential case, where recorded order is program order, and it is not
enough for the concurrent case, where recorded order is arrival order across
workers and the step does not say which kind of order it is.

**6. Does MCP require a change to authoritative matching, or only a protocol
profile?** A profile is enough for interpretation, and enough for replay when
the client reproduces the same id sequence — the control proves that. Anything
more requires both a matching change and the projection from 3. Which of those
we want is a product decision; the measurements bound it, they do not settle it.

**7. Does early-close currently produce a false negative PASS?** No. The claim
answers UNKNOWN. The `complete` field is misleading as a name — transport debt,
not an evidence bug.

**8. Does a positive witness survive later loss of closure?** Yes, and the
negative claim on the same recording stays UNKNOWN. Both directions confirmed
against controls.

**9. Does malformed SSE plus a terminal marker incorrectly restore
negative-claim closure?** Yes. Confirmed false PASS, indistinguishable from the
correct PASS in every measured field.

**10. Exact smallest change required before JSON-RPC.** Nothing in capture,
nothing in the format. The smallest honest version interprets `req`/`body`
read-side into `rpc_request / rpc_response / rpc_error / rpc_notification` as
evidence **with no claim attached** — then no closure rule and no evaluator
changes. Giving that evidence a claim is a second, separable step, and it is
the step that needs the domain split.

**11. Exact smallest change required before MCP.** One requirement, and it is
small: the profile must be version-pinned, and the version must come from the
recording or be UNKNOWN. In this recording it is recoverable twice over — in
the `initialize` params and in the stored response headers — but request
headers are never stored, only hashed into `hdr_fp`, so a recording that
contains no `initialize` step and whose server does not echo the header has no
recoverable protocol version, and the profile must resolve to UNKNOWN rather
than to a guess. Separately, and before MCP is described as supported, the
plaintext session-id finding in D needs a decision.

**12. Exact smallest change required before process I/O.** **NOT DETERMINED.**
Nothing measured here touches a process boundary — no `Popen`, no stdin/stdout
framing, no exit codes, no child lifetime. Two structural facts are relevant
and neither determines the answer: a step already carries a type discriminator
`t`, and `chain.DIGEST_FIELDS` is one HTTP-shaped tuple. A per-type digest
projection is consistent with both, and calling it minimal before a
process-boundary experiment exists would be a guess.

---

## What these measurements do not establish

- One server, one client shape, one protocol version (2025-11-25), one SDK
  (1.26.0). Not exercised: a POST answered with `text/event-stream` instead of
  JSON, the legacy session handshake, batched JSON-RPC, server-to-client
  requests, or concurrency inside one MCP session.

  > Three of those were measured later, in `lab/MCP_PROFILE.md`: a POST
  > answered with `text/event-stream`, a server-to-client request on the GET
  > stream, and the client's answer to it arriving in a different exchange.
  > The sentence above stands for *this* experiment; the profile contract
  > says what those three measure out as. Batched JSON-RPC is covered by
  > `lab/JSONRPC.md`. The legacy session handshake and concurrency inside one
  > session remain unexercised.
- Cases A, B, D and E drive the wire with httpx and SDK-serialized bodies, not
  with the SDK's streamable-HTTP client. They measure Orientim's matching
  against faithful bytes, not against the SDK's connection lifecycle.
- Case C uses the real SDK client, but over memory streams. The id-correlation
  behaviour is the SDK's own; no HTTP was involved in that measurement.
- The SSE torn frame sits in the middle of a stream. Real providers usually
  tear the last frame. What was measured is that an unreadable frame leaves no
  trace at all, which does not depend on where it sits.
- No production code was changed, and no full suite was run — only these two
  labs and ruff over them.
