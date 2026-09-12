"""Run one (or all) of the 4 demo scenarios as a simulated text conversation
and print the transcript plus the structured "Agent Decision" panel.

Usage:
    python -m scripts.run_demo easy_payment
    python -m scripts.run_demo payment_plan --llm     # use Claude instead of the offline heuristic
    python -m scripts.run_demo all

Run scripts/init_db.py first to create and seed the local demo DB.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(__file__).parent.parent / ".env")

from app import db  # noqa: E402
from app.agent import CollectionsAgent  # noqa: E402
from app.scenarios import SCENARIOS  # noqa: E402


def find_customer(name: str) -> dict:
    conn = db.get_connection()
    cust = conn.execute("SELECT * FROM customers WHERE name = ?", (name,)).fetchone()
    if cust is None:
        conn.close()
        raise SystemExit(f"No customer named '{name}' — run `python -m scripts.init_db` first.")
    invoice = conn.execute(
        "SELECT * FROM invoices WHERE customer_id = ? ORDER BY created_at DESC LIMIT 1", (cust["id"],)
    ).fetchone()
    conn.close()
    return {"customer_id": cust["id"], "invoice_id": invoice["id"]}


def run_scenario(key: str, use_llm: bool) -> None:
    scenario = SCENARIOS[key]
    ref = find_customer(scenario["customer_name"])

    print("=" * 72)
    print(f"SCENARIO: {key}  —  {scenario['description']}")
    print(f"Customer: {scenario['customer_name']}  (backend: {'LLM' if use_llm else 'offline heuristic'})")
    print("=" * 72)

    agent = CollectionsAgent(customer_id=ref["customer_id"], invoice_id=ref["invoice_id"], use_llm=use_llm)

    print(f"\nAGENT: {agent.opening_line()}")

    for line in scenario["turns"]:
        print(f"CUSTOMER: {line}")
        result = agent.step(line)
        print(f"AGENT: {result.agent_message}")
        if result.escalated or result.ended:
            break

    decision = agent.finalize()

    print("\n--- Agent Decision " + "-" * 51)
    print(f"Outcome: {decision['outcome']}")
    print("\nWhy:")
    for r in decision["reasoning"]:
        print(f"  - {r}")
    print("\nPolicy checks:")
    for rule, check in decision["policy_checks"].items():
        mark = "✅" if check["result"] == "pass" else "❌"
        print(f"  {mark} {rule.replace('_', ' ')}: {check['detail']}")
    print(f"\nFinal state: {agent.sm.current.value}   (call_id={agent.call_id})")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=list(SCENARIOS.keys()) + ["all"])
    parser.add_argument("--llm", action="store_true", help="Use Claude instead of the offline heuristic backend.")
    args = parser.parse_args()

    keys = list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]
    for key in keys:
        run_scenario(key, use_llm=args.llm)


if __name__ == "__main__":
    main()
