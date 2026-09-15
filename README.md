# Uptime Monitor

#### Video Demo: <TODO: paste the YouTube URL here>

#### Description

A minimalistic self-hosted uptime monitor. It checks a list of hosts on a schedule,
keeps the history, shows a dashboard, and sends a Telegram message when a host
goes down and again when it comes back.

The part worth looking at is not the CRUD. It is the three things a request
handler cannot do on its own:

1. **Work that happens without a user.** A scheduler runs inside the web
   process and checks every monitor on its own interval, whether or not anyone
   has the page open.
2. **A decision with memory.** A single failed request is not an outage. A
   monitor only changes state after several checks in a row agree, so a blip
   never becomes an alert.
3. **An effect outside the program.** A confirmed change of state sends one
   message to Telegram. Exactly one, on the transition, not one per failed
   check.

---

## Running it

### With Docker

```
cp .env.example .env        # optional, for Telegram alerts
docker compose up --build
```

Then open <http://localhost:8000>. The database lives on a named volume, so
the history survives `docker compose down` and a rebuild.

### Without Docker

```
python -m venv .venv
.venv\Scripts\activate          # Linux and macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app:app --reload
```

`monitor.db` is created next to the code on first start. Set `MONITOR_DB` to
put it somewhere else.

### Telegram

Both variables are optional. Without them the project runs normally and the
alerts go to the log instead of a chat, which is enough to see the state
machine work.

| Variable | Where it comes from |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | `@BotFather` in Telegram, `/newbot` |
| `TELEGRAM_CHAT_ID` | send the bot a message, then read `result[0].message.chat.id` from `https://api.telegram.org/bot<TOKEN>/getUpdates` |

### Tests

```
pytest
```

---

## The files

| File | What is in it |
| --- | --- |
| `app.py` | The HTTP layer. Monitor CRUD, the dashboard page, the JSON endpoint the dashboard polls, and the lifespan hook that owns the scheduler task. |
| `checker.py` | The probes and the scheduler. `check_http` and `check_tcp` produce a `CheckResult`; the loop decides what is due and runs it. |
| `state.py` | The state machine. `decide` is the entire rule as one pure function; the rest of the module writes the transition and asks for an alert. |
| `notifier.py` | Sends one message to Telegram, or logs it when no bot is configured. |
| `dashboard.py` | Builds the single JSON document the dashboard renders: cards, latency series, incident feed, and the uptime figure. |
| `db.py` | One short-lived SQLite connection per operation, with the pragmas that matter. |
| `config.py` | Every environment variable the project reads, in one place. |
| `schema.sql` | The three tables, applied on every start. |
| `test_state.py` | Tests for the state machine, including the restart case. |
| `templates/base.html` | Page shell and navigation. |
| `templates/monitors.html` | The management table: every monitor, its last check, and the demo kill switch. |
| `templates/monitor_form.html` | Create and edit, one form for both. |
| `templates/dashboard.html` | The dashboard shell. The three panels are filled by JavaScript. |
| `static/dashboard.js` | Fetches the JSON every three seconds and redraws the cards, the chart and the feed. |
| `static/style.css` | All the styling. No framework. |
| `static/chart.umd.min.js` | **Not my code.** Chart.js 4.5.1, MIT. See `static/VENDOR.md`. |
| `Dockerfile`, `docker-compose.yml` | One image, one service, one volume for the database. |

---

## Design choices

### One process, not a worker queue

The obvious "production" answer is Celery with Redis: a beat process schedules
the checks, workers run them. I rejected it. There is no distributed load to
spread here, and it would have meant two more containers and two more moving
parts to explain. The scheduler is an `asyncio` task started in the FastAPI
lifespan, so the whole project is one process with one command to start it.

The price is that the scheduler is tied to the web server, which forces
**exactly one Uvicorn worker**. Two workers would mean two schedulers: every
monitor checked twice per interval and every alert sent twice. That is why the
worker count is pinned in the `Dockerfile` rather than left to a default.

### Blocking calls and the event loop

`sqlite3` is synchronous, and the scheduler lives on the event loop, so a
careless query would stall every check in flight.

Two rules keep that from happening, and between them they remove any need for
`aiosqlite`:

- The request handlers are written as plain `def`, not `async def`. FastAPI
  runs a synchronous handler in a worker thread, so a slow query in a handler
  never touches the loop.
- The scheduler's own reads and writes go through `asyncio.to_thread`.

### Timeouts are results, not errors

