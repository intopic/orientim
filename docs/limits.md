# What Orientim cannot do

Everything here is known, deliberate, and checked by the test suite. If you hit
something that is not on this page, that is a bug and worth an issue.

The reason this page exists in this much detail: the product's only real claim
is that `IDENTICAL` means identical. A tool that overstates its coverage cannot
make that claim credibly, so the coverage is stated at its true size.

## Capture

### We intercept `httpx`, `httpx2` and `requests`

Sync and async, wherever the client was built. That covers the OpenAI,
Anthropic, Cohere and Mistral SDKs, most MCP servers, and anything built on
them, with no change to your code.

The hook is `HTTPTransport.handle_request` — the point every request passes
through on its way to a socket — not the client constructor. That distinction
was learned the expensive way: `openai` 3.x and `anthropic` 1.x stopped
subclassing `httpx.Client` *and* moved onto `httpx2`, a separate package with the
same transport API. A constructor hook on `httpx` saw neither. Both libraries are
instrumented now, whichever is installed; `httpx2` is optional and never becomes
a dependency of this package.

`requests` is covered as well, hooked at `HTTPAdapter.send` — the same seam one
layer down, drawing from the same recorded queue. That matters because model
traffic almost always goes through `httpx`, while *tools* — the part that
usually breaks — are often written with `requests`. A run that mixes the two
replays in one recorded order.

It does not cover `aiohttp`, `urllib`, `urllib3` used directly, or `pycurl`.

**What happens instead of silence.** During a recording, calls through `aiohttp`
and `urllib` are counted and their URLs noted. They are
not captured and cannot be replayed, but a replay of a recording holding any of
them can never report `IDENTICAL` — it reports `UNCAPTURED_LIBRARY` and names
them, and the timeline says so above the steps it does have.

Only libraries already imported when `record()` starts are watched. One imported
later is missed.

`orientim conformance` tells you which of these are installed on your machine.

### Local reads never cross the boundary

A tool that reads a file, queries a database in-process, or consults a local
cache is invisible. The replay sees today's data. Sources 15 and 18.

Detected, never prevented: the request built from that data will not match the
recording, and the replay says which request and where.

### A cache inside your framework

If your framework serves a result from its own cache during a replay, no request
is made and there is nothing to see. The replay makes fewer calls than the
recording and reports `FEWER_STEPS`. Source 19.

### Library version drift

If an upgrade changes the bytes your agent sends, the request is genuinely
different. Nothing can make it identical; the divergence is reported clearly.
Source 20.

Since format 4 the versions themselves are recorded, and a divergence report
names any that moved. That is not a fix — we cannot make a replay use the
`httpx` that recorded it — and it never counts as a divergence on its own,
because a verdict nobody can act on is not worth failing a build over.

## The execution model is inference, and can be wrong

Step typing (`role: model` / `tool`), the model metadata and the token usage are
**derived**, not observed. They are guesses about the meaning of a request, made
from its URL and shape.

They will sometimes be wrong:

- a model call through a proxy on an unfamiliar path, whose body does not look
  like a prompt, is labelled `tool`;
- an endpoint that happens to sit at `/v1/completions` and takes an `input`
  field is labelled `model`;
- token usage for a **streamed** response is recovered from the events that
  carry it, bounded at the first 400. Providers put usage in the final events,
  so it is usually there; when it is not, it is simply absent. It is never
  guessed at or reconstructed.

None of this can change the **replay verdict**. These fields are outside
`chain.DIGEST_FIELDS` by construction, and the suite asserts it directly by
inverting every label in a recording and requiring `IDENTICAL` anyway.

That is the exact scope of the claim, and it used to be written more broadly
than it is true. An **evaluator** verdict is a different thing, and `role` does
reach it: `did_not_call` looks through the responses of steps labelled `model`,
so a model call this heuristic labels `tool` is a response nobody searches. An
audit found the consequence — the same body was a violation at a path we
recognise and a silent pass at one we do not.

Since then a step that looks like inference and is not labelled `model` makes
`model_tool_calls` incomplete, so the question is withheld rather than answered
wrongly. The heuristic can still be wrong; it can no longer be wrong *and*
quiet. What it costs now is an UNKNOWN, not a false PASS.

