# -*- coding: utf-8 -*-
"""Every CLI command, driven through cli.main and judged on behaviour.

The commands are the product's actual interface and most of them had no test at
all — only the functions underneath them did, which does not prove the wiring.
Assertions are on exit codes and on the lines somebody depends on, never on
exact formatting, which should stay free to change.

`case`, `baseline` and `test` are covered in test_cases.py, where the fixtures
for them already live.
"""
import contextlib
import io as _io
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orientim
from orientim import ci, cli, storage

B = "http://127.0.0.1:8731"
ROOT = "tests/_runs/cli"


def agent(run):
    c = run.client()
    c.post(B + "/v1/chat/completions", content=json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "where is 4471"}],
        "n_tools": 1}).encode())
    c.post(B + "/search", content=b'{"q":"4471"}')
    run.output = "order 4471 not found"
    return run.output


def alt_agent(run):
    """A second entry point, at module level.

    `orientim ci --entry` resolves module:function through the import system
    and gets its own copy of this module, so a function stitched into globals()
    at call time is invisible to it — the command reports "cannot load entry"
    and the check would pass for the wrong reason.
    """
    run.client().post(B + "/search", content=b'{"q":"something-else"}')


def _fresh():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT, exist_ok=True)
    storage.reset_cache()


def _record(tags=None):
    with orientim.record(root=ROOT, always=True, tags=tags or {}) as h:
        agent(h)
    return h


def run(argv):
    """cli.main, with stdout captured and the exit code recovered."""
    buf = _io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


# --- ls -----------------------------------------------------------------------

def t_cli_ls():
    _fresh()
    h = _record(tags={"customer": "4471"})
    out, code = run(["--root", ROOT, "ls"])
    empty, _ = run(["--root", ROOT + "/nothing", "ls"])
    # `ls` reports the total step count, which includes the clock and
    # randomness shims and not only the HTTP calls. Asserted as it is rather
    # than as it might read, because the number on screen is the contract.
    http_steps = len([x for x in h.rec.steps if x.get("t") == "http"])
    total = len(h.rec.steps)
    ok = (code == 0 and h.rec.run_id in out and "customer=4471" in out
          and "%d steps" % total in out and total > http_steps
          and "no runs stored yet" in empty)
    return ok, "listed %r" % (out.strip().splitlines()[:1],)


def t_cli_ls_filters():
    _fresh()
    _record(tags={"team": "support"})
    _record(tags={"team": "billing"})
    matched, _ = run(["--root", ROOT, "ls", "--tag", "team=support"])
    missed, _ = run(["--root", ROOT, "ls", "--tag", "team=nobody"])
    bad_status, _ = run(["--root", ROOT, "ls", "--status", "failed"])
    return (matched.count("steps") == 1 and "no runs matched" in missed
            and "no runs matched" in bad_status), \
        "matched %d line(s)" % matched.count("steps")


# --- play ---------------------------------------------------------------------

def t_cli_play():
    _fresh()
    h = _record()
    out, code = run(["--root", ROOT, "play", h.rec.run_id])
    detailed, _ = run(["--root", ROOT, "play", h.rec.run_id, "--step"])
    ok = (code == 0 and h.rec.run_id in out and "completions" in out
          and "search" in out and len(detailed) > len(out))
    return ok, "%d lines, --step adds %d chars" % (
        len(out.splitlines()), len(detailed) - len(out))


# --- view ---------------------------------------------------------------------

def t_cli_view_writes_a_timeline():
    _fresh()
    h = _record()
    out, code = run(["--root", ROOT, "view", h.rec.run_id, "--no-open"])
    written = [ln.split(":", 1)[1].strip() for ln in out.splitlines()
               if ln.startswith("timeline:")]
    exists = bool(written) and os.path.exists(written[0])
    ok = (code == 0 and exists and "--entry" in out)
    if exists:
        page = open(written[0], encoding="utf-8").read()
        ok = ok and "<html" in page.lower() and h.rec.run_id in page
    return ok, "wrote %r" % (written[:1],)


# --- diff ---------------------------------------------------------------------

def t_cli_diff_two_recordings():
    _fresh()
    a = _record()

    def longer(run_):
        agent(run_)
        run_.client().post(B + "/chat-stable", content=b"{}")

    with orientim.record(root=ROOT, always=True) as b:
        longer(b)

    out, code = run(["--root", ROOT, "diff", a.rec.run_id, b.rec.run_id])
    same, _ = run(["--root", ROOT, "diff", a.rec.run_id, a.rec.run_id])
    ok = (code == 0 and "CHANGED" in out and "INSERTED" in out.upper()
          or "inserted" in out)
    ok = ok and "UNCHANGED" in same
    return ok, "diff exit %s, self-diff says unchanged=%s" % (
        code, "UNCHANGED" in same)


