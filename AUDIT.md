# Orientim — repository audit

Written against the target of a production-grade execution reliability
infrastructure for AI agents. Nothing here is assumed: every claim below was
checked by reading the code, running the suite, or querying CI.

**Method.** Full module inventory; every module read; the test suite executed
(`pytest tests/test_suites.py`); conformance executed both ways; the declared
public surface mapped against the tests that touch it; documentation claims
checked against the code they describe.

**Verified state at the time of the audit**

| | |
|---|---|
| Package | 4,809 lines across 20 modules |
| Tests | 1,741 lines · **55 passing** (48 audit checks, 5 real-SDK, 2 conformance) |
| Docs | 2,656 lines across 11 files |
| CI | 20 jobs green — 3 operating systems x 5 Python versions, httpx 0.24.1/0.27.2/0.28.1, packaging, pip-audit, CodeQL |
| Conformance | 15/20 strict, 16/20 loose, 4 declared limits, **0 undeclared failures** |
| Capture | `httpx`, `httpx2`, `requests` |
| Runtime dependency | `httpx` only |

---

## 1. What actually works

Everything in this section is exercised by a test that can fail.

**Capture.** The hook is `HTTPTransport.handle_request` on `httpx` and `httpx2`,
and `HTTPAdapter.send` on `requests` — the point every request passes on its way
to a socket, not the client constructor. Verified against the real OpenAI and
Anthropic SDKs at two major versions each (openai 2.x and 3.x, anthropic 0.x and
1.x), including the clients they build for themselves.

**Replay.** Responses are served from the file in the recorded order. Ordering is
forced, so parallel calls are deterministic. All three libraries draw from one
shared queue, so a run whose model calls go through httpx2 and whose tools use
requests replays in a single recorded order.

**No egress on replay.** Verified, not asserted: the lab server's own call
counter does not move during a replay. An unmatched request receives a synthetic
599 and is not sent.

**Streaming.** Chunks reach the caller as they arrive during recording, and the
recorded boundaries come back on replay — for httpx and for requests. Optional
real-time pacing reproduces the gaps between them.

**Divergence detection.** A hash chain over method, url, header fingerprint,
status, body hash, lookup key and error yields the first differing step. 19 named
verdicts, none of them a bare "diverged", including `FIXED` / `STILL_BROKEN` (did
the recorded failure recur) and `NEW_CALL` (the code called something the
recording does not contain).

**Counterfactuals.** `replay(..., patch={i: {...}})` replaces what a step
returned and reports where the agent's own requests began to differ. Judged on
what the agent asked, not on what it was handed.

**Redaction.** Request headers are never stored, only a hash of the
response-affecting ones. Credential-shaped fields are removed from query strings,
JSON and form bodies in both directions; `user:pass@` is stripped from the
request line, from URL-valued response headers, and from URLs sitting inside JSON
values. Environment variables are opt-in, and a secret-shaped *name* is refused
even when it is named explicitly.

**Capture policy.** Ring buffer with trigger-based writing: exception, 5xx,
uncaptured source, explicit trigger, or `always=True`. Retention by count, age,
size, or failure signature.

**CI surface.** `orientim ci` replays a whole store, exits 0/1/2 correctly, emits
GitHub annotations and a step summary, and writes a machine-readable report
containing only ids, verdicts, hashes and counts — no prompts, no bodies. It can
compare against an earlier report to say which runs started failing on *this*
change.

**Concurrency attribution.** ContextVar-based; correct for async tasks and for
thread-per-request servers. Where attribution is genuinely ambiguous — a worker
thread with an empty context while several recordings are open — it refuses to
guess rather than file one run's traffic under another. A test forces the
ambiguity and asserts no cross-contamination.

---

## 2. What does not work, or does not exist

Measured against the target architecture. None of these is a bug; they are
absences, and some are load-bearing.

### 2.1 The execution model is HTTP-step-centric, not execution-centric

This is the single most consequential gap.

| Target field | Reality |
|---|---|
| `execution_id`, `timestamp`, `timing`, `schema_version` | present |
| external responses | present |
| **final output** | **not stored at all** |
| **model metadata** | absent — no model name, parameters or version |
| **agent metadata** | absent — only free-form `tags` |
| **tool calls** | not a concept; only raw HTTP steps |
| environment metadata | partial — opt-in variables and an OTel trace id |

The consequence is concrete: an evaluation layer and a "changed final answer"
diff cannot be built on this format. You cannot assess an answer that was never
recorded.