## A replay driven by a different principal is not detected

**This is the sharpest limitation in the tool.** The lookup key is method, URL
and request body. Headers are not in it — they are covered by a separate
fingerprint that goes into the step digest — and every header that carries
principal identity is excluded from that fingerprint too:

    authorization    proxy-authorization    cookie
    x-api-key        api-key                x-goog-api-key

So a recording made as one user, replayed by another, matches. The second user
is served the first user's recorded response, and the verdict is `IDENTICAL`:

```
recorded as alice   {"user": "alice", "balance": 10}
live, as bob        {"user": "bob", "balance": 999999}
replay served bob   {"user": "alice", "balance": 10}
verdict             IDENTICAL
```

That is pinned by `t_P0_3_a_principal_change_is_not_detected` so it cannot
change without someone noticing.

The exclusion is not an oversight. A fingerprint over a bearer token turns
every credential rotation into a divergence, and every recording would break
on the next token refresh — and a hash of a secret in a file that lives in the
repository is a credential-equality oracle nobody asked for. The two things
have to be told apart:

- **secret bytes changed** — a rotation. Nothing about the run is different.
- **principal or tenant changed** — a different subject. Everything about the
  expected response is different.

Nothing in a recording distinguishes them today, because nothing in an HTTP
request does. Fixing it means the run *declaring* who it is — a label the
caller chooses, not a credential.

### Half of it is now closable, and the half that is not is the credential

That declaration exists. A run may say who it is acting as, a replay may say
who *it* is, and a contract says which of those have to agree before a
recorded response is released:

```python
with orientim.record(root="runs", context={"tenant": "acme"}) as run:
    agent(run)

orientim.replay(path, agent,
                context={"tenant": "globex"}, contract=("tenant",))
# FIXTURE_REFUSED — nothing out of the file reaches the agent
```

Before this, a replay driven as another tenant was **handed the first
tenant's response, parsed it and branched on it**, and `HEADERS_CHANGED`
arrived after the run had already finished. That is the difference this
closes: detection after the fact is not mediation, and the agent had already
acted. See [regression.md](regression.md) for the contract and its verdicts.

What it does **not** close is the sentence this section opens with. The gate
compares what the two runs *declared*; it cannot see the credential, so a
rotation and a different principal are still indistinguishable from the wire,
and a replay that declares nothing is refused rather than resolved. A
`credential` relation is named and refused on purpose: storing the value
redacted would let two different secrets collapse to one stored string and
then compare **equal**, which is a false pass manufactured by the privacy
measure. It needs a keyed local commitment, and that decision has not been
made.

So, unchanged: **do not replay a recording under a different principal and
read `IDENTICAL` as a pass.** Either declare a context and run under a
contract, keep one recording per principal, or put the identity in the request
body where the key can see it — `t_P0_3_a_body_change_is_still_detected`
proves that half works.

## A session token protects a header, and nothing wider

`session_tokens=True` replaces an `Mcp-Session-Id` with an opaque token in the
stored headers and in the fingerprint over them. Measured, and true only that
far:

- the same identifier written into a **response body**, a tool result or the
  run's declared output survives untouched. An agent that returns the session
  as its output will also report `OUTPUT_CHANGED` on every replay, because the
  replayed run reads the token;
- a session your client was **configured** with is not covered. If the server
  never echoes it, it never reaches the file at all; if it does echo it, the
  value stays in the clear, and the `transforms` block names it as
  untransformed with the reason;
- the rule is an **observation** — no earlier request carried this value — and
  not proof that the client took the value from the response. The condition is
  yours to assert by enabling the feature. It is caught when the mismatch
  reaches a captured request header, and not otherwise;
- **sequential runs only.** A step is opened when the response headers arrive,
  so with several requests in flight the rule can examine a response before
  the request that preceded it. v1 claims nothing about concurrent sessions;
- **old recordings cannot be scrubbed.** Their fingerprint was taken over the
  real value and request headers are never stored, so there is nothing left to
  recompute from.