def t_cli_diff_json():
    _fresh()
    a = _record()
    out, code = run(["--root", ROOT, "diff", a.rec.run_id, a.rec.run_id,
                     "--json"])
    blob = json.loads(out)
    return (code == 0 and blob["kind"] == "orientim-diff"
            and blob["identical"] is True), "keys %r" % (sorted(blob)[:5],)


def t_cli_diff_needs_two_things():
    _fresh()
    _record()
    out, code = run(["--root", ROOT, "diff", "only-one"])
    return (code == ci.EXIT_CANNOT_RUN and "two recordings" in out), \
        "exit %s: %r" % (code, out.strip()[:60])


# --- prune --------------------------------------------------------------------

def t_cli_prune_needs_a_rule():
    """An empty policy deletes nothing rather than everything."""
    _fresh()
    _record()
    _record()
    out, code = run(["--root", ROOT, "prune"])
    left, _ = run(["--root", ROOT, "ls"])
    return (code == 0 and "No rule given" in out
            and left.count("steps") == 2), "prune said %r" % (out.strip()[:70],)


def t_cli_prune_dry_run_removes_nothing():
    _fresh()
    for _ in range(3):
        _record()
    out, code = run(["--root", ROOT, "prune", "--keep", "1", "--dry-run"])
    left, _ = run(["--root", ROOT, "ls"])
    return (code == 0 and "would remove" in out and left.count("steps") == 3), \
        "dry run kept %d" % left.count("steps")


def t_cli_prune_keeps_the_newest():
    _fresh()
    for _ in range(3):
        _record()
    out, code = run(["--root", ROOT, "prune", "--keep", "1"])
    left, _ = run(["--root", ROOT, "ls"])
    return (code == 0 and "removed 2" in out and left.count("steps") == 1), \
        "after prune: %d recording(s)" % left.count("steps")


# --- ci -----------------------------------------------------------------------

def t_cli_ci_green_and_red():
    _fresh()
    _record()
    green, code_green = run(["--root", ROOT, "ci",
                             "--entry", "tests.test_cli:agent"])

    red, code_red = run(["--root", ROOT, "ci",
                         "--entry", "tests.test_cli:alt_agent"])
    ok = (code_green == ci.EXIT_OK and "replayed identically" in green
          and code_red == ci.EXIT_CHANGED and "changed behaviour" in red)
    return ok, "green exit %s, red exit %s" % (code_green, code_red)


def t_cli_ci_reports_and_no_fail():
    _fresh()
    _record()
    out_path = os.path.join(ROOT, "ci.json")
    out, code = run(["--root", ROOT, "ci", "--entry", "tests.test_cli:agent",
                     "--report", out_path, "--no-fail"])
    blob = json.load(open(out_path, encoding="utf-8"))
    return (code == ci.EXIT_OK and blob["totals"]["replayed"] == 1
            and "runs" in blob), "report totals %r" % (blob["totals"],)


def t_cli_ci_cannot_run():
    """A bad entry point and an empty store are both 'could not run', not 'failed'."""
    _fresh()
    bad_entry, code_a = run(["--root", ROOT, "ci", "--entry", "nope:nothing"])
    _record()
    empty, code_b = run(["--root", ROOT + "/empty", "ci",
                         "--entry", "tests.test_cli:agent"])
    return (code_a == ci.EXIT_CANNOT_RUN and "cannot load" in bad_entry
            and code_b == ci.EXIT_CANNOT_RUN and "no recordings" in empty), \
        "bad entry %s, empty store %s" % (code_a, code_b)


# --- stability ----------------------------------------------------------------

def t_cli_stability():
    _fresh()
    out, code = run(["--root", ROOT, "stability",
                     "--entry", "tests.test_cli:agent", "--runs", "3"])
    return (code == 0 and "3 runs" in out and "STABILITY" in out), \
        "head %r" % (out.strip().splitlines()[1][:60] if out.strip() else "",)


# --- conformance --------------------------------------------------------------

def t_cli_conformance():
    """The numbers the README publishes, regenerated through the CLI."""
    loose, code_loose = run(["conformance"])
    strict, code_strict = run(["conformance", "--strict"])
    ok = (code_loose == 0 and code_strict == 0
          and "16" in loose and "15" in strict)
    return ok, "loose exit %s, strict exit %s" % (code_loose, code_strict)


