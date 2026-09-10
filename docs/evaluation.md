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
- `used_tool` on a run with no model call at all;
- any evaluator whose observation domain is incomplete — see below.

## Observation validity

An evaluator asks a question of an execution. Whether the execution can *answer*
it is a separate question, and the answer is not one flag for the whole run.

**Completeness is relative to the question.** A replay that diverged at its
first request still observed every request the agent sent — the recorder writes
down the ones it could not match — so a question about what was sent is
answerable, while a question about what the model replied is not. Same trace,
two different answers.

`orientim.observation` names five domains and works out, from fields the trace
already carries, which of them are complete:

| domain | what it covers | incomplete when |
|---|---|---|
| `emitted_requests` | what the agent sent | the ring buffer evicted steps |
| `served_responses` | what came back, for every request | any step went unanswered |
| `model_responses` | what came back, for model calls | any model call went unanswered |
| `model_tool_calls` | what the model asked the agent to do | a model response arrived in a shape the extractor cannot read, or a stream ended without a terminator |
| `final_output` | the answer the run declared | none was declared, capture failed, or it was stored as a prefix |
| `timing` | when each step ran | `t0` / `ms` missing |

A step counts as **unanswered** when it is `unmatched` — a replay had nothing
recorded for it and served a synthetic 599 — or when the request raised instead
of answering. A 4xx or 5xx *is* an answer: the server said no, and that is an
observation like any other. Only the absence of a response is an absence of
evidence.

A call that *raised* is a third case, and it lands on the evidence side. It has
no response, so it makes `served_responses` incomplete — but the raising itself
was observed, so `no_step_failed` still reports it as a failure. Only the
replay's own synthetic 599 is excluded there, because that one is evidence of
nothing.

**Arriving and being readable are two different facts.** `served_responses` is
the transport one: bytes came back. `model_tool_calls` is the interpretation
one: those bytes were in a shape whose tool-call channel we could enumerate. An
independent audit found the gap between them, and it is not a corner case — a
provider whose envelope the extractor does not recognise returns HTTP 200 and
yields no tool calls, and reading that as *the model asked for no tools* is how
a prohibition passes a run that violated it.

### Three words that are not interchangeable

| | what it means | what it does not mean |
|---|---|---|
| **capture fidelity** | the bytes we claim were observed were stored, with transforms recorded | that we understood them |
| **extraction validity** | the facts derive from a schema we actually support, and the enumeration reached its end | that the facts are complete for the run |
| **claim soundness** | the verdict follows from those facts *and* their coverage | that the property is true of the world |
| **gate disposition** | what a build does about a finding | what the finding is |

The phrase *sound trace* is avoided here because it silently mixes the first
three. A response can be captured perfectly and be unreadable; it can be read
correctly and still leave the run's tool set unknown.

### Coverage comes back from the same parse as the facts

The first attempt at this put a separate function beside the extractor to
decide whether the extraction had been exhaustive. Two sources of one truth
disagree, and a second audit found eight responses where the certifier was the
more optimistic of the pair — a container key present with the wrong shape
under it, a tool call past the event bound, a `[DONE]` the model had written
into its own prose.

So `model.extract_tool_calls` returns facts and coverage together:

```python
{"calls": [...],        # each with `partial` and `name_confirmed`
 "complete": False,     # the enumeration is exhaustive for this response
 "issues": ["events_truncated"],
 "schema": "sse",
 "extractor": 2}
```

`model_tool_calls` is complete exactly when every model response's extraction
was. The named reasons it may not be:

| issue | what happened |
|---|---|
| `unsupported_schema` | no container we know how to read |
| `schema_mismatch` | a container we know, holding something else |
| `events_truncated` | more stream events than we parse |
| `limit_reached` | more tool calls than we keep |
| `channel_open` | a channel that never said it was finished |
| `partial_call` | a call whose arguments were cut |
| `unconfirmed_name` | a name assembled from fragments, never seen whole |
| `no_response` / `binary_body` / `no_body` / `parse_error` | nothing to read |
| `unplaced_step` | a step that looks like inference and is not labelled `model` |

A bound that was reached is coverage loss, never a shorter answer. An endpoint
with no tool-call channel at all, like `/embeddings`, is complete by
definition: nothing can be there, so nothing is missing.

### A witness and an enumeration are different claims

Coverage limits what you can say about an *absence*. It does not take away
what was actually seen, and the two evaluators split on exactly that line:

- `did_not_call(T)` **fails** on any call named `T` whose name we saw whole,
  even if its arguments were cut. The model asked; a prohibition is on asking.
- `used_tool(T)` **passes** only on a *finalized* request — name confirmed and
  arguments intact. An unfinished call is a proposal, and answering UNKNOWN
  there is the difference between "the model requested it" and "the model
  started to".
- A name reassembled from fragments on a stream that never closed decides
  nothing in either direction. `send_` followed by `email` is not evidence
  about `send_email`; it is a prefix of a name we never saw the end of.

Each evaluator declares the domains it reads, and you can ask it:

```python
orientim.evaluate.did_not_call("refund.issue").reads
# ('model_responses', 'model_tool_calls')
```

### What each verdict requires

| verdict | means | admissible when |
|---|---|---|
| `fail` | a violation was observed | **always** — the trace never invents a step, so anything in it really happened |
| `pass` | the property holds | the domain the property reads was complete |
| `warn` / UNKNOWN | the observation does not decide it | the domain was incomplete and no violation was found |

`evaluate.UNKNOWN` is an alias for `WARN`. It is the same wire value — reports,
exit codes and stored baselines all read `warn` already — with the name that
says what it has always meant: *a fact about the observer, not about the run*.

There is a fourth verdict this deliberately does **not** implement. A property
is **vacuous** when it held but nothing in the trace exercised it — the classic
case being `G(p → q)` on a run where `p` never happens. That is a fact about the
trace and the specification rather than about the observer, and it applies to
implication-shaped rules. Orientim has none, so a vacuity detector here would be
machinery for a rule shape that does not exist yet.

### The asymmetry that makes this necessary

The trace is **sound but incomplete**: everything in it really happened, and
things that happened may be missing from it. So

- a question of the form *did this ever happen* (`used_tool`) is settled by one
  observed witness, and only needs completeness to answer **no**;
- a question of the form *did this never happen* (`did_not_call`) cannot be
  answered **yes** from an incomplete observation at all, because the missing
  response is exactly where the forbidden request would be.

One unanswered model call is enough to lose a prohibition. That is stricter
than it sounds and it is the whole point: `did_not_call("refund.issue")` used to
return `pass` on a replay whose model call was never answered, which is the same
verdict it gives a run that genuinely never asked for a refund.

### Custom checks

`check(fn)` takes an optional `reads=`:

```python
orientim.check(my_rule, name="no_pii", reads=("model_responses",))
```

Declare it and the check is skipped with UNKNOWN when that domain is
incomplete, the same rule the built-ins follow. Leave it out and nothing is
assumed — the check runs and its answer stands, because guessing which domains
someone else's code reads would turn working suites red for a reason their
author never wrote down.

A name that is not a domain raises `ValueError` at the point you write it:

```python
orientim.check(my_rule, reads=("moddel_responses",))
# ValueError: not an observation domain: 'moddel_responses'. Known domains are …
```

It used to be accepted. An unknown domain had no gaps, no gaps read as
complete, and the declaration was worth nothing while looking exactly like a
declaration that was worth something — a typo that silently removed the
protection it appeared to add.

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
| `.observed_failures()` | the same, minus steps a replay could not match |
| `.observation` | which domains this trace is complete for |
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
