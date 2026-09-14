# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""The state machine: pending / up / down, and what a transition costs.

`decide` is the whole rule and touches nothing outside its arguments, so the
tests in test_state.py can feed it check results directly instead of waiting
for a scheduler. Everything around it only fetches rows, writes rows and sends
a message.

The confirmation streak is not kept in memory and has no column of its own: it
is read back from the last few rows of `checks`. That is what makes a restart
harmless. A monitor that was down stays down in the database, its history is
still there, and one fresh success right after startup is one success out of
the threshold, not a recovery.
"""

import asyncio
import logging
from datetime import datetime, timezone

import db
import notifier

log = logging.getLogger("state")


def decide(state: str, recent: list[bool], threshold: int) -> str | None:
    """Return the state to move to, or None to stay put.

    `recent` holds the results of the last checks, newest first. A transition
    needs `threshold` of them in a row agreeing with each other and disagreeing
    with the current state; anything less is flapping and is ignored.
    """
    if len(recent) < threshold:
        return None
    streak = recent[:threshold]
    if any(ok != streak[0] for ok in streak):
        return None
    confirmed = "up" if streak[0] else "down"
    return confirmed if confirmed != state else None


def _recent_results(monitor_id: int, limit: int) -> list[bool]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ok FROM checks WHERE monitor_id = ? ORDER BY id DESC LIMIT ?",
            (monitor_id, limit),
        ).fetchall()
    return [bool(row["ok"]) for row in rows]


def _transition(monitor_id: int, new_state: str, cause: str | None) -> str | None:
    """Record the transition and return the start of the incident it closed.

    The state column and the incidents table are written in one transaction,
    so a crash cannot leave a monitor marked down with no incident open.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("UPDATE monitors SET state = ? WHERE id = ?", (new_state, monitor_id))
        if new_state == "down":
            conn.execute(
                "INSERT INTO incidents (monitor_id, started_at, cause) VALUES (?, ?, ?)",
                (monitor_id, now, cause),
            )
            return None
        row = conn.execute(
            """SELECT id, started_at FROM incidents
                WHERE monitor_id = ? AND ended_at IS NULL
             ORDER BY id DESC LIMIT 1""",
            (monitor_id,),
        ).fetchone()
        if row is None:
            # First confirmation of a new monitor: it was never down, so there
            # is nothing to close and nothing to announce.
            return None
        conn.execute("UPDATE incidents SET ended_at = ? WHERE id = ?", (now, row["id"]))
        return row["started_at"]


def _downtime(started_at: str) -> str:
    seconds = int(
        (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds()
    )
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


async def update(monitor, result) -> None:
    """Apply the newest check result to the monitor's state.

    Called once per check, right after the result is stored. At most one alert
    leaves here per call, and only when the state actually changed, which is
    what keeps a long outage from becoming a message per tick.
    """
    threshold = monitor["confirmation_threshold"]
    recent = await asyncio.to_thread(_recent_results, monitor["id"], threshold)
    new_state = decide(monitor["state"], recent, threshold)
    if new_state is None:
        return

    started_at = await asyncio.to_thread(
        _transition, monitor["id"], new_state, result.error
    )
    log.info("%s: %s -> %s", monitor["name"], monitor["state"], new_state)

    if new_state == "down":
        await notifier.send(f"🔴 {monitor['name']} is DOWN\n{result.error}")
    elif started_at is not None:
        await notifier.send(
            f"🟢 {monitor['name']} is UP again\nDowntime: {_downtime(started_at)}"
        )
