# MCP over HTTP — the profile contract

A contract, and the measurements behind it. **No production implementation.**
Nothing in Orientim changes: no recording format, no chain, no field, no
matcher, no cursor, no lookup key, no replay semantics, no evaluator, no gate,
no run-level linker and no new claim. The reader is `orientim.rpc` at
`SCHEMA 2`, unmodified.

Reproduce every number here with:

```
python lab/mcp_profile.py
```

## 0. The revision this profile reads

```
spec revision    2025-11-25   pinned here, explicitly
the sdk says     2025-11-25 latest, 2025-03-26 default   (mcp 1.26.0) agrees=True
undeclared       2025-03-26   is what a peer declaring none gets
```

The revision is **pinned in the source**, not taken from the installed SDK.
Reading it from `LATEST_PROTOCOL_VERSION` would make the experiment mean
something different after an upgrade, silently — the one thing a profile may
not do about a version. What the SDK says is measured beside it and reported;
today they agree, and the day they do not, the run will say so instead of
drifting.

The second line is not a detail. A peer that declares no revision is treated
as speaking a **different string**, so *no version was declared* and *version
2025-11-25* are two readings and this profile never merges them.

**The rule.** The profile reads a recording under the revision the recording
itself declares — `protocolVersion` inside an `InitializeResult`, or the
`MCP-Protocol-Version` header. When neither is in scope the answer is
`version_undeclared`, and every fact whose meaning depends on the revision is
NOT DETERMINED. The profile never falls back to the revision it was written
against: that would be reading this machine's SDK into someone else's
recording.

## 1. Three layers, and the seams between them

```
framing      text in, records out. No JSON, no [DONE], no model conventions
parsing      each record's payload goes to the strict parser AS TEXT
profile      MCP vocabulary over what those two produced, and nothing else
```

**Framing is a transport concern and nothing else.** `lab/sse_framing.py`
knows about line endings, fields, comments and blank lines. It does not know
that a payload is JSON, that a particular string ends a stream, or that a
record must parse into an object to have arrived. Two conventions it
deliberately lacks, both of which live in `orientim.model` because model
providers earned them there: a `[DONE]` terminator, which is a model-API
convention and is not in SSE or in MCP; and JSON parsing inside the framer,
which would merge *this did not parse* with *this did not arrive* and lose
which one failed.

**The payload crosses as text.** Each record's `data` goes to
`rpc.parse_envelope` as a string, so the strict reader is the one that decides:
a repeated key is refused rather than silently collapsed to its last value, and
a number keeps the decimal it was written with rather than whatever a float
rounded to. Both are measured below, in §2, and neither is reachable by a
framer that parses on its own.

**The profile says only what those two produced.** No fact is assembled from
the shape of an exchange, the order of steps, or a method name.

## 2. The reading paths, and they are not one

```
  path                     content-type          sent  recv  records  paired links
  a JSON response          application/json         1     1     None    True CORROBORATED
  an SSE response          text/event-stream        1     1        1   False BY_ID
  an SSE stream (GET)      text/event-stream        0     2        2   False NOT_CONFIRMABLE,UNLINKED
  an SSE stream, cut       text/event-stream        1     0        0   False UNENUMERATED
  an SSE closed early      text/event-stream        1     1        1   False NOT_CONFIRMABLE,UNLINKED
  an SSE with a dupe key   text/event-stream        1     0        1   False UNLINKED
  an SSE id, no data       text/event-stream        1     1        1   False BY_ID
```

The `paired` column is the whole difference, and the `links` column is what it
costs. Over a JSON body the transport paired the request with the response
before any id was read, so the id is a *second, independent* claim about a link
that already exists: `CORROBORATED`. Over a stream the transport paired
nothing, and the same agreement of ids buys only `BY_ID`.

Two rows apart from each other on purpose:

- **cut** — the stream stopped inside a record. The record never ended, was
  never dispatched, and may have held the response. A framing loss is carried
  into the linking as lost enumeration, so the request is `UNENUMERATED`: *a
  candidate nobody read is a candidate nobody ruled out.* Reporting `UNLINKED`
  there would claim no response carried this id, which the recording cannot
  support.
