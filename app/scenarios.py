"""The 4 demo scenarios from the product spec (section 9): easy payment,
negotiated payment plan, disputed invoice, and hardship. Each maps a seeded
customer to a scripted sequence of customer lines, run turn-by-turn against
CollectionsAgent by scripts/run_demo.py.

These scripts are written for --offline (the rule-based backend), whose
parsing is intentionally simple (see app/agent.py's _extract_amount /
_is_affirmative / _is_full_payment) — that's why the lines are fairly
explicit. In --llm mode the same scenarios run through Claude instead and
tolerate far more naturalistic phrasing.
"""

SCENARIOS = {
    "easy_payment": {
        "customer_name": "John Smith",
        "description": "Customer pays the full balance without any negotiation.",
        "turns": [
            "Yes, I can pay the full amount today.",
            "Yes, go ahead.",
            "Yes, confirmed.",
        ],
    },
    "payment_plan": {
        "customer_name": "Sarah Miller",
        "description": "Customer can't pay in full; negotiates down to the policy-minimum payment plan.",
        "turns": [
            "Yeah, I know about it. I just don't have $1,280 right now.",
            "I could probably do $400 today.",
            "Ok, I can do $640 then.",
            "Yes, that works.",
            "Yes, confirmed.",
        ],
    },
    "dispute": {
        "customer_name": "Mike Johnson",
        "description": "Customer disputes the invoice; agent stops negotiating and escalates.",
        "turns": [
            "I never authorized this invoice — I don't know what this is.",
        ],
    },
    "hardship": {
        "customer_name": "Alex Chen",
        "description": "Customer discloses hardship; agent offers support and escalates instead of pressuring for payment.",
        "turns": [
            "I've lost my job and can't make any payment right now.",
        ],
    },
}
