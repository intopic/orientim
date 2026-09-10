# The explanatory diff

Three questions, three answers:

| | |
|---|---|
| **replay** | *where* did it change |
| **evaluation** | *which rule* broke |
| **diff** | *how* did the execution change, and what followed |

```bash
orientim diff --case order-support        # the recording vs a fresh replay
orientim diff run_2bea9035 run_7c31a0f2   # two recordings
orientim diff --case order-support --json # machine-readable
```

```
==========================================================================
  CHANGED: order-support
==========================================================================

  A  run_1a3c4bf1          2 steps  a066407b8cf7
  B  now                   2 steps  4f21c9de0aa1
  replay verdict: NO_MATCH_AT_ALL

  2 changed, 0 inserted, 0 deleted, 0 reordered, 0 unchanged

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
     digest:   3be047922074 -> 510c50f81aac

  4. EVALUATION
     FAIL output_matches: the answer does not match 'not found'
     WARN used_tool: lookup_order could not be established: all 1 model
                     call(s) in this run went unanswered, so what the model
                     would have asked for is unknown

  STEPS
     ~   0-> 0  *POST completions      *POST completions   not matched, so the agent got a 599
            model: gpt-4o-mini -> gpt-4o
            temperature: 0.0 -> 0.7
            request ~ temperature: 0.0 -> 0.7
     ~   1-> 1   POST search            POST search        not matched, so the agent got a 599

  CONSEQUENCE
    model configuration changed (model, temperature)
      ↓ the final answer changed
        ↓ output_matches failed: the answer does not match 'not found'

    These are observations in order, not a causal chain:
    consequence observed, causal link not established.
    Established by the records: the evaluation is about the answer
```

## Alignment, not index-by-index

The old diff walked both runs by position: step 1 against step 1, step 2
against step 2. That is correct only when nothing was inserted or removed.
Insert one call near the front and every later step reads as different — a run
that changed in one place reports as a run that changed everywhere.

So the steps are lined up first, the way a text diff lines up lines. Five
outcomes:

| | |
|---|---|
| `SAME` | aligned, and identical under the active key |
| `CHANGED` | aligned, and not identical |
| `INSERTED` | in B, with nothing in A to align it to |
| `DELETED` | in A, with nothing in B |
| `REORDERED` | in both, in a different position |

`REORDERED` is why this matters beyond tidiness. Four steps that swapped places
are **one** thing that happened, and reporting them as four independent changes
buries it.

Two steps are lined up on **method and URL**, deliberately not on the body: a
model call whose temperature changed is the same call with a different
question, and reporting it as one call removed and another added would throw
away the comparison somebody opened the diff for.

### This is an explanation layer, never a verdict

Replay matches a live request against a recorded one by lookup key,
deterministically, and that is a different problem with a different correctness
bar. Nothing here touches the hash chain, the lookup key, or how a replay
decides a match. If the alignment is wrong the report is confusing — a cost paid
by a person reading it, not by a build.

## What it explains

### Model configuration

By field, not as "the body differs":

```
model: gpt-4o-mini -> gpt-4o
temperature: 0.0 -> 0.7
```

`model`, `temperature`, `top_p`, `max_tokens`, `seed`, `stream`,
`tools_offered`, and from the response `model_served`, `stop_reason` and token
usage.

When one side is a request the replay could not match, the response side is
skipped. Otherwise the report says `output_tokens: 8 -> None` about a call that
was never answered, which reads as a change in the model and is nothing of the
kind.

### Tool calls

Added, removed, called with different arguments, or moved. Each carries the
name, the arguments, the step, `arguments_kind`, and whether either side is a
`partial` stream fragment.

**No link is invented between a tool request and an HTTP call that might have
executed it.** A tool name is not a URL. The diff says what the model asked
for; whether your code ran it is a decision made where we cannot see. Better
unknown than false provenance.

### Bodies

JSON bodies diff by field path, including nested objects and list indices:

```
request ~ generationConfig.temperature: 0.0 -> 0.7
response + choices[0].message.tool_calls[1].id = call_1
```

Anything else diffs as compact text. Both are capped, and the report says when
it truncated.

**Redaction is not re-implemented here.** Bodies are already redacted when the
recorder writes them, so the diff reads what is on disk and nothing else. This
turns out to be the strongest possible arrangement: both sides store a
credential as the same placeholder, so the field cannot differ, so it is never
printed at all.

### The final answer

When both runs declared `run.output`:

```
expected: order 4471 not found
actual:   your order shipped
digest:   3be047922074 -> 510c50f81aac
```

The digest is over the whole value before truncation, so *changed* and
*unchanged* are always safe to say. What is **shown** may be a prefix, and when
it is the report says so and skips the structured comparison — a field-level
diff of two truncated prefixes would be a claim about text that was cut off.

When only one side declared an output, the state is `unknown`, not `changed`.

### When a tool decision cannot be read

A tool request lives in a model *response*. A replay that diverged at its first
request never got one, so that side made no tool requests at all — and every
tool the recording asked for would compare as "removed" whatever the agent
actually did. Measured on a four-agent lab: a change that added one argument to
`order.lookup` reported as `order.lookup no longer requested`, which is true of
the replayed run and false about the agent.