- **closed early** — the stream closed cleanly at a record boundary, with zero
  losses, and the response simply never came. That request is `UNLINKED`, and
  it means what it says: no response *in this exchange*.

## 3. Counts, by method

```
  method                           requests notifications answered links
  initialize                              1             0        1 CORROBORATED x1
  notifications/initialized               0             1        0 NOT_CONFIRMABLE x1
  tools/call                              2             0        2 CORROBORATED x2
  tools/list                              1             0        1 CORROBORATED x1
```

A response carries no method of its own, so it is counted under the method of
the request it was **linked** to. A response linked to nothing has no method to
be counted under and lands in `(no request in scope)`, rather than being
attributed to whichever request happened to be nearby. Position is never read.

## 4. The matrix

fact → evidence needed → evidence that exists → limit → measured result.

Every fact is about **one exchange and its stored representation**. There is no
row here called *the session is initialized*, *the protocol version in force*,
or *the tool ran*.

| fact | needs | has | limit | measured |
|---|---|---|---|---|
| an initialize request and a result were exchanged here | both messages, and a link between them in this exchange | both bodies; the transport paired them and the ids agree | one exchange. Not a claim that a session exists, or that either side went on to use it | **CLOSED** `CORROBORATED` |
| the revision this InitializeResult stated | `protocolVersion` inside the result of this response | the response body, and the client's ask in the request body | what the server wrote here. Not a revision in force, and not a revision for any other exchange | **CLOSED** asked `2025-11-25`, stated `2025-11-25` |
| an InitializeResult that states no revision | the same field, and the willingness to find it absent | the response body | a well-formed JSON-RPC result is not an InitializeResult, and no revision may be supplied from elsewhere | **CLOSED as undeclared** link `CORROBORATED`, result keys `['capabilities','serverInfo']`, version `None`, serverInfo an object `False` |
| a session identifier appeared in this response | `Mcp-Session-Id` among the stored response headers | stored response headers | the stored representation: plaintext, or a token if pseudonymisation was on. Not proof the client then used it | **CLOSED** `'mcp-sess-0f1e2d3c4b5a6978'` |
| a later request belonged to that session | `Mcp-Session-Id` on the request | `hdr_fp` only: a 16-character digest over all request headers | not extractable, and not comparable on its own | **NOT DETERMINED** recoverable=False |
| an initialized notification was sent in this exchange | the message in a request body | the request body, read as a notification | `202` is the POST accepted. Not the notification processed, and not a lifecycle completed | **CLOSED** `NOT_CONFIRMABLE`, answered 0 |
| a tools/call request was sent in this exchange | a `tools/call` request carrying `params.name` | the request body | a request was sent. Not that a tool ran | **CLOSED, by method** `{initialize: 1, tools/list: 1, tools/call: 2}` |
| a response carrying result was linked to it here | a link in `CONFIRMED` inside this exchange | `rpc.correspond` over this exchange's messages | the result is the server's text, not the world's state | **CLOSED, answered by method** `{initialize: 1, tools/list: 1, tools/call: 2}` |
| a tool error, apart from a protocol error | `result.isError` true, versus a JSON-RPC `error` member | both response bodies | two different facts; merging them loses which one failed | **CLOSED** `isError=True`, `rpc_error=False` |
| a message carried in an event stream | framing, then the payload read as text by the strict parser | `lab/sse_framing.py`, then `rpc.parse_envelope` | the transport paired nothing here: the id is the only link | **CLOSED** 1 record → `ok`, `BY_ID`, answered 1 |
| a stream payload with a repeated key | a parser that refuses two readings rather than choosing one | `rpc.parse_envelope` over the record's text | a lenient parser keeps the last value and reports nothing | **CLOSED as refused** parses `duplicate_keys`, messages 0, `UNLINKED`, answered 0 |
| a stream payload whose id is written another way | exact decimal equality from the text, never through a float | `rpc.ids_equal` over values parsed as `Decimal` | equality is of the stored representation | **CLOSED** `100` vs `1E+2` → `BY_ID`, answered 1; `0.1` vs its float neighbour → `UNLINKED`,`UNLINKED`, answered 0 |
| a record that carried an id and no data | framing that dispatches nothing, and keeps the id | `lab/sse_framing.py` losses | an empty record is not an empty message; inventing one adds a message the stream never sent | **CLOSED** `no_data=3`, records 1, `last_id='q4'`, `BY_ID` |
| a stream that stopped inside a record | a framing loss, declared, and carried into the linking | framing losses, consumed as enumeration | HTTP 200 says the response ended, not that it was complete. A record that arrived unread is a candidate nobody ruled out | **CLOSED as a loss, not an absence** `{unterminated: 1}`, records 0, `UNENUMERATED`, answered 0 |
| a stream that closed cleanly before the response | framing losses at zero, and still no response message | the framed records, and the correspondence over them | a clean close is not an answer. Absence here is absence in this exchange and nowhere wider | **CLOSED as unanswered** losses `{}`, records 1, `NOT_CONFIRMABLE`,`UNLINKED`, answered 0 |
| the server sent a request of its own | a request message in the received direction | framing, then rpc keeps it and refuses to pair it | nothing inside that exchange can answer it | **CLOSED as unlinked** `notifications/tools/list_changed`, `sampling/createMessage` → `NOT_CONFIRMABLE`,`UNLINKED` |
| the client answered that request | the two joined across two exchanges | both bodies exist; the reader's scope is one exchange | needs a run-level linker, and stream identity to scope it | **NOT DETERMINED** `response_result`, `UNLINKED`, answered 0 |
| two clients that both minted id 1 | something outside the id to tell the two apart | two exchanges, each internally consistent | an id is unique inside an exchange, and not inside a recording | **MEASURED, AND NOT A LINK** shared ids `['1']`, each exchange `CORROBORATED` |
| the stream was resumed where it stopped | `Last-Event-ID` on the request, and `id:` fields in the stream | the ids are in the body; the request header is not | `hdr_fp` changes, and never says which header changed | **NOT DETERMINED** `last_id='e3'`, header readable=False, fp differs=True |
| the order of messages within a session | stream identity, and a sequence inside it | the step index `i`, which is per recording | two streams in one recording interleave by step, not by stream | **NOT DETERMINED** no stream identity is stored |

