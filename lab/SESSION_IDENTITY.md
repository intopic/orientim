# A session identifier: what it costs to store, and what it costs to remove

`python lab/session_identity.py`

Nothing in Orientim changed to produce this. No format, no chain, no field, no
matcher. Case C below *simulates* the proposed recorder change by wrapping two
functions inside the lab process; nothing on disk was modified.

An MCP server mints a session identifier and returns it in a response header.
The client reads it **there** and echoes it on every later request. So it is
not configuration the agent was started with — it is a value the run derived
from a response, which is exactly what makes removing it hard.

Protocol 2025-11-25. Measured on `805fefd`.

---

## 1. Where the identifier is, today

| place | what is there | measured |
|---|---|---|
| the recording, response side | the identifier, in the clear, in `step["headers"]["mcp-session-id"]` | `real_id_in_file: True` |
| the recording, request side | only `hdr_fp`, a truncated SHA-256 over the header set — not recoverable | `_hdr_fp`, transport.py:204 |
| replayed responses | handed back to the agent verbatim | transport.py:645, reqs.py:192 |
| reports, baselines, exports, the viewer | nothing: no path reads stored header values | grep over `orientim/` |
| a divergence message | `"different request headers"` — the fact, never the value | diff.py:221 |

So the exposure is **the recording file and anything that copies it**, and it
is one field. `hdr_fp` is a hash and is in `chain.DIGEST_FIELDS`; it is *not*
in the lookup key.

One adjacent case, by inspection and out of scope here: the older HTTP+SSE
transport carries the session in a **query parameter**, `SECRET_PARAMS` has no
`session*` entry, and the URL *is* in the lookup key — so that shape would
both store the identifier in the clear and make the fixture unreachable for a
different session. Not measured, not proposed; named so it is not forgotten.

## 2. How a client uses it, record to replay

Measured, with a server that returns 400 to any request carrying the wrong
session — so "the client echoed it" is a fact of the run and not an assumption
of the experiment:

```
A  record    the client read Mcp-Session-Id from the initialize response
             and sent it on the two requests that followed
   replay    IDENTICAL, 3/3 matched, headers_changed False
             the replayed client derived the *same identifier* from the
             recorded response and echoed it
```

This is the whole mechanism, and it is why the naive fixes fail:

```
B  the header dropped from the stored response      HEADERS_CHANGED
B2 one shared placeholder in its place              HEADERS_CHANGED
```

B2 fails even though the client faithfully echoes what it was given, because
the recorded *request* fingerprint was taken over the real value. Any fix that
touches one side of the recording and not the other turns every replay into a
divergence — and a placeholder would also merge two sessions into one, which
is the thing that must not happen.

## 3. The smallest change that protects it and keeps the correspondence

**Pseudonymise at record time, on both sides, per recording.**

```
on a response header named Mcp-Session-Id
    mint a distinct token for that value, once, and store the token

when fingerprinting a request
    substitute the token for any value already in the map, then hash

at replay time
    nothing changes
```

The last line is the point. The map is built while recording; a replay records
nothing, so the map is empty, the stored token is served, the client echoes
the token, and the fingerprint is taken over the token — which is what the
recorded fingerprint was taken over. The file is consistent with itself in
token space, and every comparison behaves as it does today.

```
C  record with the transform, replay with untouched code
   replay        IDENTICAL, 3/3 matched, headers_changed False
   client got    the stored token, not the identifier
   exposure      real_id_in_file: False
```

A value seen in the named header from *either* side gets a token, so a client
that was configured with a session it did not derive is covered by the same
rule.

## 4. Two sessions stay two sessions

One token per distinct value. Different values, different tokens — inside one
recording and across recordings:

```
D  stored token 1     sess-27424d166294747d
   stored token 2     sess-a5761a52ec046ab7
   tokens differ      True
   recording C replayed by a client holding token 2   HEADERS_CHANGED
```

A mismatch is reported the way it is reported today: a post-hoc
`HEADERS_CHANGED` verdict naming the fact and not the value. It is **not** a
pre-serve refusal — the fixture is still served first and the disagreement
arrives after the run, which is the tier separation R2 exists to name.

