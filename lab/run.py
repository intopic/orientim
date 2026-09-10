# -*- coding: utf-8 -*-
"""Start the company, record it, and put it through Orientim.

    python lab/run.py up                  # start every service and agent
    python lab/run.py record              # record all 12 scenarios
    python lab/run.py cases               # turn the recordings into cases
    python lab/run.py baseline            # freeze what the suite says
    python lab/run.py regress             # run all 10 regressions
    python lab/run.py regress model_change   # just one
    python lab/run.py down

`up` leaves the fleet running in the background; every other command talks to
it. The whole thing is local and deterministic: no provider, no key, no cost.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from scenarios import SCENARIOS, BY_NAME          # noqa: E402
from variants import VARIANTS                     # noqa: E402

RUNS = os.path.join(HERE, "_runs")
SUP_RUNS = os.path.join(RUNS, "supervisor")
PIDS = os.path.join(RUNS, "_pids.json")
LOGS = os.path.join(RUNS, "_logs")

SUPERVISOR = "http://127.0.0.1:9200"
SERVICES = [
    ("model", ["services/model.py", "9101"], "http://127.0.0.1:9101"),
    ("state", ["services/state.py", "9103"], "http://127.0.0.1:9103/db/health"),
    ("tools", ["services/tools.py", "9102", "9103"],
     "http://127.0.0.1:9102/tool/_effects"),
    ("research", ["agents/children.py", "research", "9201"],
     "http://127.0.0.1:9201/health"),
    ("support", ["agents/children.py", "support", "9202"],
     "http://127.0.0.1:9202/health"),
    ("risk", ["agents/children.py", "risk", "9203"],
     "http://127.0.0.1:9203/health"),
    ("supervisor", ["agents/supervisor.py", "9200"],
     SUPERVISOR + "/health"),
]


def say(*a):
    print(*a, flush=True)


def _env(variant):
    e = dict(os.environ)
    e.update({"LAB_REPO": REPO, "LAB_RUNS": RUNS, "LAB_VARIANT": variant,
              "PYTHONPATH": HERE + os.pathsep + REPO})
    return e


def up(variant="v1", quiet=False):
    os.makedirs(LOGS, exist_ok=True)
    pids = {}
    for name, argv, health in SERVICES:
        log = open(os.path.join(LOGS, name + ".log"), "w", encoding="utf-8")
        p = subprocess.Popen([sys.executable] + [os.path.join(HERE, argv[0])]
                             + argv[1:], cwd=HERE, env=_env(variant),
                             stdout=log, stderr=subprocess.STDOUT)
        pids[name] = p.pid
        if not wait(health, 30):
            say("  !! %s did not come up — see %s"
                % (name, os.path.join(LOGS, name + ".log")))
            return None
        if not quiet:
            say("  up   %-11s pid %d" % (name, p.pid))
    with open(PIDS, "w", encoding="utf-8") as f:
        json.dump({"pids": pids, "variant": variant}, f)
    return pids


def down(quiet=False):
    if not os.path.exists(PIDS):
        if not quiet:
            say("  nothing running")
        return
    with open(PIDS, encoding="utf-8") as f:
        info = json.load(f)
    for name, pid in info["pids"].items():
        try:
            os.kill(pid, signal.SIGTERM)
            if not quiet:
                say("  down %-11s pid %d" % (name, pid))
        except OSError:
            pass
    os.remove(PIDS)
    time.sleep(0.6)


def running_variant():
    if not os.path.exists(PIDS):
        return None
    with open(PIDS, encoding="utf-8") as f:
        return json.load(f).get("variant")


def wait(url, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(url, timeout=5).read()
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.2)
    return False


def ask(task, timeout=180):
    req = urllib.request.Request(SUPERVISOR + "/run",
                                 data=json.dumps(task).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def effects():
    with urllib.request.urlopen("http://127.0.0.1:9102/tool/_effects",
                                timeout=10) as r:
        return json.loads(r.read().decode())


def reset_effects():
    req = urllib.request.Request("http://127.0.0.1:9102/tool/_effects/reset",
                                 data=b"{}", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


# --- recording ----------------------------------------------------------------

def record():
    """Run every scenario against the live fleet and keep what it did."""
    if running_variant() != "v1":
        say("  the fleet must be running v1 to record a baseline; "
            "run `python lab/run.py up` first")
        return 2
    # Every agent's recordings, not only the supervisor's. Leaving the
    # children's behind meant a re-record produced a fresh supervisor run and a
    # stale child one, and the two disagreed about what the fleet had just
    # done.
    for agent, _ in SUITES:
        d = os.path.join(RUNS, agent)
        if os.path.isdir(d):
            shutil.rmtree(d)
    reset_effects()
    say("\n  recording %d scenarios\n" % len(SCENARIOS))
    made = {}
    for s in SCENARIOS:
        task = dict(s["task"], scenario=s["name"])
        t0 = time.monotonic()
        out = ask(task)
        made[s["name"]] = out["_recording"]
        say("  %-26s %6.0f ms  %s" % (s["name"], (time.monotonic() - t0) * 1000,
                                      os.path.basename(out["_recording"])))
    with open(os.path.join(RUNS, "_recorded.json"), "w", encoding="utf-8") as f:
        json.dump(made, f, indent=2)
    say("\n  side effects during recording: %r" % effects())
    return 0


def _child_recordings(agent):
    """Which recording each child wrote for each scenario.

    The children tag every recording with the scenario, which is the only
    thing joining a child's run to the supervisor's — see LAB FINDING 3.
    """
    from orientim import store
    root = os.path.join(RUNS, agent)
    out = {}
    # Newest wins. Oldest-wins pinned each case to the first time a scenario
    # ever ran, so a fix to the app changed the supervisor's recording and left
    # the child's pointing at the behaviour from before it.
    for meta in sorted(store.list_runs(root),
                       key=lambda m: m.get("started_at") or 0):
        scenario = (meta.get("tags") or {}).get("scenario")
        if scenario:
            out[scenario] = store.locator_for(root, meta["run_id"] + ".jsonl")
    return out


def cases():
    """Every recording becomes a case, with the rules its own agent can hold.

    One suite per agent, because evaluation is per agent. The supervisor's
    cases live beside its recordings; each child's live beside its own.
    """
    from orientim import cases as C
    recorded = json.load(open(os.path.join(RUNS, "_recorded.json"),
                              encoding="utf-8"))
    say("")
    made = 0
    for name, path in sorted(recorded.items()):
        C.save(name, path, "agents.entries:" + name.replace("-", "_"),
               root=SUP_RUNS,
               description=BY_NAME[name]["why"],
               expect=BY_NAME[name]["expect"],
               tags={"scenario": name, "agent": "supervisor"})
        made += 1
        say("  supervisor  %-26s %d rule(s)"
            % (name, len(BY_NAME[name]["expect"])))

    for agent in ("research", "support", "risk"):
        found = _child_recordings(agent)
        root = os.path.join(RUNS, agent)
        for name, scenario in sorted(BY_NAME.items()):
            expect = (scenario.get("agents") or {}).get(agent)
            if not expect or name not in found:
                continue
            C.save(name, found[name],
                   "agents.entries:%s_%s" % (agent, name.replace("-", "_")),
                   root=root, description=scenario["why"], expect=expect,
                   tags={"scenario": name, "agent": agent})
            made += 1
            say("  %-11s %-26s %d rule(s)" % (agent, name, len(expect)))
    say("")
    say("  %d case(s) across 4 agents" % made)
    return 0


# --- the driver ---------------------------------------------------------------

SUITES = [("supervisor", SUP_RUNS)] + [
    (a, os.path.join(RUNS, a)) for a in ("research", "support", "risk")]


def orientim(*args, root=None, env=None):
    e = _env(running_variant() or "v1")
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "orientim.cli",
                           "--root", root or SUP_RUNS] + list(args),
                          cwd=HERE, env=e, capture_output=True, text=True)


def task_env(name):
    """Left in place for `agents.supervisor:handle`, which reads LAB_TASK.

    The cases do not use it — see LAB FINDING 2 in agents/entries.py. A case
    cannot pass an argument to its entry point, so each scenario has an entry
    of its own with the task bound at definition time.
    """
    return {"LAB_TASK": json.dumps(dict(BY_NAME[name]["task"],
                                        scenario=name))}


def baseline(name="main"):
    """Freeze every agent's suite. Four baselines, one per agent."""
    worst = 0
    for agent, root in SUITES:
        r = orientim("baseline", "create", name, root=root)
        tail = [ln for ln in (r.stdout or r.stderr).splitlines()
                if "case(s)" in ln or "baseline" in ln]
        say("  %-11s %s" % (agent, " | ".join(t.strip() for t in tail[-2:])))
        worst = max(worst, r.returncode)
    return worst


