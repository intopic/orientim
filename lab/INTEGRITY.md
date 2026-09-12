# Recording integrity: the contract first, then four designs

`python lab/integrity.py`

Nothing in Orientim changed to produce this. No format, no chain, no field.

R2 made this urgent rather than academic. A recorded context now decides
whether a historical fixture is released to a replay, so the question stopped
being *would we notice a corrupted file* and became *what is the trust
boundary of a decision that reads one*.

> **Read to the end.** Two later experiments follow, and they change this one.
> `lab/artifact_integrity.py` **withdraws design D**, because
> `sha256(stored body)` is not `body_sha` and never was, and **dissolves the
> subset question** this file closes on, because an artifact digest needs no
> subset. `lab/integrity_contract.py` then attacks the contract with eight
> counterexamples. The measurements here stand; the recommendation is
> superseded by **Contract v1 — final** at the bottom.

## What is true today, measured

Six mutations, each one something a person could do to a recording on disk:

```
mutation                               loads   root moved  replay verdict     caught by a check
1 a response body, alone               True    False       IDENTICAL          NO
2 a response body and its body_sha     True    True        IDENTICAL          NO
3 the recorded replay context          True    False       IDENTICAL          NO
4 a step removed                       True    True        NEW_CALL           NO
5 two steps reordered                  True    True        UNCAPTURED_SOURCE  NO
6 everything recomputed to agree       True    True        FEWER_STEPS        NO
```

**Zero of six are caught by an integrity check, because there is no integrity
check.** No code path validates a recording against anything: `store.load`,
`storage` and `chain` contain no verification at all.

Rows 4, 5 and 6 diverged, and that is *not* detection. The edit also made the
recording disagree with the agent being replayed, so the replay noticed the
disagreement the way it notices any disagreement. Tamper that happens to match
plausible agent behaviour produces nothing.

**Row 2 settles the design question.** The chain root moved and the replay was
still `IDENTICAL` — because nothing compares that root to a trusted value.
There is no trusted value: the root does not appear in the file
(`root appears in the file: False`); `store.signature` computes one on demand
for deduplication and discards it.

### And one thing that is already there, unread

`recorded_root` — the chain root of the recording — is computed on every
replay, put on every row, written into every baseline and every report, and
**compared to nothing**. The anchor this needs already exists in the artefacts
that reference a recording. Nobody wrote the comparison.

## The contract, before any mechanism

**What mutation must Orientim detect?** In the order that matters:

1. a change to the recorded context, because it changes a gate decision (R2)
2. a change to a response body, because that is where the evidence lives
3. a change to the step sequence, because that changes the replay verdict
4. accidental corruption — a partial write, a bad merge, an S3 lifecycle
   rewrite, a hand-edit somebody meant well by

**What is the trusted root?** Not anything inside the recording. A root stored
in the file it protects is not a root: whoever edited the file runs the same
code we do and recomputes it. Candidates that are actually outside it: the
case file, the baseline, a manifest — all of which live in git — or a key that
is not in the repository at all.

**Who holds it?** The customer, in the same place they keep the code the
recording is evidence about. That is the only answer compatible with
local-first: a control plane that held the roots would be holding the thing
that decides whether customer evidence is trusted.

**Which of the three is this?** Say it plainly, because the three are sold as
the same word and are not:

| | |
|---|---|
| **accidental-corruption detection** | achievable today, cheaply, with no anchor at all |
| **tamper evidence** | achievable against an editor who does not control the anchor |
| **authenticity** | *not* achievable here, and not worth pretending |

Authenticity is out of reach for a reason that is not about cryptography:
anyone who can edit a recording in the customer's repository can also edit the
agent, the case, the baseline and the suite. A malicious insider does not
forge a recording; they delete the case. **Orientim should target "this
recording is not the one this case was frozen against", and must not be
described as anything stronger.**

## Four designs

### A — Anchor in the artefacts that already reference the recording

The case file and the baseline already name a recording. Have them also record
its digest, and compare at run time. Mismatch is a named verdict —
`RECORDING_CHANGED` — never a behaviour change.

The digest is the existing chain root **plus** a digest over the meta fields
that decide something (today: the context). `chain.DIGEST_FIELDS` is not
touched; this is a second, separately computed value for a different purpose.