Twenty rows: fifteen closed with the scope stated — ten plainly, and five
qualified as a loss, a refusal, an absence, an undeclared version or an
unlinked message — one measured and explicitly not a link, and four NOT
DETERMINED with the reason in the row.

### What the three new stream rows are worth

**Repeated keys.** `{"jsonrpc":"2.0","id":7,"result":{"a":1},"result":{"a":2}}`
is a payload `json.dumps` cannot produce, so the lab server writes it out as
text. A lenient parser keeps `{"a":2}` and reports nothing; the strict parser
answers `duplicate_keys` — *two readings, and neither is chosen* — and the
record yields no message. This is only reachable because framing hands the
payload over unparsed.

**Decimals.** The server answers id `100` by writing `1E+2`. Exactly the same
number, so `BY_ID` and answered 1. It answers id `0.1` by writing
`0.1000000000000000055511151231257827`, which a reader going through `float`
merges with `0.1`; read as exact decimals they are two ids, and nothing is
linked. Both directions matter: the first must not be a miss, and the second
must not be a match.

**A record with an id and no data.** The framing rules dispatch nothing for a
record that carries no `data` field, or whose data fields come to the empty
string — and the `id` such a record carried still sets the last event id. The
stream measured ends on `id: q4` with no data: three no-data records, one
message, and `last_id='q4'`. A framer that turned those into empty messages
would have invented three messages the stream never sent, and a framer that
dropped their ids would resume from the wrong place.

## 5. What the profile must refuse

