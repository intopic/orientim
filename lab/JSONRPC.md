# JSON-RPC 2.0 over HTTP: what a captured exchange proves

`python lab/jsonrpc.py`

Nothing in Orientim changes to produce this. No format, no chain, no field, no
matcher, no cursor, no replay semantics — and **no id is normalised anywhere**.
The reader is lab code, written so the contract could be attacked before a line
of it reaches production. MCP is the layer after this one and is not touched.

Everything is read out of what Orientim already stores: `req` is the request
body, `body` is the response body of the same step.

---

## The asymmetry that decides the whole design

Over HTTP, a POST and its response are already paired **by the transport**,
before any id is read. So in the ordinary case the id is *not* what links
them — it is a second, independent claim about a link that already exists, and
the two can disagree.

| where the link comes from | when | what it is worth |
|---|---|---|
| `CORROBORATED` | one request, one response, in one transaction, ids equal | the transport paired them and the id agrees. The strongest thing this layer can say |
| `TRANSPORT_ONLY` | paired by the transaction, with no id to check | a pairing, uncorroborated |
| `BY_ID` | a batch, or a stream: the transport paired nothing | only as strong as the id, with every weakness below |
| `CONFLICT` | one transaction, and the ids disagree | **not** a link. A disagreement, and worth reporting as one |
| `AMBIGUOUS` | more than one candidate carries the id | no link, and no guess |
| `UNLINKED` | nothing to attach it to | no link |
| `NOT_CONFIRMABLE` | a notification | not a link that is missing — a link that cannot exist |

## 1. Telling the four apart, from the message and not its position

Straight from the spec, and structural: nothing here reads a position in an
array or an arrival order, because neither says what a message is.

```
method, and an id member          request
method, and NO id member          notification
exactly result                    response (result)
exactly error, with an int code   response (error)
both result and error             invalid
neither method, result nor error  invalid
a response with no id member      invalid
jsonrpc != "2.0"                  unsupported — not read as 2.0
```

Two details that are easy to get wrong and that the matrix pins:

- **A notification is the absence of the `id` member, not a null id.**
  `{"id": null, "method": "x"}` is a request whose id is null. The spec
  discourages that, and this layer refuses to link it either way.
- **A notification has no Response object by definition**, so an empty
  response body is the *right* shape rather than a lost one. Case 2 reads
  `NOT_CONFIRMABLE` and raises no issue; the same empty body against a request
  would be a response that never arrived.

## 2. When the id proves the correspondence, and when it does not

**Equality is by value and by type.** `7` and `"7"` are two ids. Treating
them as one is normalisation, and normalisation is how a response gets
attached to a request nobody can show it answered. (`True == 1` in Python, so
the check is explicit about booleans too.)

```
case                                 request side    links
1  one request, one response         request         CORROBORATED(7)
2  a notification                    notification    NOT_CONFIRMABLE
3  the response id changed type      request         CONFLICT('7'), UNLINKED(7)
4  the response id is null           request         UNLINKED, UNLINKED(7)
5  the response has no id member     request         UNLINKED(7)
6  result and error together         request         UNLINKED(7)
7  neither result nor error          request         UNLINKED(7)
8  an error with no code             request         UNLINKED(7)
9  the response says jsonrpc 1.0     request         UNLINKED(7)
10 a request with a null id          request         UNLINKED
11 batch, answered out of order      3 requests + 1  NOT_CONFIRMABLE,
                                     notification    BY_ID(3), BY_ID(2), BY_ID(1)
12 batch with a repeated id          request,request AMBIGUOUS(1), AMBIGUOUS(1)
13 batch, a response nobody asked    request         BY_ID(1), UNLINKED(999)
14 an empty batch                    -               UNLINKED
15 a truncated response              request         -
```

- **Case 3 is the one worth staring at.** The transport paired a request whose
  id was `7` with a response whose id is `"7"`. That is not a link with a
  cosmetic difference; it is a transaction in which the server answered an id
  nobody sent. Reported as `CONFLICT`, and the request stays `UNLINKED`.
- **Missing (cases 5, 6, 7, 8, 9):** a message that is not a valid response is
  not a weak link, it is no link. The request remains unanswered and the
  reason names which rule the message broke.
- **Null (cases 4, 10):** the spec uses `null` for an id it *could not
  determine*, so null identifies nothing — not even a request whose own id was
  null, because the two are indistinguishable.
- **Repeated (case 12):** two requests carrying id `1` in one batch are
  unresolvable by id, and inside one transaction there is nothing else to
  resolve them with. Both responses read `AMBIGUOUS`. Order is not used as a
  tie-break, because the spec explicitly allows any order.
- **Batch, out of order (case 11):** linked by id, and the notification
  correctly produces no response. Order proves nothing and is not read.
- **Batch, an unasked id (case 13):** a response whose id nobody sent is
  `UNLINKED` and reported; it does not attach to the one request present.
- **Empty batch (case 14):** an Invalid Request per the spec, answered with a
  single null-id error — which links nothing, correctly.

