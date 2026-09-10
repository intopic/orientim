# -*- coding: utf-8 -*-
"""The same question, asked of code nobody here wrote.

    python study/sdk_probe.py

`probe.py` measures nondeterminism I introduced deliberately, one source at a
time. This measures what a *vendor's own client* emits when nobody is trying to
break anything: the OpenAI and Anthropic SDKs, pointed at a local provider,
recorded and then replayed with the agent code unchanged.

It matters because those clients attach headers of their own — runtime
versions, an async flag, a retry counter — and `hdr_fp` is in
`chain.DIGEST_FIELDS`. A header the agent never chose can therefore move a
verdict.

Three runs per client:

    same process        record and replay in one process, back to back
    fresh process       replay in a process started separately, which is what
                        CI actually does
    after a retry       the first attempt fails once, so the client's retry
                        counter is not what it was during the recording

No provider, no key, no network. Nothing here touches Orientim.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orientim                                        # noqa: E402
from orientim import session, store                    # noqa: E402

PORT = int(os.environ.get("STUDY_SDK_PORT", "9311"))
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_runs", "sdk")
FAIL_ONCE = {"armed": False, "used": False}
SEEN = {}          # client -> the header sets the provider was sent
WHO = {"client": "?"}   # which client is talking right now


class Provider(BaseHTTPRequestHandler):
    """Speaks enough of both wire protocols to answer one call."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        SEEN.setdefault(WHO["client"], []).append(
            {k.lower(): v for k, v in self.headers.items()})
        if FAIL_ONCE["armed"] and not FAIL_ONCE["used"]:
            # One 500, so the client retries and its retry counter moves.
            FAIL_ONCE["used"] = True
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "/v1/messages" in self.path:
            payload = {
                "id": "msg_fixed", "type": "message", "role": "assistant",
                "model": "claude-3-5-sonnet-20241022",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 2},
            }
        else:
            payload = {
                "id": "chatcmpl-fixed", "object": "chat.completion",
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "ok"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2,
                          "total_tokens": 10},
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# --- the agents, such as they are ---------------------------------------------
#
# Deliberately trivial. The point is not what the agent decides; it is what the
# vendor's client puts on the wire around that decision.

def openai_agent(run):
    from openai import OpenAI
    c = OpenAI(api_key="test-key", base_url=BASE + "/v1",
               http_client=run.client())
    r = c.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "where is order 4471"}])
    run.output = r.choices[0].message.content


def anthropic_agent(run):
    from anthropic import Anthropic
    c = Anthropic(api_key="test-key", base_url=BASE,
                  http_client=run.client())
    r = c.messages.create(
        model="claude-3-5-sonnet-20241022", max_tokens=64,
        messages=[{"role": "user", "content": "where is order 4471"}])
    run.output = r.content[0].text


AGENTS = {"openai": openai_agent, "anthropic": anthropic_agent}


# --- what a fresh process does ------------------------------------------------

CHILD = '''
import os, sys
sys.path.insert(0, %r)
sys.path.insert(0, %r)
os.environ["STUDY_SDK_PORT"] = %r
import sdk_probe
from orientim import session
d = session.replay(%r, sdk_probe.AGENTS[%r], strict=True)
print("IDENTICAL" if d.ok else d.diagnosis[0])
'''


def in_fresh_process(name, path):
    here = os.path.dirname(os.path.abspath(__file__))
    src = CHILD % (os.path.dirname(here), here, str(PORT), path, name)
    r = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, cwd=here)
    out = (r.stdout or "").strip().splitlines()
    return out[-1] if out else ("child failed: %s"
                                % (r.stderr or "").strip()[-160:])


def headers_moved(path, divergence):
    """Which header fingerprints differ, when a verdict blames the headers."""
    try:
        _meta, recorded = store.load(path)
    except Exception:
        return []
    now = {s.get("orig_i"): s for s in (divergence.replay_steps or [])}
    out = []
    for s in recorded:
        if s.get("t") != "http":
            continue
        b = now.get(s.get("i"))
        if b and b.get("hdr_fp") != s.get("hdr_fp"):
            out.append("step %s" % s.get("i"))
    return out


def probe(name, agent, arm_retry=False):
    root = os.path.join(ROOT, name + ("-retry" if arm_retry else ""))
    if os.path.isdir(root):
        shutil.rmtree(root)
    FAIL_ONCE["armed"] = False
    FAIL_ONCE["used"] = False

    # A replay never contacts the provider — every response comes out of the
    # file — so a failure can only be arranged while recording. That is also
    # the realistic shape: the recording was made through one transient error,
    # and the replay, which cannot have one, sends whatever the client sends
    # on a first attempt.
    FAIL_ONCE["armed"] = bool(arm_retry)
    WHO["client"] = name
    with orientim.record(root=root, always=True) as run:
        agent(run)
    path = run.path
    retried = FAIL_ONCE["used"]
    FAIL_ONCE["armed"] = False

    d = session.replay(path, agent, strict=True)
    verdict = "IDENTICAL" if d.ok else d.diagnosis[0]
    return {"client": name,
            "case": "recorded via a retry" if arm_retry else "same process",
            "verdict": verdict, "headers_moved": headers_moved(path, d),
            "retry_actually_happened": retried, "path": path}


def main():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    rows = []
    for name, agent in AGENTS.items():
        try:
            r = probe(name, agent)
        except Exception as e:
            rows.append({"client": name, "case": "same process",
                         "verdict": "%s: %s" % (type(e).__name__, e)})
            continue
        rows.append(r)
        FAIL_ONCE["armed"] = False
        FAIL_ONCE["used"] = False
        rows.append({"client": name, "case": "fresh process",
                     "verdict": in_fresh_process(name, r["path"]),
                     "headers_moved": []})
        try:
            rows.append(probe(name, agent, arm_retry=True))
        except Exception as e:
            rows.append({"client": name, "case": "recorded via a retry",
                         "verdict": "%s: %s" % (type(e).__name__, e)})

    srv.shutdown()

    print()
    print("  A VENDOR'S OWN CLIENT, RECORDED AND REPLAYED UNCHANGED")
    print("  " + "-" * 76)
    print("  %-11s %-16s %-24s %s"
          % ("client", "case", "verdict", "headers that moved"))
    print("  " + "-" * 76)
    for r in rows:
        print("  %-11s %-16s %-24s %s"
              % (r["client"], r["case"], r["verdict"][:24],
                 ", ".join(r.get("headers_moved") or []) or "-"))

    for r in rows:
        if r["case"].endswith("retry") and not r.get("retry_actually_happened"):
            print("  !! the retry case did not actually retry - it measures nothing")

    # What the clients put on the wire, which is the reason any of this can
    # move a verdict: hdr_fp is in chain.DIGEST_FIELDS.
    # Per client. Pooling two vendors' headers would report their differing
    # package versions as "varying", which is an artefact of the pooling and
    # not a fact about either client.
    print()
    for who, sets in sorted(SEEN.items()):
        keys = sorted({k for h in sets for k in h
                       if k.startswith("x-")
                       or k in ("idempotency-key", "traceparent")})
        varying = [k for k in keys
                   if len({h.get(k) for h in sets if k in h}) > 1]
        print("  %-10s sends %d vendor header(s); varying across its own "
              "requests: %s" % (who, len(keys), ", ".join(varying) or "none"))

    clean = [r for r in rows if r["verdict"] == "IDENTICAL"]
    print()
    print("  replayed identically: %d of %d" % (len(clean), len(rows)))
    print()
    with open(os.path.join(ROOT, "_sdk.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    print("  written to %s" % os.path.join(ROOT, "_sdk.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
