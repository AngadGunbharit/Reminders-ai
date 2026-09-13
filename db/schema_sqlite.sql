-- Ava Reminders — SQLite schema
-- Simple: contacts (optional), calls, conversation turns.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS contacts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    notes      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    id             TEXT PRIMARY KEY,
    contact_id     TEXT REFERENCES contacts(id) ON DELETE SET NULL,
    contact_name   TEXT NOT NULL,
    contact_phone  TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    status         TEXT NOT NULL DEFAULT 'in_progress'
                       CHECK (status IN ('in_progress', 'completed', 'failed')),
    outcome        TEXT CHECK (outcome IN (
                       'confirmed', 'declined', 'callback_requested',
                       'wrong_number', 'completed', NULL)),
    notes          TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_contact ON calls(contact_id);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id          TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_index  INTEGER NOT NULL,
    speaker     TEXT NOT NULL CHECK (speaker IN ('agent', 'contact')),
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_call ON conversation_turns(call_id, turn_index);
