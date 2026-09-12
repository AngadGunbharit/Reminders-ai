"""Tool definitions in Anthropic tool-use JSON schema format.

The agent never invents customer data, payment numbers, or account
actions — every fact and every action goes through one of these. See
app/tools.py for the implementations and app/state_machine.py for which
of these are offered to the model in a given state.
"""

TOOLS = [
    {
        "name": "get_customer",
        "description": (
            "Look up a customer's profile and current invoice: name, balance owed, "
            "days overdue, number of previous collection attempts, and payment "
            "history rating. Always call this before discussing any balance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer's unique ID."},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "get_payment_options",
        "description": (
            "Get the policy-approved menu of payment options for this customer's "
            "balance. If the customer has proposed a specific upfront amount, pass "
            "proposed_upfront to check whether it's eligible under policy — do not "
            "judge this yourself, always ask the system. Never state a dollar figure "
            "to the customer that didn't come from this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "proposed_upfront": {
                    "type": "number",
                    "description": "An upfront payment amount the customer proposed, if any.",
                },
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "make_payment",
        "description": (
            "Simulate collecting an immediate payment from the customer. This is a "
            "SIMULATION — no real money moves. Only call this after the customer has "
            "explicitly agreed to pay a specific amount right now."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["customer_id", "amount"],
        },
    },
    {
        "name": "schedule_payment",
        "description": (
            "Schedule a future installment payment as part of an agreed payment "
            "plan. Only call this after the customer has explicitly agreed to the "
            "specific amount and date, and those numbers came from get_payment_options."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount": {"type": "number"},
                "date": {"type": "string", "description": "ISO-8601 date, e.g. 2026-10-12."},
            },
            "required": ["customer_id", "amount", "date"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Hand the case off to a human collections specialist and stop "
            "negotiating. REQUIRED — do not keep negotiating — whenever the customer: "
            "disputes owing the invoice, describes a hardship (job loss, medical, "
            "no income), threatens legal action, asks for a manager/human, or becomes "
            "hostile/abusive. Do not argue with a dispute or hardship claim; just "
            "acknowledge it and escalate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "enum": [
                        "dispute", "hardship", "legal_threat", "manager_request",
                        "identity_verification_failed", "angry_customer",
                        "payment_failed", "other",
                    ],
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "description": "Leave unset to use the policy-default priority for this reason.",
                },
                "notes": {"type": "string", "description": "Brief context for the human specialist."},
            },
            "required": ["customer_id", "reason"],
        },
    },
    {
        "name": "add_account_note",
        "description": "Append a short note to the customer's account record.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["customer_id", "note"],
        },
    },
    {
        "name": "advance_state",
        "description": (
            "Move the call forward to the next step of the state machine once the "
            "current step's goal is met. Call this as many times as needed in one turn "
            "to chain through states — for example, if the customer's message satisfies "
            "both DISCUSS_BALANCE and UNDERSTAND_SITUATION, call advance_state twice "
            "before calling get_payment_options. You cannot skip steps, but you can "
            "chain multiple advances in one turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "One short phrase for why this step is complete."},
            },
            "required": ["reason"],
        },
    },
]


def tools_for_state(tool_names: list[str]) -> list[dict]:
    by_name = {t["name"]: t for t in TOOLS}
    return [by_name[n] for n in tool_names if n in by_name]
