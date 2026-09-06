# How much to keep, and for how long

Recordings accumulate. A production agent that fails one request in a hundred,
serving ten thousand requests a day, writes a hundred files a day forever. At
roughly 50 KB each that is 5 MB a day, 1.8 GB a year, on a disk that usually
belongs to something else as well.

That is the operational half. The half that matters more: **a recording holds
your prompts and your customers' data**, so keeping one for two years is a
decision, not an accident. Retention is the same question as data minimisation
wearing different clothes.

## What is written in the first place

Nothing, unless something fires. Steps live in a ring buffer in memory and the
file is written only on:

- an exception out of the block,
- a `5xx` response,
- an uncaptured source,
- an explicit `run.rec.trigger("...")`,
- or `always=True`.

**This is the first retention control and the one people miss.** A recording
that is never written costs nothing and leaks nothing. Before tuning any policy
below, ask whether the run needed saving at all.

```python
# development: keep everything, you are looking at it anyway
with orientim.record(always=True) as run:
    my_agent()

# production: keep the runs your own quality check disliked
with orientim.record(tags={"customer": cid}) as run:
    answer = my_agent()
    if not looks_right(answer):
        run.rec.trigger("failed the quality check")
```

The second form is the one that matters. An agent giving a *wrong* answer
raises nothing and returns `200`, so nothing fires on its own — and a wrong
answer is the case this tool exists for. If you take one thing from this page:
wire your quality signal to `trigger()`.

## Nothing is ever deleted on its own

`prune` runs when you run it. Deleting somebody's recordings as a side effect of
writing one is a surprise that is never worth the disk it saves — especially
when the thing being deleted might be the only copy of an incident somebody is
mid-way through debugging.

```bash
orientim prune                              # says what is there, removes nothing
orientim prune --keep 200 --dry-run         # says what would go
orientim prune --keep 200                   # does it
```

With no rule at all, `prune` removes nothing rather than everything. An empty
policy that deleted the store would be unrecoverable, and someone would find
that out in a shell script.

## The four rules

They apply together: a recording goes if **any** of them condemns it. Newest
first throughout, so `--keep` always keeps the most recent.

| rule | what it does |
|---|---|
| `--keep N` | keep the newest N recordings |
| `--older-than DAYS` | remove anything older |
| `--max-bytes N` | keep newest recordings that fit the budget |
| `--per-signature N` | keep at most N copies of each **distinct failure** |

### `--per-signature` is the one to reach for first

The others are generic disk hygiene. This one is specific to what a recording
is.

Five hundred runs that failed in exactly the same way are one thing you need to
look at, not five hundred. A signature is the trigger reason plus the chain
root — the hash the tool already computes — so two runs that took the same path
and got the same answers collapse into one entry:

```bash
orientim prune --per-signature 3
```

```
  312 recordings, 15 MB
    - run_7c1a9f02      48 KB   copy 4 of the same failure
    - run_9e4b1105      47 KB   copy 5 of the same failure
    ...
  removed 287, freeing 13 MB
```

Twenty-five recordings left, covering every distinct way the agent broke, with
three examples of each so you can tell a pattern from a one-off. That is a
better store than the three hundred you started with, not just a smaller one.

## Suggested policies

**A laptop, while developing.** Nothing. Delete `runs/` when it annoys you.

**CI.** Recordings are build artifacts; let the CI system expire them. If you
keep them yourself, `--keep 50` per branch is plenty.

**A production agent.**

```bash
orientim prune --root /var/lib/orientim \
    --per-signature 5 --older-than 30 --max-bytes 2000000000
```

Run it from cron, daily. Five examples of each distinct failure, nothing over a
month old, and a hard 2 GB ceiling so a new failure mode appearing at 3 a.m.
cannot fill the volume.

**Anything holding personal data.** Set `--older-than` to your organisation's
retention period and treat it as the ceiling, not the target. Then look at
whether you needed the recording at all — see the trigger section above.

## The opt-in cap

If a cron job is not available — a container, a short-lived worker — there is a
cap that applies after each save:

```bash
export ORIENTIM_MAX_RUNS=500
```

Off by default. Local stores only: listing and stat-ing a bucket after every
save would put a network round trip on the hot path of a tool whose whole point
is staying out of the way. It is the crude rule (`--keep`), not the good one, so
prefer the cron job where you can have it.

## What a recording actually costs

Measured, not estimated:

| | |
|---|---|
| an HTTP step, small body | ~600 bytes |
| an HTTP step, 2 KB prompt | ~2.6 KB |
| generator state, when the run drew from `random` | ~3.3 KB, once per file |
| recording overhead per call | ~0.09 ms |

The generator state is stored only when the process actually drew from the
shared generator during the run, so most files do not carry it.

Two things make files bigger than you expect:

- **The ring counts clock and randomness steps too.** Three HTTP calls produced
  ten steps in one measurement, because httpx reads the clock internally. A ring
  of 512 is closer to 150 real HTTP calls than to 512 — worth raising with
  `ring=` for a long agent, and worth knowing before a `TRUNCATED` verdict
  surprises you.
- **Response bodies are stored whole.** An agent that downloads a document
  stores that document.

## Where they live

Local disk by default. `ORIENTIM_STORE=s3://bucket/prefix` moves them to
anything S3-shaped, and `prune` works the same there — with the caveat that
`survey()` reads every object to compute signatures, so a bucket prune costs a
`GET` per recording. Run it on a schedule, not in a loop.

## In code

```python
from orientim import store

store.survey(root)          # every recording, with size, age and signature
store.prune(root, per_signature=3, older_than_days=30, dry_run=True)
store.signature(meta, steps)
```

`prune()` returns the rows it removed — or would have, under `dry_run` — each
with the reasons that condemned it, so a scheduled job can log what it did.

---

New to Orientim? Start with [how-to-use.md](how-to-use.md).
