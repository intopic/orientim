# How to use Orientim

Eight steps, in order. Each one takes a minute. By the end you will have
recorded a run, replayed it, and know exactly where your recordings live.

```bash
pip install orientim
```

---

## What you just installed

Orientim sits at one place: the HTTP boundary. Everything interesting an agent
does crosses it — the model, every tool, every MCP server — so one interception
point covers all of them.

```
        your agent code
              │
              │  client.chat.completions.create(...)
              ▼
   OpenAI SDK · Anthropic SDK · LangChain · your own tool
              │
              │  they all use httpx underneath
              ▼
      ┌───────────────────┐
      │     Orientim      │   ← writes down what goes past
      └───────────────────┘
              │
              ▼
           network
```

You do not rewrite your agent. Nothing above this line changes.

---

## Step 1 · Find out what it can capture here

```bash
orientim conformance
```

Twenty probes run against a local server on your machine, with your installed
libraries, and print what is captured and what is not.

```
  Python 3.12.1 on Linux
  Matching:     loose (normalised)
  Intercepting: httpx 0.28.1
  Covered SDKs: openai, anthropic

  clock
    01   ok   time read into a request
    ...
    15   --   local read, never on network      (declared limit)

  Captured 16 of 20 sources (16 of 16 claimed).
  4 declared limits, not captured by design.
  No undeclared failures. Every gap on this machine is one we told you about.
```

Read the bottom line. It exits non-zero if anything we claim to capture failed
on **your** machine — so you learn the limits now, not at three in the morning.

If it warns that `requests` or `aiohttp` is installed, note it: tools using
those are not captured. Step 3 explains what happens instead of silence.

---

## Step 2 · Wrap your agent

```python
import orientim

with orientim.record(tags={"customer": "4471"}) as run:
    answer = my_agent("what is the status of order 4471?")
```

Every `httpx` client built inside that block is instrumented, including the ones
SDKs build for themselves. `tags` are yours — anything you would want to search
by later.

> **One rule.** `record()` patches `httpx` for the whole process while the block
> is open. Wrap the request handler, not a long-lived server.

Concurrent requests are isolated from each other — async on one event loop, or
one thread per request, both work. Budget memory: each in-flight run holds its
own steps, roughly 14–25 KB per call until the ring caps it at about 6.5 MB.

---

## Step 3 · Decide when a run is kept

**This is the step people get wrong.** Nothing is written to disk unless
something fires. Steps live in memory until then, which is how a long-running
agent avoids writing gigabytes nobody opens.

```
   run finishes
        │
        ├── raised an exception?       ──▶  saved
        ├── a response came back 5xx?  ──▶  saved
        ├── an uncaptured source hit?  ──▶  saved
        ├── you called trigger("…")?   ──▶  saved
        ├── always=True?               ──▶  saved
        │
        └── none of the above          ──▶  NOT saved · run.path is None
                                                    ▲
                        a wrong-but-successful answer lands here
```

An agent that returns a **wrong** answer raises nothing and returns `200`. It
falls into the last branch, and that is the case you bought this for. So either
tell Orientim, or keep everything:

```python
# production — keep the runs your own quality check disliked
with orientim.record(tags={"customer": cid}) as run:
    answer = my_agent(question)
    if not looks_right(answer):
        run.rec.trigger("failed the quality check")

# development — keep everything, you are looking at it anyway
with orientim.record(always=True) as run:
    my_agent(question)
```

`ORIENTIM_ALWAYS=1` does the same as `always=True` without touching code.

To be told the moment something is captured:

```python
with orientim.record(on_capture=lambda path, meta: notify_slack(path)) as run:
    ...
```

---

## Step 4 · Look at what was recorded

```bash
orientim ls                          # runs that triggered a capture
orientim play run_2bea9035 --step    # the run, in the terminal
orientim view run_2bea9035           # scrubbable timeline in your browser
```

```
   0 ◉━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   POST chat/completions   200
   1 ━━━━━━━━━━━━━◉━━━━━━━━━━━━━━━━━━━━━━━━━   POST search             200
   2 ━━━━━━━━━━━━━━━━━━━━━━━━━━━◉━━━━━━━━━━━   POST chat/completions   200
   3 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━◉ ▲ POST send-email         200

  ▲ step 1: the search returned nothing — look at what the agent did next
```

Recordings are plain `jsonl`. You can `grep` them without this package
installed — that is deliberate.

---

## Step 5 · Run it again

```python
report = orientim.replay(run.path, lambda _: my_agent(question))
print(report.report())
```

