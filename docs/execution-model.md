# The execution model

A recording used to answer one question: which HTTP calls happened, in what
order, with what bytes. That is enough to detect **that** an agent's behaviour
changed. It is not enough to say **what** changed.

Format 4 adds four things, so the second question has an answer:

| | what it answers |
|---|---|
| **typed steps** | was this call the model thinking, or the agent acting |
| **model metadata** | which model, at what temperature, with which tools offered |
| **final output** | what the agent actually returned |
| **agent metadata** | whose agent this was, and at what version |

Plus the runtime the run happened in, because a library that moved between a
recording and a replay is the most common explanation for a divergence nobody
can otherwise account for.

## The one rule everything here obeys

**Nothing in the execution model can decide whether a replay matches.**

The hash chain is computed from an allowlist of fields (`chain.DIGEST_FIELDS`):
method, URL, header fingerprint, status, response digest, plus the lookup key
and the error. Everything on this page is outside that list. A recording with
every field below stripped out replays to exactly the same verdict as one with
all of them.

That is not an accident of the implementation, it is the design. Classification
is a heuristic over URLs and request shapes, and heuristics are wrong sometimes.
A wrong hint costs a misleading label in a report — annoying, visible, fixable.
A wrong match would cost a false `IDENTICAL`, which is the one thing this tool
must never produce. So the hint is kept away from the match.

The suite asserts it directly: `t_classification_does_not_touch_matching`
inverts every `role` in a recording, replaces the model metadata with a lie, and
requires `IDENTICAL` anyway.

## Typed steps

Each HTTP step carries a `role`:

- `model` — a call to an inference endpoint
- `tool` — anything else the agent did to the outside world
- `unknown` — we could not parse the URL at all

It is called `role` and not `kind` because `kind` already means something else
on a `shim` step (`"time"`, `"random"`). One field name with two meanings in one
file is how a reader, or a loop over every step, gets it wrong.

### How the guess is made

A call is a **model** call when either:

1. the host is one we recognise — `api.openai.com`, `api.anthropic.com`,
   `generativelanguage.googleapis.com`, `*.openai.azure.com`, Bedrock, Mistral,
   Cohere, Groq, Together, DeepSeek, Fireworks, OpenRouter; or
2. the **path** looks like inference — `/chat/completions`, `/completions`,
   `/responses`, `/embeddings`, `/v1/messages`, Ollama's `/api/chat`, Gemini's
   `:generateContent`, Bedrock's `/invoke` and `/converse` — **and** the request
   body carries a prompt-shaped key (`model`, `messages`, `prompt`, `input`,
   `contents`).

Rule 2 needs both halves. Path alone would label your own `/v1/messages` API a
model call. Host alone would miss every self-hosted vLLM, Ollama, LiteLLM proxy
and company gateway — which is most production traffic that is not going
straight to a provider.

Everything else is a `tool` call, which is true by construction: it left the
process to affect something outside it.

### Where it is wrong

- A model call through a proxy on an unrecognised path, with a body that does
  not look like a prompt, reads as `tool`.
- A non-inference endpoint that happens to sit at `/v1/completions` and takes a
  field called `input` reads as `model`.
- A provider we have not heard of reads as `tool` until its path shape is
  recognised.

None of these change a verdict. All of them change a label.

## Model metadata

For a step with `role: "model"`, a `model` object is stored:

```json
{"model": "gpt-4o-mini", "temperature": 0.3, "max_tokens": 256,
 "stream": false, "top_p": 1.0, "seed": 42,
 "tools_offered": ["lookup_order", "send_email"]}
```

Read out by **allowlist**, not by copying the body minus the messages. A copy
would eventually carry a field somebody put a credential in. The whole request
body is already stored — redacted — in `req`; this exists so a diff can say
"temperature moved from 0 to 0.7" without re-parsing a prompt.

`tools_offered` holds **names only**. A tool schema is large and frequently
generated; what is worth diffing is whether the set changed, not how a
description was reworded.

The knobs are read from where each provider puts them: top level for
OpenAI-shaped APIs, `generationConfig` for Gemini, `inferenceConfig` for
Bedrock's Converse. The model name comes from the body, or from the URL for
Bedrock (`/model/<id>/invoke`), Gemini (`/models/<id>:generateContent`) and
Azure (`/deployments/<name>/…`), which name it there instead.

### From the response

A `served` object, when the response yields one:

```json
{"model_served": "gpt-4o-mini-2024-07-18",
 "usage": {"input_tokens": 11, "output_tokens": 7}}
```

`stop_reason` joins it only where the provider puts it at the top level of the
body, which Anthropic does and OpenAI does not — OpenAI nests `finish_reason`
inside `choices[]`, and this reads the top level only. Absent rather than
guessed at, like everything else here.

`model_served` is worth having on its own: an alias like `gpt-4o-mini` resolves
to a different concrete model over time, and that is a behaviour change with no
diff anywhere in your code.

For streamed responses the events are scanned — bounded at 400 — for the ones
carrying usage and the model name. Providers put usage in the final events, so
it is usually recoverable. When it is not, `served` is simply absent. Nothing
here ever guesses.

## Final output

The one thing Orientim cannot see for itself.

Everything else is observed at the HTTP boundary. The value an agent returns
never crosses that boundary: `record()` is a context manager, so the return
value goes to the caller's own variable and nothing passes through us. There is
no way to capture it implicitly, and every way to fake one — walking stack
frames, re-invoking the callable — fails in exactly the situations where a
person most needs to trust the recording.

