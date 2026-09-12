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
    """The two measurements the counting rule has to make agree.

    The rule is *a node is counted when it is inspected, and it is inspected
    only while the budget has something left*, so the definitional measurement
    is the budget actually spent — `MAX_NODES` minus what is left. The earlier
    version of this counter measured something else: it counted calls into the
    recursion and it let the root call past the wrapper, so it was off by one
    and, worse, it could not see a node that was inspected without being
    charged. That is the whole hole this measures, so both numbers are taken
    and the root is wrapped like every other node.

    `charged` is the definition. `entered` is how many times the function was
    entered, the root included. They come apart exactly when some node is
    inspected for free or entered after the budget is gone.
    """
    entered, budget = [0], [rpc.MAX_NODES]
    real = rpc.scan_non_json

    def counted(o, depth=0, b=None):
        entered[0] += 1
        return real(o, depth, b)

    rpc.scan_non_json = counted
    try:
        state = counted(obj, 0, budget)
    finally:
        rpc.scan_non_json = real
    return state, entered[0], rpc.MAX_NODES - budget[0]


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


def _nest(inner, levels):
    """`inner` placed exactly `levels` deep, the root counting as depth 0."""
    for _ in range(levels):
        inner = {"a": inner}
    return inner


def _at_depth(items, levels):
    return json.dumps(_nest(items, levels)).replace('"@"', "NaN")


def _bodies():
    """The depth limit is where the old rule leaked, so it is measured from
    both sides of it, with a constant before and after exhaustion."""
    plain = list(range(6000))
    early, late = list(plain), list(plain)
    early[5] = late[-1] = "@"
    deep = json.dumps(_nest("x", rpc.MAX_DEPTH + 4))
    return (
        ("an array of 6000 one level above the limit",
         _at_depth(plain, rpc.MAX_DEPTH - 1)),
        ("an array of 6000 exactly at the limit",
         _at_depth(plain, rpc.MAX_DEPTH)),
        ("an array of 6000 one level below it",
         _at_depth(plain, rpc.MAX_DEPTH + 1)),
        ("at the limit, a constant before exhaustion",
         _at_depth(early, rpc.MAX_DEPTH)),
        ("at the limit, a constant after exhaustion",
         _at_depth(late, rpc.MAX_DEPTH)),
        ("a cut branch, then a constant sibling",
         '{"deep":%s,"then":{"z":NaN}}' % deep),
        ("a cut branch, then a clean sibling",
         '{"deep":%s,"then":{"z":1}}' % deep),
        ("neither cut nor constant", '{"a":[1,2,3]}'),
    )


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

    print("  one counting rule, and the two measurements it makes agree")
    print("     MAX_DEPTH %d   MAX_NODES %d" % (rpc.MAX_DEPTH, rpc.MAX_NODES))
    print()
    print("     %-44s %-14s %7s %7s %7s"
          % ("body", "answered", "nodes", "entered", "charged"))
    print("     " + "-" * 82)
    for label, text in _bodies():
        state, entered, charged = _visits(rpc._loads(text))
        print("     %-44s %-14s %7d %7d %7d"
              % (label, state, _nodes(rpc._loads(text)), entered, charged))
    print()
    print("     entered == charged in every row: no node is inspected")
    print("     uncharged, and none is entered once the budget is gone")
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
