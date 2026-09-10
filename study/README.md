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

## Raw results

`study/_runs/_probe.json` and `study/_runs/sdk/_sdk.json`, rewritten on every
run.
