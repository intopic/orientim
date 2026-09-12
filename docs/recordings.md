# What is in a recording

A recording is a file. It holds what your agent said to the outside world and
what came back. Read this before you put one in a bug report, a bucket, or a
repository.

The short version: **treat a recording the way you treat your logs.** It is
roughly the same category of thing, with the same category of risk.

## The format

One JSON object per line. The first line is metadata; every line after it is a
step.

```
{"_meta": {"run_id": "run_2bea9035", "started_at": 1788664972.1, ...}}
{"t": "http", "role": "model", "method": "POST", "url": ".../v1/chat/completions", ...}
{"t": "shim", "kind": "time", "value": "1788664972.63"}
{"t": "http", "role": "tool", "method": "POST", "url": ".../v1/search", ...}
```

It is `jsonl`, deliberately: you can `grep` it, `head` it, and read it without
this package installed. A debugging format you cannot open with the tools you
already have is a format you will not trust.

### A step

| field | what it is |
|---|---|
| `method`, `url` | the request line, with credentials redacted from the URL |
| `req` | the request body, with credential-shaped fields redacted |
| `status`, `body` | the response |
| `b64` | true when the body was not valid UTF-8 and is base64 |
| `chunks` | `[offset_ms, n_bytes]` per streamed chunk — the shape of the stream |
| `key_strict`, `key_loose` | lookup hashes, computed from the real bytes |
| `hdr_fp` | a hash of the request headers that can change the response |
| `body_sha` | a hash of the response bytes |
| `headers` | response headers, minus `Set-Cookie` and encoding headers, with credentials redacted from any URL-valued header (`Location`) |
| `t0`, `ms` | when it started and how long it took |
| `error` | the exception type, for a call that failed or timed out |
| `complete` | false if the response was never fully read |
| `i` | position in the run |
| `patched` | present only on a step whose response a counterfactual replaced |
| `role` | `model`, `tool` or `unknown` — a **heuristic**, never used to match |
| `model` | model, temperature, max_tokens, stream, tool names offered |
| `served` | token usage, the model that actually answered, the stop reason, and the tool calls it requested |

The last three are the execution model, described in
[execution-model.md](execution-model.md). They are outside
`chain.DIGEST_FIELDS`, so they cannot change a verdict — strip them from a file
and it replays to exactly the same result.

The metadata line carries `outcome` (the final output, if you declared one),
`agent` (who the agent is, if you declared it), `runtime` (python, platform and
library versions), and `format`. A recording written by format 3 is upgraded
when it is read and carries `migrated_from: 3`; the file on disk is never
rewritten.

## What is never written down

- **Request headers.** Your `Authorization: Bearer …` is used to make the call
  and then forgotten. Only `hdr_fp` survives — a SHA-256 of the headers that can
  change a response, with credential-bearing and volatile ones excluded. A hash
  cannot be read back into a token, and it is enough to notice that a request
  changed.
- **Credential-shaped fields**, wherever they appear: query parameters, JSON
  keys, and `x-www-form-urlencoded` fields, in the **request and the response**.
  The name list is in `transport.SECRET_PARAMS` and covers `api_key`, `token`,
  `access_token`, `refresh_token`, `client_secret`, `password`, `signature` and
  their neighbours.
- **`user:password@` in a URL** — in the request line, in a response header such
  as `Location`, and in a URL that appears as a value inside a JSON body (a
  callback, a webhook, a next-page link). Wherever a `scheme://user:pass@host`
  appears, the userinfo is stripped.
- **Response `Set-Cookie`**, and credential-shaped query parameters in any
  URL-valued response header (a redirect `Location`, above all).
- **The environment.** Nothing from `os.environ`, unless you name variables:

  ```python
  with orientim.record(env=["MODEL", "REGION"]) as run:
      ...
  ```

  or `ORIENTIM_CAPTURE_ENV=MODEL,REGION`. Name only variables that are not
  secrets — and a variable whose **name** looks like a secret (`*_KEY`,
  `*_SECRET`, `*_TOKEN`, `*PASSWORD*`, `*CREDENTIAL*`, and the like) is **refused
  with a warning** and left out of the file, even when you name it. For the
  variables that are stored, redaction does not apply — you asked for these by
  name, so they are stored as they are.

Redaction happens before anything is written, and the lookup keys are computed
from the real bytes, so a redacted recording replays exactly like an unredacted
one. There is no fidelity cost — with one honest exception: if your client
**follows a redirect** whose `Location` URL carried a credential, a replay hands
back the redacted `Location`, so the follow-up request goes to the redacted URL
and reports a divergence instead of `IDENTICAL`. That is deliberate. A leaked
credential cannot be un-leaked; a divergence is just re-recorded.

