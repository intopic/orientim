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

None of this can change a verdict. These fields are outside
`chain.DIGEST_FIELDS` by construction, and the suite asserts it directly by
inverting every label in a recording and requiring `IDENTICAL` anyway. A wrong
hint costs a misleading label in a report; it cannot cost a wrong answer.

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