### 2.2 Absent subsystems

**Evaluation layer.** There is `assert_replays` (binary pass/fail on the replay
verdict) and a `check=` callback that re-runs one predicate. There are no
evaluators, no structured assertions, no warnings, no evaluation metadata, no
failure explanations from an evaluator.

**Saved cases.** No `case save`, `load`, `list`, `delete`, `run`, `run-all`.
`orientim ci` replays a *directory* against *one* entry point; there is no named
case, no per-case entry point, no expected result, no case metadata.

**Baselines.** `--baseline` compares two JSON *reports*. There is no
`baseline create` / `baseline compare` as a first-class object, and no notion of
an expected outcome per case.

**`orientim test`.** Does not exist.

**SDK surface.** `observe`, `inspect` and `export` do not exist. The public API is
`record`, `replay`, `assert_replays`, `Divergence`, plus the `store`, `chain` and
`diff` modules.

**Multi-repo, GitLab, policy enforcement.** Do not exist.

### 2.3 A documented claim that is false

`moto` is a declared dev dependency and appears in **zero** tests. `S3Backend`
has **zero** tests. Both `docs/limits.md` and `docs/recordings.md` state that the
S3 backend is "tested against `moto`". It is not. This must be resolved in one of
the two available directions.

### 2.4 Advertised surface with no tests

| Component | Lines | Tests referencing it |
|---|---|---|
| `stability.py` — a headline README feature with its own CLI command | 203 | **0** |
| `server.py` — the live replay server | 165 | 0 (only the auth helper and the entry loader) |
| CLI command handlers (`cmd_ls`, `cmd_play`, `cmd_view`, ...) | — | **0** |
| `S3Backend` | — | **0** |
| OpenTelemetry span path | — | 0 (only the `TRACEPARENT` fallback) |

The core is tested thoroughly. The periphery is largely not.

---

## 3. What is fragile

**The diff is positional, not aligned.** `diff.compare` walks index by index. One
inserted call shifts every subsequent row into "different", so the report is
loudest exactly when the change is smallest. There is no alignment pass, and it
cannot express "changed order" — a swap appears as two unrelated differences.

**Capture is coupled to third-party internals.** The httpx2 episode is the
precedent: openai 3.x and anthropic 1.x changed HTTP library entirely and capture
silently fell to zero steps. Moving to the transport layer reduced this class of
break but did not remove it. Every SDK major remains a candidate, and *silent* is
the dangerous part.

**The ring buffer counts shim steps.** Roughly 8,000 steps per 2,000 HTTP calls,
so the default `ring=512` holds about 128 HTTP calls. Longer agents truncate. It
is reported (`TRUNCATED`) rather than hidden, but a first-time user with a long
agent gets an unusable recording and no warning until replay.

**Ambiguous concurrency is dropped, not captured.** Refusing to guess is the right
call, but the result is a recording that is quietly incomplete for worker threads
under several concurrent recordings.

**Redaction is name-based.** Secrets in a URL *path*, and secrets a prompt carries
as prose, are not redacted. Documented — but it means a recording is not safe to
share by default, and the docs must keep saying so.

**`requests` bodies that are files or generators** are matched on method and URL
alone, because reading them would consume them before they are sent.

**No schema migration path.** A recording written under an older `FORMAT` is
refused with `STALE_FORMAT`. There is no upgrade path, so bumping the format
invalidates every recording already on disk.

---

## 4. What is missing for production

1. **An execution model worth evaluating** — final output, model and agent
   metadata, typed steps (model call vs tool call), and a migration path.
2. **Evaluation** — assertions, custom evaluators, pass / fail / warning,
   evaluation metadata, failure explanations.
3. **Cases and baselines** — named, versioned, with expected results.
4. **A test runner** — `orientim test`, with thresholds and a failure summary.
5. **Tests for the periphery** — CLI, stability, live server, S3.
6. **Quality gates** — no linter, no formatter, no type checking, no coverage
   measurement. Added naively to 4,800 existing lines these turn CI red on the
   first commit, so they need a cleanup pass first.
7. **Performance regression detection** — the cost figures in `docs/limits.md`
   were measured once by hand; nothing would notice if they doubled.
8. **A release path** — no publish workflow, not on PyPI, so every documented
   install is a git URL.
9. **An example project** — nothing a newcomer can clone and run end to end.
10. **Type information** — no `py.typed`, partial annotations.

---

## 5. What should be removed

