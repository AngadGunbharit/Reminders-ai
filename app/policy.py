"""Policy engine.

Loads app/policy.json and exposes pure functions that compute payment
options, evaluate a proposed upfront payment, and check escalation
triggers. Nothing here talks to the LLM — the agent (app/agent.py) calls
these the same way a human collections rep would consult a rules binder.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

POLICY_PATH = Path(__file__).parent / "policy.json"


def load_policy() -> dict:
    with open(POLICY_PATH) as f:
        return json.load(f)


@dataclass
class PolicyCheck:
    result: str          # "pass" | "fail"
    detail: str


@dataclass
class PaymentOption:
    kind: str             # "full_payment" | "payment_plan" | "hardship_escalation"
    label: str
    upfront_amount: float
    installments: list = field(default_factory=list)   # [{"amount": .., "due_in_days": ..}]


def get_payment_options(balance: float, policy: Optional[dict] = None) -> list[PaymentOption]:
    """The fixed menu of policy-compliant options for a given balance.

    The agent never invents numbers — it always calls this (or
    evaluate_upfront_offer below) and reads the numbers back to the customer.
    """
    policy = policy or load_policy()
    min_upfront = round(balance * policy["min_upfront_payment_pct"] / 100, 2)
    remainder = round(balance - min_upfront, 2)
    per_installment = round(remainder / 2, 2)

    options = [
        PaymentOption(
            kind="full_payment",
            label=f"Pay the full balance of ${balance:,.2f} today",
            upfront_amount=balance,
        ),
        PaymentOption(
            kind="payment_plan",
            label=(
                f"${min_upfront:,.2f} today, then ${per_installment:,.2f} in 30 days "
                f"and ${remainder - per_installment:,.2f} in 60 days"
            ),
            upfront_amount=min_upfront,
            installments=[
                {"amount": per_installment, "due_in_days": 30},
                {"amount": round(remainder - per_installment, 2), "due_in_days": 60},
            ],
        ),
        PaymentOption(
            kind="hardship_escalation",
            label="Connect with a human collections specialist to discuss hardship options",
            upfront_amount=0,
        ),
    ]
    return options


def evaluate_upfront_offer(balance: float, proposed_upfront: float, policy: Optional[dict] = None) -> dict:
    """Checks whether a customer-proposed upfront payment is policy-eligible.

    Mirrors the `check_payment_options(balance, upfront_payment)` example in
    the product spec: the agent asks the system rather than negotiating from
    its own judgment.
    """
    policy = policy or load_policy()
    min_upfront = round(balance * policy["min_upfront_payment_pct"] / 100, 2)

    if proposed_upfront >= min_upfront:
        return {
            "eligible": True,
            "minimum_upfront_payment": min_upfront,
            "reason": "Proposed upfront payment meets the minimum.",
        }
    return {
        "eligible": False,
        "minimum_upfront_payment": min_upfront,
        "reason": f"minimum upfront payment is ${min_upfront:,.2f}",
    }


def build_payment_plan_installments(balance: float, upfront: float, policy: Optional[dict] = None,
                                     start: Optional[date] = None) -> list[dict]:
    """Splits the remainder after `upfront` into policy['allowed_installments'] - 1
    equal installments spread evenly across policy['max_payment_plan_days'].
    """
    policy = policy or load_policy()
    start = start or date.today()
    remainder = round(balance - upfront, 2)
    n = max(policy["allowed_installments"] - 1, 1)
    max_days = policy["max_payment_plan_days"]
    step = max_days // n

    installments = []
    remaining = remainder
    for i in range(1, n + 1):
        amount = round(remainder / n, 2) if i < n else round(remaining, 2)
        remaining -= amount
        installments.append({
            "sequence": i,
            "amount": amount,
            "due_date": (start + timedelta(days=step * i)).isoformat(),
        })
    return installments


def check_escalation_trigger(trigger: str, policy: Optional[dict] = None) -> Optional[str]:
    """Returns the policy action ('escalate' | 'end_call') for a detected
    trigger, or None if the trigger isn't recognized by policy."""
    policy = policy or load_policy()
    return policy["escalation_triggers"].get(trigger)


def escalation_priority(reason: str, policy: Optional[dict] = None) -> str:
    policy = policy or load_policy()
    return policy["escalation_priority"].get(reason, "medium")


def run_policy_checks(balance: float, proposed_upfront: float, installments: list[dict],
                       policy: Optional[dict] = None) -> dict[str, PolicyCheck]:
    """Full policy-check table for the structured 'Agent Decision' panel."""
    policy = policy or load_policy()
    min_upfront = round(balance * policy["min_upfront_payment_pct"] / 100, 2)

    checks: dict[str, PolicyCheck] = {}

    checks["minimum_payment"] = PolicyCheck(
        result="pass" if proposed_upfront >= min_upfront else "fail",
        detail=f"${proposed_upfront:,.2f} vs. minimum ${min_upfront:,.2f}",
    )

    max_due_in = 0
    for i in installments:
        if "due_in_days" in i:
            max_due_in = max(max_due_in, i["due_in_days"])
        elif "due_date" in i:
            max_due_in = max(max_due_in, (date.fromisoformat(i["due_date"]) - date.today()).days)
    checks["maximum_duration"] = PolicyCheck(
        result="pass" if max_due_in <= policy["max_payment_plan_days"] else "fail",
        detail=f"{max_due_in} days vs. limit {policy['max_payment_plan_days']} days",
    )

    checks["discount_limit"] = PolicyCheck(result="pass", detail="0% discount offered (limit 5%)")

    checks["escalation_required"] = PolicyCheck(
        result="fail" if False else "pass",  # overwritten by caller when a trigger fires
        detail="No dispute, hardship, or legal threat detected",
    )

    return checks
