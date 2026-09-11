# A session identifier: what it costs to store, and what it costs to remove

`python lab/session_identity.py`

Nothing in Orientim changed to produce this. No format, no chain, no field, no
matcher. The cases that need the proposed recorder behaviour *simulate* it by
wrapping two functions inside the lab process; nothing on disk was modified.

An MCP server mints a session identifier and returns it in a response header.
A client that reads it **there** and echoes it on every later request is the
case this contract is for. Protocol 2025-11-25, measured on `805fefd`. The
server answers 400 to any request carrying a session it did not issue, so "the
client echoed it" is a fact of every run below and not an assumption.

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

## Where the identifier is, today

| place | what is there | measured |
|---|---|---|
| the recording, response side | the identifier, in the clear, in `step["headers"]["mcp-session-id"]` | A |
| the recording, request side | only `hdr_fp`, a truncated SHA-256 over the header set — not recoverable | E: a session that only ever travelled on requests leaves nothing in the file |
| replayed responses | handed back to the agent verbatim | transport.py:645, reqs.py:192 |
| reports, baselines, exports, the viewer | nothing: no path reads stored header values | grep over `orientim/` |
| a divergence message | `"different request headers"` — the fact, never the value | diff.py:221 |
| response bodies, tool results, the run's output | **whatever the server or the agent put there** | I |

One adjacent case, by inspection and out of scope: the older HTTP+SSE
transport carries the session in a **query parameter**, `SECRET_PARAMS` has no
`session*` entry, and the URL *is* in the lookup key.

## How a client uses it, record to replay

The client reads `Mcp-Session-Id` from the initialize response and sends it on
every request after that. On replay the recorded response is served verbatim,
the client re-derives the same value, and the request fingerprint matches
(A: IDENTICAL). That is why the naive fixes fail: B and B2 change one side of
the recording and not the other, and B2 would also merge two sessions.

---

# Contract v1

Decided: versioned metadata **yes**; random per-recording tokens **yes**;
deterministic or keyed tokens **out of scope**; automatic global activation
**no**; explicit activation with a usage contract **yes**.

## 1. Activation, and the condition the operator asserts

**Off by default. Enabled per recording, explicitly.**

The recorder can check one thing: that no earlier request in this recording
carried this value. Call it what it is — an **observation rule**,
`first_observed_in_response` — and not proof that the client took the value
from the response. The counterexample is concrete:

```
the client is configured with S
initialize carries no session
the server answers with S
the client ignores the header and keeps using S from its configuration
```

The observation rule says "first seen in a response" and the recorder
tokenises. At replay the client sends S while the recorded fingerprint is over
the token, and the replay diverges. So the rule needs a condition that only
the operator can assert, and asserting it is what enabling the feature means:

> **Usage condition.** The client takes the session identifier from the
> response and re-sends it as an opaque value. If your client sources the
> session from configuration, do not enable this.

One property makes explicit activation safe rather than reckless, and it
should be stated wherever the switch is documented: **when the condition does
not hold, the failure is loud.** The first replay reports `HEADERS_CHANGED`
(measured as the naive rule in F). There is no configuration of this feature
that produces a false `IDENTICAL`.

## 2. The transform

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

**The token.** An opaque value of one documented shape: `sess-` followed by 32
lowercase hex characters, 128 bits from a CSPRNG, never derived from the
identifier.

**Length is not preserved**, and the earlier proposal was wrong to ask for it.
Truncating a random token to the length of the value cuts off exactly the
random part:

```
"A" -> "s"        "S" -> "s"        two sessions, one token
```

**Collisions are prevented inside the recording**: a minted token is drawn
again if it equals a token already minted, or a session value already observed,
in this recording.

**Compatibility with a client that reads structure into a session identifier
is outside this contract.** The token is opaque by construction; no claim is
made that a client which parses or validates the shape of an identifier will
accept it, and none will be made without a real client recorded and replayed.

## 3. What is guaranteed, and what is not

**Guaranteed:** a session identifier covered by the transform does not appear
in the recording's **stored headers**, and the fingerprint that covers those
headers is taken over the token instead.

**Not guaranteed, and not to be claimed:**

- anything about **response bodies, tool results, the run's output, or the
  agent's own logs** — measured in I: the header was a token and the
  identifier was still in the body;
- that a **configured** session is protected when the server echoes it back
  (F), or that anything protects a session travelling in a URL;
- that a **strict client** accepts the token;
- anything about **old recordings**, which are unchanged and unscrubbable:
  their `hdr_fp` was taken over the real value and the request headers were
  never stored, so there is nothing left to recompute from.

## 4. Metadata, and what has to read it

```json
"transforms": [
  {"id": "session_pseudonym",
   "v": 1,
   "activation": "explicit",
   "scope": {"header": "mcp-session-id",
             "rule": "first_observed_in_response",
             "condition": "client_reuses_response_value",
             "applies_to": ["stored_headers", "hdr_fp"]},
   "transformed": {"values": 2, "steps": [0, 1, 2, 4, 5]},
   "untransformed": [{"values": 1, "steps": [7, 8],
                      "reason": "carried_by_an_earlier_request"}],
   "comparable_across_recordings": false}
]
```

