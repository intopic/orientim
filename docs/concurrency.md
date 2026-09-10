# Concurrency semantics

The problem, in one line:

```
v1:  research || risk       →  then support
v2:  risk || research       →  then support
```

is a real change in an agent, and **a replay cannot see it**. That is not a
defect. Replay serves recorded steps *in the recorded order* — that is what lets
an agent with four calls in flight reproduce deterministically at all — so the
agent asks in the new order, is served in the old one, and produces exactly the
chain that was recorded. The mechanism that makes concurrent replay work is the
same one that absorbs a reordering.

This page is about seeing it anyway, without touching any of that.

## The design in one sentence

**A reader over fields the recording already has.**

| field | already stored | already outside `chain.DIGEST_FIELDS` |
|---|---|---|
| `i` | yes | yes |
| `t0` — when the call started | yes | yes |
| `ms` — how long it took | yes | yes |
| `worker` — which thread issued it | **new**, additive | yes |

`t0` and `ms` are an interval, and an interval is all concurrency is. So the
analysis needs no format change, no new capture, and — structurally — cannot
move a verdict. `tests/test_concurrency.py::t_no_concurrency_field_is_in_the_digest`
asserts the last part rather than asserting it in prose.

## What an interval proves, and what it does not

```
a.end <= b.start      a happened before b            PROVABLE
intervals intersect   a and b overlapped             PROVABLE
a started first       a was issued first             PROVABLE, and weak
a caused b            —                              NOT PROVABLE. Ever.
```

Every finding carries the sentence

> timing establishes precedence, not cause: no causal claim is made here

and a check greps the whole output for "root cause", "caused by" and "because
of" to keep it that way.

## The three findings, and why they are not one

| | when | strength |
|---|---|---|
| `PARALLEL_ORDER_CHANGED` | both runs ran them **together**, and the start order flipped | **weak** |
| `SEQUENTIAL_ORDER_CHANGED` | one provably finished before the other began, and that reversed | **strong** |
| `CONCURRENCY_CHANGED` | overlapping in one run, sequential in the other | **strong** |

The separation is the whole point. Which of two *overlapping* calls the
scheduler started first is a fact about the machine, so reporting it as a
failure would train people to ignore the report. Two calls that never overlapped
and now run in the other order is a change in the agent. Collapsing them into
one signal makes the first noise and the second invisible.

## The policy decides; the diff only reports

```python
from orientim import concurrency as K

conc = K.compare(steps_a, steps_b, meta_a, meta_b)

K.policy(conc)                                   # default
K.policy(conc, parallel_order_matters=True)      # an agent that staggers on purpose
```

| setting | default | why |
|---|---|---|
| `parallel_order_matters` | `False` | scheduling, until somebody says otherwise |
| `sequential_order_matters` | `True` | a provable reversal is a change in the agent |
| `concurrency_matters` | `True` | doing two things at once and then not is a change |

Nothing in a recording can decide whether a parallel start order is meaningful.
An agent that staggers its children deliberately has a real order; one that fires
them into a pool does not, and the recordings look identical. So it is a
**setting**, not an inference.

`orientim diff` runs the default policy and prints what it found. `identical`
stays a statement about the bytes; a weak concurrency finding is reported beside
it and does not change it.

## What it looks like

```
  CONCURRENCY
    1 group(s) then, 1 now
     ? PARALLEL_ORDER_CHANGED (weak)
       these two ran concurrently in both runs and started in the other order
       was: POST slow?child=research then POST slow?child=risk
       now: POST slow?child=risk then POST slow?child=research

    timing establishes precedence, not cause: no causal claim is made here.
    policy: concurrency_matters=True, parallel_order_matters=False,
            sequential_order_matters=True
```

## Where it earns its keep

When the concurrent calls have **different URLs**, the step alignment already
reports one `REORDERED`. Concurrency adds what alignment cannot know: they
*overlapped*, so the order is scheduling and the finding is weak.

When they share a URL and differ only by body — two children behind one
endpoint, which is common — the alignment pairs them by method and URL and
reports **two changed steps**: noise that grows with the size of the parallel
group and never names what happened. The concurrency view reports the same
event as **one reordering**, because its invocation id includes the request key.

Both shapes have a check.

## The invocation id

A name for a call that survives being moved. Position cannot be part of it —
the whole question is what happens when positions change — so it is a digest of
`method + url + key_loose`, plus an occurrence number among calls sharing that
identity.

Two *identical* calls that swap are therefore invisible, and that is the correct
answer: nothing in the recording separates them. Asserted, so the silence is not
later mistaken for a bug.

## `worker`

Each step records which thread issued it, numbered **per run** in the order the
threads first appear. Not an OS id — that would leak and mean nothing to a
reader.

It is useful *within* a run, for seeing that a group came from different
workers. It is **not comparable between runs**: the numbering depends on which
thread got there first, which is the very thing being measured. Treated that way
in the code, and stated here so nobody builds on it.

## LIMITS

- **A parallel order change cannot be proved meaningful.** Weak by default, and
  a setting flips it. Nothing in the data decides.
- **Two indistinguishable concurrent calls that swap are invisible.** By
  construction.
- **No timing, no finding.** A step without `t0` yields `UNKNOWN` and is skipped
  rather than guessed at.
- **`worker` does not cross runs**, and does not cross processes at all.
- **No causality, at any strength.** The strong findings are strong about
  *sequencing*, not about cause.
- **`orientim test` still does not fail on a reordering**, and that is
  deliberate: it asks whether the code reproduces the recording, and by the
  recorded-order contract it does. Use `orientim diff` on two recordings, with
  a policy, for this question.
- **One agent at a time.** An invocation already carries `agent`, which is the
  field a fleet view would join on, but nothing joins recordings across
  processes and nothing here pretends to. Parent/child across a fleet is not
  built.

## How this extends later, and what it would take

The abstraction is deliberately shaped so a fleet view is additive rather than a
rewrite:

- an **invocation** already has an id, an interval, a worker and an agent;
- what is missing for a fleet is a **parent execution id** shared by the
  recordings of one logical request — a value the supervisor would generate and
  pass to its children, and each would store in `_meta`;
- with that, `groups()` runs unchanged across the merged invocation list, and
  `compare()` gains cross-agent findings for free.

That is one field and a propagation convention. It is not built, because a
propagation convention nobody has asked for is a protocol invented in advance.