| | |
|---|---|
| local-first | perfect — the anchor is a git-tracked file the customer already reviews |
| secrets | none |
| old recordings | no digest → UNKNOWN, never assumed. Same rule as the analysis stamp |
| reproducibility | deterministic; two machines compute the same value |
| CI usability | excellent — a changed recording shows up in the PR that changed it |
| migration | zero. New cases carry it, old ones do not |
| catches | rows 2, 3, 4, 5, 6 |
| misses | **row 1** — a body edited without its `body_sha`, because the chain digests `body_sha`, not `body` |

### B — Keyed MAC over the file, with a local secret

| | |
|---|---|
| local-first | poor — a colleague who clones the repo cannot verify anything |
| secrets | a key that must not be in the repo, must reach CI, must rotate |
| old recordings | unverifiable forever |
| reproducibility | broken across machines by construction |
| CI usability | a new secret in every pipeline |
| migration | high |
| catches | everything, for whoever holds the key |

Rejected: it buys tamper evidence against an editor without the key, and the
editor we are actually worried about is the one with the whole repository
checked out.

### C — Detached signature

Real authenticity, and it needs asymmetric crypto. Orientim's runtime
dependency is `httpx` and nothing else; this adds `cryptography`, plus key
distribution and rotation, to defend against a threat model the product does
not have. Rejected on cost, and on the trust argument above.

### D — Self-consistency check, no anchor at all

Verify at load that `sha256(body) == body_sha`, and that the step count
matches. Free, no anchor, no secret, works on every recording ever written.

| | |
|---|---|
| catches | **row 1**, and every accidental corruption |
| misses | rows 2, 3, 6 — anyone who edits deliberately recomputes |

Not tamper evidence, and must never be described as it. It is the cheapest
half of the problem and it is the half that happens by accident.

### The null option — declare and stop

Keep the limitation documented, leave R2 trust-open. Costs nothing, closes
nothing.

## Recommendation

**A + D together, and neither called authenticity.**

D catches what happens by accident and costs nothing. A catches what an editor
does when the anchor is somewhere they did not think to look — and the anchor
already exists, unread, in files the customer keeps under review. Together
they cover all six measured rows. Neither defends against someone who controls
the repository, and the documentation must say so in the same paragraph that
claims the protection.

The one design decision that is genuinely open, and the reason this stops
here:

> **Does the recording digest cover metadata?**

It must, or R2's trust boundary stays open — the context is in `meta`. But
`meta` also holds `started_at`, `ended_at`, timing and the runtime block,
which differ on every machine and would make the digest useless. So the digest
has to cover a *named subset* of meta, and choosing that subset is a contract:
everything in it becomes frozen for the life of a case, and everything out of
it stays mutable and unprotected.

That is an architectural decision with more than one defensible answer, so it
is yours. The candidates:

1. `context` only — the minimum that closes R2, nothing else protected
2. `context` + `agent` + `input` — everything a case's meaning depends on
3. an explicit `integrity` block in meta, listing what it covers, so the
   subset is data rather than a hardcoded tuple

## What this experiment did not do

No production change. No format change. No chain change. The recommendation is
not implemented, and the subset question above is not answered.

---

# What the hashes cover, measured field by field

`python lab/artifact_integrity.py` — a second experiment, after the six
mutations above. Same rule: nothing in Orientim changes, and no existing hash
is given a new meaning.

`chain.step_digest` covers exactly seven things: `method`, `url`, `hdr_fp`,
`status`, `body_sha`, the active lookup key, and `error`. That is the whole
list. One edit each, on a recording of a model call that asked for a tool:

```
field                                  root moved  replay says        it decides
step.body (what a replay serves)       NO          IDENTICAL          served bytes
step.body + body_sha together          yes         IDENTICAL          served bytes
step.headers (served to the agent)     NO          IDENTICAL          served headers
step.req (the recorded request)        NO          IDENTICAL          what a reader sees
step.status                            yes         IDENTICAL          replay + evaluation
step.error                             yes         REPLAY_RAISED      replay + evaluation
step.role                              NO          IDENTICAL          evaluation
step.key_strict                        yes         UNCAPTURED_SOURCE  matching
step.hdr_fp                            yes         HEADERS_CHANGED    divergence
step.complete                          NO          STREAM_INCOMPLETE  nothing today
meta.context (the R2 gate)             NO          IDENTICAL          fixture release
meta.transforms                        NO          IDENTICAL          how a diff reads it
meta.outcome                           NO          OUTPUT_CHANGED     evaluation
```

