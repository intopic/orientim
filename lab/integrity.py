# -*- coding: utf-8 -*-
"""What a recording is, and is not, evidence of once it is written.

    python lab/integrity.py

An experiment. Nothing in Orientim changes: no format, no chain, no field.

R2 made this urgent rather than academic. A recorded context now decides
whether a historical fixture is released to a replay, so the question stopped
being "would we notice a corrupted file" and became "what is the trust
boundary of a decision that reads one".

**The question this measures, before any design.** Six mutations, each one a
thing somebody could do to a recording on disk, and for each: does anything
notice?

    1  edit a response body, alone
    2  edit a response body and its body_sha together
    3  edit the recorded replay context
    4  remove a step
    5  reorder two steps
    6  recompute every hash in the file after editing it

The sixth is the one that decides the design. A mechanism whose trust anchor
lives inside the file it protects cannot survive an editor who can run the
same code we can.
"""
import hashlib
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import orientim                                     # noqa: E402
from orientim import chain, session, store          # noqa: E402

PORT = 8804
BASE = "http://127.0.0.1:%d" % PORT
ROOT = os.path.join(HERE, "_runs", "integrity")
OUT = os.path.join(HERE, "_runs", "_integrity.json")


class _Two(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(n) or b"{}")
        body = json.dumps({"step": sent.get("step"), "balance": 100}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Two)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _agent(h):
    for step in ("one", "two"):
        h.client().post(BASE + "/do",
                        content=json.dumps({"step": step}).encode())
    h.output = "done"


def _root(path):
    _meta, steps = store.load(path)
    return chain.build_steps([s for s in steps if s.get("t") == "http"])[1]


def _objs(path):
    return [json.loads(x) for x in
            open(path, encoding="utf-8").read().splitlines()]


def _write(objs, name):
    p = os.path.join(ROOT, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(o) for o in objs) + "\n")
    return p


# --- the six mutations --------------------------------------------------------

def body_only(objs):
    for o in objs:
        if o.get("t") == "http":
            o["body"] = o["body"].replace("100", "999")
            break


def body_and_sha(objs):
    for o in objs:
        if o.get("t") == "http":
            o["body"] = o["body"].replace("100", "999")
            o["body_sha"] = hashlib.sha256(o["body"].encode()).hexdigest()
            break


def context_only(objs):
    objs[0]["_meta"].setdefault("context", {})
    objs[0]["_meta"]["context"] = {
        "tenant": {"value": "globex", "evidence": "declared_unchained"}}


def remove_a_step(objs):
    http = [i for i, o in enumerate(objs) if o.get("t") == "http"]
    del objs[http[-1]]
    objs[0]["_meta"]["steps"] = len(http) - 1


def reorder_steps(objs):
    http = [i for i, o in enumerate(objs) if o.get("t") == "http"]
    a, b = http[0], http[1]
    objs[a], objs[b] = objs[b], objs[a]
    for n, i in enumerate(sorted(http)):
        objs[i]["i"] = n


def recompute_everything(objs):
    """The editor who can run our code.

    Change the response, then make every derived field in the file agree with
    the change: `body_sha`, and anything else a reader recomputes. There is no
    stored root to fix, which is the finding — nothing in the file disagrees
    with anything else afterwards.
    """
    body_and_sha(objs)
    for o in objs:
        if o.get("t") == "http" and "chunks" in o and o["chunks"]:
            o["chunks"] = [o["body"]]


MUTATIONS = [
    ("1 a response body, alone", body_only),
    ("2 a response body and its body_sha", body_and_sha),
    ("3 the recorded replay context", context_only),
    ("4 a step removed", remove_a_step),
    ("5 two steps reordered", reorder_steps),
    ("6 everything recomputed to agree", recompute_everything),
]


def measure():
    srv = _serve()
    rows = []
    try:
        if os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT, exist_ok=True)
        with orientim.record(root=ROOT, always=True,
                             context={"tenant": "acme"}) as h:
            _agent(h)
        path = h.path
        before = _root(path)
        clean = session.replay(path, _agent, strict=True)

        for label, fn in MUTATIONS:
            objs = _objs(path)
            fn(objs)
            edited = _write(objs, "m.jsonl")
            try:
                after = _root(edited)
                loaded = True
            except Exception as e:
                after, loaded = "load failed: %s" % type(e).__name__, False
            try:
                d = session.replay(edited, _agent, strict=True)
                verdict = d.diagnosis[0]
            except Exception as e:
                verdict = "replay raised %s" % type(e).__name__
            rows.append({
                "mutation": label,
                "loads": loaded,
                "chain_root_moved": (after != before) if loaded else None,
                "replay_verdict": verdict,
                # Never "detected". No code path validates a recording
                # against anything, so a divergence here means the edit also
                # made the file disagree with the agent being replayed — a
                # side effect of the tamper, not a check that caught it.
                "diverged_from_the_agent": bool(loaded
                                                and verdict != "IDENTICAL"),
                "caught_by_an_integrity_check": False,
            })
        return {"clean_verdict": clean.diagnosis[0], "clean_root": before,
                "root_stored_in_the_file": _stored_root(path), "rows": rows}
    finally:
        srv.shutdown()
        srv.server_close()


def _stored_root(path):
    """Is a chain root written into the recording at all?

    `store.signature` computes one on demand for deduplication. If the file
    itself never carries it, there is nothing an editor has to forge, and
    nothing a reader can check the steps against.
    """
    raw = open(path, encoding="utf-8").read()
    root = _root(path)
    return root[:16] in raw or root in raw


def main():
    m = measure()
    print("what a recording is evidence of, once written")
    print()
    print("  a clean replay of the untouched file: %s" % m["clean_verdict"])
    print("  the chain root appears in the file:   %s"
          % m["root_stored_in_the_file"])
    print()
    print("  %-38s %-7s %-11s %-18s %s"
          % ("mutation", "loads", "root moved", "replay verdict",
             "caught by a check"))
    print("  " + "-" * 96)
    for r in m["rows"]:
        print("  %-38s %-7s %-11s %-18s %s"
              % (r["mutation"], r["loads"], r["chain_root_moved"],
                 r["replay_verdict"],
                 "yes" if r["caught_by_an_integrity_check"] else "NO"))
    print()
    seen = [r for r in m["rows"] if r["diverged_from_the_agent"]]
    print("  %d of %d mutations are caught by an integrity check." %
          (sum(1 for r in m["rows"] if r["caught_by_an_integrity_check"]),
           len(m["rows"])))
    print("  %d diverged from the agent being replayed, which is a side" %
          len(seen))
    print("  effect of the edit rather than a check that caught it:")
    for r in seen:
        print("      %-38s %s" % (r["mutation"], r["replay_verdict"]))
    print()
    print("  Row 2 is the one that settles it: the chain root moved and the")
    print("  replay was still IDENTICAL. Nothing compares the root to a")
    print("  trusted value, because no trusted value exists.")
    print()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, default=str)
    print("  written: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