- `rule` is what the recorder checked; `condition` is what the operator
  asserted by enabling it. Two fields, because they are two different kinds of
  claim.
- `transformed.steps` and `untransformed.steps` identify the affected
  interactions **by index**. No values, no map, no counts of anything secret.
- The residue is named with its reason, which is what makes a count legible:
  `transformed.values: 0` with an empty `untransformed` means no session
  header was seen at all, while `transformed.values: 0` with a non-empty one
  means a session was seen and deliberately left in the clear.
- Absence of the whole `transforms` key means no transform — which is what
  every existing recording says by saying nothing.

**Writing it is not enough. Three rules for reading it:**

1. **Inside one recording's replay, nothing relaxes.** `hdr_fp` is checked
   exactly as it is today. The file is self-consistent in token space, so a
   divergence there is a real divergence.
2. **Between two transformed recordings**, a step that both sides declare as
   affected, and whose `hdr_fp` differs, is reported as *"request headers not
   comparable: session_pseudonym v1"* — a comparison limited by the
   instrument. Not a proved session change, and certainly not a change in the
   agent.
3. **It must not hide other header changes.** `hdr_fp` is one aggregate hash;
   when it differs on an affected step, nobody can say which component moved.
   So the step stays reported as changed-and-uncertain and is **never folded
   into `same`**. The honest output is a stated limit on the comparison, not a
   manufactured equality — G must keep producing a row, with a better reason
   on it.

## 5. Acceptance criteria

1. A derived session replays `IDENTICAL`, and the replayed client echoes the
   **stored token**.
2. **(corrected)** No identifier covered by the transform appears in the
   recording's **stored headers**. Not "anywhere in the file" — I measures why
   that would be false.
3. Two derived sessions in one recording produce two distinct tokens and the
   replay is `IDENTICAL` for both (H).
4. Two recordings produce different tokens; a replay whose client holds the
   other's token reports `HEADERS_CHANGED`, and no message contains either
   value.
5. Two different values never receive one token, including at the shortest
   identifier a server may mint. The truncating generator is a counterexample
   and must fail this test.
6. **Boundary, not echoed:** a configured session the server never echoes
   produces a recording identical in every respect to today's (E).
7. **Boundary, echoed:** a configured session the server echoes replays
   `IDENTICAL`, the value is **still stored**, and the metadata's
   `untransformed` entry says so with its reason (F).
8. **Loud failure:** a client that ignores the response header and keeps a
   configured value produces `HEADERS_CHANGED` on the first replay — never a
   false `IDENTICAL`.
9. **Compatibility control:** a recording written before the change replays
   `IDENTICAL`.
10. **Negative control:** a recording that never saw a session header carries
    no `transforms` key and behaves exactly as it does today.
11. The metadata declares `id`, `v`, `activation`, `scope`, `transformed`,
    `untransformed` and `comparable_across_recordings`; it never carries a
    value or a map.
12. A diff of two transformed recordings that shared one real session reports
    the affected steps as **not comparable on request headers**, naming the
    transform — and still reports them as steps that differ, so a real change
    to another header is not hidden.
13. **Explicitly not an acceptance criterion:** that a strict client accepts
    the token. It is a declared unknown until a real client is recorded and
    replayed.

## 6. What would change, concretely

**Production, five files.**

| file | change |
|---|---|
| `orientim/transport.py` | in `_open_step` only — it is the one place that computes `hdr_fp` and stores `headers` for a *recorded* step, and it already has `rec` in scope. A small helper substitutes the token on both. `_hdr_fp` and `_resp_headers` keep their signatures |
| `orientim/store.py` | the recorder holds the map, the drawn tokens, the values seen on requests, the affected step indices and the residue; writes the `transforms` block at close |
| `orientim/session.py` | one explicit parameter on `record(...)`, off by default, carried to the recorder |
| `orientim/diff.py` | read `meta["transforms"]` from both sides; one new branch in `_why` for the comparison limit. `_pair_state` and alignment untouched, so an affected step still differs |
| docs | `docs/recordings.md` — what is stored and what is transformed, with the usage condition; `docs/limits.md` — the boundary, the residue, the comparability limit |

**Tests**: a focused set for the thirteen criteria, registered in
`tests/test_suites.py`; `tests/labserver.py` gains a session-issuing endpoint
with switches for echo and for a session in the body.

**Deliberately not changed**, and each for a reason:

- the lookup key and the matcher — the session was never in the key;
- `chain.DIGEST_FIELDS` and the chain — `hdr_fp` stays a field; only what it
  is computed over changes, and only in a recording that asked for it;
- the replay-side `_hdr_fp` call sites (transport.py:692, 817, 845) — the map
  is empty at replay, so replay is untouched by construction rather than by a
  branch;
- evaluate, observation, ci, baselines — none of them reads a header;
- every existing recording.

**One risk worth naming before the first line is written**: the new metadata
key travels into reports, and the suite's privacy canaries match on
substrings — `extractor` once tripped the canary for `extra`. The name has to
be checked against them rather than discovered by a red build.
