# Security

Orientim reads every request your agent makes, writes prompts and responses to
disk, patches a library the whole process shares, and can run your code from a
browser page. Each of those is a place to get it wrong, so each is written down
here.

## Reporting a vulnerability

Open a [GitHub security advisory](../../security/advisories/new) rather than a
public issue. If that is not available to you, open an issue saying only that
you have a security report and how to reach you — no details in the issue.

Expect a first reply within 72 hours. There is no bounty; this is a free package
with no company behind it.

## What the threat model actually is

The realistic bad outcomes, in the order they matter:

1. **A recording containing a credential ends up somewhere it should not** — a
   bug report, a shared bucket, a repository.
2. **A recording containing customer data is treated as harmless** because
   people think of it as a log file rather than as a transcript.
3. **A page built from a recording executes code** taken from a response the
   agent received.
4. **The local replay server is driven by a website** the developer happens to
   have open.
5. **The httpx patch damages the host application.**

All five have been exercised; four of them were real findings before release.

## 1 and 2 · What reaches the file

The full contract is in [docs/recordings.md](docs/recordings.md). The security
summary:

**Never written:** request headers (only a hash of the non-credential ones);
credential-shaped fields in query strings, JSON bodies and form bodies, in both
the request and the response; `user:password@` in a URL — the request line, a
`Location` (or other URL-valued) response header, and a URL that is a value
inside a JSON body; credential-shaped query parameters in a URL-valued response
header; response `Set-Cookie`; anything from `os.environ` unless you name the
variable — and a variable whose **name** looks like a secret is refused with a
warning even when named.

**Always written:** prompts and responses in full. That is the purpose of the
tool. If your prompts carry personal data, so do your recordings.

**Not redacted, and you need to know:** a secret inside a URL *path* — a Slack
webhook, a Telegram bot token — because nothing distinguishes it from an
ordinary path segment. And a secret your own code wrote into the middle of a
prompt, because redaction works on field names and a sentence has none.

Before sharing a recording:

```bash
grep -aoE '(sk|xox|ghp|AKIA|ya29)[A-Za-z0-9_\-]{10,}' runs/*.jsonl
```

### An earlier version stored the whole environment

Every recording contained a copy of `os.environ`, which on a normal developer
machine means `OPENAI_API_KEY`, `AWS_SECRET_ACCESS_KEY` and everything else. It
was found and fixed before the first release, and there is a regression test for
it. If you are running a build from before the first tagged release, treat any
recording it produced as a secret and delete it.

### Keeping less is the cheapest control

A recording that was never written cannot leak. Before anything else, decide
whether the run needed saving: nothing is written unless something triggers, and
`always=True` is a development convenience rather than a production setting.

Then bound what survives:

```bash
orientim prune --per-signature 5 --older-than 30
```

Set `--older-than` to your organisation's retention period and treat it as a
ceiling, not a target. `--per-signature` keeps a few examples of each distinct
failure instead of every copy of the same one, which is usually a smaller store
*and* a more useful one. Details in [docs/retention.md](docs/retention.md).

Nothing is ever deleted on its own, so this is a decision you have to make
rather than one the package makes quietly on your behalf.

## 3 · The viewer

`orientim view` builds a self-contained HTML page from a recording and opens it
from `file://`. Recorded response bodies are embedded in that page as JSON
inside a `<script>` block.

Any agent that reads a web page will eventually record a response containing the
literal string `</script>`, which closes the tag early and turns the rest of the
recording into markup the browser executes. `<`, `>`, `&`, U+2028 and U+2029 are
escaped before embedding. There is a regression test that feeds a real payload
through a real socket and checks the generated page.

Recordings are untrusted input. If you build your own tooling on the `jsonl`,
treat every field that way.

## 4 · The live replay server

`orientim view <run> --entry module:function` starts a local HTTP server that
**executes the entry point you named** when the page asks it to.

- It binds to `127.0.0.1`, never `0.0.0.0`.
- `/api/replay` and `/api/progress` require `X-Orientim-Token`, generated per
  process and known only to the page that server produced. A custom header
  cannot be sent cross-origin without a preflight this server never answers.
- A request carrying a foreign `Origin` is refused.

Without the token check, any website open in the same browser could `POST` to
`127.0.0.1:8740` with a simple content type and start a replay. That was a real
finding.

The generated `.live.html` file on disk contains that process's token. It stops
working when the server exits, but delete the file if you do not want it lying
around.

## 5 · The httpx patch

`record()` replaces `httpx.Client.__init__` and `httpx.AsyncClient.__init__`
process-wide for the duration of the block. It is the reason the package works
with code nobody wrote for it, and the reason it can hurt.

- Overlapping and nested blocks are reference-counted; the true original is
  restored exactly once. Save-and-restore used to leave httpx permanently
  patched whenever two blocks overlapped — a real finding, with a regression
  test that runs two blocks from two threads in the corrupting order.
- If httpx changes its internals, the wrapper warns once and captures nothing
  rather than breaking every client the host application builds.
- The dependency is pinned `>=0.24,<1.0`.

**Do not wrap a long-lived server process in `record()`.** While a block is
open, every client built anywhere in the process is instrumented, including ones
belonging to requests you are not recording. Wrap the request handler.

## Deliberate non-features

- **No network egress.** The package talks to your storage backend and to
  nothing else. There is no server, no account, no telemetry, no update check.
  Verify it: run the suite with the network blocked.
- **No credentials of ours.** S3 access uses boto3's normal chain — environment,
  `~/.aws/credentials`, or an instance role. We never see it.
- **No forwarding during replay.** `ReplayTransport` holds no reference to a
  network transport. There is no code path from a replay to a socket, so a
  replay cannot repeat a side effect regardless of configuration.

## Supported versions

Pre-1.0. Fixes go to the latest release only. Python 3.9+, httpx `>=0.24,<1.0`,
both ends of both ranges exercised in CI.
