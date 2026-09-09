# Orientim — implementation plan

Follows [AUDIT.md](AUDIT.md). Ordered by dependency, not by appeal. Each phase
ships on its own and leaves the repository green.

The governing constraint from the audit: **the stored format records what
crossed the wire, not what the agent did.** Evaluation, cases and an explaining
diff all read an execution model that does not exist yet. So the format comes
first — and the migration comes before the format.

---

## Phase 0 — Make true what is already claimed

Small, immediate, no behaviour change. It closes the distance between what the
repository asserts and what it proves.

### 0.1 Resolve the S3 claim

`docs/limits.md` and `docs/recordings.md` state the S3 backend is tested against
`moto`. It is not. `moto` is already a dev dependency.

Write `tests/test_storage.py` covering `S3Backend` through moto: `write`, `read`,
`list` (including pagination past one page), `exists`, `stat`, `delete`, and
`url` returning a signed URL. Also cover `open_store` routing for `s3://`,
`memory://`, `file://` and a bare path, and `split_locator` for each.

If any part cannot be tested, delete the corresponding sentence from the docs
instead. The claim and the test suite must agree in one direction or the other.

### 0.2 Tests for `stability.py`

203 lines, a headline README feature with its own CLI command, zero tests.

Cover: a deterministic agent yields one path and 100% agreement; an agent that
branches yields the expected number of distinct paths; control limits are
computed from the mean moving range; a run whose function raises is counted in
`failed` and does not masquerade as agreement; `measure` survives `record()`
itself failing without double-counting the previous run.

### 0.3 Tests for the CLI handlers

`cmd_ls`, `cmd_play`, `cmd_view`, `cmd_diff`, `cmd_prune`, `cmd_ci`,
`cmd_conformance` have no tests. They are the primary interface.

Drive them through `cli.main([...])` against a temporary root, capturing stdout.
Assert on exit codes and on the presence of the lines a user depends on — not on
exact formatting, which should stay free to change.

### 0.4 A smoke test for the live replay server

`server.serve` is untested beyond its auth helper. Start it on an ephemeral port,
fetch the page, call `/api/replay` without a token and assert refusal, call it
with the token and assert a replay runs. Shut it down cleanly.

**Done when:** every advertised component has at least one test that can fail,
and no document makes a claim the suite does not back.

---

## Phase 1 — Execution model v2

The linchpin. Nothing in the product vision is reachable without it.

### 1.1 Migration first, format second

Non-negotiable ordering. `FORMAT` is 3 today. A recording below it sets
`stale`, and `session.py:602` excludes `stale` from `ok` — so the moment the
format becomes 4, **every recording already on disk stops being able to reach
`IDENTICAL`**. It still replays; it just permanently reports as a failure. That
is worse than refusing it, because it is a silent false alarm on history that
was fine.

Build `store.migrate(meta, steps)` before anything else changes:

- Readers accept format 3 **and** 4.
- A format 3 recording is upgraded **in memory** on load: new fields take
  explicit defaults (`outcome: null`, `role: "unknown"`, `model: null`).
- Files on disk are never rewritten. Migration is a read-time concern.
- A migrated recording is **not** `stale`, so it can still be `IDENTICAL`.
  `stale` is reserved for a format no migration handles.

Ship a checked-in format 3 fixture and a test that it still replays under the new
code. That test is the contract; it must exist before the format moves.

### 1.2 Runtime metadata

Cheapest item with real value. Record, in `_meta`:

```
runtime: { python, platform, orientim, libraries: {httpx, httpx2, requests, openai, anthropic} }
```

Source 20 — library version drift — is a declared limit precisely because we
cannot replay it. Recording the versions turns "we cannot capture that" into "we
can tell you it changed", which is most of the value at a fraction of the cost.

### 1.3 Final output

The audit surfaced a genuine API constraint: `record()` is a context manager, so
the agent's return value never passes through Orientim. There is no way to
capture it implicitly, and inventing one would be magic that breaks in ways
nobody can debug.

Two honest options:

**(a) Explicit, additive — recommended.**

```python
with orientim.record() as run:
    answer = my_agent(question)
    run.output = answer          # or run.set_output(answer)
```

**(b) A function-shaped API alongside the existing one.**

```python
run = orientim.execute(my_agent, question)   # captures the return value
```

Recommendation: (a) now, because it is additive, breaks nothing, and requires no
migration of user code; (b) later if usage shows the block form is the friction.

(a) is confirmed feasible against the current code: `record()` already yields a
`_Holder`, and `rec.save()` runs in the `finally` block *after* the body, so an
attribute set inside the block is available at write time. It costs one field on
`_Holder` and one line in `Recording.meta()`.

The output is stored through the same redaction path as a body. `replay()`
already captures the replayed function's return value — added for `check=` — so
the comparison "recorded answer vs replayed answer" becomes available the moment
the recorded side exists.

### 1.4 Step classification (a hint, never a claim)

Add `role` to each HTTP step: `model`, `tool`, or `unknown`.

Derived from the request: host and path against known provider shapes
(`/v1/chat/completions`, `/v1/messages`, `/v1/responses`, …). Everything else is
`tool` when it left the process and `unknown` when the shape is unrecognised.

This is inference and it will sometimes be wrong. Therefore:

- it is stored as a hint, never used to decide whether a step matches;
- replay never depends on it;
- it is overridable;
- the documentation calls it a heuristic in those words.

### 1.5 Model metadata

