# What a replay can tell you

Twenty verdicts. Three describe a faithful reproduction — including whether
the bug the recording was kept for is gone — two describe a counterfactual, and
the rest name a specific reason the run did not reproduce. None of them is a
bare "diverged".

```python
report = orientim.replay(run.path, my_agent)
print(report.report())
print(report.diagnosis[0])   # the code, e.g. "HEADERS_CHANGED"
report.ok                    # True for a faithful reproduction:
                             # IDENTICAL, FIXED or STILL_BROKEN
```

## IDENTICAL — and what it actually promises

`report.ok` is `True` only when **all** of these hold:

- the same requests, in the same order, with the same bodies;
- the same request headers, judged by the fingerprint described in
  [architecture.md](architecture.md);
- the same responses;
- no exception the recorded run did not also raise;
- nothing about the recording that makes it an incomplete record of the run.

That last clause is doing more work than it looks like. Almost every failure
verdict below exists because a replay could otherwise have said `IDENTICAL`
about a run it had not actually reproduced.

## Did the fix work?

A faithful reproduction answers two different questions, and only one of them
was ever reported. "Nothing changed" is the regression answer. "The failure this
recording was kept for is gone" is the debugging answer — and it is the one
somebody actually asked. When a recording was kept because the run failed, that
same faithful reproduction now says which.

### `FIXED`
The recording was kept because the run failed — it raised, or your own quality
check rejected the answer — and the replay walked the same path without failing
that way. `report.ok` is `True`: nothing regressed, and the thing you were
chasing is gone. Keep the recording; it is the regression test for that bug now.

### `STILL_BROKEN`
The same failure happened again, exactly. `report.ok` is `True` — nothing
regressed — but the bug is not fixed. You have it offline now, on your machine,
in a loop you can step through as often as you like.

The exception case is automatic: the trigger already records what the run died
of. For a wrong-but-successful answer, hand the replay the same check you used
to decide it was wrong:

```python
orientim.replay(run.path, my_agent, check=looks_right)
```

When the path also changed, a divergence verdict stays the headline — but the
report still carries a line telling you whether the recorded failure recurred.

## The recording is not a whole run

These are ranked above everything else. If part of the run was never captured,
nothing said about the captured part is the whole answer.

### `STALE_FORMAT`
The file was written by a version whose step hash meant something different, so
a verdict of identical would not mean what it says.
→ Re-record, or pin the version that wrote it.

### `UNCAPTURED_LIBRARY`
The recorded run made calls through `aiohttp` or `urllib`, which we do not
intercept. The rest of the recording is faithful, but it is not the whole
run. The verdict names the calls.
→ Route that tool through `httpx`, or treat the replay as partial.

### `STREAM_INCOMPLETE`
A response was still streaming when the file was written — the agent opened it
and never drained it, so the stored body is the part that had arrived.
→ Read the response to completion, or close it before the run ends.

### `TRUNCATED`
The ring buffer evicted steps before the trigger fired. The recording does not
hold the whole run.
→ Raise `ring=`, or trigger the capture earlier.

### `NOTHING_CAPTURED`
The recording holds no HTTP steps at all, so a replay compares nothing against
nothing. Either the run made no calls, or it made them through something we do
not intercept.
→ Run `orientim conformance` to see what is intercepted on your machine.

## The recording is fine; the run went elsewhere

### `NO_MATCH_AT_ALL`
Zero of the requests were found in this recording. Almost always the recording
and the entry point do not belong together — not that your code changed.
→ Check `--entry` and the run id.

### `FEWER_STEPS`
The replay stopped earlier than the recording continued. **If you changed the
code, this is what should happen** — your change halts the flow here. If you
changed nothing, there is a silent failure before this step.

### `MORE_STEPS`
The replay made requests the recording does not contain. The code is taking a
longer path than the one recorded.

### `NEW_CALL`
Every recorded step matched, and *then* the code asked for something the
recording does not contain. Nothing about the recorded path drifted; the code
simply calls something new — which is what a fix that adds an API call looks
like. The new request was given a synthetic 599 and was not sent, because a
recording can only answer for the path it captured. Re-record to cover it.

### `UNCAPTURED_SOURCE`
Some steps matched, then a request appeared that does not exist in the
recording. Usually a source we do not capture: a local file or database read, a
timestamp built into the request body, or a cache inside your framework.