And the comparison it costs: two recordings of one real session carry two
tokens, so a diff between them says *request headers not comparable* on the
affected steps, with a `comparison_limit` field beside it. That is uncertainty,
not equality: the steps still differ, and `hdr_fp` is one hash over every
included header, so a real change to another header cannot be ruled out.

## A declared context is not protected by the hash chain

The context a recording carries decides whether a later replay is handed its
fixtures, and it lives in the metadata, which is not chained. Editing it in
the file changes what the gate decides, and nothing notices:

```
edited a response body, alone             chain root moved: no
edited a response body and its body_sha   chain root moved: yes
edited the declared context               chain root moved: no   verdict: IDENTICAL
```

Both rows are pinned, by `t_KNOWN_LIMIT_an_edited_context_flips_the_decision`
and `t_KNOWN_LIMIT_a_recording_is_not_tamper_evident_at_rest`.

The second measurement is why the context is not bound into the chain: **a
recording is not tamper-evident at rest anywhere.** The chain is a comparison
device between a recording and a replay, not a seal on the file, and there is
nothing the file is ever checked against. Binding the context alone would make
it the one protected field of an unprotected file — which protects nobody and
reads as though it did.

What the gate is for, then, stated plainly: it stops a replay that is *wrong
about itself* — a suite re-run for another tenant that quietly receives the
first tenant's fixtures. It is not a defence against someone who can edit the
recording, who could equally delete the context or replay under the legacy
contract. The evidence kind is stored as `declared_unchained` so that nothing
downstream has to infer that.

## A replay context speaks for a whole run, not for one request

A context is stated once, and a run whose requests had different callers is
not one thing:

```
request 1 → alice
request 2 → bob
```

A single statement cannot be right about both. Until a per-request context
exists, a recording that ran on more than one worker is answered `UNKNOWN`
under a contract rather than certified by a statement that was never
per-request — pinned by
`t_a_concurrent_recording_is_not_certified_by_one_context`, with the
single-worker control beside it.

## An evaluator answers UNKNOWN more often than you might expect

Since the observation layer, a verdict is only given when the trace supports it.
A prohibition needs every model response to have arrived before it can pass; a
question about whether every call succeeded needs every call to have been
answered. When they were not, the result is a warning that names the missing
domain rather than a pass or a failure.

This is stricter than the tool used to be, and deliberately so — two evaluators
previously stated more than they could see. It means a diverged replay will
report several questions as unanswered, and those do not fail a build. The
divergence itself still does.

Three more cases answer UNKNOWN since the audit, and all three used to answer
with confidence:

- a model response in an envelope the extractor does not recognise. HTTP 200,
  bytes on disk, and no tool call comes out — because nothing knows where that
  vendor puts one, not because the model asked for none;
- a streamed response that stopped without a terminator, where the next event
  is exactly where a tool call would have been;
- `output_matches` against an answer stored as a prefix. `OK$` matches the
  stored `...OK` and does not match the `...OK ERROR` it was cut from, so
  neither a match nor a miss is a fact about the answer.

If your provider is one the extractor does not know, every tool question on
every run will be UNKNOWN. That is the honest reading of the trace and it is
also useless, so it is worth saying plainly: the fix is a shape the extractor
recognises, not a flag that turns the warning off.

An independent audit added more of these, and they are all the same shape — a
bound, a boundary, or a shape we do not know, reported instead of assumed:

- a response carrying `choices`, `content`, `output` or `candidates` with
  something other than the expected list under it;
- a stream with more events than the parser reads (400), or a response with
  more tool calls than are kept (50). The bound stays; what changed is that
  reaching it is recorded rather than silently truncating the answer;
- a stream that never said it was finished, including one where only some of
  its channels closed;
- a tool name that arrived in fragments on a stream that was then cut;
- a step that looks like inference and is not labelled `model`.

The last one deserves naming: **`did_not_call` cannot be answered on a run that
talks to an inference endpoint Orientim does not recognise.** The conservative
behaviour is all this pass claims — the classification heuristic is unchanged,
and making it *right* for arbitrary gateways is a capture-boundary question
this does not attempt.

## A diverged replay cannot see what the agent asked the model for

