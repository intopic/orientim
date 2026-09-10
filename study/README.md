# Will an agent nobody here wrote replay at all?

Every number Orientim has about itself comes from `lab/`, which I built to be
replayable. This asks the question that assumption was hiding.

Nothing in here touches Orientim. Both probes import the frozen package at
`c942b0e` and report what it does.

```bash
python study/probe.py       # nondeterminism, one source at a time
python study/sdk_probe.py   # the vendors' own clients
```

---

## Why the question is sharp

The replay match key is `sha256(method|url|body)`. Loose mode normalises JSON
key order, whitespace and float rounding — and nothing else. **There is no
field-level exclusion in either mode.** One timestamp in a prompt is enough.

Separately, `hdr_fp` is in `chain.DIGEST_FIELDS`, so a header the agent never
chose can move a verdict even though headers are not in the lookup key.

Every row below is classified by the only question that decides what may be
done about it:

- **ENVIRONMENT** — a code change cannot alter this value. Safe to neutralise,
  because neutralising it cannot hide a behavioural change.
- **CONTENT** — a code change can alter it. Neutralising it deletes the signal
  Orientim exists to detect.

---

## Result 1 — one source at a time

Record an agent, then replay **the same code, unchanged**.

| source | strict | loose | verdict |
|---|---|---|---|
| control — nothing moves | IDENTICAL | IDENTICAL | harness is sound |
| `time.time()` in the body | IDENTICAL | IDENTICAL | shimmed |
| `uuid.uuid4()` in the body | IDENTICAL | IDENTICAL | shimmed |
| `uuid.uuid4()` in a header | IDENTICAL | IDENTICAL | shimmed |
| `random.random()` | IDENTICAL | IDENTICAL | shimmed |
| `random.randint()` | IDENTICAL | IDENTICAL | generator state restored |
| server-minted id, echoed back | IDENTICAL | IDENTICAL | **safe by construction** |
| JSON key order | UNCAPTURED_SOURCE | IDENTICAL | loose handles it, as documented |
| float precision `0.1+0.2` | UNCAPTURED_SOURCE | IDENTICAL | loose handles it, as documented |
| **`datetime.now()` in the prompt** | UNCAPTURED_SOURCE | UNCAPTURED_SOURCE | **breaks** |
| **`os.urandom()`** | UNCAPTURED_SOURCE | UNCAPTURED_SOURCE | **breaks** |
| **`secrets.token_hex()`** | UNCAPTURED_SOURCE | UNCAPTURED_SOURCE | **breaks** |
| **a header that changes out of band** | HEADERS_CHANGED | HEADERS_CHANGED | **breaks** |
| a changed prompt — *negative control* | UNCAPTURED_SOURCE | UNCAPTURED_SOURCE | must diverge, and does |

**Four environment sources break unchanged code even in loose mode.** All four
are safe to neutralise by the test above. The negative control diverges in both
modes, so nothing here is being hidden.

`datetime.now()` is the one that matters most, because putting the current time
into a prompt is ordinary. The shim patches `time.time` and `time.time_ns`;
CPython's `datetime.now()` reads the clock directly and never passes through
either.

The echoed-id row is the pleasant surprise: a conversation id minted by the
provider and sent back by the agent replays cleanly **because** replay serves
the recorded response, so the agent reads the recorded id and echoes that.
Nothing needed to be added for it to work.

---

## Result 2 — the vendors' own clients

The OpenAI and Anthropic SDKs, against a local provider, recorded and replayed
with the agent unchanged.

| client | case | verdict |
|---|---|---|
| openai 2.24.0 | same process | IDENTICAL |
| openai | fresh process — what CI does | IDENTICAL |
| openai | recording made through a retry | IDENTICAL |
| anthropic 0.84.0 | same process | IDENTICAL |
| anthropic | fresh process | IDENTICAL |
| anthropic | recording made through a retry | IDENTICAL |

**Six of six.** Both clients attach nine to eleven headers of their own, and
`x-stainless-retry-count` genuinely varies — the retry probe confirms the retry
happened, not just that it was configured.

It replays anyway, and the reason is worth stating because it corrected a
prediction I had made. I expected the retry counter to break the verdict: the
recording is made at `retry-count: 1` and a replay cannot have a transient
failure, so it would send `0`. That is wrong. **The failed attempt is itself a
recorded step.** The replay reproduces it — first attempt at `0` matching the
recorded 500, then the retry at `1` — so the whole sequence lines up.

The rule this establishes is more useful than the prediction was:

> A header that changes **as part of recorded behaviour** replays cleanly.
> A header that changes **for a reason not in the recording** breaks the verdict.