### `UNCAPTURED_CLOCK`
The agent asked for time or randomness more times than the recording holds.
Usually a clock or randomness library outside what we shim — see
[limits.md](limits.md).

## The calls were the same

### `OUTPUT_CHANGED`
Every HTTP call replayed identically — same requests, same responses, in the
same order — and the agent still returned a different answer.

The difference therefore did not come over the network. It came from inside the
process: an unshimmed source of randomness, dict or set iteration order, a local
file, a cache, or a code path that reads the clock without going through us.

This is the one divergence the HTTP boundary cannot explain, which is why it is
reported on its own rather than as "chain hash differs". `index` is `None`,
because there is no step to point at — pointing at one would send you to a step
that is fine.

It can only fire for a recording that **declared** an output:

```python
with orientim.record() as run:
    run.output = my_agent(question)
```

Without that, there is nothing to compare, and the verdict is exactly what it
would have been before the field existed. See
[execution-model.md](execution-model.md).

→ Diff the two answers, then look for state that is not HTTP.

## The same path, a different request

### `BODY_CHANGED`
Same URL, different request bytes. Most often whitespace, key ordering or float
rounding.
→ Try `strict=False` (`--loose`) to see whether it passes under the normalised
key. If it does, the change was cosmetic.

### `HEADERS_CHANGED`
Same URL, same body, different headers. Matching ignores headers on purpose, so
the recorded response **was** served — read this as: *the agent asked a
different question and got the old answer.*

This is the verdict that used to be `IDENTICAL`. Two calls that differ only by a
header — two tenants, two ranges, streaming versus not — share a lookup key, so
a code change that swaps their order hands each one the other's data.
→ If the header genuinely cannot change the response, add it to
`transport.HEADER_DENY`.

## You asked a different question

A replay with `patch=` is not a reproduction and is never reported as one. It
replaces what a step returned and asks what the agent would have done:

```python
orientim.replay(path, agent, patch={
    3: {"body": '{"hits": ["order 4471 shipped"]}'},   # if search had found it
    5: {"status": 429},                                # if we had been rate limited
})
```

Patchable fields: `status`, `body`, `b64`, `headers`, `error`. Indices are
positions among the HTTP steps — the numbers `orientim play` and the timeline
show. An index out of range or an unknown field raises `ValueError` rather than
silently doing nothing.

Because the responses were changed on purpose, a counterfactual is judged on
what the agent **asked**, not on what it was handed. The verdict says where its
own requests started to differ from the recorded ones — the blast radius of the
change.

### `COUNTERFACTUAL`
The agent made the same requests up to step *n*, then took a different path.
Everything after *n* is the consequence of what you changed.

### `COUNTERFACTUAL_SAME`
The agent made exactly the same requests anyway. Whatever you changed, the code
did not branch on it — which is often the more interesting answer.

## The requests matched and the code still failed

### `REPLAY_RAISED`
Every step matched, and then the code raised an exception the recorded run did
not. The recording replays faithfully; your code does not survive it. The fault
is after the last HTTP call.

An exception the recording *itself* ended on is not a divergence — replaying a
crash and getting the same crash is a faithful replay, and reports `IDENTICAL`.

## Reading a report

```
!!  Same request, different headers   [HEADERS_CHANGED]
    Step 0 went to the same URL with the same body but different headers.
    The recorded response was served anyway — matching ignores headers on
    purpose — so read this as: the agent asked a different question and got
    the old answer.
    -> if the header is irrelevant, add it to transport.HEADER_DENY
```

Every verdict has that shape: what happened, why it usually happens, what to do
next. If you ever hit one whose message does not tell you enough to act, that is
a bug worth reporting — the message is meant to be the whole support answer.

## In code

```python
d = orientim.replay(path, agent, strict=True)

d.ok                 # bool
d.diagnosis          # (code, title, message, action)
d.index              # first differing step
d.recorded_root      # chain root of the recording
d.replay_root        # chain root of this replay
d.uncaptured         # sources hit that we could not capture
d.unseen             # calls that left through a library we do not intercept
d.blocked            # side-effecting URLs the replay refused to forward
d.patched            # steps whose response you replaced
d.n_attempted        # requests attempted
d.n_recorded         # steps in the recording
d.n_matched          # steps that matched before the first divergence
```

---

New to Orientim? Start with [how-to-use.md](how-to-use.md).
