# The lab: a small AI company, and Orientim pointed at it

A fleet of four agents, three services and a database, all local and all
deterministic. It exists to put Orientim under the shape of load it claims to
handle — several agents, real execution loops, tools with consequences, work
delegated over HTTP, two children running at once — and to find out what it
catches and what it does not.

No provider, no API key, no cost. Every answer is a function of its input, so
an experiment repeated is an experiment reproduced.

**Orientim itself is not modified by anything in here.** The lab is the thing
under test's *subject*, not part of it.

## The company

```
                        supervisor  :9200
                             |
        +--------------------+--------------------+
        |                    |                    |
   research :9201       risk :9203          support :9202     ← agents
        |                    |                    |
        +--------------------+--------------------+
                             |
                    tool / API layer :9102                    ← what they may do
                             |
                       database :9103                         ← shared state
                             |
                        model :9101                           ← the LLM endpoint
```

| | |
|---|---|
| **supervisor** | plans with the model, delegates, runs **research and risk in parallel**, then support, then writes the answer |
| **research** | knowledge base, then parcel tracking |
| **support** | looks the order up and answers the customer |
| **risk** | scores the order for fraud |
| **tools** | `order.lookup`, `kb.search`, `risk.score`, `shipping.track`, `email.send`, `refund.issue` — the last two have real consequences and count themselves |
| **database** | SQLite behind HTTP, seeded identically on every start |
| **model** | OpenAI-shaped and deterministic: given a conversation it decides tools-or-answer the same way every time |

Every agent runs a **real loop** — model, tools, model, until an answer — and
every agent wraps its request handler in `orientim.record()`, which is the
pattern the documentation prescribes for a long-lived service.

State lives behind HTTP on purpose. A local file or an in-process dict would be
invisible to Orientim (source 18, a declared limit), and this lab exists to
test what it *can* see.

## Files

```
lab/
  services/model.py      deterministic OpenAI-shaped endpoint
  services/tools.py      the six tools, two of them side-effecting
  services/state.py      SQLite behind HTTP
  agents/common.py       ports, the execution loop, the variant switch
  agents/supervisor.py   plans, delegates, runs two children at once
  agents/children.py     research, support and risk, each an HTTP service
  agents/entries.py      one entry point per (agent, scenario)
  scenarios.py           twelve tasks and what each agent must do
  variants.py            the ten deliberate regressions
  run.py                 start, record, cases, baseline, regress
```

## Running it

```bash
python lab/run.py up          # start all seven processes
python lab/run.py record      # run the 12 scenarios, keep what happened
python lab/run.py cases       # 32 cases, across the four agents
python lab/run.py baseline    # freeze what each agent's suite says
python lab/run.py regress     # all ten regressions
python lab/run.py regress tool_change   # one of them
python lab/run.py down
```

Each agent has its own suite, so the raw commands are per agent:

```bash
cd lab
export LAB_REPO=.. LAB_RUNS=$PWD/_runs LAB_VARIANT=v1 PYTHONPATH=$PWD:..

python -m orientim.cli --root _runs/supervisor test --baseline main
python -m orientim.cli --root _runs/risk test --case risk-must-run
python -m orientim.cli --root _runs/support diff --case shipped-order
python -m orientim.cli --root _runs/research diff --case shipping-tracked --json
```

## The twelve scenarios

Each takes a different path: a shipped order, one still processing, a lost
high-value parcel, a delivery outside the refund window, a 2400 order from a
high-risk country opened today, a policy question, the safety check on its own,
the parallel pair, parcel tracking, the refund prohibition, a step budget, and
an order that does not exist.

## The ten regressions, and what Orientim says

Each is one environment variable away from v1 — a real code change, not a
branch nobody would write. `LAB_VARIANT=tool_change python lab/run.py up` and
the company behaves differently.

| # | variant | the change | what should catch it |
|---|---|---|---|
| 1 | `model_change` | gpt-4o-mini → gpt-4o everywhere | **diff**: MODEL CONFIG. Replay diverges at the first model call |
| 2 | `tool_change` | support calls `kb.search` where it called `order.lookup` | **evaluation**: `used_tool(order.lookup)` fails. **diff**: TOOL DECISION |
| 3 | `arg_change` | `order.lookup` gains `include_history=True` | **diff**: TOOL DECISION, arguments changed. Evaluation stays green — the tool is still used |
| 4 | `tool_unused` | risk stops calling `risk.score` | **evaluation**: `used_tool(risk.score)` fails — the safety check went quiet |
| 5 | `forbidden_tool` | support issues a refund on its own | **evaluation**: `did_not_call(refund.issue)` fails, with the arguments as evidence |
| 6 | `output_change` | support answers in a formal template | **diff**: OUTPUT, with digests. **evaluation**: `output_matches` fails |
| 7 | `new_call` | research adds an `order.lookup` | **replay**: NEW_CALL. **diff**: one INSERTED step |
| 8 | `call_removed` | research stops tracking the parcel | **evaluation**: `used_tool(shipping.track)` fails. **diff**: DELETED |
| 9 | `order_change` | research tracks first, reads second | **diff**: REORDERED — one move, not two changes |
| 10 | `parallel_order` | the supervisor starts risk before research | **`orientim test` does NOT catch it.** `orientim diff` of two recordings does — see below |

Regression 10 needs a word. Two concurrent HTTP calls return in whatever order
the scheduler allows, so v1 **staggers** them: research is asked first and given
a quarter-second head start, which makes the recorded order a fact about the
code. Without that, every run would differ for no reason and the experiment
would be measuring the operating system.

