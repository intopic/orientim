# Product Validation Lab Report

Ten deliberate regressions injected into a running four-agent fleet, measured
with `lab/validation.py`. Every number below was produced by that script; none
is an estimate.

## The grading rule

A regression is **not** "detected" because a command exited non-zero. An exit
code says something moved; a developer needs to know *what*. So each regression
carries the signals that must appear in the output — the model name, the tool
name, the changed argument — and a red build without them is recorded as
detected **without** evidence, which is a much weaker result.

This run adds a second, stricter bar:

> **DETECTED WITH ACTIONABLE EVIDENCE** — `orientim test` *by itself* prints
> enough for a developer to understand what changed, without running a second
> command.

Everything else that is found is **DETECTED, NEEDS `orientim diff`**: real, but
a red build that sends you elsewhere to find out why.

The required signals were **not** relaxed between runs. Where the product now
says the same thing in different words, that is reported as a wording gap, not
graded as a pass.

---

## Summary

```
REGRESSIONS TESTED             10
EXPLAINED BY CI ITSELF          4   (orientim test alone)
DETECTED, NEEDS orientim diff   6
UNIDENTIFIABLE                  0
FALSE POSITIVES                 0
AVERAGE TIME TO DIAGNOSE       4.5 s
CASES CREATED                  32 across 4 agents
```

False positives were measured separately: v1 replayed against its own baseline,
all four suites, **0 failing lines** (supervisor 12.5 s, research 5.4 s,
support 5.3 s, risk 4.9 s — all exit 0).

---

## The four-way breakdown

### 1. Explained by CI itself — 4 of 10

`orientim test` alone names the change.

| # | regression | what CI prints, unaided |
|---|---|---|
| 1 | `model_change` | `model config  model: gpt-4o-mini -> gpt-4o` |
| 2 | `tool_change` | `request  step 0 plan[0][0][0]: 'order.lookup' -> 'kb.search'` |
| 4 | `tool_unused` | `tool decision  risk.score no longer requested` + the answer's own tool list losing `risk.score` |
| 5 | `forbidden_tool` | `request  step 0 added plan[1][0][0] = 'refund.issue'` |

**Disclosure on #5.** Its required signals are `refund.issue` and `requested`.
The first is genuinely present and is the whole substance. The second matched
incidentally, inside the sentence explaining that the tool view is unreadable.
The verdict is right for the right reason, but one of its two strings is a weak
grader string, and that is worth knowing rather than hiding. It was left as it
is: loosening or tightening a grading string after seeing the result is how a
measurement stops meaning anything.

### 2. Detected, but needs `orientim diff` — 6 of 10

