# -*- coding: utf-8 -*-
"""The ten deliberate regressions, and what Orientim is expected to say.

Each is one environment variable away from v1, so the "code change" is real
without being a branch nobody would write. `catches` is the claim being tested:
if Orientim does not produce it, that is a finding about Orientim, not about
the lab.
"""

VARIANTS = [
    {
        "id": "model_change",
        "n": 1,
        "what": "every agent asks gpt-4o instead of gpt-4o-mini",
        "where": "agents/common.py:model_name",
        "catches": {
            "replay": "the request body differs, so the step diverges",
            "diff": "MODEL CONFIG — model: gpt-4o-mini -> gpt-4o",
            "evaluation": "unchanged: the rules are about tools and answers",
        },
    },
    {
        "id": "tool_change",
        "n": 2,
        "what": "support calls kb.search where it used to call order.lookup",
        "where": "agents/children.py:support",
        "catches": {
            "replay": "a request the recording does not contain",
            "diff": "TOOL DECISION — order.lookup no longer requested, "
                    "kb.search newly requested",
            "evaluation": "used_tool(order.lookup) FAILS",
        },
    },
    {
        "id": "arg_change",
        "n": 3,
        "what": "order.lookup is called with include_history=True",
        "where": "agents/children.py:support",
        "catches": {
            "replay": "same URL, different request bytes",
            "diff": "TOOL DECISION — order.lookup arguments changed",
            "evaluation": "unchanged: the tool is still used",
        },
    },
    {
        "id": "tool_unused",
        "n": 4,
        "what": "risk stops calling risk.score — the safety check goes quiet",
        "where": "agents/children.py:risk",
        "catches": {
            "replay": "fewer steps than recorded",
            "diff": "TOOL DECISION — risk.score no longer requested",
            "evaluation": "used_tool(risk.score) FAILS",
        },
    },
    {
        "id": "forbidden_tool",
        "n": 5,
        "what": "support issues a refund on its own",
        "where": "agents/children.py:support",
        "catches": {
            "replay": "a request the recording does not contain",
            "diff": "TOOL DECISION — refund.issue newly requested",
            "evaluation": "did_not_call(refund.issue) FAILS, with the "
                          "arguments as evidence",
        },
    },
    {
        "id": "output_change",
        "n": 6,
        "what": "support answers in a formal template",
        "where": "agents/children.py:support",
        "catches": {
            "replay": "the prompt differs, so the model step diverges",
            "diff": "OUTPUT — expected/actual, with digests",
            "evaluation": "output_matches FAILS where a scenario pins the text",
        },
    },
    {
        "id": "new_call",
        "n": 7,
        "what": "research adds an order.lookup it did not make before",
        "where": "agents/children.py:research",
        "catches": {
            "replay": "NEW_CALL — every recorded step matched, then a new one",
            "diff": "one INSERTED step, and the later steps still SAME",
            "evaluation": "max_steps may fail if the budget is tight",
        },
    },
    {
        "id": "call_removed",
        "n": 8,
        "what": "research stops tracking the parcel",
        "where": "agents/children.py:research",
        "catches": {
            "replay": "FEWER_STEPS",
            "diff": "one DELETED step, the rest SAME",
            "evaluation": "used_tool(shipping.track) FAILS",
        },
    },
    {
        "id": "order_change",
        "n": 9,
        "what": "research tracks first and reads the article second",
        "where": "agents/children.py:research",
        "catches": {
            "replay": "a divergence at the first swapped step",
            "diff": "REORDERED — one move, not two changes",
            "evaluation": "unchanged: both tools are still used",
        },
    },
    {
        "id": "parallel_order",
        "n": 10,
        "what": "the supervisor starts risk before research",
        "where": "agents/supervisor.py:run_task",
        "catches": {
            "replay": "the two delegations arrive in the other order",
            "diff": "REORDERED across the parallel pair",
            "evaluation": "unchanged: both children still run",
        },
    },
]

BY_ID = {v["id"]: v for v in VARIANTS}