Every probe has its own timeout and returns `CheckResult(ok=False)` for a
timeout, a refused connection or a DNS failure alike. Nothing propagates as an
exception, because "the host did not answer" is exactly the thing being
measured.

A tick does not wait for the checks it starts: the batch runs as its own task
while the loop moves on. One host that hangs for its full timeout therefore
cannot delay the next tick for every other monitor. Each result is also
written on its own rather than batched per tick, so a fast monitor sharing a
tick with a hanging one still gets a timestamp that says when it was actually
checked.

### The state machine, and what a restart does to it

A monitor is `pending`, `up` or `down`. It changes state only when the last
*N* checks agree with each other and disagree with the current state, where
*N* is that monitor's confirmation threshold. A single failure inside a run of
successes confirms nothing. Alerts hang off the transition, so an outage that
lasts an hour is one message, and the recovery message comes out of the same
mechanism for free.

The confirmation streak is deliberately **not** a counter in memory, and it has
no column of its own either. It is read back from the last few rows of
`checks`. That is what makes a restart uneventful: the state is already in the
database, the history is still there, and the first check after startup is one
result out of the threshold, not a transition. The alternative, an in-memory
counter, would have to be rebuilt on startup anyway, and getting that wrong
means a false alert every time the process restarts.

`decide` therefore takes `(current state, recent results, threshold)` and
returns the new state or `None`. It reads nothing and writes nothing, which is
why the tests can hand it a sequence of results directly instead of waiting
out real intervals.

### Uptime comes from `incidents`, not from `checks`

Counting failed rows in `checks` would make uptime depend on how often a
monitor is checked: the same one-minute outage would read differently for a
monitor checked every 5 seconds and one checked every 5 minutes. `incidents`
stores only confirmed transitions, so uptime is measured in seconds of
downtime over a window of time, which is what the word means. For a monitor
younger than the window, the window starts at its first check instead, so a
monitor added ten minutes ago is not blamed for the 23 hours before it
existed.

### Frontend without a build step

Jinja2 renders the pages; the dashboard then polls one JSON endpoint every
three seconds and redraws itself. No SPA, no bundler, no WebSocket. A
WebSocket would be the right answer for hundreds of clients; for a dashboard
one person keeps open it is a connection to manage in exchange for nothing.

The three panels come from one endpoint rather than three, so they always
describe the same moment.

A failed check breaks the latency line: it is sent as `null` based on `ok`, so
an HTTP 5xx breaks it too. A card's border turns red on the confirmed state,
not on the last check. The gap between where the line breaks and when the
border turns red is the confirmation threshold, made visible.

Chart.js is committed to `static/` instead of being loaded from a CDN, because
the container should work with no internet access.

### SQLite, raw SQL

One writer, small tables, a file that can be copied. WAL mode lets the
dashboard read while the checker writes. No ORM: the queries here are short
enough that SQL is the clearer way to write them, and an ORM would be a layer
to explain rather than a layer that helps.

---

## Testing

`pytest` covers the state machine only, which is where a silent bug would
actually cost something: a missed alert or a duplicated one. It checks that a
transition needs a full streak, that flapping confirms nothing, that a long
outage produces exactly one alert and one open incident, that recovery closes
it, and that repeating the same history produces no second alert — the restart
case.

The rest of the project is faster to verify by using it than to pin down in a
test, and `decide` being pure is what keeps those tests free of a scheduler, a
test client, and a temporary database.

## Known limitations

Deliberate, and each one would be the next thing to do rather than a bug:

- `checks` grows forever. The schema is ready for a retention job; there is
  none yet.
- No authentication. Anyone who can reach the port can edit the monitors.
- The container runs as root.
- ICMP ping is not supported; it needs a raw socket and root, which breaks the
  point of running in a container. HTTP and TCP cover what this project is
  for.

## AI use

Parts of this project were written with the help of Claude (Anthropic), an
AI assistant, as permitted for the CS50x final project. Every file that
contains generated or AI-assisted lines carries a two-line note at the top
saying so.

Concretely: the architecture, the technical decisions recorded above, the
stage plan and the review of every change are mine. The assistant was used to
draft implementation code from those decisions, to write docstrings and
comments, and to help with debugging. Every file it touched was read and
accepted by me before it went in.

<!-- TODO: add a sentence or two of your own here, naming a place where you
     changed or rejected what the assistant produced. Only you can write that
     honestly, and it is the part of this section a reader will trust most. -->

`static/chart.umd.min.js` is third-party code, not written for this project:
Chart.js 4.5.1, MIT licensed. Its origin and licence are recorded in
`static/VENDOR.md`.
