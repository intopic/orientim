# Response eligibility: an experiment, and what broke it

`python lab/eligibility.py`

> **Read [R2A.md](R2A.md) after this one.** It measures what evidence the
> recorder actually has, rather than what a decision model would do with
> evidence, and it falsifies one framing used here: this page treats
> eligibility as an identity question with an authority-state exception.
> R2-A shows the exception is not exceptional — a world that moves under an
> identical caller defeats every identity relation — so identity is one
> replay dependency among several rather than the axis.

Nothing in Orientim changed to produce this. No matcher, no serving, no
recording format, no new field. This is a measurement of whether an
abstraction from the research report is **correct enough** and **practical
enough** to be worth building here, taken by trying to break it.

## The claim under test

> Finding a recorded response is a lookup. Deciding that it may be served to
> the caller in front of you is a separate judgement.

Orientim today has only the first half. The lookup key is method + URL + body,
and every header that carries principal identity — `authorization`, `cookie`,
`x-api-key` — is excluded from the header fingerprint as well, so they change
nothing. That is P0-3, pinned in [docs/limits.md](../docs/limits.md).

Measured, not assumed. Record as Alice, replay the identical request as Bob:

```
verdict                          IDENTICAL
bob_was_served_alices_response   True
credentials_in_the_file          []          <- nothing to compare, by design
identity_fields_in_a_step        []
```

Both halves of that are deliberate. The credential is not in the file because
storing it would be worse than the gap it leaves, and the gap it leaves is
that a replay by a different principal is silently served the first
principal's response.

**So the distinction is real and Orientim does not make it.** That much of the
report is confirmed. The rest of this is about what it would take, and what it
would still be wrong about.

## The vocabulary, fixed before the experiment ran

```
ELIGIBLE        this response may be served to this caller
INELIGIBLE      it may not
UNKNOWN         the evidence does not decide it
ANALYSIS_ERROR  the evidence is malformed; not a verdict about the caller
```

And the rules: no plaintext `Authorization`, no plaintext `Cookie`, no
plaintext tokens or secrets in anything the experiment writes; no replayed
`/me` counted as proof of identity, because a recorded response is not
evidence about the caller replaying it; no live calls at replay time; no
production change of any kind.

## Two models, four evidence sources, nine adversarial cases

The models:

| | |
|---|---|
| **R** | the research model: eligibility is decided by the principal |
| **O** | the asymmetric model: only INELIGIBLE is provable; ELIGIBLE never is |

The evidence sources, from what exists to what would have to be built:

| source | what it is | what it costs |
|---|---|---|
| `none` | what a recording holds today | nothing, and it answers nothing |
| `declared` | a principal the harness asserts beside the run | one argument per run |
| `witness` | a keyed digest of the credential actually sent | a capture-time hook over request headers, and a key kept outside the recording |
| `bound` | the witness, plus the principal resolved from that credential **while the run is live** | that hook, plus one resolution per credential at record time |

Ground truth is known by construction, so every verdict can be graded. `+`
sound and useful, `!` unsound, blank sound but silent.

```
  scenario                                  none/R   none/O   decl/R   decl/O   witn/R   witn/O   boun/R   boun/O
  1 same principal, rotated token           UNKNOWN  UNKNOWN  ELIGIBL+ UNKNOWN  UNKNOWN  UNKNOWN  ELIGIBL+ UNKNOWN
  2 a different principal, same request     UNKNOWN  UNKNOWN  INELIGI+ UNKNOWN  UNKNOWN  UNKNOWN  INELIGI+ INELIGI+
  3 same principal, wider scope             UNKNOWN  UNKNOWN  INELIGI+ UNKNOWN  UNKNOWN  UNKNOWN  INELIGI+ INELIGI+
  4 same principal, another tenant          UNKNOWN  UNKNOWN  INELIGI+ UNKNOWN  UNKNOWN  UNKNOWN  INELIGI+ INELIGI+
  5 same token, authority revoked           UNKNOWN  UNKNOWN  ELIGIBL! UNKNOWN  ELIGIBL! UNKNOWN  ELIGIBL! UNKNOWN
  6 an opaque token nothing resolves        UNKNOWN+ UNKNOWN+ UNKNOWN+ UNKNOWN+ UNKNOWN+ UNKNOWN+ UNKNOWN+ UNKNOWN+
  7 the declaration disagrees with the cred UNKNOWN  UNKNOWN  ELIGIBL! UNKNOWN  UNKNOWN  UNKNOWN  INELIGI+ INELIGI+
  8 two principals in one run, in parallel  UNKNOWN  UNKNOWN  ELIGIBL! UNKNOWN  UNKNOWN  UNKNOWN  INELIGI+ INELIGI+
  9 a legacy recording, no identity evidence UNKNOWN UNKNOWN  UNKNOWN  UNKNOWN  UNKNOWN  UNKNOWN  UNKNOWN  UNKNOWN
```