### The limit a per-step reader cannot cross

The MCP-over-HTTP shape that matters most is the one where the transport pairs
nothing at all: the POST is accepted with no body, and the answer arrives on a
stream some other transaction opened.

```
step 1  POST   UNLINKED(7)   no response in this transaction carries this id
step 4  GET    UNLINKED(7)   no request in this transaction carries this id
```

Both answers are right and together they are useless: the request is in one
step and its response in another, and a reader that works one step at a time
can never join them. **Linking across transactions is a different contract**
and needs three things this one does not have: a run-level view, a statement
of which stream belongs to which session, and an explicit ordering claim —
the response must be shown to have arrived after the request, which the
recorded `i`/`t0` can support for a sequential run and cannot for a concurrent
one. Not proposed here.

## 3. Incomplete, invalid and unsupported, without inventing anything

```
15 a truncated response   no message could be read from the response,
                          so nothing is linked and nothing is claimed
```

The rules, and each is a refusal to produce something:

- **Unreadable bytes** produce no message, therefore no link, therefore no
  verdict. Not an empty result: an unreadable response is not a response that
  said nothing.
- **An invalid message** is named by the rule it broke (`both result and
  error`, `an error with no integer code`) and is not counted as a response.
  The matching request stays unanswered.
- **An unsupported message** — `jsonrpc: "1.0"`, or a version this reader does
  not know — is `unsupported`, never silently read as 2.0. The distinction
  matters because *invalid* is a fact about the message and *unsupported* is a
  fact about the reader.
- **A partial link is never promoted.** There is no "probably this one". Every
  row above is `CORROBORATED`, `BY_ID`, or an explicit non-link.
- **No verdict about the agent** comes out of this layer at all. It says what
  was sent, what came back, and what is linked — not whether a tool ran.

## What this contract deliberately does not do

- **It does not normalise ids.** Not type coercion, not trimming, not case.
- **It does not touch matching or the cursor.** The lookup key stays
  `method | url | body`, which means a JSON-RPC `id` inside a request body is
  still part of the key — the finding from `lab/BOUNDARIES.md`, unchanged and
  still open. Nothing here makes a recorded call findable that was not.
- **It does not link across transactions**, as above.
- **It claims nothing about MCP.** `tools/call` is a method name to this
  layer; that it means an invocation request was sent — and not that a tool
  ran — belongs to the profile above.
- **It adds no field to a recording.** Everything measured here is read from
  `req` and `body` as they are already stored.

## Open, and to be decided before any implementation

1. **Where the reader lives.** A per-step reader answers cases 1–15 and
   nothing else. A run-level linker answers the streaming shape and needs an
   ordering claim; that is a second contract, and the sequential limit already
   declared for session identity applies to it too.
2. **What claims this evidence may feed.** `rpc_request`, `rpc_response`,
   `rpc_error`, `rpc_notification` as evidence with no claim attached is the
   smallest honest step — nothing in closure or in the evaluators changes.
   Attaching a claim needs the domain split, and that is the decision from
   `lab/BOUNDARIES.md` question 10, still open.
3. **Whether a `CONFLICT` is a finding.** It is a server answering an id
   nobody sent. This reader reports it; whether a case should refuse on it is
   a policy question, not a reading one.

---

# Contract v1 — final, and implemented as `orientim/rpc.py`

The study above became a reader. Read-side only: capture, redaction, the
recording format, the chain, the lookup keys, the matcher, the cursor, replay
semantics, the evaluators and the gates are untouched, and the module adds no
field to a recording.

## Four things kept apart, because they fail separately

| | the question | what it cannot answer |
|---|---|---|
| **validation** | is this a well-formed JSON-RPC 2.0 message | whether anybody was listening |
| **message origin** | which side sent it: the request body, or the response | what it answers |
| **correspondence scope** | *within what* a link holds — here, one HTTP exchange | anything outside that exchange |
| **observation coverage** | what the recording lets anyone see at all | what was never captured |

The third is the one that is easy to lose, so the wording is fixed: **"no
response" always means no response *in this exchange*** — never "no response
anywhere". A response delivered on a stream that another transaction opened is
out of scope, and joining the two is a run-level contract that does not exist.

## The evidence is about the stored representation

Not the wire. Capture stores a request body as `redact_body(bytes)` and a
response as `redact_response(text)`, both of which **re-serialise any JSON
they can parse**; a binary body is stored as base64. So every statement the
reader makes is a statement about the representation on disk.

Measured, not assumed: an id whose value carries `scheme://user:pass@host` is
rewritten by capture —

```
sent    {"jsonrpc": "2.0", "id": "http://user:pass@example.test/x", ...}
stored  {"jsonrpc": "2.0", "id": "http://<redacted>@example.test/x", ...}
```

— so two sides of an exchange can arrive on disk carrying the same marker and
agree there. That is `REPRESENTATION_ONLY`: they agree in the stored
representation, the representation was transformed at the field the link
depends on, and the original correspondence is **not shown**. It is not an
answer, and `answered` excludes it.

