# -*- coding: utf-8 -*-
"""Tuesday's story: the agent searches, finds nothing, and makes something up."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim, labserver

B = "http://127.0.0.1:8731"
RUNS = "tests/_runs/demo"


def agent(h, question="status of order 4471"):
    c = h.client()
    c.post(f"{B}/chat-stable", content=json.dumps({"plan": question}).encode())
    hits = c.post(f"{B}/search", content=json.dumps({"q": None}).encode()).json()
    if not hits.get("hits"):
        # the defect: instead of saying "I don't know", it carries on
        c.post(f"{B}/chat-stable", content=b'{"action":"fabricate an answer"}')
        c.post(f"{B}/send-email", content=b'{"to":"customer","text":"Your order shipped!"}')
        h.rec.status = "failed"
        h.rec.trigger("fabricated answer")
    return "ok"


if __name__ == "__main__":
    srv = labserver.start(); time.sleep(0.3)
    with orientim.record(root=RUNS, tags={"order": "4471", "customer": "acme"}) as h:
        agent(h)
    print("saved:", h.path)
    print("emails actually sent during recording:", labserver.STATE.get("emails", 0))

    d = orientim.replay(h.path, agent, strict=True)
    print("replay:", d)
    print("emails after replay:", labserver.STATE.get("emails", 0),
          "| blocked:", len(d.blocked))
    srv.shutdown(); srv.server_close()


def fixed_agent(h, question="status of order 4471"):
    """The same agent, with the defect fixed: it does not make things up."""
    c = h.client()
    c.post(f"{B}/chat-stable", content=json.dumps({"plan": question}).encode())
    hits = c.post(f"{B}/search", content=json.dumps({"q": None}).encode()).json()
    if not hits.get("hits"):
        # the fix: stop and say you don't know — no email, no fabrication
        return "I don't know"
    return "ok"
