# -*- coding: utf-8 -*-
"""Divergence diagnostics — the message that is itself the support answer.

The single most important design decision in the product: when a replay
diverges, the customer should not have to write to us. The message must say
what happened, why, and what to do about it.

All user-facing strings are English. This is the surface the market sees.
"""

CODES = {
    "OUTPUT_CHANGED": (
        "Same calls, different answer",
        "Every HTTP call replayed identically — same requests, same responses, "
        "in the same order — and the agent still returned something else. The "
        "difference therefore did not come over the network. It came from inside "
        "the process: an unshimmed source of randomness, a dict or set iteration "
        "order, a local file, a cache, or a code path that reads the clock "
        "without going through us. This is the one divergence the HTTP boundary "
        "cannot explain, which is exactly why it is worth reporting on its own.",
        "compare the two answers, then look for state that is not HTTP",
    ),
    "FIXTURE_REFUSED": (
        "The replay contract did not release these fixtures",
        "This replay ran under a contract that asks who it is, and the answer "
        "did not license handing it what the recorded run was handed: "
        "{detail}. Nothing came out of the file, which is the point — a "
        "response this caller is not entitled to is one it must not be able "
        "to act on. Nothing about the recorded path changed and the agent did "
        "not call anything new, so this is not a behaviour change: it is the "
        "harness declining. Correct the replay context, or replay under the "
        "legacy contract, which asks for nothing.",
        "check the context you passed to replay(), or drop the contract",
    ),
    "STALE_FORMAT": (
        "Recording is an older format",
        "This file was written by a version of Orientim whose step hash meant "
        "something different, so a verdict of identical would not mean what it "
        "says. Re-record the run with this version.",
        "re-record, or pin the version that wrote it",
    ),
    "NOTHING_CAPTURED": (
        "Nothing to replay",
        "This recording holds no HTTP steps, so a replay compares nothing "
        "against nothing. Either the run made no calls, or it made them through "
        "a library we do not intercept: we capture httpx, httpx2 and requests, "
        "not aiohttp or urllib.",
        "run `orientim conformance` to see what is intercepted here",
    ),
    "UNCAPTURED_LIBRARY": (
        "Part of this run was never captured",
        "The recorded run made {n_unseen} call(s) through a library Orientim "
        "does not intercept — the first was '{detail}'. We capture httpx, httpx2 "
        "and requests; not aiohttp or urllib. "
        "The recording holds the rest of the run faithfully, but it is not the "
        "whole run, so no replay of it can honestly be called identical.",
        "route that tool through httpx, or treat this replay as partial",
    ),
    "STREAM_INCOMPLETE": (
        "A response was never fully read",
        "Step {index} was still streaming when the recording was written. The "
        "agent opened the response and did not drain it, so what we have is the "
        "part that had arrived — a replay would hand back a truncated body.",
        "read the response to completion, or close it before the run ends",
    ),
    "NO_MATCH_AT_ALL": (
        "No steps matched",
        "Are you replaying the right recording with the right entry point? "
        "Zero of {n_attempted} requests were found in this recording. That almost "
        "always means the recording and the agent function do not belong together "
        "— not that your code changed.",
        "check --entry and the run id",
    ),
    "UNCAPTURED_SOURCE": (
        "Uncaptured source of non-determinism",
        "{n_matched} steps matched, then it diverged at step {index}. The request "
        "'{detail}' does not exist in the recording. This usually means a source we "
        "do not capture: a local file or database read, a timestamp built into the "
        "request body, or a cache inside your framework.",
        "see the known limits in your install report",
    ),
    "UNCAPTURED_CLOCK": (
        "Uncaptured clock or randomness",
        "The agent asked for '{detail}' more times than were recorded. Usually a "
        "time or randomness library we do not shim.",
        "re-record in strict mode to surface it at capture time",
    ),
    "FEWER_STEPS": (
        "Fewer steps than recorded",
        "The replay stopped at step {index} while the recording continued. If you "
        "changed the code, this is exactly what should happen — your change halts "
        "the flow here. If you changed nothing, there is a silent failure before "
        "this step.",
        "did you change the code on purpose?",
    ),
    "MORE_STEPS": (
        "More steps than recorded",
        "The replay made {n_extra} requests that were not in the recording. The code "
        "is taking a longer path than the one recorded.",
        "compare against the step list in the sidebar",
    ),
    "BODY_CHANGED": (
        "Same path, different bytes",
        "The path is identical but the request bytes differ at step {index}. Most "
        "often this is whitespace, key ordering, or float rounding.",
        "try --loose to see whether it passes",
    ),
    "HEADERS_CHANGED": (
        "Same request, different headers",
        "Step {index} went to the same URL with the same body but different "
        "headers. The recorded response was served anyway — matching ignores "
        "headers on purpose — so read this as: the agent asked a different "
        "question and got the old answer.",
        "if the header is irrelevant, add it to transport.HEADER_DENY",
    ),
    "REPLAY_RAISED": (
        "Every step matched, then the replay raised {detail}",
        "The requests were identical up to the end, and then the code failed "
        "with an exception the recorded run did not hit. The recording replays "
        "faithfully; your code does not survive it.",
        "the fault is after the last HTTP call",
    ),
    "TRUNCATED": (
        "Recording was truncated",
        "The ring buffer evicted {dropped} step(s) before this run was saved. "
        "The recording does not hold the whole run, so it cannot replay "
        "faithfully — this is not your code changing.",
        "raise the ring size, or trigger the capture earlier",
    ),
    "COUNTERFACTUAL": (
        "Counterfactual — {n_patched} step(s) replaced",
        "This was not a reproduction: you replaced what step(s) {patched} "
        "returned and asked what the agent would have done. It made the same "
        "requests up to step {index}, then took a different path. Everything "
        "from there on is the consequence of the change.",
        "compare the step list against the recorded run",
    ),
    "COUNTERFACTUAL_SAME": (
        "Counterfactual — {n_patched} step(s) replaced, same path",
        "You replaced what step(s) {patched} returned, and the agent made "
        "exactly the same requests anyway. Whatever you changed, the code did "
        "not branch on it.",
        "the change had no effect on the path taken",
    ),
    "FIXED": (
        "Fixed — the recorded failure did not happen again",
        "This recording was kept because the run failed with {failure}. The "
        "replay walked the same path and did not fail that way. That is the "
        "answer a divergence report cannot give you: not 'something changed', "
        "but 'the thing you were chasing is gone'.",
        "keep this recording as the regression test for that bug",
    ),
    "STILL_BROKEN": (
        "Still broken — {failure} happened again",
        "This recording was kept because the run failed with {failure}, and the "
        "replay reproduced it exactly. Nothing regressed; the bug is simply not "
        "fixed yet. You now have it offline, on your machine, in a loop you can "
        "step through as many times as you like.",
        "change the code and replay again — the failure is reproducible here",
    ),
    "NEW_CALL": (
        "Every recorded step matched, then the code called something new",
        "The replay reproduced all {n_recorded} recorded step(s) and then made a "
        "request this recording does not contain: '{detail}'. A recording can "
        "only answer for the path it captured, so the new call was given a "
        "synthetic 599 and was not sent. This is what a fix that adds an API "
        "call looks like — it is not an uncaptured source.",
        "re-record to cover the new path",
    ),
    "IDENTICAL": (
        "Identical",
        "Every step matched byte for byte.",
        "",
    ),
}


