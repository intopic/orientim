# Second audit: the counterexamples, before and after

An independent review of `2e7d514` raised eight counterexamples against the
first pass at observation validity, plus one against obligation identity and
one against the diff. Every one was reproduced before it was believed. The
BEFORE column is what the reproduction run printed at `2e7d514`; the AFTER
column is `taska-after.txt` from the same probe against TASK A.

The reproduction probe and the after-state are both in this directory. The
regression tests are `tests/test_evidence.py`.

## The root cause, stated once

The first pass produced tool calls with an extractor and then asked a *second*
function whether the extraction had been exhaustive. Two sources of one truth
disagree, and in all eight cases the certifier was the more optimistic of the
pair: it recognised a field name, or a substring, or the fact that a response
had arrived at all, and called that enumeration.

Coverage now comes back from the same parse as the facts. There is one
derivation and its limits are part of its output.

## P0-1

| # | counterexample | BEFORE | AFTER | why the new result is sound |
|---|---|---|---|---|
| 1 | `output` holds an object; the Responses API puts a list there | `did_not_call` **PASS**, `used_tool` FAIL | UNKNOWN / UNKNOWN, `schema_mismatch` | a container we know, holding something we do not, was never walked — no absence was established |
| 2 | HTTP 200 with `choices: 7` | **PASS** / FAIL | UNKNOWN / UNKNOWN, `schema_mismatch` | the extractor cannot iterate it; the key being present says nothing about whether it was read |
| 3 | tool call at SSE event 401, parser bound is 400, real `[DONE]` after it | **PASS** / FAIL | UNKNOWN / UNKNOWN, `events_truncated` | the stream did close and the enumeration still did not reach the call. A bound that was hit is coverage loss |
| 4 | 51st tool call, `MAX_TOOL_CALLS` is 50 | **PASS** / FAIL | UNKNOWN / UNKNOWN, `limit_reached` — and `used_tool("lookup_order")` still **PASS** | a truncated list is not an exhaustive one; the 50 that *were* read stay usable as witnesses |
| 5 | the model wrote `[DONE]` into its own content; stream never closed | **PASS** / FAIL | UNKNOWN / UNKNOWN, `channel_open` | a terminator is a `data:` frame, not six characters somewhere in the body |
| 6 | choice 0 finished, choice 1 did not, no `[DONE]` | **PASS** / FAIL | UNKNOWN / UNKNOWN, `channel_open` | one closed channel closes one channel; the tool call could still have been coming on the other |
| 7 | tool call with arguments cut mid-JSON | FAIL / **PASS** | FAIL / UNKNOWN, `partial_call` | the model asked, so the prohibition is violated; it never finished asking, so "the model requested it" is not established |
| 8 | model endpoint `classify` does not recognise | **PASS** / WARN | UNKNOWN / UNKNOWN, `unplaced_step` | a step that looks like inference and is not labelled `model` is a response nobody searched |

Controls, all unchanged: the same body at a recognised path is still FAIL/PASS;
an ordinary readable response with no tool calls is still complete; the
anthropic envelope is complete; `/embeddings` is complete because no tool call
can be there.

### Two more, found by asking what the fix did not cover

| finding | BEFORE | AFTER |
|---|---|---|
| unrecognised path **and** unrecognised envelope | `did_not_call` **PASS** — neither classification signal was available, so nothing flagged it | UNKNOWN. A request carrying `messages`, `contents` or `prompt` is inference-shaped on its own |
| streamed name fragments | `send_` then `email` produced `email` — a name the model never asked for | fragments concatenate, and a name is a witness only if the stream closed or arguments began |

## P0-4

| finding | BEFORE | AFTER |
|---|---|---|
| two `did_not_call` rules over different tools | keyed by evaluator name; the second overwrote the first, and the new violation was invisible | keyed by `did_not_call:<tool>`; both stay separate |
| declaration order | reordering the same rules changed the report with no change in behaviour | order-invariant, including for rows that carry no obligation, which report `legacy_uncomparable` rather than resolving by last-write-wins |
| exit code | only `newly_changed` reached the gate | unchanged under `--gate legacy`; `--gate protected` blocks on protection losses |

## Diff

| BEFORE | AFTER |
|---|---|
| evaluation `warn`, diff `removed` — a contradiction from the same two runs | evaluation `warn`, diff `observed_only_on_a` with the reason attached |

`added` and `removed` are claims about an absence, so each needs the side where
the tool is missing to have been enumerated. Both real-change controls still
report `added` and `removed`.
