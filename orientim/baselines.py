# -*- coding: utf-8 -*-
"""Baselines: what the suite said at a point in time, kept as an object.

`orientim ci --baseline path/to/report.json` already compares against an earlier
report. That works, and it makes the baseline a *file somebody remembered to
keep* — which means in practice it is a CI artifact with a retention policy, a
path that differs between machines, and nothing that says which commit it came
from.

A baseline here is the same data with a name, a home, and provenance. It is
stored beside the cases, so `main` means the same thing to everyone with the
repository. The comparison itself is not reimplemented: it is `ci.compare`,
keyed on the case name, the same function `orientim ci` uses.
"""
import json
import os
import re
import time

from . import ci

FORMAT = 1
DIRNAME = "baselines"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class BaselineError(Exception):
    """Something about a baseline is wrong, and the message says what."""


def _dir(root):
    return os.path.join(root or "runs", DIRNAME)


def check_name(name):
    """Branch names contain slashes, so those are allowed — but nothing that
    could walk out of the directory."""
    if not name or not _NAME_RE.match(name) or ".." in name:
        raise BaselineError(
            "%r is not a usable baseline name: letters, digits, dot, dash, "
            "underscore and slash, up to 128 characters" % (name,))
    return name


def path_for(name, root):
    check_name(name)
    return os.path.join(_dir(root), name.replace("/", os.sep) + ".json")


# --- the object ---------------------------------------------------------------

def create(name, rows, root="runs", strict=True, note=None, overwrite=True):
    """Freeze the current result of the suite under a name.

    Stores the rows in the same shape `ci.compare` reads, so the comparison
    below is the one `orientim ci` already uses rather than a second one that
    would eventually disagree with it.
    """
    check_name(name)
    p = path_for(name, root)
    if os.path.exists(p) and not overwrite:
        raise BaselineError("a baseline named %r already exists" % name)
    os.makedirs(os.path.dirname(p), exist_ok=True)

    failed = [r for r in rows if not r.get("ok")]
    obj = {
        "format": FORMAT,
        "kind": "orientim-baseline",
        "name": name,
        "created_at": time.time(),
        "strict": bool(strict),
        "note": note or "",
        "totals": {"cases": len(rows), "passed": len(rows) - len(failed),
                   "failed": len(failed)},
        # Verdicts and counts, not evidence. A baseline is compared against,
        # not read for detail — it is the *current* run that has to explain
        # itself — and keeping prompts and tool arguments in a file that lives
        # in the repository forever is a cost with no matching benefit.
        "runs": [{"case": r.get("case"), "run_id": r.get("run_id"),
                  "ok": bool(r.get("ok")), "verdict": r.get("verdict"),
                  "steps": r.get("steps", 0),
                  "recorded_root": r.get("recorded_root", ""),
                  "failed_evaluators": [
                      res["evaluator"]
                      for res in (r.get("evaluation") or {}).get("results", [])
                      if res.get("status") == "fail"]}
                 for r in rows],
    }
    obj.update({k: v for k, v in ci._github().items() if v})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    return p


def load(name, root="runs"):
    p = path_for(name, root)
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
    except OSError:
        raise BaselineError("no baseline named %r under %s" % (name, _dir(root)))
    except ValueError as e:
        raise BaselineError("baseline %r is not readable JSON: %s" % (name, e))
    if obj.get("format", 1) > FORMAT:
        raise BaselineError(
            "baseline %r was written by a newer version of Orientim (format %s)"
            % (name, obj.get("format")))
    return obj


def load_any(ref, root="runs"):
    """Accept a baseline name or a path to a report.

    `orientim ci --baseline` takes a path, and a report has the same `runs`
    shape a baseline does. Refusing a path here would mean a team that already
    has one has to migrate before they can try this.
    """
    if os.path.sep in ref or ref.endswith(".json") or "/" in ref:
        p = path_for(ref, root) if not os.path.exists(ref) else ref
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return load(ref, root)


def list_baselines(root="runs"):
    d = _dir(root)
    out = []
    if not os.path.isdir(d):
        return out
    for cur, _dirs, files in os.walk(d):
        for fn in sorted(files):
            if not fn.endswith(".json"):
                continue
            rel = os.path.relpath(os.path.join(cur, fn), d)
            name = rel[:-5].replace(os.sep, "/")
            try:
                out.append(load(name, root))
            except BaselineError as e:
                out.append({"name": name, "broken": str(e)})
    return sorted(out, key=lambda b: b.get("name", ""))


def delete(name, root="runs"):
    p = path_for(name, root)
    if not os.path.exists(p):
        raise BaselineError("no baseline named %r under %s" % (name, _dir(root)))
    os.remove(p)
    return p


# --- comparing ----------------------------------------------------------------

def compare(rows, baseline):
    """Case-keyed comparison. `ci.compare`, not a copy of it."""
    return ci.compare(rows, baseline, key="case")


def describe(cmp_, baseline, width=74):
    """The human half of a comparison, for `orientim baseline compare`."""
    when = time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(baseline.get("created_at") or 0))
    L = ["", "  against baseline %r  (%s)" % (baseline.get("name", "?"), when)]
    if baseline.get("commit"):
        L.append("  recorded at commit %s%s"
                 % (baseline["commit"][:12],
                    " on " + baseline["branch"] if baseline.get("branch") else ""))
    if baseline.get("note"):
        L.append("  note: %s" % baseline["note"])
    L.append("-" * width)
    rows = [
        ("newly_changed", "started failing on this change"),
        ("fixed", "fixed on this change"),
        ("still_changed", "already failing before this"),
        ("new_recordings", "not in the baseline"),
        ("missing_recordings", "in the baseline but gone now"),
    ]
    quiet = True
    for key, label in rows:
        names = cmp_.get(key) or []
        if not names:
            continue
        quiet = False
        L.append("  %-32s %s" % (label + ":", ", ".join(str(n) for n in names)))
    if quiet:
        L.append("  Nothing moved: every case says what it said in the baseline.")
    L.append("-" * width)
    L.append("")
    return "\n".join(L)