| source | model | right | silent | **unsound** |
|---|---|---:|---:|---:|
| none | R | 1 | 8 | 0 |
| none | O | 1 | 8 | 0 |
| declared | R | 5 | 1 | **3** |
| declared | O | 1 | 8 | 0 |
| witness | R | 1 | 7 | **1** |
| witness | O | 1 | 8 | 0 |
| bound | R | 7 | 1 | **1** |
| **bound** | **O** | **6** | **3** | **0** |

## What broke

### 1. ELIGIBLE is not provable from a recording. Ever.

Scenario 5 is the counterexample, and it defeats **every** evidence source
including the strongest:

```
record:  GET /orders/4471   Authorization: Bearer tok_alice_1111
replay:  GET /orders/4471   Authorization: Bearer tok_alice_1111
                            (alice's access was revoked in between)
```

The bytes on the wire are identical. The principal is identical. The witness
matches, the resolver agrees, and serving the recorded response is wrong.

Eligibility is a property of the **authorization state on the server at the
moment the response was produced**, and that state is not on the wire. A
recorder can observe that a credential changed. It can never observe that the
authority behind an unchanged credential did not. Model R's positive verdict
is therefore unsound in principle, not because the evidence is thin.

This is the same shape as everything else in this codebase: a witness is
admissible, an absence needs the enumeration to have closed, and here the
enumeration is over facts that live on a server nobody recorded.

### 2. A declared principal is not evidence

`transport.py` currently says, in the comment that documents P0-3, that "the
fix is a declared principal". The experiment says that is half a fix:

- **scenario 7** — the harness declares `alice` and bob's credential goes out.
  `declared/R` says ELIGIBLE. It is a lie told by the same code that was
  already wrong.
- **scenario 8** — two principals running in parallel inside one run. A
  run-level declaration cannot be right for both steps, and it is ELIGIBLE for
  the step it is wrong about.

Three of `declared/R`'s nine answers are unsound, and all three are false
ELIGIBLEs — the direction that serves one caller's data to another. A
declaration must be **bound** to the credential that actually went out, or it
is an assertion, not evidence.

### 3. Resolution cannot happen at replay time

A resolver maps a credential to a principal, tenant and scope. Doing that at
replay time means either a live call — which replay forbids, and which is the
whole reason replay is deterministic — or trusting a recorded `/me` response,
which is a recorded response and proves nothing about who is replaying it.

So the resolution has to happen **while the run is live**, and what gets
stored is the resolved identity, never the credential. That is what `bound`
models, and it is the only configuration in the matrix that is both useful and
never wrong.

### 4. The practical cost is a capture-time hook that does not exist

`on_capture(path, meta)` fires after the file is written and never sees a
request header. Producing a witness needs a hook at the point the request goes
out, plus key management the customer has to get right: the key must not be in
the recording, or the digest is a guessable-token oracle rather than a
witness.

## Recommendation

**Do not implement response eligibility. Implement credential continuity, and
call it that.**

| | |
|---|---|
| what it can prove | the credential, principal, tenant or scope behind this replay is not the one behind the recording |
| what it can never prove | that serving the recorded response is *correct* |
| where it belongs | a divergence signal, beside the hash chain's — not a gate on serving |
| the verdict it emits | INELIGIBLE, or UNKNOWN. Never ELIGIBLE |

That closes P0-3 in the direction that matters — a replay driven by a
different principal stops being silently IDENTICAL — without claiming the half
of the abstraction that cannot be true. It also needs no matcher change: the
lookup stays method + URL + body, and the mismatch is reported rather than
used to withhold a response, which keeps replay deterministic and keeps the
"an accurate UNKNOWN beats an unproven PASS" rule intact.

`ELIGIBLE` should not enter Orientim's vocabulary. The three-way vocabulary
that fits the evidence is `INELIGIBLE / UNKNOWN / ANALYSIS_ERROR`, and the
first of those is the only new thing it would be able to say.

## What this experiment did not do

No production code changed. No recording format changed. No matcher or serving
change. Nothing here stores a credential, in plaintext or as a digest — the
script checks its own output for both and reports the result:

```
credentials in it: []   witness digests in it: 0
```

The resolver, the capture-time hook and the key management are modelled, not
built. Whether to build them is the design decision this was run to inform,
and it is not taken here.
