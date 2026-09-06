# Orientim

**Deterministic tests for AI agents.** Record every HTTP call your agent makes —
to the model, to its tools, to MCP servers — then run it again from the
recording: same responses, same order, no network, no API bill. This is replay
at the **HTTP boundary**, not reproduction of the whole process — what that
does and does not cover is in [what it captures](#what-it-captures).

```bash
pip install orientim
orientim conformance      # what it can and cannot capture on your machine
```

MIT. No server, no account, no telemetry. One dependency.

New here? **[docs/how-to-use.md](docs/how-to-use.md)** is eight steps from
install to CI, with diagrams.

---

## Start by measuring

Before replay, a simpler question: **how consistent is your agent with itself?**
Most teams have never measured it. They ran it three times, it worked, they
shipped.

```bash
orientim stability --entry myapp.agent:run --runs 30
```

```
  Distinct paths            3
  Agreement with modal   45.8%    13 of 24 runs differed
  Steps      mean   2.71   control limits 0.63 - 4.79
  Duration   mean 1793ms   control limits  957 - 2630 ms

  The odd runs — go inside them:
    orientim view run_2bea9035 --root runs/_stability    # non-modal path
```

Thirty runs, one number, and it is usually not the number people expect. This
command needs no recording infrastructure to be useful.

Then go inside the run that behaved differently.

## Then replay

A customer says your agent gave a wrong answer on Tuesday. The logs say it
called a search tool, got results, and replied. You type the same question and
get a different answer. You try ten more times. Sometimes it works.

A recording turns that into a fixture. The agent runs again on your machine and
receives Tuesday's answers instead of new ones — same order, same bodies, same
timeouts, same failures.

```python
import orientim
from openai import OpenAI

def my_agent():
    client = OpenAI()              # your code, unchanged
    return client.chat.completions.create(...)

with orientim.record(tags={"order": "4471"}) as run:
    my_agent()

report = orientim.replay(run.path, lambda _: my_agent())
print(report.report())
```

Every `httpx` client built inside the block is instrumented, including the ones
the OpenAI and Anthropic SDKs build for themselves. You do not rewrite your
agent.

Change your code, replay, and the report says whether behaviour changed only
where you meant it to:

```
!!  Fewer steps than recorded   [FEWER_STEPS]
    The replay stopped at step 3 while the recording continued. If you changed
    the code, this is exactly what should happen — your change halts the flow
    here.
    -> did you change the code on purpose?
```

Sixteen verdicts, none of them a bare "diverged" —
[docs/verdicts.md](docs/verdicts.md).

### Then change one thing

A log tells you what happened. A replay lets you ask what would have happened
instead — replace what a step returned and watch the agent take the other
branch:

```python
orientim.replay(run.path, my_agent, patch={
    3: {"body": '{"hits": ["order 4471 shipped"]}'},   # if search had found it
    5: {"status": 429},                                # if we had been rate limited
})
```

```
!!  Counterfactual — 1 step(s) replaced   [COUNTERFACTUAL]
    You replaced what step 3 returned and asked what the agent would have done.
    It made the same requests up to step 4, then took a different path.
```

Nothing is forwarded, so the email the other branch sends is not sent.

### In CI

```yaml
- uses: intopic/orientim@v0.1.0
  with:
    entry: myapp.agent:run
    recordings: tests/recordings
```

```
  ok  run_2bea9035      4 steps     31.2 ms  customer=4471
  !!  run_7c1a9f02      6 steps     28.9 ms  customer=8812   HEADERS_CHANGED @ step 4
  ok  run_9e4b1105      3 steps     19.4 ms

  1 of 3 recordings changed behaviour.
  NEW on this change: run_7c1a9f02
```

Everything runs on your runner: your code, your recordings, no network. The
job fails when behaviour changed, annotates the pull request, and writes a
report of ids, verdicts and hashes — no prompts, no bodies, nothing you would
have to audit before uploading. `orientim ci --help` for the same thing without
Actions.

### In a test

```python
def test_no_regression():
    orientim.assert_replays("runs/run_2bea9035.jsonl", lambda _: my_agent())
```

The assertion carries the whole diagnosis, so a CI failure tells you which step
changed rather than `assert False`.

## Isn't this vcrpy?

Fair question, and the honest answer is: for stubbing HTTP in tests, use
`vcrpy`. It is mature and it works.

Orientim differs in six ways, all specific to agents:

| | vcrpy and friends | Orientim |
|---|---|---|
| clock, uuid, randomness | not shimmed | shimmed and replayed |
| parallel calls returning out of order | not enforced | forced to the recorded order |
| when a replay is "the same" | the request matched | request, headers, order, exceptions, completeness |
| counterfactuals | out of scope | replace a response and see the other branch |
| divergence output | mismatch or error | sixteen named diagnoses with next steps |
| repeated-run variance | out of scope | control charts over N runs |

If none of those six is a problem you have, this is over-engineering and
`vcrpy` is the better choice.

The observability tools — LangSmith, Braintrust, Arize, Langfuse — are a
different category. They tell you what happened. None of them runs it again.
They compose fine with this.

## What it captures

Twenty documented sources of non-determinism, each with a probe that triggers
it, a mutation that changes the world between recording and replay, and an
expectation declared before the run.

**16 of 20 with the normalised key, 15 of 20 with strict bytes, and zero
undeclared failures in both.** Read those as coverage of *this taxonomy* on
*your* machine — how many of these twenty specific sources Orientim reproduces
here — not as a determinism score for your agent. The number that matters is the
last one: nothing claimed as captured actually failed. It is the one you can
check yourself:

```bash
orientim conformance            # normalised key
orientim conformance --strict   # identical bytes
```

It runs on *your* machine against *your* installed libraries, and exits non-zero
if anything claimed as captured failed. The full taxonomy — including why two
probes that used to pass could not fail — is in
[docs/nondeterminism.md](docs/nondeterminism.md).

The four sources it does **not** capture: local reads that never touch the
network, filesystem state, caches inside your framework, and library version
drift. Narrower limits — `httpx` only, shim call sites, no WebSockets — are in
[docs/limits.md](docs/limits.md).

## Nothing happens twice

An agent that sent an email during the recorded run must not send it again.

The guarantee is structural, not a list of dangerous paths: **during a replay
nothing is forwarded anywhere.** Every response comes out of the file, and a
request with no match gets a synthetic `599`. There is no code path from a
replay to a socket.

## Streaming behaves like streaming

Chunks reach your code as they arrive, so turning the recorder on does not
change how a token-streaming agent behaves. Boundaries and inter-chunk gaps are
recorded; a replay hands back the same chunks in the same shape — instantly by
default, and at the recorded pace with `replay(..., realtime=True)`.

## When the recording is not the whole run

A replay that cannot honestly claim to have reproduced the run says so instead
of saying `IDENTICAL`:

```
!!  Part of this run was never captured   [UNCAPTURED_LIBRARY]
    The recorded run made 2 call(s) through a library orientim does not
    intercept — the first was 'POST https://api.example.com/search'.
    -> route that tool through httpx, or treat this replay as partial
```

Calls through `requests`, `aiohttp` and `urllib` are counted during recording
even though they cannot be captured. Same treatment for a truncated ring buffer,
a half-read stream, and an older file format.

## What ends up in a recording

Prompts and responses in full — that is the point of it. Never: request headers,
credential-shaped fields in query strings, JSON and form bodies (request *and*
response), `user:password@` in a URL (the request line, a `Location` header, or a
URL sitting inside a JSON body), credential query params in a redirect
`Location`, response `Set-Cookie`, or anything from `os.environ` unless you name
it — and a variable whose *name* looks like a secret is refused even when named.

Not redacted, and worth knowing: a secret inside a URL *path* (a Slack webhook,
a Telegram bot token), and a secret your own code wrote into a prompt.

Full contract: [docs/recordings.md](docs/recordings.md). Read it before you
share a file.

## The rest of the CLI

```bash
orientim ls                                   # runs that triggered a capture
orientim play run_2bea9035 --step             # the run in the terminal
orientim view run_2bea9035                    # scrubbable timeline in a browser
orientim view run_2bea9035 --entry app:run    # ...wired to a live replay
orientim diff run_2bea9035 run_9f1c04ab      # Tuesday against Wednesday
orientim prune --per-signature 3 --dry-run   # what a retention policy would remove
orientim ls --trace 4bf92f3577b34da6...      # the run behind an OTel trace
```

## What gets saved, and for how long

Recordings are written only when something triggers — an error, a `5xx`, an
uncaptured source, or `run.rec.trigger("...")`. Steps live in a ring buffer
until then, so a long-running agent does not write gigabytes nobody will open.

**The trap:** an agent that returns a *wrong* answer raises nothing and returns
`200`, so nothing triggers and `run.path` is `None` — and a wrong answer is the
case this tool exists for. Wire your quality check to `trigger()`, or use
`always=True` while developing:

```python
with orientim.record(tags={"customer": cid}, on_capture=notify_slack) as run:
    answer = my_agent()
    if not looks_right(answer):
        run.rec.trigger("failed the quality check")
```

`on_capture(path, meta)` fires after a file is written, so a recording announces
itself instead of waiting to be found.

Nothing is ever deleted on its own. When you want it bounded:

```bash
orientim prune --per-signature 5 --older-than 30
```

`--per-signature` is the rule worth knowing: five hundred runs that failed in
exactly the same way collapse to five examples, because the chain root already
says they are the same run. [docs/retention.md](docs/retention.md).

Local disk by default; `ORIENTIM_STORE=s3://your-bucket/prefix` moves them to
anything S3-shaped. Credentials come from boto3's normal chain and go nowhere
near us.

If your application already emits OpenTelemetry, a recording is stamped with the
`trace_id` it belonged to — no dependency added — so an engineer looking at a
failed span can find the run with `orientim ls --trace <id>`.

`record()` patches `httpx.Client.__init__` process-wide for the duration of the
block. Overlapping and nested blocks are reference-counted and the original
always goes back — but do not wrap a long-lived server process; wrap the request
handler.

## Documentation

| | |
|---|---|
| [docs/how-to-use.md](docs/how-to-use.md) | eight steps from install to CI, with diagrams |
| [docs/nondeterminism.md](docs/nondeterminism.md) | the twenty sources — the specification this is built against |
| [docs/verdicts.md](docs/verdicts.md) | what each verdict means, and what `IDENTICAL` promises |
| [docs/recordings.md](docs/recordings.md) | what is in a recording, what is redacted, what is not |
| [docs/retention.md](docs/retention.md) | what gets saved, how much to keep, and why |
| [docs/limits.md](docs/limits.md) | everything it cannot do, in one place |
| [docs/architecture.md](docs/architecture.md) | how it works and why, including what was wrong before |
| [SECURITY.md](SECURITY.md) | threat model and reporting |
| [CHANGELOG.md](CHANGELOG.md) | including the pre-release audit that found eight defects |

## Layout

| | |
|---|---|
| `transport.py` | capture at the HTTP boundary, streaming, redaction, forced ordering |
| `chain.py` | the step hash the divergence report falls out of |
| `session.py` | `record()`, `replay()`, and the httpx patch |
| `shims.py` | clock, randomness, identifiers |
| `detect.py` | traffic through libraries we cannot capture, noticed anyway |
| `store.py` / `storage.py` | ring buffer and triggers; disk, S3-shaped, in-memory |
| `diagnose.py` | the sixteen verdicts |
| `diff.py` | two recordings side by side |
| `ci.py` | replay a whole store and judge the build |
| `stability.py` | statistical process control over repeated runs |
| `patterns.py` / `conformance.py` | the twenty sources, and what they do on your machine |
| `viewer.py` / `server.py` | the timeline and the live replay |

Around 2,900 lines. `httpx` is the only runtime dependency.

## Prior art

Record–replay debugging after LeBlanc & Mellor-Crummey (1987) and Mozilla's
`rr` (2011), which works at the instruction level; this works at the I/O
boundary, which is enough when you do not need to replay a CPU. Statistical
process control after Shewhart (1924). HTTP record–replay for tests has a long
history in VCR and vcrpy — worth reading before repeating their mistakes.

## Contributing

The most useful contribution is source twenty-one: a divergence on your machine
that is not one of the declared limits. See
[CONTRIBUTING.md](CONTRIBUTING.md), which also carries the one rule about tests
— a test that cannot fail is worse than no test, and this project learned that
the expensive way.

MIT.
