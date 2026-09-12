"""Tool implementations.

Each function mirrors one entry in app/tool_schemas.py. They're the only
way the agent touches data or takes action — see the module docstring in
tool_schemas.py. All DB access goes through app/db.py; all money math goes
through app/policy.py. Nothing here calls the LLM.

Every function takes `call_id` (to scope the action to the invoice being
discussed on this call and to attribute notes/escalations/payments back to
the call) plus the tool's declared arguments, and returns a JSON-serializable
dict — exactly what gets handed back to the model as the tool result.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from . import db
from . import policy as policy_engine


class ToolError(Exception):
    """Raised for invalid tool calls (unknown customer, etc.). Caught by the
    orchestrator and turned into a tool_result the model can react to,
    rather than crashing the call."""


def _get_call(conn, call_id: str) -> dict:
    row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        raise ToolError(f"No such call: {call_id}")
    return dict(row)


def _get_invoice(conn, invoice_id: str) -> dict:
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise ToolError(f"No such invoice: {invoice_id}")
    return dict(row)


def get_customer(call_id: str, customer_id: str) -> dict[str, Any]:
    with db.tx() as conn:
        call = _get_call(conn, call_id)
        cust = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if cust is None:
            raise ToolError(f"No such customer: {customer_id}")
        invoice = _get_invoice(conn, call["invoice_id"])

        days_overdue = (date.today() - date.fromisoformat(invoice["due_date"])).days

        return {
            "customer_id": customer_id,
            "name": cust["name"],
            "balance": invoice["balance"],
            "original_amount": invoice["original_amount"],
            "late_fee": invoice["late_fee"],
            "days_overdue": days_overdue,
            "previous_attempts": invoice["previous_attempts"],
            "payment_history": cust["payment_history"],
            "invoice_status": invoice["status"],
        }


def get_payment_options(call_id: str, customer_id: str, proposed_upfront: Optional[float] = None) -> dict[str, Any]:
    with db.tx() as conn:
        call = _get_call(conn, call_id)
        invoice = _get_invoice(conn, call["invoice_id"])
        pol = policy_engine.load_policy()
        balance = invoice["balance"]

        options = policy_engine.get_payment_options(balance, pol)
        result: dict[str, Any] = {
            "balance": balance,
            "options": [
                {"kind": o.kind, "label": o.label, "upfront_amount": o.upfront_amount, "installments": o.installments}
                for o in options
            ],
        }

        if proposed_upfront is not None:
            result["proposed_upfront_evaluation"] = policy_engine.evaluate_upfront_offer(balance, proposed_upfront, pol)

        return result


def make_payment(call_id: str, customer_id: str, amount: float) -> dict[str, Any]:
    """Simulated payment capture — no real money moves. Fails (deterministically)
    only if the amount is non-positive or exceeds the current balance; otherwise
    always succeeds, since this is a demo/mock payment rail."""
    with db.tx() as conn:
        call = _get_call(conn, call_id)
        invoice = _get_invoice(conn, call["invoice_id"])

        if amount <= 0:
            return {"status": "simulated_failed", "reason": "Amount must be positive."}
        if amount > invoice["balance"] + 0.01:
            return {"status": "simulated_failed", "reason": f"Amount exceeds balance of ${invoice['balance']:,.2f}."}

        db.insert(conn, "payments", {
            "customer_id": customer_id,
            "invoice_id": invoice["id"],
            "call_id": call_id,
            "amount": amount,
            "status": "simulated_success",
            "method": "mock_card",
            "created_at": db.now(),
        })

        new_balance = round(invoice["balance"] - amount, 2)
        new_status = "paid" if new_balance <= 0.01 else "partial"
        conn.execute("UPDATE invoices SET balance = ?, status = ? WHERE id = ?", (new_balance, new_status, invoice["id"]))

        return {
            "status": "simulated_success",
            "amount_charged": amount,
            "remaining_balance": new_balance,
            "invoice_status": new_status,
        }


def schedule_payment(call_id: str, customer_id: str, amount: float, date_str: str) -> dict[str, Any]:
    with db.tx() as conn:
        call = _get_call(conn, call_id)
        invoice = _get_invoice(conn, call["invoice_id"])

        plan_row = conn.execute(
            "SELECT * FROM payment_plans WHERE call_id = ? AND status IN ('proposed','active')",
            (call_id,),
        ).fetchone()

        if plan_row is None:
            plan_id = db.insert(conn, "payment_plans", {
                "customer_id": customer_id,
                "invoice_id": invoice["id"],
                "call_id": call_id,
                "total_amount": invoice["balance"],
                "installment_count": 1,
                "status": "active",
                "created_at": db.now(),
            })
        else:
            plan_id = plan_row["id"]
            conn.execute(
                "UPDATE payment_plans SET installment_count = installment_count + 1 WHERE id = ?",
                (plan_id,),
            )

        seq_row = conn.execute(
            "SELECT COUNT(*) AS n FROM payment_plan_installments WHERE payment_plan_id = ?", (plan_id,)
        ).fetchone()
        sequence = seq_row["n"] + 1

        installment_id = db.insert(conn, "payment_plan_installments", {
            "payment_plan_id": plan_id,
            "sequence": sequence,
            "amount": amount,
            "due_date": date_str,
            "status": "pending",
        })

        return {
            "status": "scheduled",
            "payment_plan_id": plan_id,
            "installment_id": installment_id,
            "amount": amount,
            "due_date": date_str,
        }


def escalate_to_human(call_id: str, customer_id: str, reason: str, priority: Optional[str] = None,
                       notes: Optional[str] = None) -> dict[str, Any]:
    pol = policy_engine.load_policy()
    resolved_priority = priority or policy_engine.escalation_priority(reason, pol)

    with db.tx() as conn:
        escalation_id = db.insert(conn, "escalations", {
            "customer_id": customer_id,
            "call_id": call_id,
            "reason": reason,
            "priority": resolved_priority,
            "status": "open",
            "created_at": db.now(),
        })
        if notes:
            db.insert(conn, "account_notes", {
                "customer_id": customer_id,
                "call_id": call_id,
                "author": "Ava (AI agent)",
                "note": f"[Escalation: {reason}] {notes}",
                "created_at": db.now(),
            })
        if reason == "dispute":
            call = _get_call(conn, call_id)
            conn.execute("UPDATE invoices SET status = 'disputed' WHERE id = ?", (call["invoice_id"],))

    return {
        "status": "escalated",
        "escalation_id": escalation_id,
        "reason": reason,
        "priority": resolved_priority,
    }


def add_account_note(call_id: str, customer_id: str, note: str) -> dict[str, Any]:
    with db.tx() as conn:
        note_id = db.insert(conn, "account_notes", {
            "customer_id": customer_id,
            "call_id": call_id,
            "author": "Ava (AI agent)",
            "note": note,
            "created_at": db.now(),
        })
    return {"status": "saved", "note_id": note_id}


# Dispatch table used by the orchestrator. `advance_state` is handled
# directly by app/agent.py since it mutates the state machine, not the DB.
DISPATCH = {
    "get_customer": get_customer,
    "get_payment_options": get_payment_options,
    "make_payment": make_payment,
    "schedule_payment": lambda call_id, customer_id, amount, date: schedule_payment(call_id, customer_id, amount, date),
    "escalate_to_human": escalate_to_human,
    "add_account_note": add_account_note,
}


def invoke(tool_name: str, call_id: str, args: dict) -> dict:
    fn = DISPATCH.get(tool_name)
    if fn is None:
        raise ToolError(f"Unknown tool: {tool_name}")
    try:
        return fn(call_id=call_id, **args)
    except TypeError as e:
        raise ToolError(f"Bad arguments for {tool_name}: {e}")
