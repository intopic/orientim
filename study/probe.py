# -*- coding: utf-8 -*-
"""Does an unchanged agent replay cleanly when its requests are not identical?

    python study/probe.py

Every number Orientim has about itself comes from a lab built to be replayable.
The match key is `sha256(method|url|body)` with no field-level exclusion in
either mode, so one timestamp or one identifier in a request body is enough to
diverge a replay of code that did not change. Whether that happens, and to
which sources of nondeterminism, has never been measured.

This measures it. For each source: record an agent that emits it, then replay
**the same code, unchanged**, and see what Orientim says. A source that comes
back IDENTICAL is already handled. A source that comes back as a divergence is
a false positive against real code, and it is what a stranger adopting the tool
would hit on their first run.

The second column is the one that matters for what to do about it:

    ENVIRONMENT   a code change cannot alter this value — a clock, a random
                  identifier, a retry counter. Safe to neutralise, because
                  neutralising it cannot hide a behavioural change.
    CONTENT       a code change can alter this value — a prompt, a plan, a
                  tool argument, a model name. Neutralising it would delete
                  exactly the signal Orientim exists to detect.

Nothing here touches Orientim. It imports the frozen package and reports.
"""
import json
import os
import random
import shutil
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orientim                                        # noqa: E402
from orientim import session                           # noqa: E402

PORT = 9310
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_runs")

ENVIRONMENT = "ENVIRONMENT"
CONTENT = "CONTENT"


# --- a provider that answers the same way every time --------------------------

