# -*- coding: utf-8 -*-
"""One entry point per scenario.

LAB FINDING 2 — a case cannot carry its own input.

A case stores a recording, an entry point and its expectations. The entry point
is called as `fn(run)` and there is no way to hand it parameters, so twelve
scenarios that differ only by which order they ask about cannot share one
function. Passing the task through an environment variable works for
`orientim test --case X`, one at a time, and breaks the moment the whole suite
runs in one process: every case gets whichever task was set last.

The recording already knows the task — it is in the recorded request bodies —
so the information exists; there is just no channel from the case to the entry.

The workaround, and what a real project would end up doing: bind the argument
at definition time, one function per case. Generated here rather than written
out twelve times, because twelve near-identical functions is how the workaround
starts to cost something.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.environ.get("LAB_REPO", ".")))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from agents import children
from agents.supervisor import run_task
from scenarios import SCENARIOS

# LAB FINDING 3 — evaluation is per agent; there is no fleet-level view.
#
# `used_tool("risk.score")` against the supervisor's recording fails, and it is
# right to: the supervisor's model never asks for risk.score. The *risk agent's*
# model does, in the risk agent's own recording, which the supervisor's does not
# contain and cannot reach. Nothing in Orientim says "these four recordings are
# one execution of one fleet".
#
# So fleet coverage means a case per agent. That is not unreasonable — each
# agent is separately deployable and separately testable — but it has to be
# said, because the natural first attempt is to write fleet-level rules against
# the supervisor and watch them all fail for a reason that looks like a bug.


def _supervisor_entry(scenario):
    task = dict(scenario["task"], scenario=scenario["name"])

    def entry(run):
        out = run_task(run, task)
        run.output = json.dumps(out, sort_keys=True)
        return run.output

    entry.__name__ = scenario["name"].replace("-", "_")
    entry.__doc__ = scenario["why"]
    return entry


def _child_entry(agent, scenario):
    fn = children.AGENTS[agent]
    task = dict(scenario["task"], scenario=scenario["name"])

    def entry(run):
        out = fn(run, task)
        run.output = json.dumps(out, sort_keys=True)
        return run.output

    entry.__name__ = "%s_%s" % (agent, scenario["name"].replace("-", "_"))
    entry.__doc__ = "%s, for %s" % (agent, scenario["name"])
    return entry


for _s in SCENARIOS:
    globals()[_s["name"].replace("-", "_")] = _supervisor_entry(_s)
    for _a in ("research", "support", "risk"):
        name = "%s_%s" % (_a, _s["name"].replace("-", "_"))
        globals()[name] = _child_entry(_a, _s)


def entry_name(scenario_name, agent=None):
    """`agents.entries:shipped_order`, or `agents.entries:risk_shipped_order`."""
    base = scenario_name.replace("-", "_")
    return "agents.entries:" + (("%s_%s" % (agent, base)) if agent else base)
