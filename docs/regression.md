# Cases, baselines, and `orientim test`

A recording is one thing that happened. A **case** is a recording you decided to
keep, plus how to run it again, plus what has to stay true about it. A
**baseline** is what the whole suite said at a point in time. `orientim test`
runs the cases and compares the result to the baseline.

The workflow, end to end:

```bash
orientim case save runs/run_2bea9035.jsonl --name order-support \
    --entry myapp.agent:run \
    --used-tool lookup_order \
    --never-call send_email \
    --expect-match 'order \d+' \
    --max-steps 6 --no-step-failed

orientim baseline create main --note "before the retrieval change"

# ... change the code, the prompt, or the model ...

orientim test --baseline main
```

```
==========================================================================
  ORIENTIM TEST  ·  1 case(s)  ·  strict key
==========================================================================

  !!  order-support           2 steps     563.2 ms
       NEW_CALL at step 2 — the replay reproduced all 2 recorded step(s) and
       then made a request this recording does not contain
       !!  output_matches   the answer does not match 'order \d+'

--------------------------------------------------------------------------
  1 of 1 case(s) failed.
  NEW on this change: order-support
--------------------------------------------------------------------------
```

Exit code 1. A build stops, and the reason is on the screen rather than in a
follow-up investigation.

## A case

```json
{
  "format": 1,
  "name": "order-support",
  "recording": "runs/run_2bea9035.jsonl",
  "run_id": "run_2bea9035",
  "entry": "myapp.agent:run",
  "expect": {
    "used_tool": ["lookup_order"],
    "did_not_call": ["send_email"],
    "output_matches": "order \\d+",
    "max_steps": 6,
    "no_step_failed": true
  },
  "description": "must look the order up, must never email on a miss",
  "tags": {"team": "support"},
  "created_at": 1788664972.1
}
```

Stored at `<root>/cases/<name>.json`, beside the recordings, in the same
greppable style as everything else.

**The entry point belongs to the case.** `orientim ci` applies one entry point
to a whole store, which is right for a directory of production captures and
wrong for a suite: a real application has more than one way in.

**The expectations are data, not code.** A case is a file, so what it expects
has to be storable. The vocabulary is exactly `evaluate.SPEC_KEYS` —
`output_equals`, `output_matches`, `used_tool`, `did_not_call`, `max_steps`,
`no_step_failed`. Anything more specific is a `check()` in your own test, which
is where code belongs. An unknown key is a **load error**, not a silent no-op: a
typo that quietly checked nothing would leave the case green forever.

### `orientim case`

| | |
|---|---|
| `case save <recording> --name N --entry M:F` | write one |
| `case list` | every case, with its entry point and how many expectations |
| `case run <name>` | one case |
| `case run-all` | all of them (the same thing `orientim test` does) |
| `case delete <name>` | remove one |

`save` validates before it writes: the recording is opened, the entry point is
checked for a `module:function` shape, and the expectations are compiled. A case
that fails to load later, in CI, is a much worse outcome than one that refuses
to save now.

The name becomes a file name, so it is checked rather than trusted — letters,
digits, dot, dash and underscore, up to 64 characters.

## What makes a case pass

Both halves, and they answer different questions:

1. **the replay reproduced** — the same requests, in the same order, with the
   same responses; and
2. **nothing it promised broke** — no evaluator failed.

Either alone would be half an answer. A faithful replay of an agent that now
asks for the wrong tool is not a pass, and a run that satisfies every rule while
its traffic changed underneath is not one either.

Warnings do not fail a case. See [evaluation.md](evaluation.md) for why.

### The evaluation runs against the replay, not the recording

The point of a case is the present. So the evaluators are handed the steps the
replay produced and what the replayed function returned — what the code does
*now* — not what the recording says happened once.

This is also why a replayed step carries `role`, `model` and `served`: without
them an evaluator would see no tool calls at all and answer `used_tool()` with a
confident, wrong "no".

## A baseline

`orientim ci --baseline path/to/report.json` already compares against an earlier
report, and still does. That makes the baseline *a file somebody remembered to
keep* — in practice a CI artifact with a retention policy and a path that
differs between machines.

A baseline here is the same data with a name, a home and provenance:

```json
{
  "format": 1,
  "kind": "orientim-baseline",
  "name": "main",
  "created_at": 1788664972.1,
  "commit": "a3f91c2...", "branch": "main",
  "note": "before the retrieval change",
  "totals": {"cases": 12, "passed": 11, "failed": 1},
  "runs": [{"case": "order-support", "ok": true, "verdict": "IDENTICAL",
            "steps": 4, "failed_evaluators": []}]
}
```

Stored at `<root>/baselines/<name>.json`, so `main` means the same thing to
everyone with the repository. Slashes are allowed, so `release/2.1` is a name.

**A baseline holds verdicts, not evidence.** It lives in the repository forever
and is compared against rather than read for detail — the *current* run is the
one that has to explain itself. Keeping prompts and tool arguments in it would
be a cost with no matching benefit.

