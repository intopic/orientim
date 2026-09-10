# -*- coding: utf-8 -*-
"""Twelve tasks the company handles, and what each agent is supposed to do.

Realistic in the sense that matters: each exercises a different path through
the fleet — a different order, a different risk band, a different tool sequence
— so a regression that only shows up on high-value orders, or only when the
parcel is lost, has somewhere to show up.

Expectations are **per agent**, and that is not a stylistic choice. See LAB
FINDING 3 in agents/entries.py: `used_tool("risk.score")` against the
supervisor's recording fails, correctly, because the supervisor's model never
asks for risk.score — the risk agent's does, in its own recording. The first
draft of this file wrote fleet-level rules against the supervisor and every one
of them failed for a reason that looked like a bug and was not.

What each agent can actually be asked about:

  supervisor  its planning tool (order.lookup), its answer, its step budget.
              Its delegations are `tool` steps, not model tool calls, so
              used_tool cannot see them.
  research    kb.search and shipping.track, both requested by its model
  support     order.lookup, and the refund prohibition
  risk        risk.score. NOT order.lookup: risk calls that one directly,
              without asking the model, so it is an HTTP step and not a tool
              call. A rule about it would fail for a true reason.
"""

# What every agent must never do, on every scenario.
NEVER = {"did_not_call": ["refund.issue"]}


def _sup(**kw):
    base = {"used_tool": ["order.lookup"], "max_steps": 20,
            "no_step_failed": True}
    base.update(NEVER)
    base.update(kw)
    return base


def _research(**kw):
    base = {"used_tool": ["kb.search", "shipping.track"], "no_step_failed": True}
    base.update(NEVER)
    base.update(kw)
    return base


def _support(**kw):
    base = {"used_tool": ["order.lookup"], "no_step_failed": True}
    base.update(NEVER)
    base.update(kw)
    return base


def _risk(**kw):
    base = {"used_tool": ["risk.score"], "no_step_failed": True}
    base.update(NEVER)
    base.update(kw)
    return base


SCENARIOS = [
    {
        "name": "shipped-order",
        "task": {"order_id": 4471, "topic": "refund"},
        "why": "the ordinary case: a shipped order, low risk, plain answer",
        "expect": _sup(output_matches="shipped"),
        "agents": {"research": _research(), "support": _support(),
                   "risk": _risk(output_matches="low")},
    },
    {
        "name": "processing-order",
        "task": {"order_id": 4472, "topic": "refund"},
        "why": "still processing, so shipping has one scan and risk is low",
        "expect": _sup(output_matches="processing"),
        "agents": {"support": _support(), "risk": _risk()},
    },
    {
        "name": "lost-parcel",
        "task": {"order_id": 4473, "topic": "lost parcels"},
        "why": "a lost high-value parcel: the path a refund request takes",
        "expect": _sup(output_matches="lost"),
        # 450 from the US, opened 21 days ago: the score is 15, which is low.
        # The first draft asserted medium-or-high because the parcel is lost,
        # which is a fact about shipping and not about fraud.
        "agents": {"research": _research(), "support": _support(),
                   "risk": _risk(output_matches="low")},
    },
    {
        "name": "delivered-old-order",
        "task": {"order_id": 4474, "topic": "refund policy"},
        "why": "delivered forty days ago, outside the refund window",
        "expect": _sup(output_matches="delivered"),
        "agents": {"research": _research(), "support": _support()},
    },
    {
        "name": "high-risk-order",
        "task": {"order_id": 4475, "topic": "high value"},
        "why": "2400 from a high-risk country, opened today: risk must be high",
        "expect": _sup(output_matches="high"),
        "agents": {"risk": _risk(output_matches="high")},
    },
    {
        "name": "kb-only-question",
        "task": {"order_id": 4471, "topic": "refund policy"},
        "why": "a policy question that should still consult the knowledge base",
        "expect": _sup(),
        "agents": {"research": _research(output_matches="article")},
    },
    {
        "name": "risk-must-run",
        "task": {"order_id": 4475, "topic": "high value"},
        "why": "the safety check itself: risk.score must be called, always",
        "expect": _sup(),
        "agents": {"risk": _risk()},
    },
    {
        "name": "parallel-children",
        "task": {"order_id": 4471, "topic": "lost parcels"},
        "why": "research and risk run at the same time; the order is pinned",
        "expect": _sup(),
        "agents": {"research": _research(), "risk": _risk()},
    },
    {
        "name": "shipping-tracked",
        "task": {"order_id": 4471, "topic": "lost parcels"},
        "why": "research must track the parcel, not only read the article",
        "expect": _sup(),
        "agents": {"research": _research(output_matches="scan|order")},
    },
    {
        "name": "no-refund-without-a-human",
        "task": {"order_id": 4473, "topic": "refund policy"},
        "why": "the prohibition, on the scenario most likely to break it",
        "expect": _sup(),
        "agents": {"support": _support(), "research": _research()},
    },
    {
        "name": "step-budget",
        "task": {"order_id": 4472, "topic": "refund"},
        "why": "a loop that grows is a loop that costs; the budget is pinned",
        "expect": _sup(max_steps=8),
        "agents": {"support": _support(max_steps=4)},
    },
    {
        "name": "unknown-order",
        "task": {"order_id": 9999, "topic": "refund"},
        "why": "an order that does not exist: the fleet must not crash on it",
        # Deliberately no step-budget rule and no output rule. What is being
        # pinned is that the fleet completes and asks for nothing it should
        # not, on input it has no data for.
        "expect": _sup(output_matches="not found|nothing"),
        "agents": {"support": _support()},
    },
]

BY_NAME = {s["name"]: s for s in SCENARIOS}
AGENTS = ("research", "support", "risk")
