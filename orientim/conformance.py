# -*- coding: utf-8 -*-
"""`orientim conformance` — what we capture in YOUR environment.

Not a marketing number. This runs on the customer's machine, against their
installed libraries, and prints what it can and cannot capture there. The point
is that they learn the limits before they depend on us, not at three in the
morning when a replay diverges.
"""
import importlib.util
import json
import os
import platform
import sys
import tempfile
import time

from . import patterns, session

# Libraries whose traffic we intercept, and libraries we do not. urllib3 stays
# on the second list: requests is hooked above it, but urllib3 driven directly
# is not.
INTERCEPTED = ["httpx", "httpx2", "requests"]
NOT_INTERCEPTED = ["aiohttp", "urllib3", "pycurl"]
# Frameworks that route through an intercepted client and are therefore covered.
COVERED_SDKS = ["openai", "anthropic", "google.genai", "cohere", "mistralai"]
FRAMEWORKS = ["langchain", "langgraph", "llama_index", "crewai",
              "autogen", "pydantic_ai", "smolagents", "haystack"]


def _has(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _version(name):
    try:
        mod = __import__(name)
        return getattr(mod, "__version__", "?")
    except Exception:
        return "?"


def inspect_environment():
    """What about this machine changes what we can capture."""
    env = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "intercepted": [],
        "not_intercepted": [],
        "sdks": [],
        "frameworks": [],
        "notes": [],
    }
    for name in INTERCEPTED:
        if _has(name):
            env["intercepted"].append((name, _version(name)))
    for name in NOT_INTERCEPTED:
        if _has(name):
            env["not_intercepted"].append((name, _version(name)))
    for name in COVERED_SDKS:
        if _has(name.split(".")[0]):
            env["sdks"].append(name)
    for name in FRAMEWORKS:
        if _has(name):
            env["frameworks"].append((name, _version(name)))

    if not env["intercepted"]:
        env["notes"].append(
            ("blocker", "httpx is not installed. Capture requires it."))
    if env["not_intercepted"]:
        libs = ", ".join(n for n, _ in env["not_intercepted"])
        env["notes"].append(
            ("limit",
             f"{libs} present. Any tool that calls out through these libraries "
             f"is not captured, and a replay that reaches one will diverge."))
    if env["frameworks"]:
        fw = ", ".join(n for n, _ in env["frameworks"])
        env["notes"].append(
            ("limit",
             f"{fw} present. If the framework serves a result from its own cache "
             f"during replay, no request is made and we see nothing."))
    return env


def run(quiet=False, strict=False):
    """Run all twenty patterns here. Returns the report.

    strict=False is the loose key — normalised whitespace, key order and float
    rounding. Both numbers are published, and this is the switch between them.
    """
    from ._probe import Probe

    env = inspect_environment()
    rows = []
    scratch = os.path.join(tempfile.gettempdir(), "orientim_probe.txt")
    root = os.path.join(tempfile.gettempdir(), "orientim_conformance")

    with Probe() as B:
        for group, num, name, fn, mutate, expect in patterns.PATTERNS:
            patterns.reset(scratch)
            probe = (lambda f: (lambda h: f(h, B)))(fn)
            status, detail = "error", ""
            try:
                from . import _probe
                with session.record(root=root, env=["ORIENTIM_PROBE"]) as h:
                    probe(h)
                    h.rec.trigger("conformance")
                # Count effects AFTER recording: the recorded run is allowed to
                # fire once. Only a second firing during replay is a leak.
                effects_after_record = _probe.STATE.get("effects", 0)
                if mutate:
                    mutate()
                d = session.replay(h.path, probe, strict=strict)
                leaked = _probe.STATE.get("effects", 0) > effects_after_record
                if leaked:
                    status, detail = "leaked", "a real effect fired twice"
                elif d.ok:
                    status = "captured"
                else:
                    status, detail = "diverged", d.diagnosis[0]
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"[:60]
            rows.append({"group": group, "n": num, "name": name,
                         "expect": expect, "status": status, "detail": detail})
            if not quiet:
                sys.stdout.write("\r  checking %s/20..." % num)
                sys.stdout.flush()

    if not quiet:
        sys.stdout.write("\r" + " " * 32 + "\r")

    # A loose-key source is a claim under --loose and a declared limit under
    # strict bytes. Counting it as claimed in both modes is how "17 of 20"
    # became a number the strict run could not reproduce.
    def is_claimed(r):
        return (r["expect"] == patterns.CAPTURED
                or (r["expect"] == patterns.LOOSE and not strict))

    declared = [r for r in rows if not is_claimed(r)]
    claimed = [r for r in rows if is_claimed(r)]
    ok = [r for r in claimed if r["status"] == "captured"]
    undeclared = [r for r in claimed if r["status"] != "captured"]

    return {
        "when": time.time(),
        "strict": strict,
        "env": env,
        "rows": rows,
        "captured": len(ok),
        "claimed": len(claimed),
        "declared_limits": len(declared),
        "undeclared_failures": undeclared,
        "total": len(rows),
    }


def format_report(rep):
    env, L = rep["env"], []
    W = 66
    L.append("")
    L.append("=" * W)
    L.append("  Orientim conformance — this machine")
    L.append("=" * W)
    L.append("")
    L.append("  Python %s on %s" % (env["python"], env["platform"]))
    L.append("  Matching:     %s"
             % ("strict bytes" if rep.get("strict") else "loose (normalised)"))
    if env["intercepted"]:
        L.append("  Intercepting: " + ", ".join("%s %s" % t for t in env["intercepted"]))
    if env["sdks"]:
        L.append("  Covered SDKs: " + ", ".join(env["sdks"]))
    if env["frameworks"]:
        L.append("  Frameworks:   " + ", ".join("%s %s" % t for t in env["frameworks"]))
    L.append("")

    group = None
    for r in rep["rows"]:
        if r["group"] != group:
            group = r["group"]
            L.append("  " + group)
        mark = {"captured": "  ok  ", "diverged": "  --  ",
                "leaked": "  !!  ", "error": "  ??  "}[r["status"]]
        note = ""
        if r["expect"] == patterns.LIMIT:
            note = "  (declared limit)"
        elif r["expect"] == patterns.LOOSE and rep.get("strict"):
            note = "  (declared limit under strict bytes)"
        elif r["status"] != "captured":
            note = "  <- UNDECLARED  " + r["detail"]
        L.append("    %s %s %-32s%s" % (r["n"], mark, r["name"], note))
    L.append("")
    L.append("-" * W)
    L.append("  Captured %d of %d sources (%d of %d claimed)."
             % (rep["captured"], rep["total"], rep["captured"], rep["claimed"]))
    L.append("  %d declared limits, not captured by design." % rep["declared_limits"])
    if rep["undeclared_failures"]:
        L.append("")
        L.append("  !! %d UNDECLARED failure(s). Do not rely on replay for these:"
                 % len(rep["undeclared_failures"]))
        for r in rep["undeclared_failures"]:
            L.append("       %s %s — %s" % (r["n"], r["name"], r["detail"]))
    else:
        L.append("  No undeclared failures. Every gap on this machine is one we told you about.")
    L.append("-" * W)

    if env["notes"]:
        L.append("")
        L.append("  Specific to your environment")
        for kind, text in env["notes"]:
            tag = "BLOCKER" if kind == "blocker" else "limit"
            L.append("    [%s] %s" % (tag, _wrap(text, W - 12, 12)))
    L.append("")
    return "\n".join(L)


def _wrap(text, width, indent):
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    out.append(line)
    return ("\n" + " " * indent).join(out)


def save(rep, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, default=str)
    return path
