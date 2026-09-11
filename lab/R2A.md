# R2-A: what evidence is actually there, and one case that breaks the framing

`python lab/r2a.py`

Before designing an eligibility monitor, find out what it would have to
mediate with. Nothing in Orientim changed to produce this: no matcher, no
serving, no recording format, no new stored field.

## The measurement

Every relation from the R2-A list, put on the wire where a real application
would put it, recorded, then replayed with that one thing changed.

| relation | carried as | verdict on replay | detected | strength |
|---|---|---|---|---|
| exact credential recurrence | `Authorization` | IDENTICAL | **no** | ABSENT |
| verified/bound subject | a JWT in `Authorization` | IDENTICAL | **no** | ABSENT |
| tenant | `X-Tenant` | HEADERS_CHANGED | yes | OBSERVED |
| tenant | the request body | UNCAPTURED_SOURCE | yes | OBSERVED |
| actor | `X-Actor` | HEADERS_CHANGED | yes | OBSERVED |
| represented subject | `X-On-Behalf-Of` | HEADERS_CHANGED | yes | OBSERVED |
| session | `Cookie` | IDENTICAL | **no** | ABSENT |
| session | `X-Session-Id` | HEADERS_CHANGED | yes | OBSERVED |
| application context | `record(tags=...)` | IDENTICAL | **no** | ABSENT |
| endpoint policy | configuration | — | no | ABSENT |
| resource/world version | an `ETag` on the response | — | no | OBSERVED, inert |
| a replay-side context channel | `replay(path, fn, strict, input, …)` | — | no | ABSENT |

Strength is what the measurement licenses: **OBSERVED** = derived from bytes
that actually went out or came back; **ASSERTED** = stated alongside the run,
not tied to the bytes; **ABSENT** = no evidence at all.

### Finding 1 — Orientim is not blind to caller context. It is blind to one class of it.

Tenant, actor, represented subject and session are all **already detected
today**, as `HEADERS_CHANGED`, with no change to anything. The header
fingerprint covers every request header, it is in `chain.DIGEST_FIELDS`, and a
replay that sends a different one diverges. Context carried in the body
diverges even harder, because the body is part of the lookup key.

What is invisible is exactly `transport.HEADER_DENY` — `authorization`,
`cookie`, `x-api-key` — excluded on purpose, because a fingerprint over a
bearer token turns every credential rotation into a false regression and
storing one is worse than the gap it leaves.

That narrows P0-3 from "Orientim cannot tell who is calling" to **"Orientim
deliberately does not look at the credential, and some applications carry the
only distinguishing context there."** A different framing, a much smaller
problem, and a measured one.

### Finding 2 — the falsification case lands

Same identity, same credential, same method, URL and body. Only the world
moved:

```
verdict                IDENTICAL
replay served          balance 100      (what was true when it was recorded)
the world now          balance 25
recorded validator     ETag "w1"
live validator         ETag "w2"
detected by anything   False
```

The agent under replay is handed a balance that is wrong by 75, and every
identity relation in the system is satisfied. **A perfect identity answer
would not have helped.** Response eligibility is not primarily an identity
problem; identity is one dependency among several, and this one was never
about identity at all.

### Finding 3 — there is no replay side to declare anything to

```python
session.replay(path, fn, strict, input, ...)
```

Nothing on that signature says who the replay is. `record(tags=...)` gives a
run-level channel at record time and there is no counterpart, so even an
ASSERTED relation has only one of its two sides. **Any eligibility monitor
needs this channel before it can mediate anything at all** — and that is API
surface, not architecture.

### Finding 4 — the validator is recorded, and inert

`ETag: "w1"` is in the recording: Orientim stores response headers, so for any
endpoint that sends a validator, world-version evidence is already captured.
At replay there is nothing to compare it against, because comparing it means
asking the server, and replay makes no network calls — the property that makes
replay worth having.

So the honest status of the world dimension is: **observed at record time,
undischargeable offline**. The useful product surface for it is "this fixture
may be stale, re-record to find out", not a gate that pretends to know.

## R2-B: the three architectures, against that evidence

### A. Independent eligibility monitor

Candidate lookup stays as it is; serving crosses a separate mediated decision
over typed obligations.

- **Needs:** the replay-side context channel (Finding 3), plus evidence to
  discharge each obligation.
- **Can discharge today:** nothing. With no replay-side context, every
  obligation returns UNKNOWN on every recording that exists.
- **With the channel:** only ASSERTED relations — and the previous lab showed
  an unbound assertion is wrong exactly when the harness is wrong about
  itself, and wrong by construction when two principals run in parallel
  ([ELIGIBILITY.md](ELIGIBILITY.md)).
- **Verdict:** architecturally right, and it is not the bottleneck. Built
  first, it is a gate with nothing to gate on.

### B. Hermetic replay dependency set / reuse domain

A fixture is a memoized result over a declared set of response-affecting
inputs; reuse is sound when that set is equivalent.

The measurement changes how this reads. **Orientim already implements a coarse
version of it.** The dependency set is `chain.DIGEST_FIELDS` plus the lookup
key — method, URL, body, and every request header outside `HEADER_DENY` — and
`HEADERS_CHANGED` *is* a dependency-mismatch verdict. This is not a new
architecture; it is the one that is running.

Two gaps, both specific:

1. Credentials are excluded from the set by policy, not by oversight.
2. The set is implicit and global. An endpoint cannot say "I also depend on
   the world", so a dependency that is not on the wire cannot be declared,
   and therefore cannot be reported as undischarged.

- **Soundness:** only ever relative to the declared contract. Bazel can
  *enforce* hermeticity by sandboxing; Orientim cannot make someone's server
  hermetic, so a declaration here is an unverifiable assertion by the user and
  must be labelled as one. Orientim can never prove an arbitrary remote server
  has no hidden dependencies.
- **Verdict:** the right frame, already half-built, and the half that is
  missing is declaration rather than mechanism.

### C. Hybrid — recommended

In this order:

1. **Keep the implicit dependency set as the default.** It works, it is
   tested, and it already covers tenant, actor, subject and session whenever
   they travel outside the deny list.
2. **Add the replay-side context channel.** Without it nothing else is
   possible. Smallest useful shape: replay accepts the same typed context
   record() accepts, so both sides of a relation exist.
3. **Let an endpoint declare dependencies that are not on the wire** — world
   version, policy epoch, session identity carried in a cookie. A declared
   dependency with no value on one side is an **undischarged obligation**,
   reported as UNKNOWN and visible, never silently PASS.
4. **Never claim ELIGIBLE.** Claim INELIGIBLE on an observed mismatch, UNKNOWN
   on an undischargeable obligation. The asymmetry is the same one the
   evidence model already uses for witnesses, and the earlier lab showed the
   positive claim is not provable from a recording under any evidence source.

The credential dimension stays where it is until a separate decision is taken:
binding it needs a capture-time hook that does not exist, and the rule against
storing credentials is not up for renegotiation to make a lab result nicer.

## What this does not decide

No production change was made and none is proposed here for implementation.
The capture-time hook, the context channel's exact shape, the declaration
syntax and whether the credential dimension is worth binding at all are design
decisions, and this is the evidence for taking them rather than the taking.

Nothing this wrote contains a credential:

```
credentials in it: []
```
