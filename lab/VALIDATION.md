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

Everything else that is found is split by what it costs to find out: a second
command against the recording already in hand, or a second live recording with
the system up and the change still in place.

The required signals were **not** relaxed between runs. Where the product now
says the same thing in different words, that is reported as a wording gap, not
graded as a pass.

---

## Summary

```
REGRESSIONS TESTED              10
EXPLAINED BY CI ITSELF           4   orientim test alone
DETECTED, NEEDS orientim diff    2   same recording, one more command
DETECTED, NEEDS A 2nd RECORDING  4   the system has to be up again
UNIDENTIFIABLE                   0
FALSE POSITIVES                  0
CASES CREATED                   32   across 4 agents
```

The middle two used to be one number. They are not one cost: `orientim diff
--case` is a command a developer runs against the recording they already have,
while a second live recording needs the system running and the change still in
place. In an incident where the change was already reverted, the second is not
available at all.

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

### 2. Detected, but the explanation is elsewhere — 6 of 10

**2a — a second command against the same recording (2).**

| # | regression | absent from CI | present in `orientim diff --case` |
|---|---|---|---|
| 7 | `new_call` | `inserted` | the step-level view names the inserted call |
| 9 | `order_change` | `reordered` | the alignment names the reordering |

CI does print the change for both, in request fields: `added plan[2][0][0] =
'order.lookup'`, and the two halves of the swap. What it does not print is the
word the grader was written against. Cheap to close, and the second command is
one line away.

**2b — a second live recording (4).** The expensive bucket, and the one worth
arguing about.

| # | regression | absent from CI *and* from `diff --case` | why |
|---|---|---|---|
| 3 | `arg_change` | `arguments changed` | the replay never answered, so no tool request exists on either side of the replay diff |
| 8 | `call_removed` | `no longer requested` | same, and see below |
| 6 | `output_change` | `dear customer` | the run collapsed before producing an answer; there is no new answer to show |
| 10 | `parallel_order` | `parallel_order_changed` | replay serves the recorded order by contract, so scheduling cannot appear in one |

Three notes on that table.

**`call_removed` is in this bucket because of a fix made during this
measurement.** It used to match on `no longer requested`, printed by the tool
comparison — a sentence that a collapsed replay produces whatever the agent
did. Withdrawing it as unsupported moved this regression from the cheap bucket
to the expensive one. That is the honest cost of the correction and it is
recorded here rather than smoothed over.

**`output_change` will not close.** CI names the cause exactly — `request step
0 style: 'plain' -> 'formal'` — but a replay of a diverged run has no new
answer, so no amount of work on the report will make one appear.

**`parallel_order` does not fail the build at all**: `orientim test` exits 0.
See L3.

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

Measured six times: **0** with no evidence block, **5** with the first version
of it, **4** after the unsupported tool claim was withdrawn, and **4** on three
further runs — one against the shipped code, then two after the instrument
learned to tell a second command from a second recording. Every run after the
withdrawal produced the same ten verdicts. Only the last run's raw results are
on disk; `lab/_runs/_validation.json` is rewritten every time.

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
the new evidence block reads, and it is why four of the ten are fully explained
by CI despite the collapse.

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
the fields of the request that carried it. Not wrong, and not the same words.

**L6 — four of ten need a second live recording, not a second command.** A
replay cannot show a tool request that was never answered, an answer that was
never produced, or a scheduling order it is contractually bound to reproduce.
The fix for all four is the same and it is expensive: run the changed system
again, record it, and diff two recordings. In an incident where the change has
already been reverted, that is not available. *This is the strongest limit the
lab found, and it is new in this measurement because the previous instrument
counted a second recording and a second command as the same thing.*

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

One clean run, medians across the ten:

| command | min | median | max | spread |
|---|---|---|---|---|
| `orientim test` | 2.6 s | **3.3 s** | 4.0 s | 1.4 s |
| `orientim diff --case` | 2.5 s | **3.0 s** | 3.6 s | 1.1 s |
| a second recording, diffed | 7.2 s | **8.1 s** | 11.2 s | 4.0 s |
| test + diff, the path walked | 5.4 s | **6.4 s** | 7.2 s | 1.8 s |

**Read these as seconds, not tenths.** The same ten regressions on identical
code, measured three times, gave means of 4.5 s, 6.3 s and 7.1 s for test+diff.
The verdicts were the same all three times; only the clock moved. The
process-start floor is about 0.35 s, so it is not interpreter spawn, and the
per-case spread within a single run (1.4 s on `orientim test` alone) is as
large as the drift between runs. The cause is not isolated, and quoting a
single average to one decimal would imply a precision this instrument does not
have.

What the measurement does support: **diagnosis is a few seconds, not minutes,
and needs no provider, no network and no key.**

---

## What the numbers support, and what they do not

**Supported by this measurement:**

- Behavioural regressions in an agent fleet are detectable — 10 of 10 — from
  recordings alone, with no provider and no key.
- Nine of ten fail the build; the tenth is a scheduling change that replay does
  not gate, by contract.
- Four of ten are fully explained by the failing command itself, up from zero.
- Diagnosis takes seconds, not minutes: a median of 3.3 s for the build's own
  command and 6.4 s for the two-command path. See the caveat above about how
  precisely this can be quoted.
- The signal is clean: 0 false positives across 32 cases on an unchanged
  system.

**Not supported, and not claimed:**

- That `orientim test` alone is sufficient. It explains four of ten in the
  words the grader asked for; two more need one command, and four need the
  system running again.
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