For steps classified `model`, extract from the request body: model name,
temperature, `stream`, `max_tokens`, and whether tools were offered.

Enables "changed model configuration" in the diff, which is one of the most
common real causes of an agent changing behaviour and is currently invisible.

### 1.6 Tool calls

For model responses that request tool use, extract the tool names and arguments
(OpenAI `tool_calls`, Anthropic `content[].type == "tool_use"`).

Enables `used_required_tool()` in evaluation and "changed tool arguments" in the
diff. Provider-specific, degrades to empty when the shape is unfamiliar — never
to a wrong answer.

**Done when:** a recording answers *what the agent did* — which model, which
tools, what it finally returned — and every format 3 recording still replays.

---

## Phase 2 — Evaluation, cases, baselines, `orientim test`  ·  **done**

Shipped. `docs/evaluation.md` and `docs/regression.md` are the reference; what
follows is the plan as written, kept for the record.

Now buildable, because there is something to evaluate.

### 2.1 Evaluators

An evaluator takes a completed execution and returns pass, fail or warn with a
reason. Built-ins, each justified by a question people actually ask:

| Evaluator | Question |
|---|---|
| `output_equals` / `output_matches` | did the answer change |
| `used_tool(name)` | did it call what it was supposed to |
| `did_not_call(name)` | did it do something it must not |
| `max_steps(n)` | did it loop |
| `no_step_failed()` | did any call error |

Plus any user callable. Results carry a structured explanation, not a boolean.

### 2.2 Cases

A case is a recording, an entry point, and an expected outcome:

```
orientim case save runs/run_2bea9035.jsonl --name order-support --entry app:run
orientim case list
orientim case run order-support
orientim case delete order-support
```

Stored beside the recordings, in the same jsonl-adjacent, greppable style. The
entry point belongs to the case — the current `orientim ci` applies one entry to
a whole directory, which is why per-case entries do not exist yet.

### 2.3 Baselines

A baseline is the result of every case at a commit. `baseline create` writes one;
`baseline compare` says which cases changed since. The existing `--baseline`
report comparison in `ci.py` is the seed and will be folded into this rather than
duplicated.

### 2.4 `orientim test`

Runs every case, evaluates, compares to the baseline, prints a human summary and
a machine-readable report, exits 0 or 1.

**Done when:** a company can put `orientim test` in CI and have it fail for the
right reason, with an explanation a reviewer can act on.

---

## Phase 3 — A diff that explains  ·  **done**

Shipped. `docs/diff-contract.md` is the reference; what follows is the plan as
written, kept for the record.

Currently `diff.compare` walks index by index, so one inserted call marks every
later row as different, and "changed order" cannot be expressed at all.

Replace the positional walk with sequence alignment over step digests
(`difflib.SequenceMatcher` is sufficient and is in the standard library). The
opcodes give, directly and correctly: **new call**, **missing call**, **changed
step**, and — by comparing the aligned sequences — **changed order**.

On top of the alignment:

- body-level differences for changed steps, not just "different response";
- model configuration differences, from 1.5;
- tool argument differences, from 1.6;
- final answer difference, from 1.3;
- consequence: where the agent's own subsequent requests began to differ, which
  the counterfactual path already computes.

---

## Phase 4 — Only when a user asks

Multi-repo, GitLab, policy enforcement, control plane. Written down so they are
not forgotten, deliberately unscheduled. The strongest available demand signal —
someone hitting `UNCAPTURED_LIBRARY` and asking for `aiohttp` — has not arrived.

---

## What this plan deliberately does not do

- **No rewrite.** Every phase is additive on a codebase that is green.
- **No public API breakage.** `record`, `replay`, `assert_replays` keep their
  signatures; `output` and `check=` are additions.
- **No cloud.** Everything above runs on the developer's machine and in their
  CI. Nothing here needs a server.
- **No dashboard** before the diff explains itself in a terminal.
- **No new integrations** without tests, and none at all without a user.
- **No promise of absolute determinism.** Sources 15, 18, 19 and 20 stay
  declared limits, and step classification stays a documented heuristic.

---

## Definition of done

Per phase, and per feature inside it. A thing is done when all of these hold —
not when the code exists:

1. it works on a real execution, not only in a unit test;
2. it has a test that can fail;
3. it is documented, including its limits;
4. there is an example a stranger can run;
5. it handles its own errors;
6. it breaks nothing that worked before — the suite and CI are green;
7. its output is legible to someone who did not write it.

---

## Documentation this plan produces

Written as the phases land, not in advance:

| Document | Phase |
|---|---|
| `docs/execution-model.md` | 1 |
| `docs/replay-contract.md` | 1 |
| `docs/evaluation.md` | 2 |
| `docs/regression.md` | 2 |
| `docs/diff-contract.md` | 3 |
| `docs/roadmap.md` | maintained throughout |

`docs/architecture.md`, `docs/limits.md` and `SECURITY.md` already exist and are
updated in place as behaviour changes — the audit found that stale claims in them
are the failure mode to avoid.

---

## Sequencing summary

```
Phase 0  make true what is claimed        small, immediate, unblocks trust
   │
Phase 1  execution model v2               migration → runtime → output → kind → model → tools
   │
Phase 2  evaluation, cases, baselines, orientim test
   │
Phase 3  diff that explains
   │
Phase 4  multi-repo (only on demand)
```

Phase 0 can start now and is independent. Phase 1.1 — the migration layer and its
format 3 fixture — must land before any field is added to the format.
