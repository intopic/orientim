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

## What is supported, and what is not

Six execution shapes, each with a check in `tests/test_concurrency.py` and each
run against the live fleet in `lab/concurrency_check.py`.

| shape | | |
|---|---|---|
| **single agent, sequential calls** | supported | no groups invented, replay IDENTICAL |
| **parallel calls in one agent** | supported | one group, found by overlap |
| **parallel child agents over HTTP** | supported | real sockets, real pool, one group |
| **three or more in parallel** | supported | a group is any size |
| **nested agent that records itself** | supported *on one thread* | see the limit below |
| **sequential reorder** | supported, **strong** | fails the default policy |
| **parallel reorder** | reported, **weak** | does not fail by default — see below |
| **fleet / parent-child join** | **not built** | a planned extension, described at the end |

Three things this page states plainly because they are easy to assume the other
way round:

1. **Replay stays deterministic.** Nothing here is consulted when a replay
   decides a match. Stripping `t0`, `ms` and `worker` from a recording entirely
   leaves the verdict unchanged, and there is a check that does exactly that.
2. **A parallel order change is not automatically a regression.** It is
   reported, marked weak, and passed by the default policy.
3. **Fleet execution is not a feature.** One recording is one agent. Nothing
   joins the recordings of a fleet, and nothing here pretends to.

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

## LIMIT — a nested record drops calls made from worker threads

Narrow, and worth knowing exactly how narrow:

| | |
|---|---|
| concurrency, not nested | **works** |
| nesting, one thread | **works** — the inner run records its own calls, the outer sees only its own, and the inner replays IDENTICAL |
| nesting **and** worker threads | the worker's calls are recorded by **neither** run |

With two `record()` regions open and a thread carrying no context of its own,
`scope.current()` refuses to guess. That is the right refusal — guessing would
file a call under the wrong agent and produce a recording that lies — but
refusing means the call is filed under nothing.

The drop is not silent at replay: an empty recording reports
`NOTHING_CAPTURED`. Nothing at **record** time says a call went nowhere.

Both halves are pinned by checks, so this cannot quietly change into a surprise.

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

### The minimal abstraction, written down but not implemented

If it is built later, this is the smallest thing that would do:

```
_meta.execution = {
    "id":     "<shared by every recording of one logical request>",
    "parent": "<the id of the agent that delegated to this one, or null>",
    "agent":  "<already present, as _meta.agent.name>",
}
```

One field, generated by whichever agent starts the request and passed to each
child by whatever transport already carries the task. Nothing else changes:

- `concurrency.invocations()` already carries `agent` per invocation;
- `groups()` runs unchanged over the concatenation of several runs' invocations;
- `compare()` gains cross-agent findings without a new code path;
- the hash chain is untouched, because `_meta` is not in it.

What it would **not** give, and should not be claimed for: a happens-before
relation across processes. Two machines' clocks are not one clock, and an
interval from one recording cannot be compared to an interval from another
without a shared time base. Within a fleet, order would be provable only where
one agent's call *contains* another's whole run — parent to child — which the
parent's own interval already shows.
