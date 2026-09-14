# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""Everything the dashboard shows, assembled into one JSON payload.

The page fetches this on a timer, so all three panels always describe the same
moment. Uptime is computed from `incidents`, never by counting rows in
`checks`: a monitor checked every 5 seconds and one checked every 5 minutes
would otherwise get different uptime figures for the same outage.
"""

from datetime import datetime, timedelta, timezone

import db

# How much history the latency chart and the uptime figure cover.
CHART_MINUTES = 30
UPTIME_HOURS = 24
# Guard against a fast monitor filling the chart with more points than the
# canvas has pixels.
MAX_POINTS = 300


def _parse(value: str) -> datetime:
    """Read a stored timestamp. The checker writes UTC with an offset; the
    tests use SQLite's own datetime(), which has none."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def uptime_percent(incidents, window_start, now, first_check):
    """Share of the window the monitor was not in a confirmed outage.

    The window starts at the first check when the monitor is younger than the
    window itself, so a monitor added ten minutes ago is not punished for the
    23 hours it did not exist. An open incident (ended_at is NULL) counts up
    to now.
    """
    start = max(window_start, first_check)
    total = (now - start).total_seconds()
    if total <= 0:
        return None
    down = 0.0
    for incident in incidents:
        began = max(_parse(incident["started_at"]), start)
        ended = _parse(incident["ended_at"]) if incident["ended_at"] else now
        down += max(0.0, (ended - began).total_seconds())
    return round(100 * (1 - min(down, total) / total), 2)


def payload():
    now = datetime.now(timezone.utc)
    uptime_from = now - timedelta(hours=UPTIME_HOURS)
    chart_from = now - timedelta(minutes=CHART_MINUTES)

    with db.connect() as conn:
        monitors = conn.execute("SELECT * FROM monitors ORDER BY name").fetchall()
        incidents = conn.execute(
            """SELECT i.*, m.name AS monitor
                 FROM incidents i JOIN monitors m ON m.id = i.monitor_id
             ORDER BY i.id DESC LIMIT 20"""
        ).fetchall()
        recent = conn.execute(
            """SELECT monitor_id, checked_at, ok, latency_ms, error
                 FROM checks
                WHERE checked_at >= ?
             ORDER BY id""",
            (chart_from.isoformat(timespec="seconds"),),
        ).fetchall()
        # Uptime needs incidents that reach into the window, which includes one
        # that started before it and has not ended.
        window_incidents = conn.execute(
            """SELECT monitor_id, started_at, ended_at FROM incidents
                WHERE ended_at IS NULL OR ended_at >= ?""",
            (uptime_from.isoformat(timespec="seconds"),),
        ).fetchall()
        firsts = conn.execute(
            "SELECT monitor_id, MIN(checked_at) AS first FROM checks GROUP BY monitor_id"
        ).fetchall()
        # Asked for separately rather than taken from `recent`: a monitor with
        # an interval longer than the chart window has no check inside it, and
        # its card should still show the last result it did produce.
        last_checks = conn.execute(
            """SELECT * FROM checks
                WHERE id IN (SELECT MAX(id) FROM checks GROUP BY monitor_id)"""
        ).fetchall()

    first_check = {row["monitor_id"]: _parse(row["first"]) for row in firsts}
    last = {row["monitor_id"]: row for row in last_checks}

    cards, series = [], []
    for monitor in monitors:
        mine = [c for c in window_incidents if c["monitor_id"] == monitor["id"]]
        newest = last.get(monitor["id"])
        cards.append(
            {
                "id": monitor["id"],
                "name": monitor["name"],
                "target": monitor["target"],
                "state": monitor["state"],
                "forced_down": bool(monitor["forced_down"]),
                "uptime": uptime_percent(
                    mine,
                    uptime_from,
                    now,
                    first_check.get(monitor["id"], now),
                ),
                "last_checked_at": newest["checked_at"] if newest else None,
                "last_ok": bool(newest["ok"]) if newest else None,
                "last_latency_ms": newest["latency_ms"] if newest else None,
                "last_error": newest["error"] if newest else None,
            }
        )
        # x is minutes ago, so the chart needs no date adapter and no shared
        # set of labels between monitors that tick at different rates.
        points = [
            {
                "x": round((_parse(c["checked_at"]) - now).total_seconds() / 60, 3),
                "y": round(c["latency_ms"], 1),
            }
            for c in recent
            if c["monitor_id"] == monitor["id"] and c["latency_ms"] is not None
        ]
        series.append({"name": monitor["name"], "points": points[-MAX_POINTS:]})

    feed = [
        {
            "monitor": i["monitor"],
            "started_at": i["started_at"],
            "ended_at": i["ended_at"],
            "seconds": int(
                (
                    (_parse(i["ended_at"]) if i["ended_at"] else now)
                    - _parse(i["started_at"])
                ).total_seconds()
            ),
            "cause": i["cause"],
        }
        for i in incidents
    ]

    return {
        "now": now.isoformat(timespec="seconds"),
        "window_minutes": CHART_MINUTES,
        "uptime_hours": UPTIME_HOURS,
        "monitors": cards,
        "series": series,
        "incidents": feed,
    }