`context.RELATIONS` already contains `session`, so a pre-serve obligation has
a slot. This proposal does not wire it, and it should not be wired silently:
today's `session` relation would be `declared_unchained` — what the
application says about itself — while the value in the header is observed on
the wire. Those are different evidence kinds and would need saying so.

## 5. Existing recordings, `hdr_fp` and diff

**Old recordings keep working, untouched.** Case A *is* the old-recording
case: the identifier is stored in the clear, the replayed client re-derives
it, and the fingerprint matches. The transform is per-recording and
self-consistent, so a file written before the change and a file written after
it each replay correctly under the same code.

**An old recording cannot be scrubbed afterwards.** Its `hdr_fp` was taken
over the real value and the request headers themselves were never stored, so
there is nothing to recompute the fingerprint from. The only options for an
existing file are to leave it and declare it, or to drop the header and accept
`HEADERS_CHANGED` on every replay of it — measured as case B. This is a hard
limit, not a design choice, and it is the strongest argument for making the
change soon rather than later.

**`hdr_fp` keeps its shape**: same 16 hex characters, same place in
`DIGEST_FIELDS`, still absent from the lookup key. New recordings take their
chain root over the token; nothing compares a chain root across recordings
(`recorded_root` is stored everywhere and compared to nothing), so no baseline
or diff changes meaning. Cross-recording diff already sees a different session
per run, because a server mints a new one per run.

---

## Recommendation

The record-time token, with four specifics:

1. **The token is random, never derived from the value.** A CSPRNG token, not
   a hash and not an HMAC, so there is no brute-force or linkage question to
   argue about at all.
2. **Same character class and same length as the value it replaces**, so a
   client that validates the shape of a session identifier is satisfied. MCP
   requires visible ASCII; a `sess-`-prefixed hex token of equal length meets
   that and carries nothing.
3. **A named header list, not a heuristic.** `Mcp-Session-Id` and nothing
   else, until a measured case adds to it. A heuristic over header names would
   eventually pseudonymise something a replay depends on.
4. **Declared in the recording metadata.** The product contract requires every
   transform to be declared and forbids calling a transformed artifact a
   lossless copy; a reader must be able to tell a token from an identifier
   without guessing. This is the one part of the proposal that touches the
   file format, in metadata only, and it is the part to decide on explicitly.

### What it costs

| | |
|---|---|
| debugging | the stored token cannot be correlated with the server's own logs by session id. This is the real price, and the mitigation — a keyed HMAC the customer can re-derive, or an opt-out — is a separate decision, not part of the minimum |
| linkage | a token is minted per recording, so two recordings of the same session do not share one. Real sessions differ per run anyway |
| live traffic | a replay that talked to a live server would send a token the server never minted. Historical replay does not, and this stays true only while that is so |
| strict clients | a client that validates more than the character class and length of a session identifier could reject a token. Unmeasured |
| old files | unchanged, and unscrubbable. The exposure that already exists stays |

### Acceptance tests

1. A recording whose session was derived from a response replays `IDENTICAL`,
   and the replayed client echoes the **stored token**.
2. The real identifier appears nowhere in the recording file.
3. Two different session values produce two different stored tokens, within
   one recording and across two.
4. A replay whose client holds another session's token reports
   `HEADERS_CHANGED`, and no message contains either value.
5. **Compatibility control:** a recording written before the change still
   replays `IDENTICAL`.
6. **Negative control:** a recording that never saw the header behaves exactly
   as it does today — no token, no map, nothing new in the file beyond the
   declaration.
7. The map is never written: the file contains no structure mapping tokens to
   anything.
8. The token satisfies the MCP character rule and matches the length of the
   value it replaced.
9. A second recording of the same real session mints a different token.

### Not in this proposal

Wiring the `session` relation into pre-serve mediation; the query-parameter
transport; scrubbing existing files; a customer-held key; and any change to
matching, the lookup key, `DIGEST_FIELDS` or the chain.
