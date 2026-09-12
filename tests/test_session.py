# -*- coding: utf-8 -*-
"""Session pseudonymisation v1: what it covers, and what it refuses to claim.

An MCP server mints a session identifier and returns it in a response header.
A client reads it there and echoes it on every later request, so the value is
in two places at once: the stored response header, in the clear, and inside
`hdr_fp`, where it is a hash. Removing it from the first breaks the second —
the replayed client echoes whatever the recording gave it — which is why the
transform writes both sides in token space or neither.

Three things these tests are careful about, because each was a correction:

    the rule is an observation, not proof
        "no earlier request carried this value" is what the recorder can
        check. That the client *took* the value from the response is the
        operator's assertion, made by turning the feature on, and where it is
        false the first replay diverges rather than quietly passing.

    the guarantee is header-shaped
        the transformed headers and the fingerprint over them. Not the body,
        not the output, not the file as a whole.

    a limit is not an equality
        two recordings cannot be compared on a fingerprint that covers a
        per-recording token. That is said in words and in a structured field,
        and the step still counts as a step that differs — `hdr_fp` is one
        hash over every included header, so a real change to another one
        cannot be ruled out.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labserver
import orientim
from orientim import diff, session, store

B = "http://127.0.0.1:8731"
URL = B + "/mcp"
ROOT = "tests/_runs/session"
HDR = "mcp-session-id"


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    labserver.STATE["minted"] = []
    labserver.STATE["sticky"] = False


def _derived(seen=None, force=None, echo=False, in_body=False):
    """initialize, read the session from the response, then use it."""
    def run(h):
        c = h.client()
        r = c.post(URL, content=json.dumps({"method": "initialize"}).encode(),
                   headers={"Content-Type": "application/json"})
        got = force or r.headers.get(HDR) or ""
        if seen is not None:
            seen.append(got)
        for _ in (1, 2):
            c.post(URL, headers={"Content-Type": "application/json",
                                 "Mcp-Session-Id": got},
                   content=json.dumps({"method": "use", "echo": echo,
                                       "in_body": in_body}).encode())
        # Not the session: an agent that returns it would report
        # OUTPUT_CHANGED on every replay of a tokenised recording, which is
        # true, correct, and a different limit from the one under test.
        h.output = "done"
    return run


def _configured(echo=False):
    """A session the client already had. No initialize, nothing derived."""
    def run(h):
        c = h.client()
        for _ in (1, 2):
            c.post(URL, headers={"Content-Type": "application/json",
                                 "Mcp-Session-Id":
                                     labserver.STATE["configured"]},
                   content=json.dumps({"method": "use", "echo": echo}).encode())
        h.output = "configured"
    return run


def _two_sessions(seen=None):
    def run(h):
        c = h.client()
        got = []
        for _ in (1, 2):
            r = c.post(URL, headers={"Content-Type": "application/json"},
                       content=json.dumps({"method": "initialize"}).encode())
            got.append(r.headers.get(HDR) or "")
        for s in got:
            c.post(URL, headers={"Content-Type": "application/json",
                                 "Mcp-Session-Id": s},
                   content=json.dumps({"method": "use"}).encode())
        if seen is not None:
            seen.extend(got)
        h.output = "two"
    return run


def _record(agent, tokens=True, name=None, fresh=True):
    if fresh:
        _fresh()
    with orientim.record(root=ROOT, always=True, session_tokens=tokens) as h:
        agent(h)
    if not name:
        return h.path
    dst = os.path.join(ROOT, name + ".jsonl")
    shutil.copyfile(h.path, dst)
    return dst


def _stored(path):
    _meta, steps = store.load(path)
    return [v for v in ((s.get("headers") or {}).get(HDR)
                        for s in steps if s.get("t") == "http") if v]


def _block(path):
    meta, _steps = store.load(path)
    blocks = meta.get("transforms") or []
    return blocks[0] if blocks else None


# --- what it does -------------------------------------------------------------

def t_a_tokenised_session_replays_identically():
    """The whole point: the file is consistent with itself in token space, so
    the replayed client echoes the token and nothing diverges."""
    seen, back = [], []
    path = _record(_derived(seen))
    d = session.replay(path, _derived(back), strict=True)
    return (d.diagnosis[0] == "IDENTICAL" and not d.headers_changed
            and back and back[0] == _stored(path)[0]), \
        "%s headers_changed=%s replay saw %r, file holds %r" % (
            d.diagnosis[0], d.headers_changed, back[:1], _stored(path)[:1])


def t_the_real_identifier_reaches_the_client_and_the_server():
    """Tokenisation is a property of the *stored* recording. The run itself
    talks to the server with the identifier the server issued — which this
    server enforces, answering 400 to anything else."""
    seen = []
    path = _record(_derived(seen))
    minted = labserver.STATE["minted"][-1]
    return (seen[0] == minted and _stored(path)[0] != minted), \
        "client had %r, server minted %r, file holds %r" % (
            seen[0], minted, _stored(path)[0])


def t_the_identifier_is_not_in_the_stored_headers():
    """The guarantee, stated as narrowly as it is true."""
    seen = []
    path = _record(_derived(seen))
    return seen[0] not in _stored(path), \
        "stored headers hold %r" % (_stored(path),)


def t_the_guarantee_does_not_reach_a_response_body():
    """And the limit of it. A server that repeats the session in a body has
    written it where no header rule looks."""
    seen = []
    path = _record(_derived(seen, in_body=True))
    raw = open(path, encoding="utf-8").read()
    return (seen[0] not in _stored(path) and seen[0] in raw), \
        "header tokenised=%s body kept it=%s" % (
            seen[0] not in _stored(path), seen[0] in raw)


def t_two_sessions_in_one_recording_get_two_tokens():
    seen, back = [], []
    path = _record(_two_sessions(seen))
    stored = sorted(set(_stored(path)))
    d = session.replay(path, _two_sessions(back), strict=True)
    return (seen[0] != seen[1] and len(stored) == 2
            and not set(seen) & set(stored)
            and d.diagnosis[0] == "IDENTICAL"), \
        "sessions=%d tokens=%d replay=%s" % (
            len(set(seen)), len(stored), d.diagnosis[0])


def t_two_recordings_do_not_share_a_map():
    """The map is per recording and in memory. Two runs of one session are
    two tokens — which is also why their fingerprints stop being comparable."""
    _fresh()
    labserver.STATE["sticky"] = True
    a = _record(_derived(), name="iso_a", fresh=False)
    b = _record(_derived(), name="iso_b", fresh=False)
    labserver.STATE["sticky"] = False
    return _stored(a)[0] != _stored(b)[0], \
        "same token in two recordings: %r" % (_stored(a)[:1],)


def t_two_values_never_share_a_token():
    """The counterexample that killed the first generator: a token truncated
    to the length of a short identifier loses the part that made it unique."""
    tokens = store.SessionTokens()
    out = []
    for v in ("A", "S", "ab", "sess-1", "sess-" + "9" * 40):
        stored = {"Mcp-Session-Id": v}
        tokens.for_response(stored)
        out.append(stored["Mcp-Session-Id"])
    return (len(set(out)) == len(out) and not set(out) & {"A", "S", "ab"}), \
        "tokens were %r" % (out,)


class _Forced(store.SessionTokens):
    """A generator whose candidates are scripted, so the redraw is testable.

    The odds of a real collision are not worth waiting for; what is worth
    testing is that each kind is rejected, and that the metadata after a
    redraw still says what is true.
    """

    def __init__(self, script):
        store.SessionTokens.__init__(self)
        self.script = list(script)
        self.drawn = []

    def _candidate(self):
        c = self.script.pop(0)
        self.drawn.append(c)
        return c


def t_a_token_is_redrawn_against_every_collision():
    """Three kinds of collision, each rejected: a token already minted, an
    identifier already seen on a request, and the value being replaced right
    now. A token equal to any of them would be indistinguishable from a real
    value in the file, and the metadata would call it transformed."""
    tokens = _Forced(["sess-MINTED", "sess-ON-A-REQUEST", "sess-CURRENT",
                      "sess-FRESH"])
    tokens._taken.add("sess-MINTED")
    tokens.for_request({"Mcp-Session-Id": "sess-ON-A-REQUEST"})
    stored = {"Mcp-Session-Id": "sess-CURRENT"}
    tokens.for_response(stored)
    got = stored["Mcp-Session-Id"]
    block = tokens.block([{"i": 0, "headers": stored},
                          {"i": 1, "headers": {HDR: "sess-ON-A-REQUEST"}}])
    left = block["untransformed"]
    return (got == "sess-FRESH" and len(tokens.drawn) == 4
            and block["transformed"]["values"] == 1
            and block["transformed"]["stored_headers"] == [0]
            and len(left) == 1 and left[0]["steps"] == [1]
            and left[0]["reason"] == "carried_by_an_earlier_request"),         "drew %r, kept %r, block %r" % (tokens.drawn, got, block)


def t_the_ring_does_not_keep_the_steps_it_evicted():
    """The bookkeeping is indices, not steps. A run long enough to evict its
    own early steps must hold no step objects, must leave nothing pending,
    and must report only interactions the file still has."""
    _fresh()
    with orientim.record(root=ROOT, always=True, ring=4,
                         session_tokens=True) as h:
        c = h.client()
        r = c.post(URL, content=json.dumps({"method": "initialize"}).encode(),
                   headers={"Content-Type": "application/json"})
        got = r.headers.get(HDR)
        for _ in range(6):
            c.post(URL, headers={"Content-Type": "application/json",
                                 "Mcp-Session-Id": got},
                   content=json.dumps({"method": "use"}).encode())
        h.output = "done"
        rec = h.rec
    meta, steps = store.load(h.path)
    http = [s for s in steps if s.get("t") == "http"]
    carried = [s.get("i") for s in http if not (s.get("headers") or {}).get(HDR)]
    reported = meta["transforms"][0]["transformed"]["hdr_fp"]
    held = rec.sessions._fp_index
    return (meta.get("dropped") and not rec.sessions._pending
            and not any(isinstance(x, dict) for x in held)
            and len(held) > len(reported)
            and reported == carried),         "dropped=%s pending=%r held=%r reported=%r in file=%r" % (
            meta.get("dropped"), rec.sessions._pending, held, reported, carried)


# --- the boundary -------------------------------------------------------------

def t_a_configured_session_that_is_not_echoed_is_untouched():
    """Nothing to protect: a value that only ever travels on requests reaches
    the file as a hash and nothing else."""
    path = _record(_configured())
    block = _block(path)
    d = session.replay(path, _configured(), strict=True)
    return (not _stored(path) and d.diagnosis[0] == "IDENTICAL"
            and block["transformed"]["values"] == 0
            and not block["untransformed"]), \
        "stored=%r replay=%s block=%r" % (_stored(path), d.diagnosis[0], block)


def t_a_configured_session_that_is_echoed_is_left_and_declared():
    """The residue. Renaming it would make the file disagree with a client
    that never read it, so it stays — and the metadata says so, with the
    reason, which is what keeps `values: 0` from being ambiguous."""
    path = _record(_configured(echo=True))
    block = _block(path)
    d = session.replay(path, _configured(echo=True), strict=True)
    left = block["untransformed"]
    return (labserver.STATE["configured"] in _stored(path)
            and d.diagnosis[0] == "IDENTICAL"
            and block["transformed"]["values"] == 0
            and len(left) == 1
            and left[0]["reason"] == "carried_by_an_earlier_request"
            and left[0]["values"] == 1 and left[0]["steps"]), \
        "stored=%r replay=%s left=%r" % (
            _stored(path), d.diagnosis[0], left)


def t_a_client_that_ignores_the_response_diverges_loudly():
    """Where the operator's condition is false *and* the value reaches a
    captured request header, the first replay says so. Not a guarantee that
    every misuse is caught — a value used only inside the agent is not
    observed here at all — but this one is not silent."""
    path = _record(_derived())
    stuck = labserver.STATE["minted"][-1]
    d = session.replay(path, _derived(force=stuck), strict=True)
    return (d.diagnosis[0] == "HEADERS_CHANGED" and d.headers_changed), \
        "replay said %s" % d.diagnosis[0]


def t_the_feature_is_off_by_default():
    """Off unless asked for, and then the recording is exactly today's: the
    identifier in the clear, no transform block, and a replay that matches."""
    seen = []
    path = _record(_derived(seen), tokens=False)
    d = session.replay(path, _derived(), strict=True)
    return (_block(path) is None and seen[0] in _stored(path)
            and d.diagnosis[0] == "IDENTICAL"), \
        "block=%r stored=%r replay=%s" % (
            _block(path), _stored(path), d.diagnosis[0])


def t_the_metadata_carries_no_value_and_no_map():
    seen = []
    path = _record(_derived(seen))
    block = _block(path)
    text = json.dumps(block)
    return (seen[0] not in text and _stored(path)[0] not in text
            and block["scope"]["rule"] == "first_observed_in_response"
            and block["scope"]["condition"] == "client_reuses_response_value"
            and block["activation"] == "explicit"
            and block["comparable_across_recordings"] is False
            and block["transformed"]["hdr_fp"]), \
        "block=%s" % text


# --- what reads it ------------------------------------------------------------

def _pair(name_a, name_b, tokens_a=True, tokens_b=True):
    _fresh()
    labserver.STATE["sticky"] = True
    a = _record(_derived(), tokens=tokens_a, name=name_a, fresh=False)
    b = _record(_derived(), tokens=tokens_b, name=name_b, fresh=False)
    labserver.STATE["sticky"] = False
    return diff.compare(a, b)


def _limited_rows(cmp):
    return [r for r in cmp["steps"] if r.get("comparison_limit")]


def t_a_diff_of_two_transformed_recordings_states_the_limit():
    """Structured, not only worded: a reader that parses the JSON must not
    take this for a proved change."""
    cmp = _pair("lim_a", "lim_b")
    rows = _limited_rows(cmp)
    lim = rows[0]["comparison_limit"] if rows else {}
    return (rows and lim.get("field") == "hdr_fp"
            and lim.get("transform") == "session_pseudonym"
            and lim.get("v") == 1
            and all("not comparable" in r["why"] for r in rows)), \
        "%d limited rows: %r" % (len(rows), rows[:1])


def t_a_limit_is_not_an_equality():
    """It explains a difference; it never excuses one. The steps still differ,
    the comparison is not identical, and nothing here produces a pass."""
    cmp = _pair("neq_a", "neq_b")
    rows = _limited_rows(cmp)
    return (not cmp["identical"] and rows
            and all(r["op"] == "CHANGED" for r in rows)), \
        "identical=%s ops=%r" % (cmp["identical"], [r["op"] for r in rows])


def t_a_transformed_recording_against_an_untransformed_one():
    """One side in token space and one side not is the same limit: the two
    fingerprints were taken over different things."""
    cmp = _pair("mix_a", "mix_b", tokens_b=False)
    return bool(_limited_rows(cmp)), "no limit reported: %r" % (
        [r.get("why") for r in cmp["steps"]],)


def t_a_recording_against_its_own_replay_is_not_relaxed():
    """The one comparison that must never be relaxed. The signal is positive —
    this side says which recording it replays — rather than the absence of a
    transform block, which a replay side would never carry anyway."""
    path = _record(_derived())
    meta_a, steps_a = store.load(path)
    d = session.replay(path, _derived(force="sess-someone-else"), strict=True)
    steps_b = [dict(s) for s in steps_a]
    for s in steps_b:                       # a replay that fingerprints anew
        s["hdr_fp"] = "0" * 16
    meta_b = {"run_id": meta_a["run_id"] + "_now",
              "replay_of": meta_a["run_id"]}
    cmp = diff.compare_executions(meta_a, steps_a, meta_b, steps_b)
    whys = [r.get("why") for r in cmp["steps"] if r.get("why")]
    return (not _limited_rows(cmp)
            and any(w == "different request headers" for w in whys)
            and d.diagnosis[0] == "HEADERS_CHANGED"), \
        "limited=%d whys=%r replay=%s" % (
            len(_limited_rows(cmp)), whys, d.diagnosis[0])
