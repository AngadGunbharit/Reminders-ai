-- Ava Reminds — SQLite schema

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS contacts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    notes      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id           TEXT PRIMARY KEY,
    purpose      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'completed', 'paused')),
    scheduled_at TEXT,           -- NULL = send immediately
    total        INTEGER NOT NULL DEFAULT 0,
    completed    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS calls (
    id             TEXT PRIMARY KEY,
    campaign_id    TEXT REFERENCES campaigns(id) ON DELETE SET NULL,
    contact_id     TEXT REFERENCES contacts(id) ON DELETE SET NULL,
    contact_name   TEXT NOT NULL,
    contact_phone  TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    scheduled_at   TEXT,          -- NULL = send immediately; future = scheduled
    started_at     TEXT,
    ended_at       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'scheduled', 'in_progress', 'completed', 'failed')),
    outcome        TEXT CHECK (outcome IN (
                       'confirmed', 'declined', 'callback_requested',
                       'wrong_number', 'completed', NULL)),
    notes          TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_campaign  ON calls(campaign_id);
CREATE INDEX IF NOT EXISTS idx_calls_scheduled ON calls(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_calls_status    ON calls(status);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id          TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_index  INTEGER NOT NULL,
    speaker     TEXT NOT NULL CHECK (speaker IN ('agent', 'contact')),
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_call ON conversation_turns(call_id, turn_index);