```
url : https://api.example.com/v1/chat?api_key=<redacted>
req : {"prompt": "What is the status of order 4471?", "api_key": "<redacted>"}
```

## What is written down, and you should think about it

**Prompts and responses, in full.** This is the point of the tool. A recording
that throws away the conversation cannot replay the run, and there is no useful
version of this package that does. If your prompts contain customer names,
addresses, medical details or anything else you would not paste into a public
issue, then your recordings contain them too.

**A secret inside a URL path.** A Slack webhook is
`https://hooks.slack.com/services/T00/B00/XXXXXXXX`; a Telegram call is
`https://api.telegram.org/bot<token>/sendMessage`. Nothing distinguishes those
segments from an ordinary path, so they are not redacted. If you call APIs
shaped like that, know it before you share a file.

**A secret your own code put in a prompt.** Redaction works on field names. A
key pasted into the middle of a sentence has no name.

## One thing you can ask it to hide: an MCP session identifier

Off by default. `orientim.record(root="runs", session_tokens=True)` stores an
opaque token in place of the `Mcp-Session-Id` a server issued, and takes the
request fingerprint over the same token, so the file is consistent with itself
and the identifier is not in it.

Both sides, or neither. A server mints the session and returns it in a
response header; the client reads it *there* and echoes it on every later
request. Hide it in the response alone and the replayed client echoes a value
the recorded fingerprint was never taken over, and every replay diverges.

**Turning it on asserts something Orientim cannot check:**

> your client takes the session identifier from the response and re-sends it
> as an opaque value.

What the recorder checks is narrower — that no earlier request carried this
value — and that is an observation, not proof. A client that keeps using a
session from its own configuration looks the same from here. Where the
assertion is false *and* the client's value reaches a captured request header,
the first replay reports `HEADERS_CHANGED`; where it does not reach one — a
value used only inside your code — nothing notices. Sequential runs only: a
step is opened when the response headers arrive, so with requests in flight at
once the rule can see a response before the request that preceded it.

**What it covers:** the stored `Mcp-Session-Id` headers and the fingerprint
over them.

**What it does not:** a session identifier in a response body, in a tool
result, in the run's declared output, in a URL, or in your own logs. A session
your client was configured with, when the server echoes it back, stays in the
file — renaming it would make the file disagree with a client that never read
it — and the recording says so, with the reason, in `transforms`.

**What it costs:** a token is minted per recording, so two recordings of one
session carry two tokens and their fingerprints are no longer comparable on
those steps. `comparable_across_recordings: false` says this in the file, and
a diff between two such recordings reports *request headers not comparable*
rather than a change. It is a limit on the comparison, not a finding, and it
never turns a difference into a match.

## Is this the recording the case was frozen against

Every recording written now carries an `integrity` descriptor in its metadata:
a scheme, a version, an algorithm, a step count, and a digest over the
artifact. And every case written now stores the same digest as an **anchor**,
because the case is the thing that lives in git and gets reviewed.

Two answers, and they are worth very different amounts:

| | detects | needs |
|---|---|---|
| the descriptor matches | corruption, including the kind that leaves the JSON valid | the file alone |
| the anchor matches | **substitution** — this is not the file the case named | the case, which the editor of the recording may not have thought to change |

The first is not the second. An editor who recomputes the descriptor produces
a perfectly self-consistent file, so self-consistency is never spent as if it
were tamper evidence.

**What the digest covers**: the step lines byte for byte, and the metadata as a
canonical mapping — the descriptor included, minus its own digest value, so
editing what the descriptor claims is a mismatch rather than a way out. Not
every byte of the file: blank lines are dropped, and the metadata cannot be
covered as written because writing the digest into it would change what the
digest is over.

**An anchor is an obligation.** Where a case declares one and the recording
cannot be verified against it, every profile refuses — a recording that cannot
be checked is not a recording that may be used. A case with no anchor field at
all declares nothing: the legacy profile runs it and reports it as unverified,
the protected profile refuses it. A field that is *there* and unusable —
`null`, `{}`, a digest with no scheme — is a declared anchor that cannot be
met, and is refused.

A refusal happens **before the agent runs**, and it is a harness outcome: the
case did not fail, it did not run, and no comparison reads it as a change in
the agent.

```bash
orientim test --fixtures protected
```

