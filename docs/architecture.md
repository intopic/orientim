# How it works, and why it works that way

Roughly 2,900 lines of Python with one dependency. This page is the reasoning
behind the shape, including the parts that were wrong and got changed.

## The one decision everything else follows from

**Capture at the HTTP boundary.**

Mozilla's `rr` records at the instruction level and can replay a CPU. That is
the right answer for a C++ debugger and the wrong answer here, because an agent
is not a computation you need to reproduce — it is a conversation you need to
reproduce. Everything interesting an agent does crosses a socket: the model,
every HTTP tool, every MCP server. One interception point covers all of them.

The cost is stated rather than hidden: anything that does *not* cross that
boundary is invisible. A tool that reads a local file, a database query in the
same process, a cache inside your framework. Those are sources 15, 18 and 19 in
[nondeterminism.md](nondeterminism.md), and they are declared limits, not bugs.

## Why patch `httpx` globally

The OpenAI SDK builds its own client, inside itself. So does Anthropic's. So
does LangChain, and so does the tool one of your colleagues wrote last year. A
recorder that only sees a client it handed you sees none of them.

So `record()` replaces `HTTPTransport.handle_request` and `httpx.AsyncClient.__init__`
for the duration of the block, and wraps the transport of every client built
inside it. This is what makes the package usable without touching anyone's code,
and it is also the thing most likely to hurt somebody.

Three rules make it safe enough to ship:

1. **Reference-counted, never save-and-restore.** Two overlapping blocks used to
   corrupt each other: the second to enter captured the first one's wrapper as
   "the original", and whichever exited last restored a wrapper instead of the
   real `__init__`. httpx stayed patched for the life of the process and every
   client built afterwards was silently attached to a recording that had ended.
   Now there is a stack, and the true original goes back exactly once.
2. **Thread-scoped attribution.** A client built while two blocks are open
   belongs to the block on its own thread. Falling back to the innermost block
   keeps capture working for clients built in worker threads, which is the
   common case.
3. **Fail open, never fail closed.** If a future httpx changes its internals,
   the wrapper warns once and captures nothing. Breaking every `httpx.Client()`
   the user constructs would be a far worse outcome than an empty recording.
   The dependency is pinned `>=0.24,<1.0` for the same reason.

It is still a process-wide patch. Do not wrap a long-lived server process in
`record()`; wrap the request handler. See [limits.md](limits.md).

## Why responses stream through

A recorder must not change the behaviour it is recording. The first version read
each response to completion before returning it, which meant an agent streaming
tokens received all of them at once, at the end. Switching the recorder on
changed what the agent did.

Now chunks pass straight through, and the recorder notes each one's arrival
offset and size on the way past. Sizes rather than copies: a chunk can split a
UTF-8 character in half, so storing the pieces would force base64 and make every
streamed recording unreadable. Sizes cost nothing and rebuild the split exactly.

Replay hands back the same chunks in the same shape — instantly by default,
because replay being fast is half the point, and at the recorded pace with
`replay(..., realtime=True)` when the bug depends on how long a stream went
quiet.

## The lookup key and the step digest are different on purpose

This is the subtlest part of the design and it was wrong for a while.

**The lookup key** — `method | url | body` — decides which recorded step answers
a request. It deliberately ignores headers, because a rotated token or a new
`user-agent` must not stop a replay from finding its own recorded response.

**The step digest** decides whether the runs were the same. It covers the
method, URL, lookup key, response status, a hash of the response bytes, *and*
`hdr_fp` — a hash of the request headers that can change a response.

Separating them is what closed a real hole. Two calls to the same URL with the
same body and different headers — two tenants, two ranges, streaming versus not
— share a lookup key. If the code changes so those calls swap order, each one
matches the other's recorded step and gets back data belonging to the other. The
old chain reported `IDENTICAL`. Now the digests differ and the verdict is
`HEADERS_CHANGED`, which says exactly what happened: the agent asked a different
question and was handed the old answer.

The digest is also computed **from what the replay actually sent**, never copied
from the recording, and under whichever key — strict or loose — did the
matching. Copying the recorded digest made the hash chain a tautology: a matched
step carried the recorded hash, so the chain could only ever notice a difference
in the *number* of steps.

## Forced ordering

An agent that fires three calls in parallel and proceeds with whichever returns
first takes a different path every run. This is the hardest of the twenty
sources and the reason a naive HTTP cassette is not enough for agents.

