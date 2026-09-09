# Evaluation

A replay answers one question: **did anything change.** That is the regression
question, and it is not the only question people have about an agent.

The others sound like:

- did it still call the tool it is supposed to call
- did it call the one it must never call
- did it loop
- did any of its calls fail
- is the answer still the answer

Those are properties of a single execution, not of the difference between two,
so they are asked here rather than in a replay.

```python
from orientim import evaluate as ev

report = ev.evaluate("runs/run_2bea9035.jsonl", [
    ev.used_tool("lookup_order"),
    ev.did_not_call("send_email"),
    ev.output_matches(r"order \d+"),
    ev.max_steps(6),
    ev.no_step_failed(),
])

print(report.report())
report.ok          # False if anything failed
```

```
run_2bea9035 — 4 passed, 1 failed, 0 unanswered
  ok  used_tool        lookup_order was requested 1 time(s)
  !!  did_not_call     send_email was requested 1 time(s), at step(s) 7
  ok  output_matches   the answer matches 'order \d+'
  ok  max_steps        3 calls, within the limit of 6
  ok  no_step_failed   all 3 call(s) succeeded
```

## Two rules this is built on

**It reads the execution model; it does not re-derive it.** Nothing here parses
a URL, matches a request, or touches the hash chain. Every question is answered
from fields the recording already decided — `role`, `served.tool_calls`,
`outcome`, `status`. Two places deciding what a model call is would eventually
be two places disagreeing about it.

**A result is never a bare boolean.** "Failed" without the reason is a support
ticket. So the answer to `used_tool("lookup_order")` is not `False`; it is:

```python
Result(status="fail", evaluator="used_tool",
       reason="lookup_order was not requested; the model asked for send_email",
       evidence={"tool": "lookup_order",
                 "requested": ["send_email"],
                 "calls": [{"step": 4, "name": "send_email",
                            "arguments": {"to": "customer@example.com"},
                            "arguments_kind": "json", "partial": False}]})
```

## Three statuses, not two

| | |
|---|---|
| `pass` | the property holds |
| `fail` | the property does not hold |
| `warn` | **we could not answer** |

A warning is not a quiet failure, and it does not make `report.ok` false.
Failing a build on a question nobody could answer is how a tool teaches people
to ignore it. Warnings happen for honest reasons:

- `output_equals` on a run that never declared an output — there is nothing to
  compare, and the fix is a line of code, not a code change;
- `output_matches` when the stored answer was truncated at 64 KB and the
  pattern might have matched past the cut;
- `max_steps` when the ring buffer evicted steps, so the count is a floor
  rather than a total;
- `used_tool` on a run with no model call at all.

## The evaluators

### `output_equals(expected)`

Compared **by digest over the whole value**, never by the stored text. The
stored text is truncated at 64 KB, and two different long answers would compare
equal below that line.

### `output_matches(pattern, flags=0)`

A regular expression, searched against the answer. Warns rather than fails when
the answer was truncated.

### `used_tool(name)`

The model asked for this tool at least once. Evidence carries every matching
call with its step and its arguments. When it fails, the evidence names what the
model *did* ask for, which is usually the actual answer.

### `did_not_call(name)`

The model never asked for this tool.

**"Asked for", not "executed".** This can see what the model requested, because
the request is in the response body the provider sent back. Whether your code
then ran it is a decision made somewhere we do not observe — a tool name is not
a URL, and pretending to link the two would be a guess wearing the clothes of
evidence.

For a prohibition that is the safer direction: a model that asked to send the
email is worth knowing about even if something downstream refused.

### `max_steps(n)`

At most `n` HTTP calls. The cheapest loop detector there is. Honest about a
truncated ring: if the surviving count already breaks the limit that is still a
fact, and if it does not, the result is a warning rather than a pass.

### `no_step_failed()`

Every call came back without an error. Status `0` counts — that is what the
recorder writes when a request raised instead of answering, and a connection
that never opened is a failure whatever HTTP thinks.

### `check(fn, name=None)` — your own

`fn` is handed the `Execution` and may return:

| return | meaning |
|---|---|
| `bool` | pass or fail |
| `(bool, reason)` | pass or fail, with the reason |
| `str` | a failure, and the string is why |
| `Result` | whatever you want to say |
| `None` | `warn` — neither a pass nor a failure |

A callable that **raises** is a failure of the check, and says so. An evaluator
that crashed must never be mistaken for one that passed.

```python
def looked_up_the_right_order(ex):
    wrong = [c for c in ex.calls_named("lookup_order")
             if (c.get("arguments") or {}).get("order_id") != 4471]
    if wrong:
        return False, "looked up the wrong order: %r" % wrong
    return True, "every lookup named order 4471"

ev.check(looked_up_the_right_order)
```

## The `Execution`

What every evaluator is handed.

| | |
|---|---|
| `.output` | the declared final answer, or `None` |
| `.agent`, `.runtime`, `.status` | run metadata |
| `.http` | every HTTP step |
| `.model_steps`, `.tool_steps` | split by `role` |
| `.tool_calls` | every tool the model asked for, each carrying the step that asked |
| `.calls_named(name)` | the calls for one tool |
| `.failed_steps()` | steps that errored or came back ≥ 400 |
| `.meta`, `.steps` | the recording, unmodified |

Built from a recording with `Execution.load(path)`, or from any `(meta, steps)`
pair with `Execution.of(meta, steps)` — which is the seam a replay-and-evaluate
command will use later, without this module having to know anything about
replay.

## The report

```python
report.ok          # False if anything failed; warnings do not count
report.passed      # [Result, ...]
report.failed
report.warnings
report.report()    # the human summary above
report.as_json()   # the same thing, for CI
```

Every evaluator runs, always. Stopping at the first failure would hide the
second one, and when an agent regresses it usually breaks more than one property
at a time — the shape of the whole set is the diagnosis.

## What this is not, yet

There is no `orientim test`, no saved cases and no baselines. Those are the next
step, and they are built **on** this rather than beside it: a case is a
recording plus an entry point plus the rules above; a baseline is what those
rules said at a commit. Until then, evaluation is a library you call from your
own test, which is the smallest thing that is useful on its own.

Evaluating a **replay** rather than a recording is also not wired up yet. The
seam exists — `Execution.of(meta, steps)` — and nothing else is needed from this
module to do it.
