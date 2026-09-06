# Changelog

Notable changes, in the format of [Keep a Changelog](https://keepachangelog.com).
This project uses [semantic versioning](https://semver.org). Pre-1.0, the minor
number moves on anything that changes behaviour.

## [0.1.0] — unreleased

First public release.

### The pre-release audit

Before publishing, the package was audited against its own claims. Eight defects
were found, all of them in code that had a passing test. They are listed rather
than quietly fixed, because the coverage numbers in the README are only worth
anything if the failures are published too.

**Credentials**

- Every recording contained a full copy of `os.environ` — `OPENAI_API_KEY`,
  `AWS_SECRET_ACCESS_KEY` and everything else — written to a file people attach
  to bug reports. The existing secrets test passed because it searched for three
  specific strings that happened not to be in the environment. The environment
  is now stored only when you name the variables.
- `client_secret` in a form-encoded body was stored in full. OAuth token
  exchange is form-encoded, which made this the single most likely way a secret
  reached the file.
- `access_token` and `refresh_token` in a *response* body were stored in full.
  Only requests were being redacted.
- `user:password@` in a URL was stored in full.

**False `IDENTICAL`**

- A recording that captured no HTTP at all — an agent using `requests`, say —
  replayed as `IDENTICAL` with the genesis hash. Now `NOTHING_CAPTURED`.
- A replay that crashed *after* the last matching step reported `IDENTICAL`.
  Now `REPLAY_RAISED`.
- Two requests differing only by a header shared a lookup key, so a code change
  that swapped their order handed each one the other's response — and reported
  `IDENTICAL`. Now `HEADERS_CHANGED`, via a header fingerprint that is part of
  the step digest but not of the lookup key.
- The replay copied each matched step's digest out of the recording, which made
  the hash chain a tautology: it could only ever detect a difference in the
  number of steps. Digests are now computed from what the replay actually sent,
  under whichever key did the matching.

**The httpx patch**

- Two overlapping `record()` blocks left `httpx.Client.__init__` patched for the
  life of the process, silently attaching every client built afterwards to a
  recording that had ended. Reference-counted now, with a regression test that
  reproduces the corrupting order across two threads.
- The shim state was a single module-level slot, so `record()` inside
  `record()` switched the outer one's clock off on the way out. It is a stack
  now. The old test asserted only that nesting did not crash — which it never
  did.

**Other**

- A response body containing `</script>` escaped the viewer's `<script>` block,
  making any recording of a web-reading agent a stored XSS in the generated
  page.
- The live replay server executed the user's entry point on any `POST` to
  `/api/replay`, with no origin or token check — reachable from any website open
  in the same browser. Now token-gated.
- Step indices duplicated once the ring buffer filled.
- Binary responses were decoded with `errors="replace"`, so a replayed image was
  a different image. Stored as base64 now, byte-exact.
- Response headers were dropped entirely on replay.
- An interrupted `replay()` left the recorded environment applied to the live
  process permanently.

### Claims that did not survive

- **"17 of 20 sources captured" was wrong.** Two probes could not fail: the
  timezone probe asked for `strftime("%Z", gmtime())`, which is `GMT` on every
  machine on every day, and the library-version probe never changed the version
  it was testing. The first is now a real clock test; the second is a declared
  limit, because no record/replay tool can make a changed request identical.
  Request formatting drift was also being counted in both matching modes when it
  only passes under the loose key. The honest numbers are **16 of 20 with the
  loose key, 15 of 20 with strict bytes**, with zero undeclared failures in both.
- **"21 ms against 921 ms"** was a ratio produced by an artificial latency in
  the test server. Replay speed is real; the ratio is a fact about your
  provider, and the README says so now.
- The twenty-source specification existed twice — in the shipped package and in
  the test suite — and the two copies had already drifted to different numbers.
  The test suite now drives the shipped code.

### Added

- **Streaming that streams.** Responses pass through to your code as they
  arrive; the recorder notes each chunk's offset and size on the way past. The
  previous version read every response to completion first, so switching the
  recorder on changed how a token-streaming agent behaved. Replay reproduces the
  chunk boundaries, and `replay(..., realtime=True)` reproduces the gaps.
- **Detection of traffic we cannot capture.** Calls through `requests`,
  `aiohttp` and `urllib` during a recording are counted and named. A replay of a
  recording holding any of them reports `UNCAPTURED_LIBRARY` instead of
  `IDENTICAL`, and the timeline says so above the steps it does have.
- `random` state is captured and restored, so `randint`, `choice`, `uniform` and
  `shuffle` replay — not only `random.random()`.
- `time.time_ns()` shimmed.
- A recording format version. Files written under an older meaning are refused
  with `STALE_FORMAT` rather than judged by a rule that did not exist.
- `STREAM_INCOMPLETE` for a response the agent opened and never drained.
- `orientim conformance --strict`.
- `docs/` — the twenty-source taxonomy, the recording data contract, the
  architecture, the limits, and the verdicts.
- CI across Python 3.9–3.13, three operating systems, and both ends of the
  supported httpx range.

### Fixed after the audit

- A timeout part-way through reading a streamed body was recorded as a
  successful short response, so the replay returned truncated bytes where the
  recording had raised. Found by the audit's own regression suite while
  verifying the streaming rewrite.
- Restoring the `random` state on the way out of `record()` rewound the caller's
  global random stream, so thirty runs of one agent in one process drew the same
  numbers thirty times. Recording now only reads the state; only replay restores
  it. Found by `orientim stability`.
- `--entry` never resolved the user's own module when Orientim was installed as
  a console script, because the working directory is not on `sys.path` there.
- `orientim stability` reported "100% agreement" when every run had raised, and
  printed a hard-coded `--root` that did not match the one in use.

### Retention, counterfactuals and integration

Added after the first end-to-end study of what a client actually experiences.
The study found the flagship case did not work, which is the first item.

- **`record(always=True)`, and `ORIENTIM_ALWAYS`.** An agent that returns a
  *wrong* answer raises nothing and returns `200`, so no trigger fired and no
  file was written — on precisely the case the README describes. `run.path` came
  back `None`. The trigger set was right for a long-running agent and wrong for
  the first thing anybody tries. Documented on the main path now, with the
  quality-check pattern shown next to it.
- **`on_capture=fn(path, meta)`.** A recording announces itself to your alerting
  instead of waiting to be found by somebody running `orientim ls`. An exception
  from the hook is warned about, never raised into the agent.
- **`replay(..., patch={step: {...}})`.** Replace what a step returned and ask
  what the agent would have done. The README had promised "change one thing and
  ask what would have happened instead" while the code could only replay what
  was recorded. A patched replay is judged on the requests the agent made, not
  on the responses it was handed — those differ by construction — and reports
  `COUNTERFACTUAL` or `COUNTERFACTUAL_SAME`. It is never `IDENTICAL`.
- **Retention.** `store.prune()` and `orientim prune`, with four rules that
  apply together. The one worth knowing is `--per-signature`: five hundred runs
  that failed in the same way collapse to however many examples you asked for,
  because the chain root already says they are the same run. Nothing is ever
  deleted automatically, and an empty policy removes nothing rather than
  everything. Opt-in `ORIENTIM_MAX_RUNS` for containers with no cron.
- **`orientim diff a b`.** Two recordings side by side — Tuesday against
  Wednesday, neither of them the code you are holding.
- **`orientim.assert_replays(path, fn)`.** Fails a test with the whole
  diagnosis instead of `assert False`.
- **OpenTelemetry link.** A recording is stamped with the `trace_id` and
  `span_id` of the host application, from an active span or `TRACEPARENT`. No
  dependency is added and nothing is imported that was not already loaded.
  `orientim ls --trace <id>` finds the run behind a failed span.
- The generator state is written **only when the process drew from it**, and
  packed rather than spelled out as 625 integers: 7.3 KB in every recording —
  three quarters of a small file — became 3.3 KB in the few that need it.
  Detection is process-wide, so another thread drawing from the generator counts
  too; that errs towards storing 3 KB nobody needs rather than towards a replay
  that cannot reproduce the numbers the run saw.

Fixed while building the above:

- A timeout part-way through reading a streamed body was recorded as a
  successful short response, so a replay returned truncated bytes where the
  recording had raised.
- `Recording.add` had no lock, and `self._seq += 1` is a read-modify-write that
  four threads reach at once.
- Clients handed out by `run.client()` were never closed; a loop calling it left
  one connection pool open per call until the garbage collector felt like it.
- The first attempt at conditional generator state set the field in the shim's
  exit, which unwinds *after* the file is serialised — so it was never stored.

### Production readiness pass

- **Concurrent recordings in an async server were mixed together.** Attribution
  used the thread id, and on an event loop every coroutine shares one thread —
  so with five requests in flight, four recordings came out holding a single
  step and the fifth held everybody's traffic. This was the exact deployment
  the documentation recommends. It is a `ContextVar` now, correct for async
  tasks and for thread-per-request alike, with a regression test that runs five
  interleaved handlers. The clock and randomness shims and the
  uncaptured-library detector had the same flaw and the same fix.
- Cost measured rather than asserted: **+0.26 ms per call**, ~14–25 KB held per
  call, ~6.5 MB at the default ring, and +0.2 MB retained after 300 completed
  runs — no leak. The ring counts clock steps too, so 512 keeps about 128 HTTP
  calls; that number is now in the docs instead of a guess.

### Proven against the vendors' own SDKs

`tests/test_real_sdk.py` runs the real `openai` and `anthropic` packages —
their httpx client, their retry policy, their headers, their SSE parser —
against a local server that speaks their wire protocol. No key and no network
are needed, and the code path under test is the one a customer runs.

Five checks: traffic captured with the API key absent from the file, the agent
replaying `IDENTICAL` without the server seeing a single request, streaming
arriving as four separate tokens over 123 ms rather than one lump, the
Anthropic SDK likewise, and a counterfactual that flips the agent from
inventing an answer to giving the right one without sending the email.

Still unproven, and it needs somebody's key: that a real `api.openai.com`
endpoint behaves like this server.

### Automation

- **`orientim ci`** replays every recording in a store against one entry point,
  prints a summary a person can read, and exits `0` / `1` / `2` for identical /
  changed / could-not-run. On GitHub Actions it also writes workflow
  annotations and the job summary — no token, no App and no account involved.
- **`--baseline`** compares against an earlier report, which is what turns
  "this build is red" into "these two runs started changing on this pull
  request".
- **`action.yml`** — a composite action wrapping the above, plus a complete
  example workflow in `examples/`.
- The report it writes holds run ids, verdicts, hashes and counts. No URLs, no
  bodies, no prompts — asserted by a test that greps the serialised report for
  every string the agent sent. That is what makes the file safe to upload as an
  artifact, or later to a service, without anyone auditing it first.

### Known limits

Four sources are not captured by design — local reads, filesystem state,
framework caches, library version drift — plus the narrower limits in
[docs/limits.md](docs/limits.md). None of them is silent: each surfaces as a
named verdict.
