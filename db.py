# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""SQLite access: one short-lived connection per operation, raw SQL, no ORM."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@contextmanager
def connect():
    """Open a connection, commit on success, always close.

    Leaving the block without an exception commits; an exception propagates
    and the pending transaction dies with the connection.
    """
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # SQLite keeps foreign keys off by default, so the ON DELETE CASCADE
    # clauses in schema.sql would silently do nothing without this.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the dashboard read while the checker writes (stage 2 onwards).
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    """Apply schema.sql. It is written with CREATE TABLE IF NOT EXISTS,
    so running it on every start is a no-op once the tables are there."""
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
