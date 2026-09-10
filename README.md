# Orientim

[![ci](https://github.com/intopic/orientim/actions/workflows/ci.yml/badge.svg)](https://github.com/intopic/orientim/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9+-blue.svg)](pyproject.toml)

**Deterministic tests for AI agents.** Record every HTTP call your agent makes —
to the model, to its tools, to MCP servers — then run it again from the
recording: same responses, same order, no network, no API bill. This is replay
at the **HTTP boundary**, not reproduction of the whole process — what that
does and does not cover is in [what it captures](#what-it-captures).

```bash
# Not on PyPI yet — install from the repo.
pip install "orientim @ git+https://github.com/intopic/orientim"
orientim conformance      # what it can and cannot capture on your machine
```

MIT. No server, no account, no telemetry. One dependency.

New here? **[docs/how-to-use.md](docs/how-to-use.md)** is eight steps from
install to CI, with diagrams.

---

## Start by measuring

Before replay, a narrower question: **how consistent is the agent with itself?**
Repeated execution of one task is the cheapest evidence available, and it
requires no recording infrastructure.

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

The output is an individuals control chart after Shewhart (1924): the mean,
control limits derived from the mean moving range, and the runs falling outside
them. Non-modal runs are named so that one can be opened directly.

## Then replay

The problem: an agent returns a wrong answer, the logs show which calls it
made, and running it again produces something else. Non-determinism makes the
failure unavailable for inspection.

A recording turns that execution into a fixture. The agent runs again locally
and receives the recorded responses in the recorded order — same bodies, same
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

Every request made inside the block is captured — `httpx`, `httpx2` and
`requests`, sync and async, whoever built the client. That includes the clients
the OpenAI and Anthropic SDKs build for themselves. The agent is not modified.

Change your code, replay, and the report says whether behaviour changed only
where you meant it to:

```
!!  Fewer steps than recorded   [FEWER_STEPS]
    The replay stopped at step 3 while the recording continued. If you changed
    the code, this is exactly what should happen — your change halts the flow
    here.
    -> did you change the code on purpose?
```

Twenty verdicts, none of them a bare "diverged" —
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
# Not on PyPI yet, so install it from the repo first.
- run: pip install "orientim @ git+https://github.com/intopic/orientim@v0.1.0"
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

What that proves is narrow, and worth stating plainly: a green replay means you
did not break the path the recording captured. It cannot vouch for a path the
recording never took — a fix that adds an API call reports `NEW_CALL` and needs a
fresh recording. This is a regression gate, not proof that new code works.

## Isn't this vcrpy?

For stubbing HTTP in tests, use `vcrpy`: it is mature and it works. Orientim
differs in six ways, each specific to agents:

| | vcrpy and friends | Orientim |
|---|---|---|
| clock, uuid, randomness | not shimmed | shimmed and replayed |
| parallel calls returning out of order | not enforced | forced to the recorded order |
| when a replay is "the same" | the request matched | request, headers, order, exceptions, completeness |
| counterfactuals | out of scope | replace a response and see the other branch |
| divergence output | mismatch or error | twenty named diagnoses with next steps |
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
drift. Narrower limits — `httpx`, `httpx2` and `requests` only, shim call
sites, no WebSockets — are in [docs/limits.md](docs/limits.md).

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

Calls through `aiohttp` and `urllib` are counted during recording but not
captured. The same treatment applies to a truncated ring buffer, a half-read
stream, and an older file format.

## What the agent did, not only what crossed the wire

Recording HTTP tells you *that* behaviour changed. It does not tell you *what*
changed, so a recording also carries the execution model:

```python
with orientim.record(agent={"name": "order-support", "version": "2.1.0"}) as run:
    run.output = my_agent("where is order 4471")
```

Every step is typed — `model` or `tool` — model calls carry which model, at what
temperature, with which tools offered, and what usage came back; the run carries
the answer, the agent identity, and the python and library versions it ran
under.

The final output has to be declared. `record()` is a context manager, so your
return value never passes through Orientim; there is no honest way to capture it
implicitly. Declaring it buys a verdict nothing else can produce:

```
!!  Same calls, different answer   [OUTPUT_CHANGED]
    Every HTTP call replayed identically — same requests, same responses, in
    the same order — and the agent still returned something else.
    -> compare the two answers, then look for state that is not HTTP
```

A model step also records the tools the model asked for, with their arguments,
which is what makes the next section possible.

Step typing is a **heuristic** and is kept away from matching by construction:
none of these fields is in the hash chain, so a wrong label can mislead a report
but can never produce a wrong verdict.

Format 4 recordings are read by the same code as format 3 ones, which are
upgraded on read and never rewritten — including retroactive step typing, since
the request was always stored. Details and limits:
[docs/execution-model.md](docs/execution-model.md). Runnable:
`python examples/execution_model.py`.

## Ask a run a question

A replay tells you whether anything changed. It does not tell you whether the
agent did the right thing. That is a property of one execution, so it is asked
directly:

```python
from orientim import evaluate as ev

report = ev.evaluate("runs/run_2bea9035.jsonl", [
    ev.used_tool("lookup_order"),
    ev.did_not_call("send_email"),
    ev.output_matches(r"order \d+"),
    ev.max_steps(6),
    ev.no_step_failed(),
])
```

```
run_2bea9035 — 4 passed, 1 failed, 0 unanswered
  ok  used_tool        lookup_order was requested 1 time(s)
  !!  did_not_call     send_email was requested 1 time(s), at step(s) 7
  ok  output_matches   the answer matches 'order \d+'
  ok  max_steps        3 calls, within the limit of 6
  ok  no_step_failed   all 3 call(s) succeeded
```

No result is a bare boolean — each carries the reason and the evidence, so a
failure is an answer rather than the start of an investigation. There is a third
status, `warn`, for a question that could not be answered; it never fails a
build, because failing on an unanswerable question is how a tool teaches people
to ignore it.

Details: [docs/evaluation.md](docs/evaluation.md). Runnable:
`python examples/evaluation.py`.

## Keep it as a test

A recording you decided to keep, plus how to run it again, plus what has to stay
true, is a **case**. What the whole suite said today is a **baseline**.

```bash
orientim case save runs/run_2bea9035.jsonl --name order-support \
    --entry myapp.agent:run \
    --used-tool lookup_order --never-call send_email --max-steps 6

orientim baseline create main

# ... change the code, the prompt, or the model ...

orientim test --baseline main
```

```
  !!  order-support           2 steps     569.2 ms
       NEW_CALL at step 2 — the replay reproduced all 2 recorded step(s) and
       then made a request this recording does not contain
       !!  did_not_call     send_email was requested 1 time(s), at step(s) 2
       evidence, from the same replay:
         output         'I could not find order 4471' -> 'your order shipped'
         steps          inserted 1, same 2

  1 of 1 case(s) failed.
  NEW on this change: order-support
```

The command that fails is the command that explains. The evidence block is
computed from what the run already produced — the recording it was made from
and the steps the replay just wrote — only when a case fails, so a green suite
pays nothing for it. It never claims a cause, and it never claims more than the
run can show: when a replay diverges at a model call, no response exists, so the
tool comparison is withheld rather than reported as tools that stopped being
requested.

A case passes when **both** halves hold: the run reproduced, and nothing it
promised broke. Either alone is half an answer — a faithful replay of an agent
that now asks for the wrong tool is not a pass.

With a baseline, only what *this* change broke fails the build; a case that was
already red is not this change's fault. Exit 0 green, 1 changed, 2 could not run
at all — because "the suite failed" and "the suite never ran" are different
facts.

Details: [docs/regression.md](docs/regression.md). Runnable:
`python examples/regression.py`.

## And why it changed

`orientim test` names what changed. `orientim diff` shows the whole
comparison: every step, both request bodies, and the consequence chain.

```bash
orientim diff --case order-support
```

```
  1. MODEL CONFIG
     model: gpt-4o-mini -> gpt-4o
     temperature: 0.0 -> 0.7

  2. TOOL DECISION
     not readable from this pair: all 1 model call(s) on the now side went unanswered,
     so no response exists that a tool request could have been in.
     the other side requested: lookup_order
     (what this run asked for is unknown, not unchanged - record both sides to compare)

  3. OUTPUT
     expected: order 4471 not found
     actual:   your order shipped

  4. EVALUATION
     FAIL output_matches: the answer does not match 'not found'

  CONSEQUENCE
    model configuration changed (model, temperature)
      the final answer changed
        output_matches failed

    These are observations in order, not a causal chain:
    consequence observed, causal link not established.
```

Steps are **aligned** rather than compared by index, so one inserted call no
longer makes every later step read as different, and four steps that swapped
places report as one reordering.

The consequence chain is built only from fields already in the records — no
language model, and no invented cause. Every link says whether the records
establish it; most say they do not, because ordering is not causation and
saying otherwise would be the most useful-sounding lie the tool could tell.

Details: [docs/diff-contract.md](docs/diff-contract.md).

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

`record()` patches process-wide for the duration of the block:
`HTTPTransport.handle_request` on `httpx` and `httpx2`, and `HTTPAdapter.send` on
`requests`. Overlapping and nested blocks are reference-counted and the
originals always go back — but do not wrap a long-lived server process; wrap the
request handler.

## Documentation

| | |
|---|---|
| [docs/how-to-use.md](docs/how-to-use.md) | eight steps from install to CI, with diagrams |
| [docs/nondeterminism.md](docs/nondeterminism.md) | the twenty sources — the specification this is built against |
| [docs/verdicts.md](docs/verdicts.md) | what each verdict means, and what `IDENTICAL` promises |
| [docs/recordings.md](docs/recordings.md) | what is in a recording, what is redacted, what is not |
| [docs/execution-model.md](docs/execution-model.md) | typed steps, model metadata, final output, and the format migration |
| [docs/evaluation.md](docs/evaluation.md) | evaluators: asking one execution a question, with evidence |
| [docs/regression.md](docs/regression.md) | cases, baselines and `orientim test` |
| [docs/diff-contract.md](docs/diff-contract.md) | how two executions are aligned, explained and connected |
| [docs/concurrency.md](docs/concurrency.md) | what ran beside what, and what timing does and does not prove |
| [docs/retention.md](docs/retention.md) | what gets saved, how much to keep, and why |
| [docs/limits.md](docs/limits.md) | everything it cannot do, in one place |
| [docs/architecture.md](docs/architecture.md) | how it works and why, including what was wrong before |
| [docs/claims.md](docs/claims.md) | every claim in these documents, and the test that backs it |
| [SECURITY.md](SECURITY.md) | threat model and reporting |
| [CHANGELOG.md](CHANGELOG.md) | including the pre-release audit that found eight defects |

## Layout

| | |
|---|---|
| `transport.py` | capture at the HTTP boundary, streaming, redaction, forced ordering |
| `chain.py` | the step hash the divergence report falls out of |
| `session.py` | `record()`, `replay()`, and the process-wide patches |
| `shims.py` | clock, randomness, identifiers |
| `reqs.py` | the same capture, hooked into `requests` |
| `detect.py` | traffic through libraries we cannot capture, noticed anyway |
| `store.py` / `storage.py` | ring buffer and triggers; disk, S3-shaped, in-memory |
| `model.py` | the execution model: step typing, model metadata, output capture |
| `evaluate.py` | evaluators over one execution, with structured evidence |
| `diagnose.py` | the twenty verdicts |
| `align.py` / `explain.py` / `diff.py` | aligning two executions, explaining the difference, and what followed |
| `concurrency.py` | intervals, overlap and ordering — a reader, never a verdict |
| `ci.py` | replay a whole store and judge the build |
| `cases.py` / `baselines.py` | saved cases, frozen suite results, `orientim test` |
| `stability.py` | statistical process control over repeated runs |
| `patterns.py` / `conformance.py` | the twenty sources, and what they do on your machine |
| `viewer.py` / `server.py` | the timeline and the live replay |

Around 8,400 lines. `httpx` is the only runtime dependency; `httpx2` and
`requests` are instrumented when present but never required.

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
