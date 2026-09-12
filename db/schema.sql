-- PayFlow AI — canonical production schema (PostgreSQL)
-- Milestone 1: Agent brain
--
-- Local dev/demo uses db/schema_sqlite.sql (a mirror of this file, SQLite
-- dialect) via scripts/init_db.py so the agent runs without a Postgres
-- instance. Keep the two in sync when columns change.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

-- ─────────────────────────────────────────────────────────────────────────
-- Customers & invoices
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE customers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    email           TEXT,
    phone           TEXT,
    payment_history TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (payment_history IN ('good', 'fair', 'poor', 'unknown')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE invoices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    original_amount NUMERIC(12,2) NOT NULL,
    late_fee        NUMERIC(12,2) NOT NULL DEFAULT 0,
    balance         NUMERIC(12,2) NOT NULL,          -- current amount owed
    due_date        DATE NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'partial', 'paid', 'disputed', 'escalated', 'closed')),
    previous_attempts INTEGER NOT NULL DEFAULT 0,     -- prior collection calls made
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_invoices_customer ON invoices(customer_id);
CREATE INDEX idx_invoices_status ON invoices(status);

-- ─────────────────────────────────────────────────────────────────────────
-- Calls & transcripts
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE calls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'in_progress'
                        CHECK (status IN ('in_progress', 'completed', 'escalated', 'failed')),
    outcome         TEXT
                        CHECK (outcome IN ('paid_full', 'payment_plan', 'escalated', 'no_resolution', 'hardship', NULL)),
    final_state     TEXT,                             -- terminal state-machine state
    summary         TEXT,
    policy_version  TEXT NOT NULL DEFAULT '1.0'
);

CREATE INDEX idx_calls_customer ON calls(customer_id);

CREATE TABLE conversation_turns (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_index  INTEGER NOT NULL,
    speaker     TEXT NOT NULL CHECK (speaker IN ('agent', 'customer', 'system')),
    message     TEXT NOT NULL,
    state       TEXT NOT NULL,                        -- state-machine state at time of turn
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_turns_call ON conversation_turns(call_id, turn_index);

CREATE TABLE tool_calls (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    tool_name   TEXT NOT NULL,
    arguments   JSONB NOT NULL,
    result      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tool_calls_call ON tool_calls(call_id);

-- Structured, non-chain-of-thought decision explanation shown in the UI.
CREATE TABLE agent_decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id         UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    outcome         TEXT NOT NULL,
    reasoning       JSONB NOT NULL,                    -- ["Customer acknowledged invoice", ...]
    policy_checks   JSONB NOT NULL,                     -- {"minimum_payment": {"result": "pass", "detail": "..."}}
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────
-- Payments (simulated — no real money moves in the MVP)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE payment_plans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id         UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    invoice_id          UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    call_id             UUID REFERENCES calls(id) ON DELETE SET NULL,
    total_amount        NUMERIC(12,2) NOT NULL,
    installment_count   INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'proposed'
                            CHECK (status IN ('proposed', 'active', 'completed', 'failed', 'cancelled')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE payment_plan_installments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_plan_id     UUID NOT NULL REFERENCES payment_plans(id) ON DELETE CASCADE,
    sequence            INTEGER NOT NULL,
    amount              NUMERIC(12,2) NOT NULL,
    due_date            DATE NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'paid', 'failed')),
    paid_at             TIMESTAMPTZ
);

CREATE INDEX idx_installments_plan ON payment_plan_installments(payment_plan_id);

-- Mock Stripe-like ledger. `status` is always a *simulated* result.
CREATE TABLE payments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    installment_id  UUID REFERENCES payment_plan_installments(id) ON DELETE SET NULL,
    call_id         UUID REFERENCES calls(id) ON DELETE SET NULL,
    amount          NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('simulated_success', 'simulated_failed')),
    method          TEXT NOT NULL DEFAULT 'mock_card',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────
-- Escalations & notes
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE escalations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    call_id         UUID REFERENCES calls(id) ON DELETE SET NULL,
    reason          TEXT NOT NULL
                        CHECK (reason IN ('dispute', 'hardship', 'legal_threat', 'manager_request',
                                           'identity_verification_failed', 'angry_customer',
                                           'payment_failed', 'other')),
    priority        TEXT NOT NULL DEFAULT 'medium'
                        CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'assigned', 'resolved')),
    assigned_to     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX idx_escalations_status ON escalations(status);

CREATE TABLE account_notes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    call_id     UUID REFERENCES calls(id) ON DELETE SET NULL,
    author      TEXT NOT NULL DEFAULT 'Ava (AI agent)',
    note        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────
-- Policy versioning — the policy engine's source of truth is app/policy.json,
-- loaded and snapshotted here on every change so calls can be audited
-- against the exact rules that were active when they happened.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE policy_versions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version     TEXT NOT NULL UNIQUE,
    config      JSONB NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