Replay forces the recorded arrival order: a request blocks on a condition
variable until it is next in the recorded sequence, with a timeout so a
genuinely diverged run fails instead of hanging. Sync and async share one queue,
so an agent that mixes them replays in order. About forty lines; it moved three
sources from failing to passing.

Recording writes a step down when the response *headers* arrive, not when the
last byte does. That keeps the recorded order equal to the order responses
actually started in, even with four calls in flight, and it is what makes
streaming and forced ordering coexist.

## The hash chain

Each step's digest is linked into the previous one, so a run reduces to a root
hash. Equal roots mean equal runs; unequal roots have a first differing link,
and that link is the step where behaviour diverged. Divergence detection falls
out of the storage format instead of being a separate comparison pass.

The chain is necessary and not sufficient. On its own it can only compare what
it was given, so a set of guards sits around it — see
[verdicts.md](verdicts.md). Every one of them exists because a replay could
otherwise have said `IDENTICAL` about a run that was not.

## Why nothing is forwarded during a replay

An email sent during the recorded run must not be sent again. The guarantee is
structural rather than a list of dangerous paths: `ReplayTransport` has no
reference to a network transport at all. Every response comes out of the file,
and a request with no match gets a synthetic `599`. There is no code path from a
replay to a socket, so there is nothing to get the list wrong about.

`transport.SIDE_EFFECTING` only decides what the timeline *marks*, so you can
see which steps mattered. Marking every non-idempotent method was tried and is
worse: a chat completion is a `POST`, so the warning appeared on nearly every
step and stopped meaning anything.

## Counterfactuals

`replay(..., patch={3: {"body": ...}})` replaces what a step returned. That is
the difference between "does the code still do what it did" and "what would it
have done" — and it is the claim the README had been making for a while before
the code could keep it.

It needs one idea that is not obvious: **a counterfactual cannot be judged on
responses**, because the responses differ by construction. Comparing them would
only rediscover the change you made. So the comparison switches to a
request-only digest — method, URL, lookup key, header fingerprint — and the
verdict reports where the agent's *own requests* began to differ. That is the
blast radius of the change, which is the thing you wanted to know.

A patched replay is never `IDENTICAL`, because it is not a reproduction. It is
`COUNTERFACTUAL` (the path changed at step *n*) or `COUNTERFACTUAL_SAME` (the
code did not branch on what you changed).

## Triggers and the ring buffer

Recording every run of a long-lived agent writes gigabytes nobody opens. Steps
live in a ring buffer and the file is written only when something fires: an
exception, a `5xx`, an uncaptured source, or an explicit
`run.rec.trigger("...")`.

The failure mode is eviction — a run so long that its early steps are gone
before the trigger. That is counted, stored, and reported as `TRUNCATED` rather
than surfacing three days later as an inexplicable divergence.

## Retention

Recordings accumulate, and they hold prompts. Two decisions:

**Nothing is deleted automatically.** `prune` runs when you run it. Deleting
somebody's recordings as a side effect of writing one is a surprise never worth
the disk it saves, particularly when the deleted file might be the incident
somebody is mid-way through debugging. An empty policy removes nothing rather
than everything, because the other way round is unrecoverable and someone would
discover it from a shell script.

**The useful rule is per-signature, not per-day.** A signature is the trigger
reason plus the chain root — a hash the tool already computes — so five hundred
runs that failed in exactly the same way collapse to however many examples you
asked to keep. Generic age and size caps exist too, but they are disk hygiene;
this one is specific to what a recording is. See
[retention.md](retention.md).

## Storage

One setting picks the backend from the store URL: local disk, anything
S3-shaped, or in-memory for tests. Backends implement five methods. The viewer
opens a bucket recording through a short-lived signed URL and always writes its
HTML to local disk, so the free path and the team path run identical code.

## The diagnostics are the product

When a replay diverges, the user should not have to write to us. There are
twelve diagnoses and none of them is a bare "diverged": each names what
happened, why it usually happens, and what to do next. Most tools in this space
stop at "mismatch at step 4". The message *is* the support answer, and it is the
part most worth reading before you copy anything else here.

## What it is not

Not an observability platform. It does not aggregate, alert, or draw dashboards,
and there is no server, account, or telemetry. The category — Braintrust,
LangSmith, Arize, Langfuse — tells you what happened. This runs it again. If you
want both, they compose fine; nothing here reads or writes anything of theirs.

The closer relative is `vcrpy`, not LangSmith. See [limits.md](limits.md) for an
honest comparison.