class Provider(BaseHTTPRequestHandler):
    """Deterministic by construction, so anything that moves came from the
    agent and not from here."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({
            # Fresh on every answer, the way a real provider behaves. Every
            # other field is fixed, so this is the only thing that moves.
            "id": "chatcmpl-%s" % uuid.uuid4().hex[:12],
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "ok"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2,
                      "total_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# --- the sources ---------------------------------------------------------------
#
# Each agent makes exactly one model call. Everything about it is fixed except
# the one thing under test, so a divergence has exactly one possible cause.

def _post(run, payload, headers=None):
    return run.client().post(BASE + "/v1/chat/completions",
                             content=json.dumps(payload).encode(),
                             headers=headers or {})


def _msg(text="where is order 4471"):
    return {"model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": text}]}


def a_control(run):
    """Nothing moves. If this is not IDENTICAL, the harness is wrong."""
    _post(run, _msg())
    run.output = "ok"


def a_time_time(run):
    """`time.time()` in the body — the shim claims to cover this."""
    p = _msg()
    p["client_ts"] = time.time()
    _post(run, p)
    run.output = "ok"


def a_datetime_now(run):
    """`datetime.now()` in the prompt. CPython reads the clock directly here,
    not through `time.time`, so patching one may not cover the other."""
    from datetime import datetime
    _post(run, _msg("where is order 4471, asked at %s"
                    % datetime.now().isoformat()))
    run.output = "ok"


def a_uuid4(run):
    """`uuid.uuid4()` as a request id — the shim claims to cover this."""
    p = _msg()
    p["request_id"] = str(uuid.uuid4())
    _post(run, p)
    run.output = "ok"


def a_shimmed_header(run):
    """A shimmed value in a header. Headers are not in the lookup key, but
    `hdr_fp` *is* in DIGEST_FIELDS, so this tests the digest path."""
    _post(run, _msg(), headers={"X-Request-Id": str(uuid.uuid4())})
    run.output = "ok"


def a_changing_header(run):
    """A header that genuinely differs between the two runs, from a source the
    shim does not cover. This is the real test of whether `hdr_fp` in
    DIGEST_FIELDS breaks a replay on its own."""
    import secrets
    _post(run, _msg(), headers={"X-Trace-Id": secrets.token_hex(6)})
    run.output = "ok"


def a_retry_header(run):
    """What the OpenAI SDK sends and changes on every retry: 0 on the first
    attempt, 1 on the next. Here, 0 while recording and 1 while replaying."""
    _post(run, _msg(),
          headers={"x-stainless-retry-count": "1" if _flip() else "0"})
    run.output = "ok"


def a_random_random(run):
    """`random.random()` — the shim claims to cover this."""
    p = _msg()
    p["jitter"] = random.random()
    _post(run, p)
    run.output = "ok"


def a_random_randint(run):
    """`random.randint()` draws from the same generator but not through
    `random.random`, so it tests the state save/restore rather than the patch."""
    p = _msg()
    p["pick"] = random.randint(0, 10 ** 9)
    _post(run, p)
    run.output = "ok"


def a_os_urandom(run):
    """Documented as not shimmed. Measured anyway, because 'documented' and
    'harmless' are different claims."""
    p = _msg()
    p["nonce"] = os.urandom(8).hex()
    _post(run, p)
    run.output = "ok"


def a_secrets(run):
    """`secrets.token_hex` — also documented as not shimmed, and the one a
    security-minded developer reaches for."""
    import secrets
    p = _msg()
    p["nonce"] = secrets.token_hex(8)
    _post(run, p)
    run.output = "ok"


def a_echoed_server_id(run):
    """The shape a conversation id really takes: the provider mints it, the
    agent reads it out of the first response and sends it back in the second.

    Worth measuring rather than assuming, because replay serves the *recorded*
    response — so the id the agent reads during a replay may be the recorded
    one, which would make the second request match by construction.
    """
    first = _post(run, _msg())
    ident = first.json().get("id", "?")
    p = _msg("and the delivery date?")
    p["conversation_id"] = ident
    _post(run, p)
    run.output = "ok"


def a_key_order(run):
    """The same data, serialised with the keys in a different order. Loose mode
    is supposed to handle this; strict is supposed not to."""
    order = ["model", "messages"] if _flip() else ["messages", "model"]
    p = _msg()
    body = "{%s}" % ",".join('"%s":%s' % (k, json.dumps(p[k])) for k in order)
    run.client().post(BASE + "/v1/chat/completions", content=body.encode())
    run.output = "ok"


def a_float_precision(run):
    """0.1 + 0.2. Loose rounds to six places; strict compares bytes."""
    p = _msg()
    p["temperature"] = 0.1 + 0.2 if _flip() else 0.3
    _post(run, p)
    run.output = "ok"


def a_prompt_change(run):
    """The negative control, and the most important row in the table.

    This is a *behavioural* change wearing the same shape as the rows above. If
    any normalisation ever proposed for the others would also silence this one,
    that normalisation is wrong.
    """
    _post(run, _msg("where is order 4471" if _flip()
                    else "where is order 4471, and refund it"))
    run.output = "ok"


_FLIP = {"n": 0}


def _flip():
    """False on the recording pass, True on the replay pass — so the value
    genuinely differs between the two runs of identical code."""
    _FLIP["n"] += 1
    return _FLIP["n"] > 1


SOURCES = [
    ("control",            a_control,         ENVIRONMENT,
     "nothing moves"),
    ("time.time()",        a_time_time,       ENVIRONMENT,
     "wall clock in the body"),
    ("datetime.now()",     a_datetime_now,    ENVIRONMENT,
     "wall clock in the prompt text"),
    ("uuid.uuid4()",       a_uuid4,           ENVIRONMENT,
     "request id in the body"),
    ("shimmed header",     a_shimmed_header,  ENVIRONMENT,
     "uuid4 in X-Request-Id"),
    ("changing header",    a_changing_header, ENVIRONMENT,
     "unshimmed value, body constant"),
    ("retry-count header", a_retry_header,    ENVIRONMENT,
     "0 recorded, 1 replayed"),
    ("random.random()",    a_random_random,   ENVIRONMENT,
     "jitter in the body"),
    ("random.randint()",   a_random_randint,  ENVIRONMENT,
     "a different draw from the same generator"),
    ("os.urandom()",       a_os_urandom,      ENVIRONMENT,
     "documented as not shimmed"),
    ("secrets.token_hex",  a_secrets,         ENVIRONMENT,
     "documented as not shimmed"),
    ("echoed server id",   a_echoed_server_id, ENVIRONMENT,
     "provider mints it, agent sends it back"),
    ("json key order",     a_key_order,       ENVIRONMENT,
     "same data, different order"),
    ("float precision",    a_float_precision, ENVIRONMENT,
     "0.1 + 0.2 vs 0.3"),
    ("a changed prompt",   a_prompt_change,   CONTENT,
     "NEGATIVE CONTROL - must always diverge"),
]


# --- the measurement ------------------------------------------------------------

def probe(name, agent, kind, note):
    _FLIP["n"] = 0
    try:
        with orientim.record(root=ROOT, always=True) as run:
            agent(run)
        path = run.path
    except Exception as e:
        return {"source": name, "kind": kind, "note": note,
                "strict": "RECORD_ERROR", "loose": "RECORD_ERROR",
                "error": "%s: %s" % (type(e).__name__, e)}

    out = {"source": name, "kind": kind, "note": note}
    for mode in ("strict", "loose"):
        _FLIP["n"] = 1          # the replay pass gets the second value
        try:
            d = session.replay(path, agent, strict=(mode == "strict"))
            out[mode] = d.diagnosis[0] if not d.ok else "IDENTICAL"
        except Exception as e:
            out[mode] = "%s" % type(e).__name__
    return out


def main():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print()
    print("  DOES UNCHANGED CODE REPLAY?  -  one model call, one variable")
    print("  " + "-" * 92)
    print("  %-20s %-12s %-22s %-22s %s"
          % ("source", "kind", "strict", "loose", "note"))
    print("  " + "-" * 92)

    rows = []
    for name, agent, kind, note in SOURCES:
        r = probe(name, agent, kind, note)
        rows.append(r)
        print("  %-20s %-12s %-22s %-22s %s"
              % (r["source"], r["kind"], r["strict"], r["loose"], r["note"]))

    srv.shutdown()

    broken = [r for r in rows
              if r["kind"] == ENVIRONMENT and r["strict"] != "IDENTICAL"]
    broken_loose = [r for r in rows
                    if r["kind"] == ENVIRONMENT and r["loose"] != "IDENTICAL"]
    control = [r for r in rows if r["kind"] == CONTENT]

    print()
    print("  environment sources that break a strict replay: %d of %d"
          % (len(broken), sum(1 for r in rows if r["kind"] == ENVIRONMENT)))
    print("  ... and still break a loose replay:             %d"
          % len(broken_loose))
    print("  negative control diverged (it must):            %s"
          % all(r["strict"] != "IDENTICAL" for r in control))
    print()
    if broken:
        print("  each of these is a false positive against unchanged code:")
        for r in broken:
            print("     %-20s %s" % (r["source"], r["strict"]))
    print()

    with open(os.path.join(ROOT, "_probe.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    print("  written to %s" % os.path.join(ROOT, "_probe.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
