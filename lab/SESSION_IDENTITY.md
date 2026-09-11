# A session identifier: what it costs to store, and what it costs to remove

`python lab/session_identity.py`

Nothing in Orientim changed to produce this. No format, no chain, no field, no
matcher. The cases that need the proposed recorder behaviour *simulate* it by
wrapping two functions inside the lab process; nothing on disk was modified.

An MCP server mints a session identifier and returns it in a response header.
The client reads it **there** and echoes it on every later request. So it is
not configuration the agent was started with — it is a value the run derived
from a response, which is exactly what makes removing it hard.

Protocol 2025-11-25. Measured on `805fefd`. The server answers 400 to any
request carrying a session it did not issue, so "the client echoed it" is a
fact of every run below and not an assumption of the experiment.

---

## What was measured

```
A  today, a derived session        IDENTICAL 3/3   the identifier IS in the file
B  the header dropped              HEADERS_CHANGED
B2 one shared placeholder          HEADERS_CHANGED   (and two sessions become one)
C  the token, both sides           IDENTICAL 3/3   the identifier is NOT in the file
D  two recordings                  two tokens; a client holding the other one -> HEADERS_CHANGED

E  configured, never echoed        stored_headers []   nothing to protect
                                   IDENTICAL, with and without the transform
F  configured, echoed back         today          IDENTICAL, identifier in the file
                                   naive rule     identifier gone, HEADERS_CHANGED
                                   derived rule   IDENTICAL, identifier still in the file
G  one real session, two recordings today          fingerprints equal, diff identical
                                   with tokens    fingerprints differ, 2 steps
                                                  "different request headers"
H  two sessions, one recording     two tokens, IDENTICAL 4/4, no identifier in the file
I  the same identifier in a body   header is a token, and the identifier is
                                   still in the response body
```

## 1. Where the identifier is, today

| place | what is there | measured |
|---|---|---|
| the recording, response side | the identifier, in the clear, in `step["headers"]["mcp-session-id"]` | A: `a_real_id_is_in_the_file: True` |
| the recording, request side | only `hdr_fp`, a truncated SHA-256 over the header set — not recoverable | E: a session that only ever travelled on requests leaves nothing in the file |
| replayed responses | handed back to the agent verbatim | transport.py:645, reqs.py:192 |
| reports, baselines, exports, the viewer | nothing: no path reads stored header values | grep over `orientim/` |
| a divergence message | `"different request headers"` — the fact, never the value | diff.py:221 |
| response bodies, tool results, the run's output | **whatever the server or the agent put there** | I |

One adjacent case, by inspection and out of scope: the older HTTP+SSE
transport carries the session in a **query parameter**, `SECRET_PARAMS` has no
`session*` entry, and the URL *is* in the lookup key.

## 2. How a client uses it, record to replay

The client reads `Mcp-Session-Id` from the initialize response and sends it on
every request after that. On replay the recorded response is served verbatim,
the client re-derives the same value, and the request fingerprint matches
(A: IDENTICAL). That is why the naive fixes fail: B and B2 change one side of
the recording and not the other, and B2 would also merge two sessions into
one.

## 3. The smallest change, and the boundary of what it can cover

**Pseudonymise at record time, on both sides, per recording — and only for a
value the run derived from a response.**

```
on a session header in a response
    if no earlier request in this recording already carried that value
        mint a distinct token for it, once, and store the token

when fingerprinting a request
    substitute the token for any value already in the map, then hash

at replay time
    nothing changes
```

The last line is the point: a replay records nothing, so the map is empty, the
stored token is served, the client echoes the token, and the fingerprint is
taken over the token — which is what the recorded fingerprint was taken over.

The condition on the first line is not decoration. It is the difference
between a mechanism that works and one that breaks every replay, and F
measures both halves:

| | replay | identifier in the file |
|---|---|---|
| tokenise every session header in a response | **HEADERS_CHANGED** | no |
| tokenise only what no earlier request carried | IDENTICAL | **yes, for this case** |

A configured session cannot be both protected and replayable by this
mechanism: the client keeps sending the value it was configured with, so any
token in the file disagrees with it. E shows the good news — a configured
session that the server does not echo never reaches the file at all — and F
shows the residue: a configured session the server *does* echo stays in the
clear, and the contract has to say so rather than imply otherwise.

## 4. Two sessions stay two sessions

One token per distinct value, inside one recording (H: two initializes, two
tokens, replay IDENTICAL 4/4) and across recordings (D). A mismatch is
reported as it is today: a post-hoc `HEADERS_CHANGED` naming the fact and not
the value. It is **not** a pre-serve refusal.

`context.RELATIONS` already contains `session`, so a pre-serve obligation has
a slot. This proposal does not wire it, and it should not be wired silently:
today's `session` relation is `declared_unchained` — what the application says
about itself — while the header value is observed on the wire.

## 5. Existing recordings, `hdr_fp`, and comparability

**Old recordings keep working, untouched.** A *is* the old-recording case.

**An old recording cannot be scrubbed afterwards.** Its `hdr_fp` was taken
over the real value and the request headers themselves were never stored, so
there is nothing left to recompute from. Leave it and declare it, or drop the
header and accept `HEADERS_CHANGED` on every replay of it (B).

