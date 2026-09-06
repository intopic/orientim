# Twenty sources of non-determinism in AI agents

This is the specification Orientim is built against, and the reason it can say
what it cannot do. It is written to be useful even if you never install the
package: the taxonomy is the same whether you solve it our way, with vcrpy, or
by hand.

Every entry below exists three times over — as prose here, as an executable
probe in [`orientim/patterns.py`](../orientim/patterns.py), and as a row in the
report `orientim conformance` prints on your machine. When those three disagree,
the code is right and this file is a bug.

## The method

A source is only "captured" if a test can fail. That sounds obvious. It is not
what the first version of this suite did: two of its twenty probes could not
fail under any circumstances, and both were counted as successes for weeks.

So each entry has four parts:

| Part | What it must do |
|---|---|
| **probe** | trigger the source on purpose, through a real socket, not a mock |
| **mutation** | change the world between the recording and the replay, the way time and deploys really do |
| **expectation** | declared in advance: captured, loose-key only, or a known limit |
| **verdict** | what actually happened on this machine, today |

A probe without a mutation is only a test that the plumbing works. Sources 14,
15, 17, 18 and 20 have real mutations — the clock advances, a file changes, an
environment variable changes, a request is reformatted, a library is upgraded —
and those are the entries that tell you something.

The declared expectation is fixed in the source file, not decided from the
result. A run cannot reclassify its own failure as a limit. That is the only
reason the phrase "no undeclared failures" means anything.

## Where the twenty come from

They are not a taxonomy invented at a whiteboard. Each is a way an agent that
worked on Tuesday gives a different answer on Wednesday, grouped by the layer it
lives in.

---

## clock — 3 of 3 captured

### 01 · Time read into a request

```python
prompt = f"Today is {datetime.now():%Y-%m-%d}. What is the status of order 4471?"
```

The request bytes change every day, so the same code produces a different call
and a different answer. This is the single most common reason an agent cannot be
replayed, and it is usually invisible because the timestamp is buried in a
system prompt somebody wrote months ago.

**Captured.** `time.time()` returns what it returned during the recording.

### 02 · Expiry and TTL branching

```python
if time.time() - token_issued > 3600:
    refresh()
```

The branch taken depends on how long ago the recording was made. Replayed a week
later, the agent takes the other path and every step after it differs.

**Captured**, by the same shim. Both reads come back from the recording, so the
comparison lands the same way.

### 03 · A clock formatted into a call

```python
stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time()))
```

Same mechanism as 01, but through a formatter, which is how it usually reaches
the wire.

> This probe used to ask for `strftime("%Z", gmtime())`, which is the string
> `GMT` on every machine on every day. It could not fail. It was counted as a
> captured source anyway.

**Captured.**

---

## randomness — 3 of 3 captured

### 04 · Generated identifiers

```python
session_id = str(uuid.uuid4())
```

A fresh identifier in every request body means the request never matches the
recorded one, so nothing after it can be replayed.

**Captured.** `uuid.uuid4()` replays its recorded value.

### 05 · Model sampling

`temperature > 0` means the same prompt returns different text. Nothing you do
to your own code fixes this; the variation is on the other side of the wire.

**Captured**, and this is the easy one — the recorded response is served back.
Any HTTP record/replay tool handles it. It is in the list because it is the
source people name first, not because it is hard.

### 06 · Randomness in your own code

```python
if random.random() < 0.1:
    use_the_expensive_model()
```

**Captured.** `random.random()` replays its recorded value, and the generator's
state is written down at the start of the run and restored before the replay, so
`randint`, `choice`, `uniform` and `shuffle` replay too.

Not covered: `secrets`, `os.urandom`, `numpy.random`. Those surface as a
divergence, never as a false match.

---

## concurrency — 3 of 3 captured

### 07 · Parallel calls that return out of order

```python
with ThreadPoolExecutor(4) as ex:
    results = list(ex.map(search, queries))
```

Four calls in flight, four different network latencies, and an agent that acts
on whichever lands first. The path through the code is different every run, even
with identical inputs.

**Captured**, and this is the hard one. Replay forces the recorded arrival
order: a request blocks until it is next in the recorded sequence, so the race
resolves the same way it resolved then. Roughly forty lines that moved three of
these twenty from failing to passing.

### 08 · A timeout race

```python
try:
    return client.get(url, timeout=0.15)
except ReadTimeout:
    return fallback()
```

Whether the timeout fires depends on the network that day. Recorded once, it
must fire again on replay — or not — exactly as it did.

**Captured.** A failed call is a recorded step in its own right, holding the
exception type, and the replay raises it again at the same point. This includes
a timeout that fires halfway through reading a streamed body.

### 09 · Task scheduling order

The same as 07 for `asyncio` and for thread pools that oversubscribe. Which
coroutine resumes first is not yours to decide.

**Captured**, by the same forced ordering. Sync and async share one queue, so an
agent that mixes them replays in the recorded order.

---

## network — 4 of 4 captured

### 10 · A streamed response

