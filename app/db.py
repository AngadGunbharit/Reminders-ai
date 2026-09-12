"""SQLite-backed mock database for local dev/demo.

Mirrors db/schema_sqlite.sql, itself a dialect mirror of the canonical
Postgres schema in db/schema.sql. Swapping this module for a psycopg/
SQLAlchemy implementation against real Postgres is the only change needed
to move from demo to production — app/tools.py and app/agent.py don't
know or care which one is underneath.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "payflow.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema_sqlite.sql"


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(reset: bool = False) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with open(SCHEMA_PATH) as f:
        schema = f.read()
    conn = get_connection()
    conn.executescript(schema)
    conn.commit()
    conn.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


# ─────────────────────────────────────────────────────────────────────────
# Generic insert helper
# ─────────────────────────────────────────────────────────────────────────

def insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> str:
    row = dict(row)
    row.setdefault("id", new_id())
    cols = ", ".join(row.keys())
    placeholders = ", ".join(["?"] * len(row))
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(row.values()))
    return row["id"]


def json_dump(value: Any) -> str:
    return json.dumps(value, default=str)