Three of those rows are not "a byte moved". They change an answer:

```
step.body       did_not_call("send_email")   fail on the recording
                                             PASS on the edited copy
                root moved: no. replay: IDENTICAL.

step.headers    the agent was told           "somebody-else"
                root moved: no. replay: IDENTICAL.

meta.context    the R2 gate                  IDENTICAL  ->  FIXTURE_REFUSED
                root moved: no.
```

The shape of it: **the chain covers a fingerprint of what was served at record
time, and not what will be served at replay time.** `body_sha` is inside the
digest and `body` is not, so a recording whose body says one thing and whose
`body_sha` fingerprints another is, to every check that exists, a healthy
recording.

# `sha256(stored body)` is not `body_sha`

Four ordinary bodies from one run:

```
body     b64    body_sha == sha256(stored)[:32]
tool     no     False        a JSON body, re-serialised on the way in
secret   no     False        redaction replaced a field
text     no     True         not JSON, and nothing to redact
binary   yes    False        stored as base64 text
```

`redact_body` parses any JSON body and re-serialises it **unconditionally** —
not only when something is redacted — so the stored text is a re-encoding of
the wire bytes for every JSON response, which is nearly all of them. Binary
bodies are stored as base64. Only a non-JSON text body that redaction left
alone matches.

**This withdraws design D above.** A self-consistency check of the form
`sha256(body) == body_sha` fails on three of these four, and shipping it would
have raised a corruption alarm on almost every recording ever written.

The two are different objects, and both are worth having under different names:

| | what it is | can it be recomputed from the file |
|---|---|---|
| `body_sha` | a fingerprint of the bytes that crossed the wire | **no** — the wire bytes are not in the file |
| an artifact digest | integrity of what the file will serve and decide with | yes, which is what makes it checkable |

# Corruption is not tampering, and neither is authenticity

```
a flipped byte inside a step line    JSONDecodeError at load
the file cut in half                 JSONDecodeError at load
an editor who rewrites the content
  and every hash derived from it     loads, internally consistent,
                                     replays IDENTICAL, and the evaluator
                                     reads what the editor wrote
                                     (no root is stored in the file, and
                                      nothing compares one)
```

Accidental corruption is already fatal — as an exception at load, not as a
named verdict. Deliberate editing is not detected at all, and it does not even
require recomputing anything: `body` sits outside the digest, so the sharpest
edit is also the cheapest.

Three levels, and they need three words:

| | what it detects | what it needs |
|---|---|---|
| **corruption** | bytes changed by accident | the file alone |
| **substitution** | this is not the recording the case was frozen against | an anchor outside the file |
| **authenticity** | who wrote it | a key, and a threat model this product does not have |

Authenticity stays out of reach for the reason stated at the top: whoever can
edit a recording in the customer's repository can edit the case and the
baseline too.

# Contract v1 — first draft, superseded

Kept for the reasoning. The four points below were then corrected by
review and the corrections are measured in **Contract v1 — final**.

**1. One digest, over the artifact, not over a chosen subset.**

The earlier open question was which metadata fields a digest should cover. The
matrix above dissolves it: the fields that decide something are scattered
across steps *and* meta, and a named subset would have to be extended every
time a field is added — silently wrong in between. An artifact digest needs no
subset, because it is never compared across recordings: it is compared to a
value taken from that same file. So it covers everything except itself.

```
artifact_digest = sha256( every step line, as written
                          + chain.digest(meta without the integrity block) )
```

`chain.digest` already canonicalises a mapping, and reusing it keeps one
serialisation rule in the codebase. The volatile fields — `t0`, `ms`,
`started_at`, `runtime` — stay *in*, deliberately: they are part of the
artifact, and nothing compares this digest between two recordings.

**2. Two places, two different guarantees.**