```
RECORD                                REPLAY
                                      
agent ──▶ Orientim ──▶ network        agent ──▶ Orientim ──╳  no network
                │                                  │
                ▼                                  ▼
       run_2bea9035.jsonl ──────────────▶  Tuesday's answers, in order
```

The agent walks the same path: same responses, same order, same timeouts, same
failures. Nothing is forwarded anywhere, so the email it sent on Tuesday is not
sent again.

Now change your code and replay. The report tells you whether behaviour changed
only where you meant it to:

```
!!  Fewer steps than recorded   [FEWER_STEPS]
    The replay stopped at step 3 while the recording continued. If you changed
    the code, this is exactly what should happen — your change halts the flow
    here.
    -> did you change the code on purpose?
```

`IDENTICAL` means identical: same requests, same order, same bodies, same
headers, same responses, no new exception. Sixteen verdicts, each naming what
happened and what to do — [verdicts.md](verdicts.md).

---

## Step 6 · Ask what would have happened

A log tells you what happened. Replace what a step returned and watch the agent
take the other branch:

```python
orientim.replay(run.path, agent, patch={
    1: {"body": '{"hits": ["order 4471 shipped"]}'},   # if search had found it
    3: {"status": 429},                                # if we had been rate limited
})
```

```
!!  Counterfactual — 1 step(s) replaced   [COUNTERFACTUAL]
    It made the same requests up to step 2, then took a different path.
```

Still no network. The email the other branch sends is not sent.

---

## Step 7 · Put it in a test

```python
def test_no_regression():
    orientim.assert_replays("runs/run_2bea9035.jsonl", lambda _: my_agent(q))
```

The assertion carries the whole diagnosis, so a CI failure names the step that
changed instead of saying `assert False`. No network, no API bill — a few
hundred replays cost seconds.

To replay a whole folder of recordings instead of naming them one by one:

```bash
orientim ci --entry myapp.agent:run --recordings tests/recordings             --report orientim-report.json
```

Exit `0` when everything replayed identically, `1` when behaviour changed, `2`
when it could not run at all. On GitHub Actions the same command annotates the
pull request and fills the job summary, and there is an action that wraps it:

```yaml
- uses: intopic/orientim@v0.1.0
  with:
    entry: myapp.agent:run
    recordings: tests/recordings
    baseline: orientim-report.json    # the base branch's report, if you kept it
```

`--baseline` is what turns "this build is red" into "these two runs started
changing on *this* pull request". A full example workflow is in
[examples/workflow.yml](../examples/workflow.yml).

---

## Step 8 · Keep the store bounded

Recordings live in `runs/` by default. Nothing is ever deleted on its own.

```bash
orientim prune                              # what is there, removes nothing
orientim prune --per-signature 3 --dry-run  # what would go
orientim prune --per-signature 3            # do it
```

`--per-signature` is the rule worth knowing: five hundred runs that failed the
same way collapse to three examples, because the chain hash already says they
are the same run. Also `--keep N`, `--older-than DAYS`, `--max-bytes N`.

To move recordings to your own bucket:

```bash
pip install 'orientim[s3]'
export ORIENTIM_STORE=s3://your-bucket/orientim
```

Credentials come from boto3's normal chain. Nothing is sent anywhere else —
there is no service behind this package.

---

## Five things that surprise people

**1 · `run.path` is `None`.** Nothing triggered. See Step 3.

**2 · The replay says `UNCAPTURED_LIBRARY`.** Part of the run went out through
`requests`, `aiohttp` or `urllib`, which are not captured. Those calls are
counted and named, and the replay refuses to call itself identical — a partial
recording never masquerades as a whole one.

**3 · The replay says `TRUNCATED`.** The ring buffer filled before the trigger
fired. Raise it: `record(ring=4000)`. Measured: the ring counts clock and
randomness entries too, so the default 512 keeps about **128 HTTP calls**.

**4 · The replay says `HEADERS_CHANGED`.** Two calls to the same URL with the
same body differ only by a header, and your change swapped their order — so the
agent asked a different question and was handed the old answer.

**5 · Your prompts are in the file.** That is the point of it, and it means a
recording carries whatever your prompts carry. Credentials are redacted;
prompts are not. Read [recordings.md](recordings.md) before sharing one.

---

## Where to go next

| | |
|---|---|
| [verdicts.md](verdicts.md) | every replay verdict, and what `IDENTICAL` promises |
| [recordings.md](recordings.md) | what is in a recording, what is redacted, what is not |
| [retention.md](retention.md) | what to keep and for how long |
| [limits.md](limits.md) | everything Orientim cannot do |
| [nondeterminism.md](nondeterminism.md) | the twenty sources it is built against |
| [architecture.md](architecture.md) | how it works, and why |