**The comparison is `ci.compare`**, keyed on the case name instead of the run
id. Not a second implementation of the same idea: one comparison with one set of
edge cases, used by both commands.

### A case verdict is one bit, and rules move underneath it

Comparing only `ok` answers "did this case start failing", and there are three
movements that question cannot carry. An independent audit found the first one:

```
BASELINE                        CURRENT
max_steps      FAIL             max_steps      FAIL
did_not_call   PASS             did_not_call   FAIL
case.ok        false            case.ok        false
```

`false -> false` files this under *already failing*, and a new violation of the
rule that exists to stop refunds being issued is never mentioned. So the
comparison reads the obligations too, and reports:

| key | what moved |
|---|---|
| `new_failures` | a rule that is failing now and was not failing in the baseline |
| `dropped_obligations` | a rule the baseline checked and this suite does not |
| `weakened` | a rule that went from PASS to UNKNOWN — nothing failed, and the proof is gone |
| `new_failing` | a case nobody had before, arriving red |

None of them changes an exit code. A build fails on failures, as it did before;
these are there to be *seen*, because a rule that quietly left the suite and a
rule that quietly stopped being provable both leave a green case behind.

A baseline therefore stores every evaluator and what it said, not only the ones
that failed — statuses, still no evidence. Baselines written before this carry
`failed_evaluators` alone, and they keep working: a name absent from that list
was not failing, which is enough for `new_failures` and not enough for the
other two. An old baseline gets the answers it supports and no others, rather
than a guess.

### `orientim baseline`

| | |
|---|---|
| `baseline create <name> [--note ...]` | run every case and freeze the result |
| `baseline list` | every baseline, with when and from which commit |
| `baseline compare <name>` | run every case and say what moved |
| `baseline delete <name>` | remove one |

Freezing a **red** suite is legitimate — it is how you record where you are
before starting to fix it — but it is never silent: `create` says how many cases
were failing when it froze them.

`compare` accepts a baseline name **or a path to a report**, so a team already
using `orientim ci --baseline report.json` can point at what they have instead
of migrating first.

## `orientim test`

Runs every case, evaluates, compares to a baseline, and exits.

| flag | |
|---|---|
| `--case NAME` | just this one |
| `--baseline NAME\|PATH` | fail only on what **this** change broke |
| `--report PATH` | write the machine-readable result |
| `--no-evidence` | leave prompts, answers and tool arguments out of the output *and* the report |
| `--loose` | ignore whitespace, key order and float rounding |
| `--no-fail` | report but always exit 0 |

### Exit codes

| | |
|---|---|
| `0` | every case passed — or, with `--baseline`, nothing *new* broke |
| `1` | a case failed |
| `2` | it could not run: no cases, or an unreadable baseline |

`2` is separate from `1` on purpose. "The suite failed" and "the suite never ran"
are different facts, and a build that treats them the same eventually ships on a
green light that meant nothing happened.

### With a baseline, only new breakage fails

Without one, any failing case fails the build. With one, only what this change
broke does — a case that was already red is not this change's fault. Without
that, adopting the tool on a suite that is not green yet is impossible, and the
first thing anybody would do is turn it off.

The comparison names five groups, and the distinction between the first three is
the whole point:

- **started failing on this change** — what a reviewer is actually asking about
- **fixed on this change**
- **already failing before this**
- **not in the baseline** — a case added since
- **in the baseline but gone now** — a case deleted since

### The report

```json
{
  "kind": "orientim-test",
  "totals": {"cases": 12, "passed": 11, "failed": 1, "warnings": 2},
  "runs": [{
    "case": "order-support", "ok": false,
    "verdict": "NEW_CALL", "index": 2,
    "reason": "NEW_CALL at step 2 — ...",
    "evaluation": {"passed": 3, "failed": 1, "warnings": 0,
                   "results": [{"status": "fail", "evaluator": "did_not_call",
                                "reason": "send_email was requested 1 time(s)",
                                "evidence": {"calls": [{"step": 7, ...}]}}]}
  }],
  "against_baseline": {"newly_changed": ["order-support"], "fixed": [], ...}
}
```

**This report carries evidence by default, and `orientim ci --report` does
not.** That difference is deliberate and worth knowing. The CI report is
narrow — ids, verdicts, hashes — which is what makes it safe to hand to a build
system or an artifact store without auditing it first. This one carries
arguments, matched text and failing URLs, because a failure without evidence is
the start of an investigation rather than the end of one. `--no-evidence` gets
the narrow property back.

## What this is not

- **No cloud, no GitHub or GitLab integration, no multi-repo.** Everything here
  is files on disk and an exit code. The GitHub surfaces that already exist in
  `orientim ci` — annotations and the step summary — are plain files and
  environment variables, and nothing new was added.
- **No scheduling, no history, no trend.** A baseline is one point in time. If
  you want a series, keep several baselines; they are files.
- **No automatic case creation.** Which recordings are worth keeping as tests is
  a judgement, and a tool that guessed would fill your repository with cases
  nobody chose.
