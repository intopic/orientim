# -*- coding: utf-8 -*-
"""Line up the steps of two executions before comparing them.

The old diff walked both runs by index: step 1 against step 1, step 2 against
step 2. That is correct only when nothing was inserted or removed. Insert one
call near the beginning and every later step reads as different — a run that
changed in one place reports as a run that changed everywhere, and the reader
has to find the real change inside the noise.

So the steps are aligned first, the way a text diff aligns lines, and only then
compared. Five outcomes:

    SAME        aligned, and identical under the active key
    CHANGED     aligned, and not identical
    INSERTED    present in B, with nothing in A to align it to
    DELETED     present in A, with nothing in B
    REORDERED   present in both, in a different position

Nothing here touches replay. Replay matches a live request against a recorded
one by lookup key, deterministically, and that is a different problem with a
different correctness bar. This is an explanation layer: if it aligns two steps
badly the report is confusing, which is a cost paid by a person reading it, not
by a verdict.
"""
import difflib

from . import chain

SAME = "SAME"
CHANGED = "CHANGED"
INSERTED = "INSERTED"
DELETED = "DELETED"
REORDERED = "REORDERED"

# The alignment budget.
#
# difflib.SequenceMatcher is O(n*m) worst case, and the worst case is the shape
# agents actually have: thousands of steps drawn from a handful of endpoints.
# Measured on this machine, 3000 such steps fully reordered took 110 seconds —
# a diff nobody would wait for.
#
# Below CHEAP the product is small enough that the worst case is still under a
# tenth of a second, so it is never worth thinking about. Above it, cost is
# driven by how repetitive the keys are, so a sequence with few distinct keys
# relative to its length takes the linear path instead.
CHEAP = 200000
REPETITION = 40


def identity(step):
    """What makes two steps 'the same call' for the purpose of lining them up.

    Method and URL, and deliberately not the body: a model call whose
    temperature changed is the same call with a different question, and
    reporting it as one call removed and another added would throw away the
    very comparison somebody opened the diff for.
    """
    return "%s %s" % (step.get("method", "?"), step.get("url", ""))


def _pair_state(a, b, field):
    if chain.step_digest(a, field) == chain.step_digest(b, field):
        return SAME
    return CHANGED


def _opcodes(keys_a, keys_b):
    """Alignment opcodes, with autojunk off.

    SequenceMatcher's autojunk heuristic treats any element appearing in more
    than 1% of a sequence of 200 or more as noise worth ignoring. An agent that
    polls one endpoint in a loop is exactly that shape, so the heuristic would
    quietly refuse to align the most repetitive — and most interesting — runs.
    """
    return difflib.SequenceMatcher(None, keys_a, keys_b,
                                   autojunk=False).get_opcodes()


def _over_budget(keys_a, keys_b):
    n, m = len(keys_a), len(keys_b)
    if n * m <= CHEAP:
        return False
    distinct = len(set(keys_a) | set(keys_b))
    return distinct * REPETITION < max(n, m)


def _linear(steps_a, steps_b, keys_a, keys_b, field):
    """The bounded fallback: prefix, suffix, and the middle paired in order.

    Correct for the shapes that matter — an unchanged run, a changed run, a run
    with steps added or removed at either end. What it does not do is find a
    step that moved, because finding moves is the quadratic part. It says so.
    """
    n, m = len(keys_a), len(keys_b)
    head = 0
    while head < n and head < m and keys_a[head] == keys_b[head]:
        head += 1
    tail = 0
    while (tail < n - head and tail < m - head
           and keys_a[n - 1 - tail] == keys_b[m - 1 - tail]):
        tail += 1

    out = []
    for k in range(head):
        out.append({"op": _pair_state(steps_a[k], steps_b[k], field),
                    "a": k, "b": k})
    mid_a = list(range(head, n - tail))
    mid_b = list(range(head, m - tail))
    for k in range(min(len(mid_a), len(mid_b))):
        out.append({"op": CHANGED, "a": mid_a[k], "b": mid_b[k]})
    for a in mid_a[len(mid_b):]:
        out.append({"op": DELETED, "a": a, "b": None})
    for b in mid_b[len(mid_a):]:
        out.append({"op": INSERTED, "a": None, "b": b})
    for k in range(tail):
        a, b = n - tail + k, m - tail + k
        out.append({"op": _pair_state(steps_a[a], steps_b[b], field),
                    "a": a, "b": b})
    return out


