-- PayFlow AI — local dev/demo mirror of db/schema.sql, SQLite dialect.
-- Used by scripts/init_db.py so the agent runs with zero external
-- dependencies. IDs are TEXT (uuid4 hex generated in Python), timestamps
-- are TEXT (ISO-8601), JSONB becomes TEXT (json-encoded).
-- Keep in sync with db/schema.sql when columns change.

PRAGMA foreign_keys = ON;

CREATE TABLE customers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT,
    phone           TEXT,
    payment_history TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (payment_history IN ('good', 'fair', 'poor', 'unknown')),
    created_at      TEXT NOT NULL
);

CREATE TABLE invoices (
    id                  TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    original_amount     REAL NOT NULL,
    late_fee            REAL NOT NULL DEFAULT 0,
    balance             REAL NOT NULL,
    due_date            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'partial', 'paid', 'disputed', 'escalated', 'closed')),
    previous_attempts   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE INDEX idx_invoices_customer ON invoices(customer_id);
CREATE INDEX idx_invoices_status ON invoices(status);

CREATE TABLE calls (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    invoice_id      TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'in_progress'
                        CHECK (status IN ('in_progress', 'completed', 'escalated', 'failed')),
    outcome         TEXT,
    final_state     TEXT,
    summary         TEXT,
    policy_version  TEXT NOT NULL DEFAULT '1.0'
);

CREATE INDEX idx_calls_customer ON calls(customer_id);

CREATE TABLE conversation_turns (
    id          TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_index  INTEGER NOT NULL,
    speaker     TEXT NOT NULL CHECK (speaker IN ('agent', 'customer', 'system')),
    message     TEXT NOT NULL,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_turns_call ON conversation_turns(call_id, turn_index);

CREATE TABLE tool_calls (
    id          TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    tool_name   TEXT NOT NULL,
    arguments   TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_tool_calls_call ON tool_calls(call_id);

CREATE TABLE agent_decisions (
    id              TEXT PRIMARY KEY,
    call_id         TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    outcome         TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    policy_checks   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE payment_plans (
    id                  TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    invoice_id          TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    call_id             TEXT REFERENCES calls(id) ON DELETE SET NULL,
    total_amount        REAL NOT NULL,
    installment_count   INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'proposed'
                            CHECK (status IN ('proposed', 'active', 'completed', 'failed', 'cancelled')),
    created_at          TEXT NOT NULL
);

CREATE TABLE payment_plan_installments (
    id                  TEXT PRIMARY KEY,
    payment_plan_id     TEXT NOT NULL REFERENCES payment_plans(id) ON DELETE CASCADE,
    sequence            INTEGER NOT NULL,
    amount              REAL NOT NULL,
    due_date            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'paid', 'failed')),
    paid_at             TEXT
);

CREATE INDEX idx_installments_plan ON payment_plan_installments(payment_plan_id);

CREATE TABLE payments (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    invoice_id      TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    installment_id  TEXT REFERENCES payment_plan_installments(id) ON DELETE SET NULL,
    call_id         TEXT REFERENCES calls(id) ON DELETE SET NULL,
    amount          REAL NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('simulated_success', 'simulated_failed')),
    method          TEXT NOT NULL DEFAULT 'mock_card',
    created_at      TEXT NOT NULL
);

CREATE TABLE escalations (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    call_id         TEXT REFERENCES calls(id) ON DELETE SET NULL,
    reason          TEXT NOT NULL
                        CHECK (reason IN ('dispute', 'hardship', 'legal_threat', 'manager_request',
                                           'identity_verification_failed', 'angry_customer',
                                           'payment_failed', 'other')),
    priority        TEXT NOT NULL DEFAULT 'medium'
                        CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'assigned', 'resolved')),
    assigned_to     TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE INDEX idx_escalations_status ON escalations(status);

CREATE TABLE account_notes (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    call_id     TEXT REFERENCES calls(id) ON DELETE SET NULL,
    author      TEXT NOT NULL DEFAULT 'Ava (AI agent)',
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE policy_versions (
    id          TEXT PRIMARY KEY,
    version     TEXT NOT NULL UNIQUE,
    config      TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
