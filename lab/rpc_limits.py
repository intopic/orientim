# -*- coding: utf-8 -*-
"""The reader's own limits, measured — and kept apart from the messages.

Four readings that a two-valued scan collapsed into one, plus the count that
shows the budget bounds the walk rather than only the verdict. Read-side
only: nothing here captures, records or replays.

    python lab/rpc_limits.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orientim import rpc     # noqa: E402

REQ = '{"jsonrpc":"2.0","id":1,"method":"m"}'
WIDE = json.dumps(list(range(6000)))
NARROW = json.dumps(list(range(10)))


def _exchange(body, batch=False):
    req = "[%s]" % REQ if batch else REQ
    return rpc.read_exchange({"t": "http", "i": 1, "b64": False,
                              "req": req, "body": body})


def _received(ev):
    return [m for m in ev["messages"] if m["ref"]["side"] == rpc.RECEIVED]


def _visits(obj):
    """How many nodes the walk actually touches, which is the whole point of
    a node budget. Counted by wrapping the recursion, not by trusting it."""
    seen = [0]
    real = rpc.scan_non_json

    def counted(o, depth=0, budget=None):
        seen[0] += 1
        return real(o, depth, budget)

    rpc.scan_non_json = counted
    try:
        return real(obj), seen[0]
    finally:
        rpc.scan_non_json = real


def _nodes(obj):
    """Every node, counted without a budget, so the budget has something to
    be measured against."""
    n = 1
    if isinstance(obj, dict):
        for v in obj.values():
            n += _nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            n += _nodes(v)
    return n


def main():
    print("a bounded scan, and the three answers it owes the caller")
    print()
    cases = (
        ("valid, over the budget",
         '{"jsonrpc":"2.0","id":1,"result":{"a":%s}}' % WIDE),
        ("a constant before the budget",
         '{"jsonrpc":"2.0","id":1,"result":{"a":%s,"z":NaN}}' % NARROW),
        ("a constant past the scanned part",
         '{"jsonrpc":"2.0","id":1,"result":{"a":%s,"z":NaN}}' % WIDE),
        ("readable, and answered",
         '{"jsonrpc":"2.0","id":1,"result":{"a":%s}}' % NARROW),
    )
    print("  %-34s %-16s %-10s %-14s %s"
          % ("case", "kind", "validated", "link", "answered"))
    print("  " + "-" * 92)
    for label, body in cases:
        ev = _exchange(body)
        got = _received(ev)[0]
        print("  %-34s %-16s %-10s %-14s %d"
              % (label, got["kind"], got["validated"],
                 ",".join(ln["link"] for ln in ev["links"]),
                 len(ev["answered"])))
    print()
    for label, body in cases:
        for f in _received(_exchange(body))[0]["findings"]:
            print("     %-34s %s" % (label, f))
    print()

    print("  the budget bounds the walk, and not only the verdict")
    state, visited = _visits(rpc._loads(
        '{"jsonrpc":"2.0","id":1,"result":{"a":%s,"z":NaN}}' % WIDE))
    print("     nodes in the body      %d" % _nodes(rpc._loads(
        '{"jsonrpc":"2.0","id":1,"result":{"a":%s,"z":NaN}}' % WIDE)))
    print("     MAX_NODES              %d" % rpc.MAX_NODES)
    print("     nodes visited          %d" % visited)
    print("     scan answered          %s" % state)
    print()

    print("  a candidate the reader could not finish is still a candidate")
    small = '{"jsonrpc":"2.0","id":1,"result":"ok"}'
    big = '{"jsonrpc":"2.0","id":1,"result":{"a":%s}}' % WIDE
    for label, order in (("small first", [small, big]),
                         ("big first", [big, small])):
        ev = _exchange("[%s]" % ",".join(order), batch=True)
        print("     %-12s %-24s answered %d   unvalidated %d"
              % (label, ",".join(sorted(ln["link"] for ln in ev["links"])),
                 len(ev["answered"]),
                 rpc.summarise([ev])["unvalidated_messages"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