## The states, and which of them is a link

```
CORROBORATED         transport-paired, one to one, and the ids agree
BY_ID                a batch: the id is the only link there is
REPRESENTATION_ONLY  equal only after capture rewrote the field
CONFLICT             transport-paired, and the ids disagree
AMBIGUOUS            more than one candidate, on either side
UNLINKED             nothing in scope to attach it to
NOT_CONFIRMABLE      a notification: not a missing link, an impossible one
```

`answered` is `CORROBORATED` or `BY_ID`, and comes out of the correspondence
result and nothing else — not from a status, not from "a response came back",
not from a count. **Correspondence checks candidates on both sides**: asking
only "which request does this response match" hides the case that matters
most, where one request has two responses and a reader that stops at the first
hit calls it answered.

## The eight acceptance groups, closed

Every row below is a test in `tests/test_rpc.py`.

**1. Structure.** Nine broken shapes, no confirmed link: an id that is an
object, an array or a boolean; `params` as a scalar and as a string; an error
with no `message`; an error `code` that is a boolean — *a boolean is not an
Integer, however Python compares it*; an error that is not an object; a method
that is not a string.

**2. Parsing.** Five distinct states, and none collapsed into another:

```
absent | empty        ABSENT           no body stored at all
"null"                JSON_NULL        a body of literal null
'{"jsonrpc": "2.0"'   MALFORMED
'{"id": 7, "id": 8}'  DUPLICATE_KEYS   two readings, neither chosen
'"hello"'             NOT_A_MESSAGE    valid JSON, no envelope
b64 body              BINARY           not read as JSON here
jsonrpc: "1.0"        unsupported      a fact about the *message*, not the envelope
```

**3. Identity.** No equality is created by conversion. Every one of
`(7, "7")`, `(0, "")`, `(0, False)`, `(1, True)`, `(1, 1.0)`,
`(10**20, float(10**20))`, `("", None)`, `(None, None)`, `(1.5, "1.5")` is two
ids — and `(7, 7)`, `("a", "a")`, `(0, 0)`, `(1.5, 1.5)`, `(10**20, 10**20)`
are each one. A 20-digit integer is never put through a float, because a float
of it is not the id anybody sent. `null` links nothing from either side.

**4. Multiplicity.** Repeated ids in the request side, in the response side,
and in both: `AMBIGUOUS`, and `answered` is empty in all three. One request
with two responses **has no unique answer**.

**5. Batch.** Permuting the response array leaves the link set identical —
order is never read, because the spec allows any order. The envelope is
validated apart from its messages: an empty array is flagged
`empty_batch` while still parsing; a mixed batch links its request, reports
its notification as `NOT_CONFIRMABLE` and names its invalid member; a
notifications-only batch with no response body is exactly right and answers
nothing.

**6. Partial evidence.** An unreadable response does not erase a readable
request: the request survives with its method, the envelope says `MALFORMED`,
and the finding says "no response **in this exchange** — which is not the same
as no response anywhere".

**7. Direction.** A request found in a response body is a request *from the
server*; a response found in a request body is the client answering something
asked earlier. Both are kept as the messages they are, both are `UNLINKED`
inside this scope with `out_of_scope_direction`, and neither is joined to
another transaction by guess.

**8. Capture and privacy.** The `REPRESENTATION_ONLY` case above, measured end
to end through a real recording. And the evidence carries **no payload**:
`params`, `result` and `error.data` are never read out of a message, so a
secret in a tool argument cannot reach an evidence object. A method name is
kept, an id is kept — they are what a link is made of — and nothing here is
wired into a report, a baseline or a gate.

## Phase C — the reader changes nothing

One real recording, taken through storage, integrity and the reader. Before
and after, byte for byte:

```
the file's sha256            unchanged
the chain root               unchanged
the integrity self-state     unchanged
the artifact digest          unchanged
the replay verdict and roots unchanged
the evaluation results       unchanged
```

And the reading itself: two exchanges, two `CORROBORATED` links, two answered.

## Declared limits

- **One exchange, and no further.** The streaming shape — a POST accepted with
  no body, the answer arriving on a GET — reads `UNLINKED` on both steps. Both
  answers are right and together useless, and that is the honest state of the
  evidence rather than a defect to paper over.
- **The recording does not store what a run-level linker would need:** which
  stream belongs to which session, and an ordering claim strong enough to say
  a response arrived after the request. Declared, not invented.
- **The id is still inside the lookup key.** `method | url | body`, unchanged,
  so a JSON-RPC `id` still takes part in matching — the finding from
  `BOUNDARIES.md`, untouched and still open. This reader makes no recorded
  call findable that was not findable before.
- **No claim is attached to this evidence.** `rpc_request`, `rpc_response`,
  `rpc_error` and `rpc_notification` exist as readings, and no evaluator, no
  closure rule and no gate reads them. Giving them a claim needs the domain
  split, which is a separate decision.
- **MCP is the next layer.** `tools/call` is a method name here. That it means
  an invocation request was sent — and not that a tool ran — belongs above.