def t_cli_conformance_saves():
    _fresh()
    out_path = os.path.join(ROOT, "conformance.json")
    _out, code = run(["conformance", "--save", out_path])
    blob = json.load(open(out_path, encoding="utf-8"))
    return (code == 0 and "captured" in blob and "rows" in blob
            and "declared_limits" in blob), \
        "saved keys %r" % (sorted(blob)[:5],)


# --- the parser itself --------------------------------------------------------

def t_cli_rejects_an_unknown_command():
    try:
        run(["not-a-command"])
    except SystemExit:
        pass
    out, code = run(["ls", "--nonsense"])
    return code != 0, "exit %s" % code


def t_every_subcommand_has_help():
    """A command whose --help crashes is a command nobody can discover."""
    names = ["ls", "play", "view", "stability", "diff", "prune", "ci",
             "case", "baseline", "test", "conformance"]
    broken = []
    for name in names:
        out, code = run([name, "--help"])
        if code != 0 or "usage" not in out.lower():
            broken.append(name)
    return not broken, "no help for %r" % (broken,) if broken else \
        "%d subcommands documented" % len(names)


# --- the CI surfaces ----------------------------------------------------------
# annotate / step_summary / emit are what action.yml puts on a pull request.
# They are plain files and environment variables, which is what makes them
# testable without a token, an App or an account.

def t_ci_annotations_name_the_failure():
    rows = [{"run_id": "run_a", "ok": True, "verdict": "IDENTICAL",
             "index": None, "steps": 2, "ms": 1.0, "tags": {}},
            {"run_id": "run_b", "ok": False, "verdict": "NEW_CALL",
             "index": 3, "steps": 4, "ms": 2.0, "tags": {}}]
    lines = ci.annotate(rows)
    return (len(lines) == 1 and lines[0].startswith("::error title=run_b::")
            and "NEW_CALL" in lines[0] and "at step 3" in lines[0]), \
        "annotations %r" % (lines,)


def t_ci_step_summary_is_markdown():
    rows = [{"run_id": "run_a", "ok": False, "verdict": "BODY_CHANGED",
             "index": 1, "steps": 2, "ms": 1.0, "tags": {}}]
    md = ci.step_summary(rows, True, {"newly_changed": ["run_a"], "fixed": [],
                                      "still_changed": [], "new_recordings": [],
                                      "missing_recordings": []})
    return ("| run |" in md and "**BODY_CHANGED**" in md
            and "New on this change" in md and "Strict key" in md), \
        "%d lines of markdown" % len(md.splitlines())


def t_ci_emit_writes_the_github_files():
    """Given the environment a workflow provides, the files are written."""
    _fresh()
    summary = os.path.join(ROOT, "step_summary.md")
    output = os.path.join(ROOT, "github_output.txt")
    rows = [{"run_id": "run_a", "ok": False, "verdict": "MORE_STEPS",
             "index": 2, "steps": 3, "ms": 1.0, "tags": {}}]
    before = {k: os.environ.get(k) for k in ("GITHUB_STEP_SUMMARY",
                                             "GITHUB_OUTPUT")}
    os.environ["GITHUB_STEP_SUMMARY"] = summary
    os.environ["GITHUB_OUTPUT"] = output
    try:
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            ci.emit(rows, True, None)
        printed = buf.getvalue()
        wrote_summary = open(summary, encoding="utf-8").read()
        wrote_output = open(output, encoding="utf-8").read()
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ("::error title=run_a::" in printed and "| run |" in wrote_summary
            and "replayed=1" in wrote_output and "changed=1" in wrote_output), \
        "output file: %r" % (wrote_output.strip().replace("\n", " "),)


def t_ci_emit_is_silent_outside_a_workflow():
    """No GITHUB_* variables means nothing to write, and no crash."""
    before = {k: os.environ.pop(k, None) for k in ("GITHUB_STEP_SUMMARY",
                                                   "GITHUB_OUTPUT")}
    try:
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            ci.emit([{"run_id": "r", "ok": True, "verdict": "IDENTICAL",
                      "index": None, "steps": 1, "ms": 1.0, "tags": {}}],
                    True, None)
        return buf.getvalue() == "", "printed %r" % buf.getvalue()
    finally:
        for k, v in before.items():
            if v is not None:
                os.environ[k] = v