A tool request lives in a model *response*. When a replay diverges at a model
call, that call is unmatched, the agent receives a synthetic 599, and no
response exists for the run — so the run made no tool requests that anything
can read.

Everything downstream of that inherits it:

- `used_tool` and `did_not_call` return **warnings**, not failures: what the
  model would have asked for is unknown, and a build should not fail on a
  guess. A prohibition therefore does not hold across a divergence — the
  forbidden call is a request the replay never answered, so no tool call is
  recorded and the rule passes.
- the tool comparison in `orientim diff` is **withheld**, because every tool the
  recording asked for would otherwise compare as "removed" whatever the agent
  did. The report says which side is blind and names the tools the other side
  requested; `tool_view_unreadable` carries the same in JSON.

What survives is the request side, which is real: the model settings, the
changed fields of the request body, the step counts and the alignment. In a
four-agent lab that is enough to name the change in most cases — the plan an
agent sends is in its first request — but it is a weaker view than two
recordings, and the honest reading is *unknown*, not *unchanged*.

A run that made **no model call at all** is a different thing and is not
withheld. There the absence is the agent's doing, not the replay's — the agent
stopped asking — so comparing it is a statement about the agent and is made.
The line is between *asked and not answered* and *never asked*.

To compare tool decisions across a change, record both sides and diff two
recordings.

## The final output has to be declared

Orientim cannot see what your agent returns. `record()` is a context manager, so
the return value goes to your own variable without passing through us, and the
ways to capture it implicitly — walking stack frames, re-invoking the callable —
fail exactly where a recording most needs to be trusted.

So `run.output = ...` is a line you write. Without it there is nothing to
compare and the `OUTPUT_CHANGED` verdict cannot fire, which means a change
entirely inside your process, invisible at the HTTP boundary, will still replay
as `IDENTICAL`.

It is stored truncated at 64 KB, but the digest is taken over the whole value
before truncation, so two long answers differing only past the limit still
compare as different.

## Formats below 3 are not migrated

A format 3 recording is upgraded on read and replays normally. Format 1 and 2
stored a step digest computed a different way, so there is nothing honest to
migrate: they keep reporting `STALE_FORMAT`. See
[execution-model.md](execution-model.md).

## Determinism

### The clock and identifier shims only reach module-attribute call sites

`time.time()`, `time.time_ns()` and `uuid.uuid4()` are covered. A library that
did `from time import time` **before** `record()` started holds a reference to
the original and is not covered.

`random` is covered more broadly: the generator's state is written down at the
start of the run and restored before the replay, so `randint`, `choice`,
`uniform` and `shuffle` replay correctly as long as the code path consumes them
in the same order.

Not covered at all: `secrets`, `os.urandom`, `numpy.random`, `datetime.now()`
via a C-level clock other than `time.time`.

Every one of these surfaces as a divergence, never as a false match — but the
diagnosis may point at the wrong cause, because from the outside an unshimmed
clock looks exactly like an uncaptured local read.

### Request formatting is a judgement, not a fact

Whitespace, key order and float rounding change the bytes without changing the
meaning. There is no correct answer, so there are two keys — strict and loose —
and the report always says which one it used. Source 14, and the whole
difference between 15/20 and 16/20.

## Running it

### Concurrent runs are isolated; spawned threads are best-effort

Which recording a call belongs to is decided by a `ContextVar`, so an async
server with many requests in flight on one event loop, and a threaded server
with one request per thread, both attribute correctly. There is a regression
test for the async case, because thread-id attribution used to put four
recordings' traffic into a fifth.

The one case a `ContextVar` cannot reach is a client built inside a worker
thread you spawned yourself: a raw `threading.Thread` or a `ThreadPoolExecutor`
starts with an empty context. (`asyncio.to_thread` copies the context and is
attributed correctly.) When **one** run is open, that worker's calls are
attributed to it — a thread pool inside a single agent is captured, and there is
a test for it. When **several** runs are open at once, the worker cannot be
attributed safely, so its calls are **not captured** rather than filed under the
wrong run: a missing call surfaces as a divergence on replay, whereas guessing
would put one run's bodies and secrets into another run's file. A regression
test forces two overlapping runs and asserts one never contains a byte of the
other's traffic.