- **The false S3/moto testing claim** in `docs/limits.md` and
  `docs/recordings.md`. Either write the moto tests or delete the sentence.
- **`SIDE_EFFECTING` / `MARK_ALL_WRITES`.** A hard-coded list of four URL
  fragments that only decorates the timeline. The mechanism it appears to guard
  is already closed by "nothing is forwarded", so it invites the belief that it
  gates something. Give it real meaning or remove it.
- **`tests/demo_agent.py` and `tests/demo_unstable.py`.** Referenced by no test
  and no document. Promote them into a documented example project, or delete.
- **The `version` input in `action.yml`.** Consulted only when `install: true`,
  which now defaults to false. Near-dead configuration.

---

## 6. What should be built

In dependency order. Each item is blocked by the one above it.

1. **Execution model v2.** Typed steps, final output, model and agent metadata,
   `schema_version` with a real migration path. Everything else depends on this.
2. **Evaluation layer.** Evaluators over a recorded execution; pass, fail, warn;
   structured explanations.
3. **Cases and baselines.** A case is a recording plus an entry point plus an
   expected outcome. A baseline is a set of case results at a commit.
4. **`orientim test`.** Runs all cases, evaluates, compares against the baseline,
   exits with a code CI can read.
5. **A diff engine that explains.** Sequence alignment instead of index walking;
   body-level differences; consequence ("the agent took the other branch");
   changed order; changed final answer.
6. **Periphery tests**, then quality gates once the code is clean enough to pass
   them.

---

## 7. Priorities

**P0 — make true what is already claimed.** Fix the S3/moto documentation claim.
Add tests for `stability`, the CLI handlers, and `S3Backend` via moto. Cheap, and
it closes the gap between what the repository says and what it proves.

**P1 — execution model v2.** Nothing in the product vision is reachable without
it, and every week it is delayed is another week of recordings written in a
format that will need migrating.

**P2 — evaluation, cases, baselines, `orientim test`.** This is the transition
from recorder to reliability infrastructure, and it is the layer a company would
pay for.

**P3 — a diff that explains.** High value, but it reads the execution model, so
it follows P1.

**P4 — multi-repo, GitLab, policy.** Not before a user asks.

---

## 8. Technical risks

**Third-party coupling.** Capture lives inside other people's internals. This is
the defining risk of the architecture and it has already fired once. Four
libraries are instrumented; each can change without notice.

**Silence is the failure mode.** When capture breaks it does not raise — it
records fewer steps. `UNCAPTURED_LIBRARY` and `NOTHING_CAPTURED` exist precisely
for this, and they are the most important code in the project. Any future
integration must fail loudly or not ship.

**A format change invalidates history.** With no migration path, execution model
v2 makes every existing recording unreadable. The migration has to be designed
before the format changes, not after.

**Single maintainer.** Four HTTP integrations, five Python versions, three
operating systems and a moving SDK ecosystem is a large surface for one person.

**Positional diff at scale.** As recordings grow, an index-based diff produces
noise, and noise trains users to ignore the report.

---

## 9. Product risks

**The core promise is load-bearing.** The value is entirely in `IDENTICAL`
meaning identical. One silent capture gap destroys it, and no feature compensates
for that. Every design decision should be weighed against this first.

**Replay proves a narrow thing.** It proves the recorded path did not break. It
cannot prove a new path works — a fix that adds a call reports `NEW_CALL` and
needs a fresh recording. Presenting this as proof that an agent is correct would
be false. The documentation currently states the limit correctly; it must stay
that way.

**Market timing.** Deterministic replay is acute for teams running agents in
production with real consequences. Most teams building agents today are
prototyping. The market is real, but earlier and narrower than it appears.

**Category collision.** Observability vendors are adjacent and better funded. The
defensible claim is the one they do not make: they tell you what happened, this
runs it again. That distinction has to stay sharp in every line of copy.

**Zero users.** Nothing in section 6 has been requested by anyone. The strongest
available signal — a user hitting `UNCAPTURED_LIBRARY` and asking for `aiohttp` —
has not arrived. Building the whole target architecture before the first user is
the most likely way this project fails.

---

## Verdict

The foundation is genuinely strong. Capture, replay, ordering, redaction and
divergence detection are correct, tested, and honest about their limits. The
periphery is thinner than the documentation implies, and one documented claim is
false.

The distance to the stated target is not measured in features. It is one
structural change — an execution model that records what the agent *did* rather
than only what crossed the wire — and most of what the vision asks for follows
from it.
