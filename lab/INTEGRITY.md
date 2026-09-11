# Recording integrity: the contract first, then four designs

`python lab/integrity.py`

Nothing in Orientim changed to produce this. No format, no chain, no field.

R2 made this urgent rather than academic. A recorded context now decides
whether a historical fixture is released to a replay, so the question stopped
being *would we notice a corrupted file* and became *what is the trust
boundary of a decision that reads one*.

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