All six fail the build (except #10, below) and all six are explained
immediately by the second command. They divide into three different reasons,
and the difference matters more than the count.

**2a — CI names the change at request level, not in tool vocabulary (4).**
The information is there; the words are not the ones the grader was written
against.

| # | regression | required, absent from CI | what CI does print |
|---|---|---|---|
| 3 | `arg_change` | `arguments changed` | `request  step 0 added plan[0][0][1].include_history = True` |
| 7 | `new_call` | `inserted` | `request  step 0 added plan[2][0][0] = 'order.lookup'` |
| 8 | `call_removed` | `no longer requested` | `request  step 0 dropped plan[1][0][0] (was 'shipping.track')` |
| 9 | `order_change` | `reordered` | `request  step 0 plan[0][0][0]: 'kb.search' -> 'shipping.track'`<br>`request  step 0 plan[1][0][0]: 'shipping.track' -> 'kb.search'` |

A developer reading those four lines knows what changed. The report does not
count them as explained anyway, because the bar was set before the run and
moving it afterwards would make the number meaningless. The honest statement is:
**CI shows the change; `orientim diff` names it.**

**2b — CI explains the change but cannot show its result (1).**

`output_change` (#6) makes the support agent answer in a formal template. CI
prints `request  step 0 style: 'plain' -> 'formal'` — the cause, exactly. What
it cannot print is the new answer (`dear customer …`), because under replay the
run diverged at its first model call and never produced one. This is not a
wording gap and will not close: a replay of a diverged run has no new answer to
show. `orientim diff` on two recordings has both.

**2c — invisible to replay by contract (1).**

`parallel_order` (#10) reorders two concurrent children. `orientim test` exits
**0** — the build is green. This is by design and documented: replay serves
recorded steps in the recorded order, so a scheduling change cannot show up in
one. `orientim diff` over two recordings reports `PARALLEL_ORDER_CHANGED`,
weak, and `concurrency.policy` can make it strict. See `docs/concurrency.md`.

### 3. Unidentifiable — 0 of 10

Every regression was located to an agent, a case, and a changed field or tool.

### 4. False positives — 0

32 cases, four suites, v1 against its own baseline. Nothing failed.

---

## What changed in the product during this measurement

**Before.** `orientim test` failed ten out of ten and explained none. The build
failed on `no_step_failed` — a consequence of the divergence — while the
evaluator written for the actual change (`used_tool`) degraded to a warning,
because it needs a model *response* to see a tool request and the diverged call
never got one. The explanation existed, in `orientim diff`. A red build was a
prompt to run a second command.

**Change 1 — the command that fails now explains itself.** A failing case
already holds both halves of a diff: the recording it was made from, and the
steps the replay produced. `orientim test` now computes the explanation from
them and prints it under the failure. Nothing new is recorded, nothing is
computed for a passing case, and the hash chain, replay semantics and the
meaning of PASS/WARN/FAIL are untouched.

**Change 2 — and it stopped claiming one thing it could not know.** The first
version of that block printed `tool decision  order.lookup no longer requested`
for `arg_change` — a regression where the agent *still* calls `order.lookup`,
with one extra argument. A tool request lives in a model response; when every
model call in a run went unanswered, every tool the recording asked for
compares as removed whatever the agent did. The sentence was true of the
replayed run, false about the agent, and printed three lines below a warning
saying that very thing could not be established.

The tool comparison is now withheld when either side has no answers, in both
`orientim test` and `orientim diff`, and the report says which side is blind
and which tools the other side requested. `model_changes` already refused the
same trade on the response side; this was the one place that took it.

The line is between *asked and not answered* — the replay's doing, withheld —
and *never asked*, which is the agent's doing and is still reported. #4
`tool_unused` is the second kind, which is why it keeps its tool line.

**What it cost.** The headline went from 5 to 4. `call_removed` was one of the
five, and it matched on the sentence that has now been withdrawn. A true
statement that is guaranteed by the measurement setup is not evidence, so the
number is lower and the report is more honest.

Measured four times: **0** with no evidence block, **5** with the first version
of it, **4** after the unsupported tool claim was withdrawn, and **4** again on
a clean re-run against the code that shipped — same ten verdicts, timings
within a few hundred milliseconds. The numbers in this report are from that
last run. Only its raw results are on disk; `lab/_runs/_validation.json` is
rewritten every time.

---

## Why every replay collapsed at step 0

Nine of ten diverged at the very first step. In this fleet the agent passes its
plan inside the model request body, so any behavioural change alters the first
request — and once step 0 does not match, the agent receives a 599 and the rest
of the run has nothing to match against.

Part of that is the lab's shape and would be milder in an agent whose first
prompt is constant. But it is not artificial: changing what an agent asks the
model *is* usually a change to the first request. And the consequence is
general — **an early divergence disables every evaluator downstream of it**,
and leaves the request side as the only readable evidence. That is exactly what
the new evidence block reads, and it is why four of the six in category 2 are
still named precisely despite the collapse.

---

## Limits found

**L1 — the evaluators need a model response to see a tool request.**
When a change diverges the model call, `used_tool` and `did_not_call` report
"could not be established" rather than failing. Honest, and it means the rules
are weakest exactly when the agent changed most. *Observed in 8 of 10.* Now
also stated in `docs/limits.md`.

**L2 — `did_not_call` cannot enforce a prohibition across a divergence.**
A forbidden tool introduced by a change is a request the replay never answers,
so no tool call is recorded and the prohibition passes. The evidence that
support tried to issue a refund comes from the request body — `added
plan[1][0][0] = 'refund.issue'` — not from the rule that exists to forbid it.
*Safety-relevant, and unchanged by this pass.*

**L3 — a parallel reorder never fails the build.** By design, and documented.
*Intentional, not a defect.*

**L4 — evaluation is per agent; there is no fleet view.** A change inside the
risk agent is invisible in the supervisor's rules. Fleet coverage means a case
per agent — 32 cases for 12 scenarios across 4 agents here.

**L5 — CI speaks request vocabulary, `orientim diff` speaks tool vocabulary.**
Under a collapsed replay the tool view is withheld, so the change is named by
the fields of the request that carried it. Four of the ten are in this
position. Not wrong, and not the same words.

Two further observations, neither a product defect:

- `orientim test --case X --baseline main` reports every *other* case in the
  baseline as "In the baseline but gone now". Noise from pairing a single-case
  run with a whole-suite baseline.
- The first version of this grader looked for "not requested" where the diff
  says "no longer requested", and marked two real findings as evidence-free. A
  grading string stricter than the product is a measurement error, and it is
  recorded here because the first run of this report was wrong because of it.

---

## Time to diagnose

| # | regression | `orientim test` | `orientim diff` | record + diff | total |
|---|---|---|---|---|---|
| 1 | `model_change` | 2.1 s | 2.1 s | 5.3 s | 4.2 s |
| 2 | `tool_change` | 2.1 s | 2.4 s | 5.6 s | 4.5 s |
| 3 | `arg_change` | 2.2 s | 2.2 s | 5.7 s | 4.4 s |
| 4 | `tool_unused` | 2.2 s | 1.9 s | 6.1 s | 4.1 s |
| 5 | `forbidden_tool` | 2.3 s | 2.2 s | 5.6 s | 4.5 s |
| 6 | `output_change` | 2.4 s | 2.2 s | 5.7 s | 4.6 s |
| 7 | `new_call` | 2.3 s | 2.2 s | 5.8 s | 4.5 s |
| 8 | `call_removed` | 2.6 s | 2.2 s | 5.8 s | 4.8 s |
| 9 | `order_change` | 2.1 s | 2.3 s | 5.8 s | 4.4 s |
| 10 | `parallel_order` | 2.7 s | 2.2 s | 5.9 s | 4.9 s |

`total` is `test` + `diff`, the path a developer actually walks. No provider,
no network, no key.

---

## What the numbers support, and what they do not

**Supported by this measurement:**

- Behavioural regressions in an agent fleet are detectable — 10 of 10 — from
  recordings alone, with no provider and no key.
- Nine of ten fail the build; the tenth is a scheduling change that replay does
  not gate, by contract.
- Four of ten are fully explained by the failing command itself, up from zero.
- Diagnosis is fast: 4.5 s from injected change to named cause.
- The signal is clean: 0 false positives across 32 cases on an unchanged
  system.

**Not supported, and not claimed:**

- That `orientim test` alone is sufficient. It explains four of ten in the
  words the grader asked for; six still want the second command.
- That the prohibition rules hold across a divergence. They do not (L2).
- That a fleet is covered by testing its supervisor. It is not (L4).
- That any of this establishes *cause*. The report prints observations in
  order, and says so.
- Anything about scale, cost or reliability beyond one machine and twelve
  scenarios.

---

## Reproducing it

```bash
python lab/run.py up
python lab/validation.py        # ~10 minutes, eleven fleet restarts
python lab/run.py down
```

Raw results, including the captured output of every command:
`lab/_runs/_validation.json`.