### Do not wrap a long-lived server process

`record()` patches `HTTPTransport.handle_request` — on `httpx`, and on `httpx2`
when it is installed — for the whole process. Overlapping and nested blocks are
reference-counted and the originals always go back, but while any block is open,
*every* request made anywhere in the process passes through it, including ones
belonging to requests you are not recording.

Wrap the request handler, not the server.

### What recording costs, measured

Against a local server with 4 KB responses, on one machine:

| | |
|---|---|
| CPU per call | **+0.26 ms** |
| memory held, per HTTP call | **~14–25 KB** for a 4 KB response |
| memory held, default `ring=512` | **~6.5 MB**, and it stops growing |
| retained after 300 completed runs | **+0.2 MB** — no leak |

Read the CPU figure in absolute terms. It is +25% against a 1 ms localhost
call and 0.005–0.1% against a real model call, which is the only comparison
that matters.

Memory is the number to budget. The ring is **per `record()` block**, so if you
wrap the request handler each in-flight request holds its own. A hundred
concurrent agent runs of 30 calls each is roughly 100–200 MB; the worst case,
every ring full, is about 650 MB. Size `ring=` for the longest run you expect,
not for the busiest second.

**The ring counts clock and randomness steps too.** Measured: 2,000 HTTP calls
produced about 8,000 steps, so `ring=512` keeps roughly **128 HTTP calls**. A
longer agent needs `record(ring=4000)` or it will replay as `TRUNCATED`.

A response body is held whole. An agent that downloads a 200 MB file holds
200 MB.

### A replay cannot test a path the recording never took

This is the structural limit of record/replay, not a gap waiting to be closed. A
recording answers for the path it captured. If your fix makes a request the
recording does not contain, there is no recorded answer to hand back: the request
is given a synthetic 599 and the replay reports `NEW_CALL`.

So a green replay proves **"I did not break what worked."** It does not prove
**"the new path works."** Real fixes often add a call, and that call needs a
fresh recording, not this one. Use replay as a regression gate; use a live run to
exercise anything new.

### Replay timing is not real timing

By default a replay hands back every chunk immediately — that is most of why it
is fast. Code that depends on how long a stream went quiet needs
`replay(..., realtime=True)`, which reproduces the recorded gaps.

Wall-clock durations in the timeline come from the recording, not from the
replay.

## Fidelity

### Binary and non-UTF-8 bodies

Stored as base64 and returned byte-exact. Readable in the file only as base64.

### Response headers

Returned on replay, except `Set-Cookie`, `Authorization`, `WWW-Authenticate`,
`Proxy-Authenticate` and the encoding headers (`Content-Encoding`,
`Content-Length`, `Transfer-Encoding`, `Connection`), which are dropped because
they either carry credentials or describe an encoding already undone. A
URL-valued header that survives (`Location`, above all) has credentials redacted
from the URL, the same as the request line.

Code that depends on a dropped header behaves differently under replay.

### HTTP/2, redirects, retries below httpx

Redirects are followed by httpx above the transport, so each hop is its own
recorded step and replays correctly — with one exception: if a `Location` URL
carried a credential (a query-parameter secret or `user:pass@`), it is redacted
in the stored header, so a *followed* redirect goes to the redacted URL on replay
and reports a divergence instead of `IDENTICAL`. Not leaking the credential is
worth the rare, visible divergence. Connection-level behaviour — pooling, HTTP/2
multiplexing, transport-level retries — is not modelled; a replay never opens a
connection.

### WebSockets

Not captured. `httpx` does not do them, and neither do we.

## What has and has not been exercised

The real `openai` and `anthropic` SDKs are in the suite, pointed at a local
server that speaks their protocol — so the SDK's own client, retries, headers
and SSE parser are covered. The dev dependencies are unpinned (`openai>=1.40`,
`anthropic>=0.34`), so CI exercises whatever is current on the day it runs.
Capture is installed on every httpx-shaped library present, which is what makes
that safe: `httpx`, `httpx2` (the separate package openai 3.x and anthropic 1.x
moved onto) and `requests` through its adapter.