And the identity travels into the comparison. A baseline row records which
artifact it was measured from, so two rows taken from two different recordings
are reported as not comparable rather than as the agent having moved — no
`fixed`, no `newly_changed`, and the build blocks saying which cases and why.
A baseline written before this carries no fixture identity at all: the
comparison still happens, and says that the identity could not be established
rather than assuming the two sides measured the same thing.

Nothing is rewritten. A recording written before this has no descriptor and
stays exactly as it is; a case written before this has no anchor and is never
given one, because anchoring an existing case would freeze whatever the file
happens to hold today.

## Before you share one

```bash
grep -aoE '(sk|xox|ghp|AKIA|ya29)[A-Za-z0-9_\-]{10,}' runs/run_2bea9035.jsonl
```

Thirty seconds, and it catches the shapes that matter most. There is no
substitute for opening the file: it is line-delimited JSON precisely so that you
can.

## Where they live

Local disk by default, under `runs/`. One setting moves them:

```bash
pip install "orientim[s3] @ git+https://github.com/intopic/orientim"
export ORIENTIM_STORE=s3://your-bucket/orientim
```

S3, R2, MinIO, B2, Spaces — anything S3-shaped, with `ORIENTIM_S3_ENDPOINT` for
the non-AWS ones. Credentials come from boto3's normal chain. Nothing is sent
anywhere else; there is no service behind this package. The S3 backend is tested
against moto — write, read, list with pagination, stat, delete, signed URLs, and
a full record-and-replay round trip — but not against a real bucket. See
[limits.md](limits.md) for what a mock does not tell you.

The viewer opens a bucket recording through a short-lived signed URL, and the
HTML page it builds is always written to local disk, never to your bucket.

## When a recording is written at all

**This is the first thing to get right, and the easiest to get wrong.**

Not on every run. Steps live in a ring buffer — 512 by default — and the file is
written only when something triggers:

- the agent raised an exception,
- a response came back `5xx`,
- a source we could not capture was hit,
- or you asked: `run.rec.trigger("wrong answer")`.

`run.path` is `None` when nothing triggered. This is how a long-running agent
avoids writing gigabytes nobody will open.

The trap: **an agent that returns a *wrong* answer raises nothing and returns
`200`**, so nothing fires and nothing is saved — and a wrong answer is the case
this tool exists for. Wire your own quality check to `trigger()`, or use
`always=True` (or `ORIENTIM_ALWAYS=1`) while developing:

```python
with orientim.record(tags={"customer": cid}) as run:
    answer = my_agent()
    if not looks_right(answer):
        run.rec.trigger("failed the quality check")
```

`on_capture=fn(path, meta)` is called after a file is written, so a recording
can announce itself to your alerting instead of waiting to be found by somebody
running `orientim ls`. An exception from the hook is warned about, never raised
into your agent.

How long to keep what gets written: [retention.md](retention.md).

If the ring fills before the trigger fires, the oldest steps are evicted, the
count is recorded, and any replay of that file reports `TRUNCATED` rather than
pretending the run is whole. Raise `ring=` or trigger earlier.

## What the file admits it does not have

Two fields exist so a recording can tell you it is incomplete:

- `unseen` / `unseen_n` — calls that left the process through `aiohttp` or
  `urllib`, which Orientim does not intercept. They are counted and
  named. A replay of a recording holding any of these can never report
  `IDENTICAL`; it reports `UNCAPTURED_LIBRARY` and lists them. The timeline says
  so above the steps.
- `complete: false` on a step — the response was still streaming when the file
  was written.

A recording that is missing part of the run and does not say so is worse than no
recording at all. Both of these exist to make that impossible.

## Format version

`_meta.format` is the recording format. A file written by an older version is
refused rather than judged by a rule that did not exist when it was written —
the replay reports `STALE_FORMAT`. Re-record, or pin the version that wrote it.

Current: **3**.

## Two fields you did not put there

- **`trace`** — the `trace_id` and `span_id` of the host application, taken from
  an active OpenTelemetry span or from `TRACEPARENT`, when either exists. No
  dependency is added and nothing is imported that was not already loaded. It
  exists so an engineer looking at a failed span in Grafana can find the run:
  `orientim ls --trace <id>`.
- **`random_state`** — the state of the shared `random` generator at the start
  of the run, packed and base64-encoded (~3.3 KB), and written **only if the
  process actually drew from it**. Detection is process-wide: another thread
  drawing from the generator counts too, which errs towards storing 3 KB nobody
  needs rather than towards a replay that cannot reproduce the numbers the run
  saw.

---

New to Orientim? Start with [how-to-use.md](how-to-use.md).