**`hdr_fp` keeps its shape** — same 16 hex characters, same place in
`DIGEST_FIELDS`, still absent from the lookup key.

**And here is the cost, measured.** Alignment pairs steps by the lookup key
and then compares step digests, which include `hdr_fp` — so a token changes
what a recording-to-recording diff says:

```
G  the same real session, recorded twice
   today        fingerprints equal        diff identical
   with tokens  fingerprints differ       2 steps, "different request headers"
```

For an ordinary derived session this changes nothing, because a server mints a
new session per run and today's fingerprints already differ. It changes
exactly the case where the session was stable across two recordings. That is a
**comparability limit**, and the file has to declare it rather than leave a
reader to discover it as a behaviour change.

---

## Contract v1

### Scope

| | |
|---|---|
| transform | replace a session identifier with a distinct random token |
| where | the `mcp-session-id` **response header**, and the request fingerprint taken over the same value |
| when | only when no earlier request in the recording carried that value |
| lifetime | one recording; the map is in memory and never written |
| token | a CSPRNG value, never derived from the identifier |

### The guarantee, and its limits

**Guaranteed:** a session identifier that this recording first observed in a
response does not appear in the recording's stored headers, and the fingerprint
that covers it is taken over the token instead.

**Not guaranteed, and not to be claimed:**

- that the identifier is absent from **response bodies, tool results, the
  run's output, or the agent's own logs** — measured in I: the header was a
  token and the identifier was still in the body;
- that a **configured** session is protected when the server echoes it back
  (F), or that anything protects a session travelling in a URL;
- that a **strict client accepts the token's shape**. The lab mints a token of
  the same length and character class, and that is a choice, not evidence. No
  compatibility claim is made until a real client is recorded and replayed
  against one;
- anything at all about **old recordings**, which are unchanged.

### Versioned metadata

The file declares what was done to it. Absence of the block means no
transform, which is what every existing recording says by saying nothing.

```json
"transforms": [
  {"id": "session_pseudonym",
   "v": 1,
   "scope": {"header": "mcp-session-id",
             "rule": "response_derived_only",
             "applies_to": ["stored_headers", "hdr_fp"]},
   "replaced": 2,
   "comparable_across_recordings": false}
]
```

- `id` + `v` name the semantics, so a later version can change the rule
  without a reader having to guess which one produced a given file. `v1` is
  exactly the table above.
- `scope` says which field was touched and under which rule, so "the header
  was transformed" is never read as "the identifier is gone from the file".
- `replaced` is a count of distinct values. Never the values, never the map.
- `comparable_across_recordings: false` is the limit measured in G: two
  recordings' `hdr_fp` values are not comparable on session-carrying steps.
  A reader — or a tool — can act on that instead of reporting a header change
  that means nothing.

This is the one part of the proposal that touches the recording format, in
metadata only, and it is the part to decide on explicitly.

### What it costs

| | |
|---|---|
| debugging | the stored token cannot be correlated with the server's own logs by session id. The mitigation — a keyed derivation the customer can reproduce, or an opt-out — is a separate decision |
| comparability | measured in G, and declared in the metadata rather than hidden |
| coverage | a configured session that is echoed back stays in the clear (F); bodies are never covered (I) |
| live traffic | a replay that talked to a live server would send a token the server never minted. Historical replay does not |

### Acceptance criteria

1. A derived session replays `IDENTICAL`, and the replayed client echoes the
   **stored token**.
2. No identifier that the recording first saw in a response appears anywhere
   in the file.
3. Two derived sessions in one recording produce two distinct tokens and the
   replay is `IDENTICAL` for both (H).
4. Two recordings produce different tokens; a replay whose client holds the
   other's token reports `HEADERS_CHANGED`, and no message contains either
   value.
5. **Boundary, not-echoed:** a configured session the server never echoes
   produces a recording identical in every respect to today's (E).
6. **Boundary, echoed:** a configured session the server echoes replays
   `IDENTICAL`, the value is **still stored**, and the metadata's scope makes
   that legible rather than surprising (F).
7. **Compatibility control:** a recording written before the change replays
   `IDENTICAL`.
8. **Negative control:** a recording that never saw a session header carries
   no transform block and behaves exactly as it does today.
9. The metadata declares `id`, `v`, `scope`, `replaced` and
   `comparable_across_recordings`; it never carries a value or a map.
10. A diff of two recordings that shared one real session reports
    `"different request headers"` on the session-carrying steps — asserted as
    the declared consequence, so the limit is tested rather than discovered.
11. Nothing in the file maps a token to anything.
12. **Explicitly not an acceptance criterion:** that a strict client accepts
    the token shape. If that is wanted it needs a real client recorded and
    replayed, and until then it is a declared unknown.

### Not in this proposal

Wiring the `session` relation into pre-serve mediation; the query-parameter
transport; scrubbing existing files; a customer-held key; a deterministic
token that would restore cross-recording comparability at the cost of
linkability; and any change to matching, the lookup key, `DIGEST_FIELDS` or
the chain.