def plan(steps_a, steps_b, field="key_strict"):
    """Align, and say whether the full comparison was affordable."""
    keys_a = [identity(s) for s in steps_a]
    keys_b = [identity(s) for s in steps_b]
    if _over_budget(keys_a, keys_b):
        return {
            "entries": _linear(steps_a, steps_b, keys_a, keys_b, field),
            "degraded": True,
            "reason": ("%d and %d steps drawn from %d distinct calls: the full "
                       "alignment is quadratic on a sequence this repetitive, "
                       "so steps were paired in order and moves were not looked "
                       "for" % (len(keys_a), len(keys_b),
                                len(set(keys_a) | set(keys_b)))),
        }
    return {"entries": _align_full(steps_a, steps_b, keys_a, keys_b, field),
            "degraded": False, "reason": None}


def align(steps_a, steps_b, field="key_strict"):
    """Return one entry per aligned position, in reading order.

    Each entry is {"op", "a", "b"} where a and b are indices into the two step
    lists, or None. Order follows B where B has a step, and A where it does not,
    so the result reads as "what the second run did, and what it stopped doing".
    """
    return plan(steps_a, steps_b, field)["entries"]


def _align_full(steps_a, steps_b, keys_a, keys_b, field):
    out = []
    for tag, i1, i2, j1, j2 in _opcodes(keys_a, keys_b):
        if tag == "equal":
            for k in range(i2 - i1):
                a, b = i1 + k, j1 + k
                out.append({"op": _pair_state(steps_a[a], steps_b[b], field),
                            "a": a, "b": b})
        elif tag == "replace":
            # Same position, different call. Pair as far as both go, then the
            # remainder is an insertion or a deletion.
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                out.append({"op": CHANGED, "a": i1 + k, "b": j1 + k})
            for a in range(i1 + n, i2):
                out.append({"op": DELETED, "a": a, "b": None})
            for b in range(j1 + n, j2):
                out.append({"op": INSERTED, "a": None, "b": b})
        elif tag == "delete":
            for a in range(i1, i2):
                out.append({"op": DELETED, "a": a, "b": None})
        elif tag == "insert":
            for b in range(j1, j2):
                out.append({"op": INSERTED, "a": None, "b": b})
    return _find_moves(out, keys_a, keys_b, steps_a, steps_b, field)


def _find_moves(entries, keys_a, keys_b, steps_a, steps_b, field):
    """Turn matching delete/insert pairs into one reordering.

    Two calls that swapped places are one thing that happened, not four. A
    delete of X and an insert of X elsewhere is the same X, and saying so is
    both shorter and truer than saying one vanished and an unrelated one
    appeared.
    """
    deleted = {}
    for pos, e in enumerate(entries):
        if e["op"] == DELETED:
            deleted.setdefault(keys_a[e["a"]], []).append(pos)

    for e in entries:
        if e["op"] != INSERTED:
            continue
        key = keys_b[e["b"]]
        waiting = deleted.get(key)
        if not waiting:
            continue
        origin = entries[waiting.pop(0)]
        if not waiting:
            deleted.pop(key, None)
        a, b = origin["a"], e["b"]
        # A move can also have changed on the way. Say which, because "it moved"
        # and "it moved and its body is different" are different problems.
        e.update({"op": REORDERED, "a": a, "moved_from": a, "moved_to": b,
                  "also_changed": _pair_state(steps_a[a], steps_b[b], field)
                  == CHANGED})
        origin["op"] = None                     # folded into the entry above
    return [e for e in entries if e["op"] is not None]


def summarise(entries):
    counts = {SAME: 0, CHANGED: 0, INSERTED: 0, DELETED: 0, REORDERED: 0}
    for e in entries:
        counts[e["op"]] = counts.get(e["op"], 0) + 1
    return counts


def first_difference(entries):
    """The first entry that is not SAME, in reading order, or None."""
    for e in entries:
        if e["op"] != SAME:
            return e
    return None