- **No lifecycle claim.** `initialize` + `InitializeResult` in one exchange is
  that exchange, not a session. `notifications/initialized` accepted with
  `202` is a POST accepted, not a notification processed. There is no
  measurement here for *the session is usable*, and the profile does not offer
  one.
- **No effective version.** Only *the revision this result stated* and *the
  revision this response's header declared*. Whether it governs any later
  exchange needs the request headers, which are not stored (§6).
- **No correspondence across exchanges.** Without stream identity, "the next
  POST after the stream" is not evidence. A response in a later request body
  stays `UNLINKED` with `out_of_scope_direction`.
- **No session correlation from `hdr_fp`.** See §6.
- **No link without an id on both sides.** `CONFIRMED` stays `CORROBORATED` or
  `BY_ID`. A method name is never a link.
- **`tools/call` is not a tool run.** The bytes show an invocation was
  requested and a result was returned.
- **No `[DONE]`, and no completeness from a clean close.** A stream that ends
  without a terminator is not thereby truncated, and a stream that ends is not
  thereby complete.
- **A loss is never erased by a later message**, and never silently downgraded
  to an absence: the `cut` row is `UNENUMERATED`, not `UNLINKED`.

## 6. The evidence gap, named once

> **Request headers are not stored.** A step keeps `hdr_fp`, a truncated
> sha256 over the request headers minus `HEADER_DENY`. The response headers
> *are* stored, minus their own deny list.

So the session id the **server issued** is readable, and the session id a
**later request claimed** is not. Neither is `Last-Event-ID`, nor
`MCP-Protocol-Version` as the client sent it. `hdr_fp` cannot stand in: it is
one digest over all headers together, so a change says *some* header changed
and never which, and equality of it requires every other header to be equal
too. Pressing it into service as a session-equality test would be the same
mistake the JSON-RPC contract already refuses for credentials.

Three of the four NOT DETERMINED rows have this one cause. Closing it would be
a recording-format change, which is out of this scope.

## 7. One question the measurement raised

A response body that is **present and unreadable** and one that **never
arrived** currently produce the same link state, and they are not the same
fact:

```
cut mid-record   framing loss -> enumeration lost -> UNENUMERATED
duplicate keys   parsed and refused             -> UNLINKED
```

`rpc.parse_envelope` reports `duplicate_keys` and `malformed` with
`enumerated: True`, so the request reads `UNLINKED`, whose finding says *no
response in this exchange carries this id*. With a payload that has two
readings, the recording cannot support that: a response may well have carried
it. The envelope's parse state sits beside the link and a reader who looks at
both is not misled — but the link state alone over-claims.

This is reported, not fixed. It is a question about `orientim.rpc`, not about
the profile, and it has at least two viable answers: an unreadable body drops
enumeration the way an unread one does, or `UNLINKED`'s finding is narrowed to
say *no readable response*. Neither is taken here.

## 8. Decisions this contract does not take

1. **Where SSE framing meets message reading in production.** `lab/sse_framing.py`
   shows the seam works; it does not say whether the shipped version belongs
   in `rpc`, in a separate `mcp` module, or in a framing primitive that
   `model` also moves onto.
2. **Whether a run-level linker exists at all.** Without stream identity its
   only honest scope is the whole recording, with repeated ids reported
   `AMBIGUOUS` rather than resolved — which may be worth less than not having
   it. The two-clients row measures exactly that cost: one recording, two
   exchanges, the same id `1` in both.
3. **Whether the recording should store selected request headers.** The one
   change that would close three rows at once, and a format change with
   privacy consequences.
4. **Whether MCP evidence gets a claim vocabulary at all**, or stays read-only
   evidence beside the verdicts.
5. **§7's question**, above.

## 9. Untouched, and verified untouched

`transport.py`, `chain.py`, `model.py`, `diff.py`, `evaluate.py`, `ci.py`,
`cases.py`, `store.py` and `rpc.py` are unchanged by this work; `git diff` over
`orientim/` and `tests/` is empty. The lab adds two files and reads recordings
it makes itself.