One gap, stated because it is easy to miss: the `httpx2` check is a **no-op on a
machine where `httpx2` is not installed**, and it says so when it skips. It is
exercised wherever an SDK that depends on it is installed, and nowhere else.

**A real vendor endpoint is not covered.** Nothing here has talked to
`api.openai.com`.

Nothing has run in a real production deployment either. The concurrency shapes
are tested and the costs are measured; the mileage is not there yet, and that
is what 0.1.0 means.

## Storage

The S3 backend is tested against `moto`, not against a real bucket:
`tests/test_storage.py` covers write, read, exists, list including pagination
past the 1000-key page boundary, stat, delete and the signed URL, plus a whole
recording written to a bucket and replayed back out of it.

A mock is not a bucket. Endpoint quirks, IAM shapes, eventual consistency and
the behaviour of an S3-compatible service that is not S3 are all outside what
moto tells you. That is a real gap and it is stated rather than glossed.

This sentence used to say "tested against moto" when moto appeared in no test
at all. It is written down here because the failure mode — a document asserting
coverage that does not exist — is worse than the gap it was hiding.

## The individuals chart can hide a lone outlier

`orientim stability` reports control limits from the mean moving range. One
extreme run contributes **two** large moving ranges, the step up and the step
back down, which widens the limits enough that the run can fall inside them.
Seven runs of one step and one of six: mean 1.83, UCL 7.15, and the six is not
flagged.

That is how the chart behaves, not a defect in it, and it is why `agreement`
and the number of distinct paths are the headline numbers rather than the
limits. Asserted in `tests/test_stability.py` so it stays a known limit.

## A nested record drops calls made from worker threads

`record()` inside `record()` works, and concurrency works, and the two together
do not: a call issued from a worker thread inside a nested block is recorded by
neither run. With two regions open and no thread context, the scope refuses to
guess rather than file the call under the wrong agent.

It surfaces at replay as `NOTHING_CAPTURED`, and nothing says so at record time.
See [concurrency.md](concurrency.md), where both halves are pinned by checks.

## A replay shims the clock for the whole process

`time.time()`, `time.time_ns()`, `uuid4()` and `random.random()` are patched
process-wide while a replay runs, and attributed to the single open scope when
a thread has no context of its own. That is what makes an agent's own worker
threads replay correctly.

The cost: any *other* code in the same process that reads the clock during a
replay consumes an entry from the recording. Orientim keeps itself out of the
way — `orientim view`'s live server holds a reference to the real clock for its
own bookkeeping — but a background thread of yours that ticks while a replay is
running will show up as `UNCAPTURED_CLOCK`. Run replays in a process that is
not doing anything else.

## Honest comparison

**`vcrpy`, `betamax`, `pytest-recording`** are the closest relatives, not the
observability platforms. They are mature, widely used, and do HTTP record/replay
well. If your need is "stub HTTP in tests", use one of them.

Orientim differs in five ways, all of them about agents specifically:

| | vcrpy and friends | Orientim |
|---|---|---|
| clock, uuid, randomness | not shimmed | shimmed and replayed |
| parallel call ordering | not enforced | forced to the recorded order |
| divergence output | mismatch or error | twenty named diagnoses with next steps |
| when a replay is "the same" | request matched | request, headers, order, exceptions, and completeness |
| repeated-run variance | out of scope | `orientim stability`, control charts |

If none of those five rows is a problem you have, `vcrpy` is the better choice
and this is over-engineering.

**LangSmith, Braintrust, Arize, Langfuse** are a different category. They tell
you what happened, across many runs, with dashboards and alerts. None of them
runs it again. They compose fine with this; nothing here reads or writes
anything of theirs.

## Reproducing all of this

```bash
python -m orientim.cli conformance            # loose key, this machine
python -m orientim.cli conformance --strict   # identical bytes
pytest tests/                                 # the audit suite
```

The conformance command exits non-zero if anything we claimed to capture failed
to. The numbers in the README are asserted in
[`tests/test_suites.py`](../tests/test_suites.py), so a coverage change breaks
CI rather than quietly making the documentation wrong.

---

New to Orientim? Start with [how-to-use.md](how-to-use.md).