The synthetic `changing header` row in Result 1 is the second kind. The SDK
retry counter is the first.

---

## What this does and does not license

**Supported.** For an agent whose nondeterminism comes from `time`, `uuid`,
`random`, from the provider's own identifiers, or from the vendor SDKs' header
machinery — including retries — Orientim replays unchanged code cleanly, in a
fresh process, with no configuration at all. That is a stronger result than the
lab could give, because none of it was built to be replayable.

**Not supported, and not claimed.** The agents in `sdk_probe.py` are trivial:
one call each. What was tested is the vendors' *HTTP clients*, not a full agent
framework's loop. No LangChain, no Agents SDK, no LlamaIndex. And the provider
is local and deterministic — real providers vary their *responses*, but on
replay responses come out of the file, so that is not the risk surface.

**The gap that remains.** Four environment sources break, three of them silently
under both matching modes, and there is no per-field escape hatch to declare.

---

## Result 3 — how far an agent gets past a divergence

I predicted that the step-0 collapse was the agent's fault: replay answers an
unmatched request with a real `httpx.Response(599)` and hands control back, and
the lab agents raise `KeyError` reading a response shape they did not expect.
That part is true. The conclusion drawn from it was wrong.

Four postures crossed with two agent shapes, recorded on v1 and replayed on v2
where only the first model request differs:

| shape | posture | recorded | reached | answer produced | evaluators answered |
|---|---|---|---|---|---|
| model-driven | naive | 4 | **0** | no | 2 of 4 |
| model-driven | status check | 4 | **0** | no | 2 of 4 |
| model-driven | retry x3 | 4 | **0** | no | 2 of 4 |
| model-driven | tolerant | 4 | **0** | yes | 3 of 4 |
| plan-driven | naive | 4 | **0** | no | 2 of 4 |
| plan-driven | status check | 4 | **0** | no | 2 of 4 |
| plan-driven | retry x3 | 4 | **0** | no | 2 of 4 |
| plan-driven | tolerant | 4 | **0** | yes | 3 of 4 |

**Tolerance buys no additional matched steps at all.** The plan-driven tolerant
agent made four requests that were byte-identical to what had been recorded and
matched none of them. That points at the matcher, not the agent.

### Why: the cursor does not advance past a miss

`_take_ordered` claims only the **head** of the recorded queue. A request that
does not match the head is not served, and the cursor stays where it was — so
every later request is compared against a step that has already been passed by
and can never match.

A straight-line agent of six identical steps, with one step's body changed,
tests it directly:

| change at | matched | unmatched | seconds | verdict |
|---|---|---|---|---|
| step 0 | 0 of 6 | 6 | 16.3 | NO_MATCH_AT_ALL |
| step 1 | 1 of 6 | 5 | 13.4 | UNCAPTURED_SOURCE |
| step 2 | 2 of 6 | 4 | 10.6 | UNCAPTURED_SOURCE |
| step 3 | 3 of 6 | 3 | 7.6 | UNCAPTURED_SOURCE |
| step 4 | 4 of 6 | 2 | 4.5 | UNCAPTURED_SOURCE |
| step 5 | 5 of 6 | 1 | 1.5 | UNCAPTURED_SOURCE |

`matched == steps before the change`, six times out of six. **One unmatched
request ends the observable part of a replay**, and no property of the agent
changes that.

### The second symptom: tolerance is actively penalised

The seconds column falls by 2.8–3.1 per step, against a `ReplayTransport`
default `timeout=3.0`. Each request after the divergence waits the full timeout
before missing — because its key *is* still in the pool ahead of the cursor, so
`_take_ordered` waits in case another thread consumes the head first.

That cost is paid only by an agent that keeps going. The lab agents crash on the
first 599 and pay nothing, which is why this never showed up in the lab: a
diverged replay there is fast precisely because the agent gave up. An agent that
handles errors properly pays `timeout × (steps after the divergence)` and
receives nothing for it.

### What tolerance does buy

One thing, and it is small but real: the tolerant agent still reaches the end
and declares an output, so `output_matches` becomes answerable — three of four
evaluators instead of two. Nothing else.

### Status

Reported, not fixed. Core is frozen, and this touches request matching, which
is explicitly out of bounds. The design question it raises is whether a request
that *differs from* the head should consume it — the agent's first call did
happen, it was simply different — which is already the assumption
`diff.merge_unmatched` makes when it puts unmatched requests back in position.
That is a change to replay semantics and belongs to a deliberate decision, not
a patch.

```bash
python study/tolerance.py
```

---

## Raw results

`study/_runs/_probe.json` and `study/_runs/sdk/_sdk.json`, rewritten on every
run.