```python
with client.stream("POST", url) as r:
    for token in r.iter_text():
        render(token)
```

Chunk boundaries and arrival times are not stable between runs, and code that
acts on partial output — a parser, a stop-sequence check, a UI — is sensitive to
both.

**Captured.** Chunks pass through to your code as they arrive, so recording does
not change how the stream behaves, and the boundaries and inter-chunk gaps are
written down. A replay hands back the same chunks in the same shape,
instantly by default and at the recorded pace with `replay(..., realtime=True)`.

> The first version read the whole response before returning it. Every stream
> arrived as one lump, which meant that switching the recorder on changed how
> the agent behaved — the one thing a recorder must never do.

### 11 · Retry with jitter

```python
for attempt in range(3):
    r = call()
    if r.status_code == 200:
        break
    time.sleep(random.uniform(0.1, 1.0))
```

How many attempts happen depends on which of them failed, which depends on the
day.

**Captured.** Each attempt is its own step, with its own status.

### 12 · Provider behaviour drift

The API answered `v1` in March and `v2` in April, and nobody told you. Your code
did not change; the world did.

**Captured** — this is the case replay exists for. The recorded response is
served, so you can put March's answer through today's code.

### 13 · Rate limiting

A `429` that appears under load and not in your test.

**Captured.** The `429` is a step like any other, so the branch it triggers
replays too.

---

## tools — 1 captured, 1 loose-key only, 1 known limit

### 14 · Request formatting drift · loose key only

```python
json.dumps(payload)                 # today
json.dumps(payload, indent=2)       # after somebody ran a formatter
```

Semantically identical, byte-different. Whether this counts as "the same
request" is a judgement, not a fact.

Orientim does not decide for you. It matches under two keys and reports which
one it used:

- **strict** — identical bytes. This case fails, correctly: the request really
  did change.
- **loose** — whitespace collapsed, object keys sorted, floats rounded to six
  places. This case passes.

This is the only one of the twenty that moves between the two columns, and it is
the entire difference between the published 15/20 and 16/20.

### 15 · A local read that never touches the network · known limit

```python
def lookup_customer(id):
    return db.query(...)        # in-process, no socket
```

Invisible to us. Capture happens at the HTTP boundary; a tool that reads a file
or a local database crosses no boundary. The replay sees today's data.

Detected, never prevented: the request built from that data will not match, and
the replay says so.

### 16 · A side effect

```python
client.post("/send-email", json={"to": customer})
```

An email sent during the recorded run must not be sent again by a replay, or the
tool is worse than the bug.

**Captured**, and the guarantee is structural rather than a list of dangerous
paths: during a replay nothing is forwarded anywhere. Every response comes out
of the file, and a request with no match gets a synthetic `599`. There is no code
path from a replay to a socket. `transport.SIDE_EFFECTING` only decides what the
timeline marks so you can see it.

---

## environment — 1 of 1 captured, 1 known limit

### 17 · Environment variables

```python
model = os.environ.get("MODEL", "gpt-4")
```

Staging and production differ; so do two laptops.

**Captured — when you ask for it.** Name the variables:

```python
with orientim.record(env=["MODEL", "REGION"]) as run:
    ...
```

Nothing from `os.environ` is stored otherwise. An earlier version snapshotted
the whole environment into every recording, which put every API key in the
process into a file people attach to bug reports. See
[recordings.md](recordings.md).

### 18 · Filesystem state · known limit

A file that existed during the recording may be gone at replay time, or hold
different bytes. Same reason as 15: it never crosses the HTTP boundary.

Detected, not prevented.

---

## framework — 2 known limits

### 19 · A cache inside your framework · known limit

If LangChain, LlamaIndex or your own memo serves a result from its own cache
during the replay, no request is made and there is nothing for us to see. The
replay makes fewer calls than the recording and reports `FEWER_STEPS`.

Clear the cache between runs, or accept the divergence as expected.

### 20 · Library version drift · known limit

```python
{"user_agent": f"myapp/{httpx.__version__}"}
```

An upgrade changes the bytes your agent sends. The request is genuinely
different, and no record/replay tool can make it identical — it can only report
the divergence clearly, which Orientim does.

> This was counted as a captured source until the pre-release audit. The probe
> sent the library version in the request body and nothing ever changed the
> version, so the test could not fail. The honest classification is a limit.

---

## The numbers

| | strict key | loose key |
|---|---|---|
| captured | 15 | 16 |
| declared limits | 5 | 4 |
| undeclared failures | 0 | 0 |

The last row is the one that matters. Sixteen is a number about our coverage;
zero is a promise about our honesty, and it is the one you can check:

```bash
orientim conformance            # loose key
orientim conformance --strict   # identical bytes
```

Both run on your machine, against your installed libraries, and exit non-zero if
anything we claimed to capture failed to.

## Source twenty-one

If a replay diverges on your machine for a reason that is not one of the
declared limits, that is a gap in the taxonomy, not just a bug. Open an issue
with the shape of it. The entry that gets added helps everyone who installs this
afterwards, which is more than the fix does.