| where | detects | notes |
|---|---|---|
| inside the file | corruption, including the kind that keeps the JSON valid | an editor recomputes it; this is not tamper evidence and must not be called that |
| in the case or baseline that names the recording | substitution: the file is not the one frozen against | design A above, unchanged, and the anchor already lives in git where it is reviewed |

**3. Behaviour, including for everything that already exists.**

| state | when | what it means |
|---|---|---|
| `INTACT` | a digest is present and matches | the file is the one that was written. Not authentic, not trusted |
| `UNVERIFIED` | no digest — every recording written before this | never verified and never failed. A protected profile may refuse to use it; the legacy profile may not |
| `CHANGED` | a digest is present and does not match | a harness outcome, like `FIXTURE_REFUSED`: nothing about the agent was measured. Never an agent failure, never `newly_changed` |
| `CORRUPT` | the file cannot be read | a suite error, and a named one rather than a `JSONDecodeError` |

A missing field inside an otherwise present digest is not a fourth state: a
recording that lost a field has a digest that does not match, and `CHANGED` is
the honest answer.

**4. What must not change, and is not proposed to.**

`body_sha` keeps its meaning — a fingerprint of the wire bytes, not of the
file. The chain keeps its field list and its meaning. Replay semantics are
untouched. The artifact digest never participates in matching, never decides a
verdict about the agent, and never turns a difference into a match.

**5. What is still yours to decide.**

- Where the anchor lives: the case file, the baseline, or both.
- Whether the protected profile *requires* `INTACT` before a fixture is
  released, or only reports it.
- Whether v1 also stores a per-step digest of stored content — which would say
  *which* step changed rather than only *that* the file changed — or whether
  that waits for a real need.

---

# Contract v1 — final, before implementation

`python lab/integrity_contract.py`

Decided: the anchor lives in the **case file**, the fixture digest in the
**baseline**, protected **refuses before use**, and there is **no per-step
digest in v1**. The digest and the verifier are written as lab code first, so
the decision table below could be attacked before any of it reaches
production. Nothing in Orientim changes yet.

## 1. Two answers, and never one standing in for the other

```
self_consistency  INTACT | CHANGED | ABSENT | UNSUPPORTED | AMBIGUOUS | CORRUPT
anchor_match      MATCH  | MISMATCH | NO_ANCHOR | NOT_CHECKED
```

Self-consistency answers *is this file the file it says it is*. Only the anchor
answers *is this the file the case was frozen against*. The distinction is not
academic, and the first counterexample is the whole reason for it:

```
1  body edited, and the internal digest recomputed
     self INTACT     anchor MISMATCH     protected refuse
```

An editor who knows how the digest works produces a file that is **perfectly
self-consistent**. Self-consistency alone would have released it, and does not
even require the editor to be clever: today it needs nothing at all, because
there is no digest to recompute.

## 2. The anchor is checked even when the file carries no digest

The anchor comparison recomputes the digest **from the bytes**, rather than
reading a value out of the descriptor. Otherwise the attack is one line long:

```
2  the integrity block deleted
     self ABSENT     anchor MISMATCH     protected refuse
```

Two consequences, both deliberate:

- `ABSENT` + `MATCH` is a **release**. The anchor subsumes what
  self-consistency adds — a corrupted file does not match the anchor either —
  so a recording with no descriptor but a matching anchor is verified.
- `INTACT` + `NO_ANCHOR` is **not** a release under protected.
  Self-consistency is not tamper evidence and must never be spent as if it
  were.

## 3. Serialisation, what the descriptor covers, and refusing ambiguity

```
digest = sha256( chain.digest(meta, with the descriptor minus its digest value)
                 + "\n" + every step line, as written )
```

Two halves for one reason. Step lines are covered **byte for byte**, because
that is what a reader and a replay consume. The meta cannot be, because
writing the digest into it changes the bytes the digest would be over — so it
goes through `chain.digest`, which already canonicalises a mapping and keeps
one serialisation rule in the codebase.

The descriptor carries `v`, `algo`, `covers`, `steps`, `digest`, and
**everything in it is inside the digest except the digest value itself**:

```
6  the descriptor's own claims edited (covers: "meta+steps" -> "steps")
     self CHANGED    anchor MISMATCH     protected refuse
```

`steps` is a declared count, and it is what catches the cut that keeps the
JSON valid:

```
3  cut at a line boundary — every remaining line parses
     self CHANGED ("declared 10 step lines, found 9")   anchor MISMATCH
     today: loads, 3 steps, replay IDENTICAL
```

**Ambiguity is refused, not resolved.** Two meta lines, a meta line that is not
first, a descriptor that is not an object: a file with two readings has two
answers to every question, and choosing one is choosing.

```
4  two meta lines
     self AMBIGUOUS   anchor NOT_CHECKED   both profiles: suite_error
7  a byte flipped mid-line
     self CORRUPT     anchor NOT_CHECKED   both profiles: suite_error
```

An unknown `v` or `algo` is `UNSUPPORTED`, and the anchor is then
`NOT_CHECKED` — deliberately, because **the digest definition belongs to the
descriptor version**, and giving a v2 file a v1 verdict would be inventing one:

```
5  unsupported descriptor version
     self UNSUPPORTED  anchor NOT_CHECKED  protected refuse / legacy release
```

No field is excluded for being volatile. `t0`, `ms`, `started_at` and
`runtime` are part of the artifact, and this digest is never compared between
two recordings — only against a value taken from the same bytes.

## 4. Verification and use are the same snapshot

The verifier takes **bytes and returns the parsed content**, never a path.
A caller cannot verify a file and then open it again, because the only thing
handed back is what was verified:

```
verified the first read        INTACT / MATCH      wire_transfer present: False
the same path a moment later   INTACT / MISMATCH   wire_transfer present: True
```

Both reads are internally consistent. A path that verified is not a path that
can be read again; what verified is a byte string, and that byte string is
what must be parsed, replayed from, and evaluated.

## The decision table, as measured

```
case                                 self         anchor       protected    legacy
0 untouched (control)                INTACT       MATCH        release      release
1 body edited, digest recomputed     INTACT       MISMATCH     refuse       refuse
2 integrity block deleted            ABSENT       MISMATCH     refuse       refuse
3 cut at a line boundary             CHANGED      MISMATCH     refuse       refuse
4 two meta lines                     AMBIGUOUS    NOT_CHECKED  suite_error  suite_error
5 unsupported descriptor version     UNSUPPORTED  NOT_CHECKED  refuse       release
6 descriptor's own claims edited      CHANGED      MISMATCH     refuse       refuse
7 a byte flipped mid-line            CORRUPT      NOT_CHECKED  suite_error  suite_error
```

Every one of those eight files, today, **loads and replays without a word**
about integrity — six of them report `IDENTICAL`. Only the flipped byte fails,
and it fails as a `JSONDecodeError`.

The rule behind the table: `CHANGED`, `MISMATCH`, `AMBIGUOUS` and `CORRUPT`
stop both profiles, because none of them is a fact about the agent.
`NO_ANCHOR`, `ABSENT` without an anchor, and `UNSUPPORTED` stop protected only,
and are reported under legacy.

## Where the two values live

| | holds | answers |
|---|---|---|
| the case file | `recording_digest` | is this the recording this case names |
| the baseline | `fixture_digest`, per case row | is this the fixture the baseline row was measured from |

The same value for the same artifact, in two places because they answer two
questions. The case anchor governs **release**; the baseline digest governs
whether a baseline row is **comparable**. If the two disagree, that is its own
refusal and says so — it means the case was re-anchored after the baseline was
frozen, and neither value can be trusted to stand for the other.

## When protected refuses

**Before use.** The check runs where the R2 gate runs — before a fixture is
released to the replay — not after the run, and never as a verdict about the
agent. A refusal is a harness outcome of the `FIXTURE_REFUSED` class: it
appears in no movement bucket, is never `newly_changed`, and blocks under its
own sentence.

## Not in v1

Per-step digests, so the answer is *the file changed* and not *which step
changed*. Keys, MACs and signatures. Authenticity. Scrubbing existing files.
And no change to `body_sha`, the chain, the lookup key, matching, or replay
semantics.

## What happens to everything that already exists

A recording with no descriptor and no anchor is `UNVERIFIED`: legacy releases
it and says so, protected refuses it. It stays readable, replayable and
comparable exactly as today — nothing about an old recording changes until a
case anchors it, and anchoring is a deliberate act.
