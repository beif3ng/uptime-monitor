-- AI assistance: parts of this file were written with Claude (Anthropic).
-- Architecture, review and debugging are my own. See README.md, "AI use".

-- Applied on every start (CREATE TABLE IF NOT EXISTS). There are no migrations:
-- the whole structure is fixed here, including columns that stages 2-4 need.
--
-- Note: the ON DELETE CASCADE clauses below only take effect on a connection
-- that has run PRAGMA foreign_keys = ON. SQLite keeps foreign keys off by
-- default; the pragma lives in db.py.

CREATE TABLE IF NOT EXISTS monitors (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT    NOT NULL UNIQUE,
    type                   TEXT    NOT NULL CHECK (type IN ('http', 'tcp')),
    target                 TEXT    NOT NULL,
    interval_seconds       INTEGER NOT NULL CHECK (interval_seconds > 0),
    timeout_seconds        REAL    NOT NULL CHECK (timeout_seconds > 0),
    confirmation_threshold INTEGER NOT NULL CHECK (confirmation_threshold >= 1),
    -- pending means no state has been confirmed yet. This cannot be derived
    -- from incidents: recoveries are never written there, so a monitor with
    -- two successes out of the three it needs would be indistinguishable
    -- from a confirmed up. Hence the column.
    state                  TEXT    NOT NULL DEFAULT 'pending'
                                   CHECK (state IN ('pending', 'up', 'down')),
    -- Manual kill switch for the demo: the checker treats such a monitor as
    -- down without touching the real target, so an up -> down transition can
    -- be shown on camera without taking an external service offline.
    forced_down            INTEGER NOT NULL DEFAULT 0
);

-- Raw result of every tick. This table grows linearly with time, which is why
-- uptime is not computed from it and why it is subject to retention cleanup.
-- The only growing table in the schema is the only one that needs an index.
CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    checked_at TEXT    NOT NULL,
    ok         INTEGER NOT NULL,
    latency_ms REAL,
    error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_checks_monitor_time
    ON checks (monitor_id, checked_at);

-- Confirmed state transitions only. An open incident has ended_at IS NULL.
-- This table grows with the number of outages rather than with time, so it
-- stays small and needs no index. The cause column duplicates the error text
-- from checks on purpose: the incident feed reads it directly instead of
-- looking up a check by timestamp.
CREATE TABLE IF NOT EXISTS incidents (
    id         INTEGER PRIMARY KEY,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    started_at TEXT    NOT NULL,
    ended_at   TEXT,
    cause      TEXT
);