def regress(only=None):
    """Restart the fleet under each variant and see what Orientim says."""
    wanted = [v for v in VARIANTS if only in (None, v["id"])]
    if not wanted:
        say("  no such variant: %s" % only)
        return 2
    report = []
    for v in wanted:
        say("\n" + "=" * 74)
        say("  %2d. %-16s %s" % (v["n"], v["id"], v["what"]))
        say("=" * 74)
        down(quiet=True)
        if up(v["id"], quiet=True) is None:
            say("  !! fleet did not start under %s" % v["id"])
            continue
        reset_effects()
        # One agent, one case per variant: enough to see it, short enough to run.
        agent, case = _case_for(v["id"])
        root = dict(SUITES)[agent]
        r = orientim("test", "--case", case, "--baseline", "main", root=root)
        say(r.stdout.rstrip() or r.stderr.rstrip())
        d = orientim("diff", "--case", case, root=root)
        say(d.stdout.rstrip() or d.stderr.rstrip())
        report.append({"variant": v["id"], "agent": agent, "case": case,
                       "test_exit": r.returncode,
                       "effects": effects()})
    say("\n" + "=" * 74)
    for row in report:
        say("  %-16s %-11s %-24s exit %d  effects %r"
            % (row["variant"], row["agent"], row["case"], row["test_exit"],
               row["effects"]))
    with open(os.path.join(RUNS, "_regressions.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 0


def _case_for(variant_id):
    """Which agent's suite and which scenario shows a regression most clearly.

    Per agent, because evaluation is per agent — see LAB FINDING 3. A change
    inside the risk agent is invisible in the supervisor's tool calls and
    obvious in the risk agent's own.
    """
    return {
        "model_change":   ("supervisor", "shipped-order"),
        "tool_change":    ("support",    "shipped-order"),
        "arg_change":     ("support",    "shipped-order"),
        "tool_unused":    ("risk",       "risk-must-run"),
        "forbidden_tool": ("support",    "no-refund-without-a-human"),
        "output_change":  ("support",    "shipped-order"),
        "new_call":       ("research",   "shipping-tracked"),
        "call_removed":   ("research",   "shipping-tracked"),
        "order_change":   ("research",   "shipping-tracked"),
        "parallel_order": ("supervisor", "parallel-children"),
    }.get(variant_id, ("supervisor", "shipped-order"))


def main(argv):
    cmd = argv[0] if argv else "help"
    if cmd == "up":
        down(quiet=True)
        say("")
        return 0 if up(argv[1] if len(argv) > 1 else "v1") else 2
    if cmd == "down":
        down()
        return 0
    if cmd == "record":
        return record()
    if cmd == "cases":
        return cases()
    if cmd == "baseline":
        return baseline(argv[1] if len(argv) > 1 else "main")
    if cmd == "regress":
        return regress(argv[1] if len(argv) > 1 else None)
    if cmd == "effects":
        say(json.dumps(effects()))
        return 0
    say(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