## What to expect

- `record` writes 12 supervisor recordings and one per child per scenario;
  **side effects during recording: `email.send: 0, refund.issue: 0`**, because
  v1 correctly never uses them.
- `cases` makes **32 cases across four agents**.
- `baseline` freezes four baselines, all green.
- `regress` restarts the fleet ten times. **Nine exit 1**; regression 10 exits
  0, for the reason in the finding below.
- Side effects during every replay stay at **zero**. That is not a promise
  taken on trust: `services/tools.py` counts its own calls and `run.py effects`
  prints the counter.

## What Orientim does not catch, and three findings from building this

### FINDING 1 — `run.client()` cannot be given a timeout  ·  **fixed**

```python
def client(self, **kw):
    c = httpx.Client(timeout=10.0, **kw)
```

`timeout` is hard-coded and `**kw` is splatted on top, so
`run.client(timeout=60)` raises `TypeError: got multiple values for keyword
argument 'timeout'`. Ten seconds is the wrong default for an agent that
delegates to other agents, and there is no way to say so. The lab works around
it with per-request timeouts. `agents/common.py`.

`timeout` is a `setdefault` now, so `run.client(timeout=60)` works and every
caller that says nothing still gets ten seconds.

### FINDING 2 — a case cannot carry its own input  ·  **fixed**

A case stores a recording, an entry point and its expectations. The entry is
called as `fn(run)`, with no channel for parameters, so twelve scenarios that
differ only by which order they ask about cannot share one function. An
environment variable works for `orientim test --case X` and breaks the moment
the whole suite runs in one process: every case gets whichever value was set
last.

The information exists — the task is in the recorded request bodies — there is
just no way for the case to hand it back. The workaround is one entry point per
scenario, generated in `agents/entries.py`.

A case carries `input` now, defaulted from what the recording was made with, and
the entry point reads `run.input` — symmetric with `run.output`, and restored to
the shape it was given rather than the JSON text it was stored as. The lab keeps
its per-scenario entries because they also document what each scenario is; the
workaround is no longer required.

### FINDING 3 — evaluation is per agent; there is no fleet view

`used_tool("risk.score")` against the supervisor's recording fails, and it is
right to: the supervisor's model never asks for `risk.score`. The *risk agent's*
does, in the risk agent's own recording, which the supervisor's does not contain
and cannot reach. Nothing says "these four recordings are one execution of one
fleet".

The first draft of `scenarios.py` wrote fleet-level rules against the supervisor
and every one failed for a reason that looked like a bug and was not. Fleet
coverage means **a case per agent** — which is defensible, since each agent is
separately deployable, but it has to be said.

The only thing joining a child's run to the supervisor's here is a tag both
sides happen to set.

### FINDING 4 — a replay cannot see a reordering of *concurrent* calls  ·  **addressed, outside replay**

This is the one regression of the ten that `orientim test` misses, and the
reason is the mechanism that makes concurrent replay work at all.

Measured, not argued. Recording the same scenario live under each variant and
diffing the two **recordings**:

```
v1               delegation order: ['9201/run', '9203/run', '9202/run']
parallel_order   delegation order: ['9203/run', '9201/run', '9202/run']

diff: identical=False  counts={'SAME': 6, 'REORDERED': 1}
  REORDERED   POST run ->  POST run
```

The order genuinely flipped, and the alignment names it correctly as **one
move**, not two changes. That is Phase 3 working.

But `orientim test --case parallel-children` exits **0** and reports "Same
steps, same order, same responses, same answer". Replay serves recorded steps
**in the recorded order** — `_take_ordered_nowait` makes a request that arrives
early wait for its turn — which is exactly what lets an agent with four calls in
flight replay deterministically at all. The same mechanism absorbs a real
reordering: the agent asked in a different order, was served in the old one, and
the chain it produced matches the chain that was recorded.

So the two tools disagree, and both are right about the question they answer:

| | question | regression 10 |
|---|---|---|
| `orientim test` | does this code still reproduce the recording | no difference — by design |
| `orientim diff` on two recordings | did two runs of this agent differ | **REORDERED**, named exactly |

**The practical rule: to catch a change in the ordering of concurrent calls,
record twice and diff the recordings. Replay will not tell you.**

`orientim.concurrency` now does exactly that, as a reader over `t0` and `ms`
which were already stored and already outside the digest: `orientim diff` reports
`PARALLEL_ORDER_CHANGED`, marked weak, and a policy decides whether it fails.
Replay is untouched — see [docs/concurrency.md](../docs/concurrency.md).

### Also worth knowing

- **A divergence at step 0 cascades.** When the very first model call does not
  match, the agent receives a 599 and everything after it is reported deleted.
  The diff says so plainly — 1 CHANGED, 6 DELETED, "6 later step(s) also
  differ" — but the reader has to know that one change caused the shape, not
  seven.
- **A correlation id in a downstream response makes every upstream recording
  differ.** A child that returns its own run id — exactly what you want for
  debugging — changes the parent's recorded response bytes on every run, so the
  parent's suite reports a change that is not one. The children here
  deliberately do **not** return theirs. Use `--loose`, or keep ids out of
  payloads that upstream agents record.
- **Delegation is a tool step, not a tool call.** The supervisor calling a child
  over HTTP is `role: tool`, but no model asked for it by name, so `used_tool`
  cannot see it. Rules about delegation have to be written against steps, and
  there is no evaluator for that.
