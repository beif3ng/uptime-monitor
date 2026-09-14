# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""Probes and the scheduler that runs them.

A probe never raises: a refused connection, a DNS failure and a timeout are all
ordinary answers here, and they all come back as CheckResult(ok=False). Only a
bug should ever escape, which is why the scheduler logs whatever does.

Recording a result and deciding what it means are separate on purpose: this
module produces facts, and state.py alone turns them into a state and an alert.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

import db
import state

log = logging.getLogger("checker")

# How often the scheduler looks for work. Nothing is checked this often; it is
# the resolution with which a monitor's own interval can be honoured.
TICK_SECONDS = 1


@dataclass
class CheckResult:
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


async def check_http(target: str, timeout: float) -> CheckResult:
    """Fetch the target and treat any 4xx or 5xx as a failure.

    A fresh client per check, so no connection is pooled between checks. That
    is the point: a reused keep-alive connection would skip the TCP and TLS
    handshake, which is part of what is being monitored.
    """
    started = time.perf_counter()
    try:
        # The limit covers the whole request. httpx's own timeout applies to
        # each phase separately and a read timeout only bounds the gap between
        # chunks, so a server trickling its body would pass a 5s check in 9s.
        # timeout=None switches it off, or its 5s default would fire first.
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                response = await client.get(target)
    except TimeoutError:
        return CheckResult(False, error=f"No response within {timeout:g}s")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # httpx messages are sometimes empty, so the class name carries the
        # meaning: ConnectError, ConnectTimeout, TooManyRedirects and so on.
        # InvalidURL is not an HTTPError subclass, so it is named separately.
        return CheckResult(False, error=f"{type(exc).__name__}: {exc}".strip(": "))
    latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        return CheckResult(False, latency_ms, f"HTTP {response.status_code}")
    return CheckResult(True, latency_ms)


async def check_tcp(target: str, timeout: float) -> CheckResult:
    """Open a TCP connection to host:port and close it again.

    Only the handshake is measured. Nothing is sent, because at this level
    there is no protocol to speak.
    """
    host, _, port = target.rpartition(":")
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), timeout
        )
    except TimeoutError:
        return CheckResult(False, error=f"No connection within {timeout:g}s")
    except (OSError, UnicodeError) as exc:
        # Refused, unreachable, unknown host: all arrive as OSError subclasses.
        # A malformed host name fails IDNA encoding with UnicodeError instead.
        return CheckResult(False, error=str(exc))
    latency_ms = (time.perf_counter() - started) * 1000
    writer.close()
    return CheckResult(True, latency_ms)


async def run_check(monitor) -> CheckResult:
    """Run the probe a monitor's type calls for.

    A forced_down monitor answers without any network traffic at all. It exists
    so that the demo can produce a real down transition on camera without
    taking a real service offline.
    """
    if monitor["forced_down"]:
        return CheckResult(False, error="Forced down from the dashboard")
    probe = check_http if monitor["type"] == "http" else check_tcp
    return await probe(monitor["target"], monitor["timeout_seconds"])


def _load_monitors():
    with db.connect() as conn:
        return conn.execute("SELECT * FROM monitors").fetchall()


def _record(monitor_id, checked_at, result):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO checks (monitor_id, checked_at, ok, latency_ms, error)
               VALUES (?, ?, ?, ?, ?)""",
            (monitor_id, checked_at, int(result.ok), result.latency_ms, result.error),
        )


async def _check_and_record(monitor):
    """Probe one monitor and store that one result.

    Each result is written on its own rather than batched per tick. A batch
    would have to wait for the slowest probe in it, so a fast monitor sharing
    a tick with a hanging one would get a row stamped seconds after the fact.
    One small transaction per check keeps the timestamp honest, and at this
    number of monitors the extra writes cost nothing.
    """
    try:
        result = await run_check(monitor)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # sqlite3 is synchronous, so the write goes to a thread rather than
        # blocking the event loop that the web handlers also run on.
        await asyncio.to_thread(_record, monitor["id"], checked_at, result)
        # Only after the result is stored: the state machine reads the streak
        # back out of the checks table, and this result is part of it.
        await state.update(monitor, result)
    except Exception:
        # One broken monitor must not take down the rest of the batch.
        log.exception("Check failed for monitor %s", monitor["id"])


async def _run_batch(monitors):
    """Run every check due in one tick concurrently."""
    await asyncio.gather(*(_check_and_record(m) for m in monitors))


async def scheduler_loop():
    """Wake every second and start the checks that are due.

    Due times live in memory, not in the database: after a restart every
    monitor is simply due at once. That costs one burst of checks at startup
    and saves a column, and the burst is harmless for a project this size.

    The batch runs as its own task rather than being awaited here. A monitor
    that hangs until its timeout must not push the next tick back for every
    other monitor, which is exactly what awaiting it would do.
    """
    due: dict[int, float] = {}
    running = set()
    while True:
        try:
            monitors = await asyncio.to_thread(_load_monitors)
            now = time.monotonic()
            # Forget monitors that were deleted, so a reused row id cannot
            # inherit the due time of its predecessor.
            due = {m["id"]: due.get(m["id"], 0.0) for m in monitors}
            batch = [m for m in monitors if due[m["id"]] <= now]
            for m in batch:
                due[m["id"]] = now + m["interval_seconds"]
            if batch:
                task = asyncio.create_task(_run_batch(batch))
                # Without a reference the task can be garbage collected mid-run.
                running.add(task)
                task.add_done_callback(running.discard)
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
