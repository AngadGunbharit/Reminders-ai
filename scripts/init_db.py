"""Creates the local SQLite demo DB and seeds it with the customers used in
the product spec's dashboard mockup (Sarah Miller, John Smith, Mike
Johnson) plus their overdue invoices.

Usage:
    python -m scripts.init_db          # create if missing
    python -m scripts.init_db --reset  # wipe and recreate
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db  # noqa: E402


SEED_CUSTOMERS = [
    {
        "name": "Sarah Miller",
        "email": "sarah.miller@example.com",
        "phone": "+1-555-0101",
        "payment_history": "good",
        "invoice": {"original_amount": 1240.00, "late_fee": 40.00, "balance": 1280.00,
                    "days_overdue": 42, "previous_attempts": 2},
    },
    {
        "name": "John Smith",
        "email": "john.smith@example.com",
        "phone": "+1-555-0102",
        "payment_history": "fair",
        "invoice": {"original_amount": 3300.00, "late_fee": 100.00, "balance": 3400.00,
                    "days_overdue": 58, "previous_attempts": 1},
    },
    {
        "name": "Mike Johnson",
        "email": "mike.johnson@example.com",
        "phone": "+1-555-0103",
        "payment_history": "poor",
        "invoice": {"original_amount": 780.00, "late_fee": 40.00, "balance": 820.00,
                    "days_overdue": 75, "previous_attempts": 3},
    },
    {
        "name": "Alex Chen",
        "email": "alex.chen@example.com",
        "phone": "+1-555-0104",
        "payment_history": "good",
        "invoice": {"original_amount": 1950.00, "late_fee": 60.00, "balance": 2010.00,
                    "days_overdue": 35, "previous_attempts": 1},
    },
]


def seed(conn) -> dict[str, dict[str, str]]:
    """Returns {customer_name: {"customer_id": ..., "invoice_id": ...}}"""
    ids = {}
    for c in SEED_CUSTOMERS:
        customer_id = db.insert(conn, "customers", {
            "name": c["name"],
            "email": c["email"],
            "phone": c["phone"],
            "payment_history": c["payment_history"],
            "created_at": db.now(),
        })
        inv = c["invoice"]
        due_date = (date.today() - timedelta(days=inv["days_overdue"])).isoformat()
        invoice_id = db.insert(conn, "invoices", {
            "customer_id": customer_id,
            "original_amount": inv["original_amount"],
            "late_fee": inv["late_fee"],
            "balance": inv["balance"],
            "due_date": due_date,
            "status": "open",
            "previous_attempts": inv["previous_attempts"],
            "created_at": db.now(),
        })
        ids[c["name"]] = {"customer_id": customer_id, "invoice_id": invoice_id}
    return ids


def main() -> None:
    reset = "--reset" in sys.argv
    db.init_db(reset=reset)
    conn = db.get_connection()
    existing = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    if existing > 0 and not reset:
        print(f"DB already seeded ({existing} customers). Use --reset to start over.")
        conn.close()
        return
    if reset:
        conn.executescript("DELETE FROM customers;")  # cascades via FK
    ids = seed(conn)
    conn.commit()
    conn.close()

    print(f"Initialized {db.DB_PATH}")
    for name, ref in ids.items():
        print(f"  {name}: customer_id={ref['customer_id']}  invoice_id={ref['invoice_id']}")


if __name__ == "__main__":
    main()