So when every model call on either side went unanswered, the tool comparison is
withheld and the report says which side is blind. `tool_changes` is empty and
`tool_view_unreadable` carries the reason. The comparison becomes readable again
by recording both sides and diffing two recordings:

```bash
orientim diff run_1a3c4bf1 run_9f0c2ee4
```

The same rule already governs the response half of `model_changes`: a field that
only exists in a response is not compared against a response that never arrived.

## The consequence chain

The part most worth being careful about.

```
model configuration changed (model, temperature)
  ↓ the final answer changed
    ↓ output_matches failed
```

It would be easy to print *root cause: the temperature change*, and it would
often even be right. But nothing in an HTTP recording establishes that one
difference **caused** another. What the records establish is weaker and
checkable:

| relation | established? | what it means |
|---|---|---|
| `earlier-in-run` | **no** | one difference is before the other, in order |
| `names-the-same-tool` | **yes** | an evaluation's own evidence names a tool that changed |
| `same-subject` | **yes** | an evaluation about the answer, and the answer changed |

Every link carries `established: true` or `false`. When any link is unproven the
report says:

> consequence observed, causal link not established

That is the honest sentence, and for somebody debugging it is more useful than a
confident guess they would have to verify anyway.

**Nothing in this chain is generated by a language model.** It is built only
from fields already in the records, so every line can be checked against the
recording it came from.

## Performance

`difflib.SequenceMatcher` is O(n·m) in the worst case, and the worst case is
the shape agents actually have: thousands of steps drawn from a handful of
endpoints. Measured before the budget existed, 3000 such steps fully reordered
took **110 seconds**.

There is a budget. Below `align.CHEAP` (n·m ≤ 200 000) the full alignment always
runs. Above it, a sequence whose distinct calls are few relative to its length
takes a linear path instead — common prefix, common suffix, the middle paired in
order — which is correct for the ordinary shapes and gives up only on finding
moves.

Measured on the development machine, worst case per scenario:

| steps | distinct calls | identical | insert at head | reversed |
|---|---|---|---|---|
| 10 | 5 / 10 | 0.3 ms | 0.2 ms | 0.3 ms |
| 100 | 5 / 100 | 2.1 ms | 2.1 ms | 6.0 ms |
| 1000 | 5 | 18 ms (linear) | 21 ms (linear) | 1.4 ms (linear) |
| 1000 | 1000 | 23 ms | 24 ms | 24 ms |
| 3000 | 5 | 57 ms (linear) | 65 ms (linear) | 2.7 ms (linear) |
| 3000 | 3000 | 103 ms | 119 ms | 75 ms |

When the linear path is taken the report says so:

```
NOTE: 3000 and 3000 steps drawn from 5 distinct calls: the full alignment is
quadratic on a sequence this repetitive, so steps were paired in order and
moves were not looked for.
```

Silently returning a worse answer would be the one unacceptable option. For
scale: `record()` caps the ring at 512 steps by default, so a recording large
enough to reach the budget is one somebody configured for it.

## Unmatched requests

A replay records a step only when it **matches** a recorded one — the chain is
built from those steps, so an unmatched request must never become one.

That left the diff with nothing to say: an agent whose requests all changed
produced zero replayed steps, and the diff reported "every step deleted" when
the truth was "every step different". So unmatched requests are kept beside the
steps, in `Divergence.unmatched_requests`, and merged back in for the
explanation. They carry `unmatched: true` and status 599, which is what the
agent actually received.

They are also part of what an evaluator judges — without that,
`no_step_failed()` reported "all 0 call(s) succeeded" for a run in which every
request came back 599.

## `--json`

```json
{
  "kind": "orientim-diff", "schema": 1,
  "a": {"run_id": "...", "steps": 2}, "b": {"run_id": "...", "steps": 2},
  "identical": false,
  "counts": {"SAME": 0, "CHANGED": 2, "INSERTED": 0, "DELETED": 0, "REORDERED": 0},
  "degraded": false, "degraded_reason": null,
  "first_difference": {"op": "CHANGED", "a": 0, "b": 0},
  "steps": [{"op": "CHANGED", "a": 0, "b": 0, "why": "...",
             "model": [{"field": "temperature", "was": 0.0, "now": 0.7}],
             "request_body": {"kind": "json", "changed": [...]}}],
  "model_changes": [...], "tool_changes": [...],
  "tool_view_unreadable": {"side": "b", "model_calls": 1,
                           "named_by_the_other_side": ["lookup_order"]},
  "output": {"state": "changed", "sha_a": "...", "sha_b": "..."},
  "runtime_changes": [...],
  "evaluation": [{"status": "fail", "evaluator": "output_matches", ...}],
  "verdict": "NO_MATCH_AT_ALL",
  "consequence": {"nodes": [...],
                  "links": [{"from": 0, "to": 1, "relation": "earlier-in-run",
                             "established": false,
                             "note": "consequence observed, causal link not established"}],
                  "established": false}
}
```

`orientim diff` exits 0 whether or not the two runs differ. A diff is a
question, not a gate; `orientim test` is what a build should fail on.

## What it does not do

- **No cause.** See above. If a link is not established the report says so.
- **No link from a tool request to the HTTP call that ran it.** That would be
  inference dressed as provenance.
- **No second redaction pass.** It reads what the recorder wrote.
- **No move detection on very large repetitive runs.** Announced when it
  happens.
- **No language model anywhere in it.**