So it is declared:

```python
with orientim.record() as run:
    run.output = my_agent("where is order 4471")
```

Left unset it stays `None`, recorded as *not declared* rather than *returned
nothing* — a distinction a report has to be able to make.

### How it is stored

```json
{"kind": "text", "sha": "9f2a…", "len": 812, "value": "Order 4471 shipped…"}
```

- `kind` is `text`, `json`, `repr` or `unavailable`. An object that is not
  JSON-serialisable is stored as its `repr` and **marked** as one, so nobody
  mistakes it for structured data. One that cannot be captured at all is
  recorded as an explicit failure rather than silently dropped.
- The value is redacted with the same rule as a request body.
- In a `repr` capture only, a memory address (`<Summary object at 0x7f…>`) is
  normalised away. It differs on every run, so leaving it in would report
  `OUTPUT_CHANGED` forever on an answer that never moved — and it is provably
  not part of the value. An address inside a string **you** built is your data
  and is left alone.
- It is truncated at 64 KB — but **`sha` is taken over the whole value, before
  truncation**. Two long answers differing only past the limit must not compare
  equal; a tool that exists to detect change cannot have a length above which it
  stops detecting it.

### What it buys you

A new verdict, `OUTPUT_CHANGED`: every HTTP call replayed identically — same
requests, same responses, same order — and the agent still returned something
else. The difference did not come over the network. It came from inside the
process: unshimmed randomness, dict or set iteration order, a local file, a
cache, a code path reading the clock outside our shims.

That is the one divergence the HTTP boundary cannot explain, which is exactly
why it is worth reporting on its own instead of as "chain hash differs".

## Agent metadata

Cannot be inferred — a process does not know which product it belongs to. So it
is declared, as a name or as a dict:

```python
with orientim.record(agent="support-bot") as run: ...

with orientim.record(agent={"name": "support-bot",
                            "version": "2.1.0",
                            "framework": "langgraph",
                            "commit": "a3f91c2"}) as run: ...
```

Or once, in the environment, so a deployment does not repeat it at every call
site:

```
ORIENTIM_AGENT=support-bot
ORIENTIM_AGENT_VERSION=2.1.0
```

## Runtime

Recorded automatically in `_meta.runtime`:

```json
{"python": "3.12.4", "platform": "linux", "orientim": "0.1.0",
 "libraries": {"httpx": "0.28.1", "openai": "3.1.0", "requests": "2.32.3"}}
```

Library version drift — source 20 in
[nondeterminism.md](nondeterminism.md) — is a **declared limit**, and recording
the versions does not fix it. We cannot make a replay use the `httpx` that
recorded it, and pretending otherwise would be the kind of promise this project
does not make.

What it does is convert *"your replay diverged and we cannot tell you why"* into
*"your replay diverged, and `openai` went from 2.3 to 3.0 in between"* — which
is the sentence somebody actually needs. It is reported alongside a divergence
and **never** counts as one on its own: a verdict nobody can act on is not worth
failing a build over.

`report.runtime_changed` holds the list.

## Migration

`FORMAT` went from 3 to 4. No recording on disk was invalidated, and none was
rewritten.

### Why this was the hard part

`stale` is not a refusal. A recording below the current format still replays —
it is just excluded from `ok`. So bumping the format without a migration would
have left **every recording already on disk permanently unable to report
`IDENTICAL`**. Recordings that were fine would start reporting as failures: a
silent false alarm across history nobody has a reason to re-examine. That is
worse than refusing to read them.

### What happens instead

Format 3 recordings are upgraded **on read, in memory**:

- new metadata fields take explicit nulls — `outcome`, `agent` and `runtime`
  were never recorded and cannot be recovered after the fact;
- `migrated_from: 3` records what actually happened;
- the file on disk is **never** rewritten. A recording is a record of something
  that happened; editing it in place to suit a newer version of the tool is the
  wrong instinct.

Because the migration happens in `store.parse`, every consumer — replay, `ls`,
`view`, `diff`, `ci`, the viewer — gets migrated data without knowing migration
exists.

### Old recordings gain typed steps

Classification and model metadata are **pure functions of the request**, and the
request was always stored. So a format 3 recording is enriched retroactively: it
gets the same `role` and `model` fields a new one would. The execution model is
useful on day one rather than only for runs recorded from here on.

### Below format 3, nothing is migrated

Format 2 stored a step digest computed a different way. "Migrating" it would
answer a question it was never asked, so it keeps reporting `STALE_FORMAT`. The
boundary is `store.MIGRATABLE_FROM`.

### What guarantees this is safe

`t_migration_preserves_chain` records a run, writes it back out as format 3
would have written it, and asserts the chain root is **byte-identical** before
and after migration. If that ever fails, every recording in every user's history
silently changes meaning and nothing else in the suite would catch it.

## Field reference

Added to `_meta`:

| field | |
|---|---|
| `format` | `4` |
| `migrated_from` | present only on a recording upgraded on read |
| `outcome` | the declared final output, or `null` |
| `agent` | declared agent identity, or `null` |
| `runtime` | python, platform, orientim and library versions |

Added to an `http` step:

| field | |
|---|---|
| `role` | `model`, `tool` or `unknown` |
| `model` | request-side model metadata, on `model` steps that yield any |
| `served` | response-side metadata: usage, model served, stop reason |

None of these are in `chain.DIGEST_FIELDS`, and none of them ever will be.
