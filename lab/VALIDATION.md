# Product Validation Lab Report

Ten deliberate regressions injected into a running four-agent fleet, measured
with `lab/validation.py`. Every number below was produced by that script; none
is an estimate.

**The grading rule.** A regression is not "detected" because a command exited
non-zero. An exit code says something moved; a developer needs to know *what*.
So each regression carries the signals that must appear in the output — the
model name, the tool name, the changed argument — and a red build without them
is recorded as detected **without** evidence, which is a much weaker result.

---

## Summary

```
REGRESSIONS TESTED         10
DETECTED (with evidence)   10
MISSED                      0
FALSE POSITIVES             0
AVERAGE TIME TO DIAGNOSE    4.7 s
CASES CREATED              32 across 4 agents
LIMITS FOUND                4
```

False positives were measured separately: v1 replayed against its own baseline,
all four suites, **0 failing lines**.

---

## The ten, and where the evidence came from

| # | regression | agent | build | evidence complete from | time |
|---|---|---|---|---|---|
| 1 | `model_change` | supervisor | fails | `diff --case` | 4.5 s |
| 2 | `tool_change` | support | fails | `diff --case` | 4.2 s |
| 3 | `arg_change` | support | fails | **record + diff only** | 4.6 s |
| 4 | `tool_unused` | risk | fails | `diff --case` | 5.1 s |
| 5 | `forbidden_tool` | support | fails | `diff --case` | 4.4 s |
| 6 | `output_change` | support | fails | **record + diff only** | 4.5 s |
| 7 | `new_call` | research | fails | `diff --case` | 4.6 s |
| 8 | `call_removed` | research | fails | `diff --case` | 4.8 s |
| 9 | `order_change` | research | fails | `diff --case` | 5.0 s |
| 10 | `parallel_order` | supervisor | **passes** | **record + diff only** | 5.5 s |

| evidence source | count |
|---|---|
| complete from `orientim test` | **0 of 10** |
| complete from `orientim diff --case` | 7 of 10 |
| needed a second live recording, diffed against the baseline | 3 of 10 |

---

## The finding that matters most

**`orientim test` caught nine of ten and explained none of them.**

For every regression the CI gate said some version of the same thing:

```
!!  shipped-order   7 steps   UNCAPTURED_SOURCE at step 0 — 0 steps matched
     ?  used_tool        order.lookup cannot be checked: all 1 model call(s)
                         in this run went unanswered
    !!  no_step_failed   1 of 1 call(s) failed
```

Three things are wrong with that as a developer's first contact with a
regression:

1. **The failure names a consequence, not a cause.** `no_step_failed` is true —
   the unmatched request got a synthetic 599 — but it is downstream of what
   actually changed.
2. **Every rule written for the change degraded to a warning.** `used_tool`,
   `did_not_call` and `output_matches` all report "cannot be checked", because
   the change diverged the first model call and the evaluators need the model's
   *response* to see a tool request. The rule authored for exactly this
   regression is the one that goes quiet.
3. **The safety rule never fired.** For `forbidden_tool`, `did_not_call(refund.issue)`
   did not fail. It passed, because in the replay the forbidden call was never
   answered and therefore never appeared. The evidence that support tried to
   issue a refund came from `orientim diff`, not from the rule that exists to
   forbid it.

`orientim diff --case` supplied it, immediately and in the right words:

```
1. MODEL CONFIG
   model: gpt-4o-mini -> gpt-4o
2. TOOL DECISION
   no longer requested: order.lookup({"order_id": 4471})
```

**Conclusion: the gate and the explanation are two different commands, and the
gate is the weaker one.** In this lab a red build is a prompt to run a second
command, not an answer.

---

## Why every replay collapsed at step 0

All ten diverged at the very first step. In this fleet the agent passes its
plan inside the model request body, so any behavioural change alters the first
request — and once step 0 does not match, the agent receives a 599 and the rest
of the run has nothing to match against.

Part of that is the lab's shape and would be milder in an agent whose first
prompt is constant. But it is not artificial: changing what an agent asks the
model *is* usually a change to the first request. And the consequence is
general — **an early divergence disables every evaluator downstream of it.**

---

## Limits found

**L1 — the evaluators need a model response to see a tool request.**
When a change diverges the model call, `used_tool` and `did_not_call` report
"cannot be checked" rather than failing. Honest, and it means the rules are
weakest exactly when the agent changed most. *Observed in 8 of 10.*

**L2 — `did_not_call` cannot enforce a prohibition across a divergence.**
A forbidden tool introduced by a change is a NEW_CALL whose response is
synthetic, so no tool call is recorded and the prohibition passes. The diff sees
it; the evaluator does not. *Safety-relevant.*

**L3 — a parallel reorder never fails the build.** By design, and documented:
replay serves recorded steps in the recorded order. `orientim diff` on two
recordings reports `PARALLEL_ORDER_CHANGED`, weak, and a policy can make it
strict. *Intentional, not a defect.*

**L4 — evaluation is per agent; there is no fleet view.** A change inside the
risk agent is invisible in the supervisor's rules. Fleet coverage means a case
per agent — 32 cases for 12 scenarios across 4 agents here.

Two further observations, neither a product defect:

- `orientim test --case X --baseline main` reports every *other* case in the
  baseline as "In the baseline but gone now". Noise from pairing a single-case
  run with a whole-suite baseline.
- The first version of this grader looked for "not requested" where the diff
  says "no longer requested", and marked two real findings as evidence-free. A
  grading string stricter than the product is a measurement error, and it is
  recorded here because the first run of this report was wrong because of it.

---

## What the numbers support, and what they do not

**Supported by this measurement:**

- Behavioural regressions in an agent fleet are detectable — 10 of 10 — from
  recordings alone, with no provider and no key.
- Diagnosis is fast: 4.7 s from injected change to named cause.
- The signal is clean: 0 false positives across 32 cases on an unchanged
  system.
- The explanation is specific enough to act on: field names, tool names,
  arguments, and the answer.

**Not supported, and not claimed:**

- That `orientim test` alone is sufficient. It is not; it detected nine and
  explained none.
- That the prohibition rules hold across a divergence. They do not (L2).
- That a fleet is covered by testing its supervisor. It is not (L4).
- Anything about scale, cost or reliability beyond one machine and twelve
  scenarios.

---

## Reproducing it

```bash
python lab/run.py up
python lab/validation.py        # ~10 minutes, ten fleet restarts
python lab/run.py down
```

Raw results, including the captured output of every command:
`lab/_runs/_validation.json`.
