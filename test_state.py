# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""Tests for the state machine, the one part worth testing here.

Checking "a transition needs N results in a row" by hand means waiting out N
intervals for every case; here the results are handed in directly. The rest of
the project (a GET returns a page, a probe reaches a host) is faster to verify
by looking at it than to pin down in a test.
"""

import asyncio

import pytest

import config
import db
import notifier
import state
from checker import CheckResult

UP, DOWN = True, False


@pytest.mark.parametrize(
    "current, recent, threshold, expected",
    [
        # Not enough history yet: a fresh monitor stays pending.
        ("pending", [DOWN], 2, None),
        ("pending", [], 1, None),
        # A streak short of the threshold decides nothing.
        ("up", [DOWN, DOWN], 3, None),
        # A single success in the middle of failures is flapping, not recovery.
        ("up", [UP, DOWN, DOWN], 3, None),
        ("down", [UP, DOWN, UP], 3, None),
        # Enough agreement, and it disagrees with the current state.
        ("up", [DOWN, DOWN], 2, "down"),
        ("down", [UP, UP], 2, "up"),
        ("pending", [UP, UP], 2, "up"),
        ("pending", [DOWN, DOWN], 2, "down"),
        # Already in that state: nothing changes, so no alert can follow.
        ("down", [DOWN, DOWN, DOWN], 2, None),
        ("up", [UP, UP], 2, None),
        # A threshold of one means every result is confirmation.
        ("up", [DOWN], 1, "down"),
        # Only the newest `threshold` results count; older ones are history.
        ("up", [DOWN, DOWN, UP, UP, UP], 2, "down"),
    ],
)
def test_decide(current, recent, threshold, expected):
    assert state.decide(current, recent, threshold) == expected


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A database of its own per test, and a monitor to drive."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO monitors (name, type, target, interval_seconds,
                                     timeout_seconds, confirmation_threshold)
               VALUES ('probe', 'http', 'http://example.com', 10, 5, 2)"""
        )


@pytest.fixture
def alerts(monkeypatch):
    """Collect what would have been sent to Telegram."""
    sent = []

    async def fake_send(text):
        sent.append(text)

    monkeypatch.setattr(notifier, "send", fake_send)
    return sent


def feed(*results):
    """Store each result and let the state machine react, as the checker does."""
    for ok in results:
        result = CheckResult(ok, 12.0, None if ok else "Connection refused")
        with db.connect() as conn:
            monitor = conn.execute("SELECT * FROM monitors").fetchone()
            conn.execute(
                """INSERT INTO checks (monitor_id, checked_at, ok, latency_ms, error)
                   VALUES (?, datetime('now'), ?, ?, ?)""",
                (monitor["id"], int(ok), result.latency_ms, result.error),
            )
        asyncio.run(state.update(monitor, result))


def current():
    with db.connect() as conn:
        monitor = conn.execute("SELECT * FROM monitors").fetchone()
        incidents = conn.execute(
            "SELECT * FROM incidents ORDER BY id"
        ).fetchall()
    return monitor["state"], incidents


def test_one_alert_per_transition(database, alerts):
    """A long outage is one alert, not one per check."""
    feed(DOWN, DOWN, DOWN, DOWN, DOWN)
    monitor_state, incidents = current()
    assert monitor_state == "down"
    assert len(incidents) == 1
    assert incidents[0]["ended_at"] is None
    assert len(alerts) == 1
    assert "DOWN" in alerts[0]


def test_recovery_closes_the_incident(database, alerts):
    feed(UP, UP, DOWN, DOWN, UP, UP)
    monitor_state, incidents = current()
    assert monitor_state == "up"
    assert len(incidents) == 1
    assert incidents[0]["ended_at"] is not None
    # Down and up, and nothing for the first confirmation of a new monitor.
    assert len(alerts) == 2
    assert "UP again" in alerts[1]


def test_flapping_never_confirms(database, alerts):
    """Alternating results never reach two in a row, so nothing is announced."""
    feed(DOWN, UP, DOWN, UP, DOWN)
    monitor_state, incidents = current()
    assert monitor_state == "pending"
    assert incidents == []
    assert alerts == []


def test_restart_does_not_realert(database, alerts):
    """The state survives in the database, so a restart repeats no alert.

    Nothing in `update` is remembered between calls, so a second run of the
    same monitor with the same history behaves exactly as a restarted process
    would: the outage is already recorded, and a single later failure adds
    nothing to announce.
    """
    feed(DOWN, DOWN)
    assert len(alerts) == 1
    # "Restart": the counter was never in memory, the history is on disk.
    feed(DOWN)
    monitor_state, incidents = current()
    assert monitor_state == "down"
    assert len(incidents) == 1
    assert len(alerts) == 1


def test_recovery_needs_the_full_streak_after_a_restart(database, alerts):
    feed(DOWN, DOWN, UP)
    monitor_state, _ = current()
    assert monitor_state == "down"
    assert len(alerts) == 1