def diagnose(divergence, n_attempted, n_recorded, dropped=0):
    """Return (code, title, message, action). Never a bare 'diverged'."""
    d = divergence
    if d.ok:
        # A faithful reproduction answers two different questions. "Nothing
        # changed" is one. "The failure this recording was kept for is gone" is
        # the other, and it is the one somebody actually asked.
        failure = getattr(d, "failure", None)
        if failure:
            code = "STILL_BROKEN" if getattr(d, "recurred", False) else "FIXED"
        else:
            code = "IDENTICAL"
        title, tpl, action = CODES[code]
        f = {"failure": failure or "?"}
        return (code, title.format(**f), tpl.format(**f), action)

    n_matched = getattr(d, "n_matched", d.index or 0)
    detail = d.uncaptured[0]["detail"] if d.uncaptured else ""
    kind = d.uncaptured[0]["kind"] if d.uncaptured else ""

    unseen = getattr(d, "unseen", None) or []
    patched = getattr(d, "patched", None) or []

    refused = [u for u in (d.uncaptured or []) if u.get("kind") == "ineligible"]

    if refused:
        # Ranked first, and above every step-level code on purpose. A refusal
        # is the cause of every miss that follows it, and the codes below
        # would name the consequence: `NEW_CALL` and `UNCAPTURED_SOURCE` both
        # say the code changed, and nothing about the code changed.
        code = "FIXTURE_REFUSED"
        detail = refused[0]["detail"]
    elif patched:
        # A patched replay answers a different question, so it never competes
        # with the reproduction verdicts. It cannot be IDENTICAL and it is not
        # a failure either.
        code = "COUNTERFACTUAL" if d.index is not None else "COUNTERFACTUAL_SAME"
    elif getattr(d, "stale", False):
        code = "STALE_FORMAT"
    elif unseen:
        # Ranked above every step-level diagnosis on purpose: if part of the run
        # was never captured, nothing said about the captured part is the whole
        # answer, and the user needs to know that first.
        code = "UNCAPTURED_LIBRARY"
        detail = unseen[0]["detail"]
    elif getattr(d, "incomplete", False):
        code = "STREAM_INCOMPLETE"
    elif dropped:
        code = "TRUNCATED"
    elif getattr(d, "no_steps", False):
        code = "NOTHING_CAPTURED"
    elif n_matched == 0 and n_attempted > 1:
        code = "NO_MATCH_AT_ALL"
    elif kind == "shim-miss":
        code = "UNCAPTURED_CLOCK"
    elif kind == "no-match" and n_recorded and n_matched == n_recorded:
        # Every recorded step was matched before the unmatched request arrived,
        # so nothing about the recorded path drifted: the code simply calls
        # something new. Saying "uncaptured source" here sends people hunting a
        # clock or a cache that is not there.
        code = "NEW_CALL"
    elif kind == "no-match":
        code = "UNCAPTURED_SOURCE"
    elif getattr(d, "headers_changed", False):
        code = "HEADERS_CHANGED"
    elif n_attempted < n_recorded:
        code = "FEWER_STEPS"
    elif n_attempted > n_recorded:
        code = "MORE_STEPS"
    elif getattr(d, "raised", None):
        code = "REPLAY_RAISED"
        detail = d.raised
    elif getattr(d, "output_changed", False) and d.index is None:
        # Ranked last among the step-level codes and gated on there being no
        # differing step: when the chain diverged too, the step is the cause and
        # the changed answer is its consequence. Reporting the consequence would
        # point the reader away from the thing they can act on.
        code = "OUTPUT_CHANGED"
    else:
        code = "BODY_CHANGED"

    title, tpl, action = CODES[code]
    fields = dict(
        n_patched=len(patched),
        patched=", ".join(str(i) for i in patched) or "-",
        n_unseen=getattr(d, "unseen_n", 0) or len(unseen),
        n_attempted=n_attempted,
        n_matched=n_matched,
        n_recorded=n_recorded,
        index=d.index or 0,
        detail=detail or "?",
        failure=getattr(d, "failure", None) or "?",
        n_extra=max(n_attempted - n_recorded, 0),
        dropped=dropped,
    )
    return (code, title.format(**fields), tpl.format(**fields), action)
