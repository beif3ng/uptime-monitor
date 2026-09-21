# Uptime Monitor

#### Video Demo: https://youtu.be/BcT3nr9sVXk

#### Description

Minimal self-hosted uptime monitor. Checks hosts on a schedule,
keeps the history, shows a dashboard, and sends Telegram notifications.

**Three differences from a typical CRUD app:**

1. **Works without a user.** The scheduler runs on its own, whether the page is open or not.
2. **Decides with memory.** State transition only after N identical results in a row.
3. **Acts outside the program.** Telegram alert once if monitor went down or recovered.

_N is `confirmation_threshold` of each monitor, explained in "Design choices"_

For each monitor, the dashboard shows: current state, uptime %, latency graph, and a list of incidents.

## Running it

### Docker

```bash
# optional, Telegram alerts
cp .env.example .env

docker compose up --build
```

Open <http://localhost:8000>.

The database lives on a named volume, so the history survives `docker compose down` and a rebuild.

### Python

Python 3.14

`monitor.db` is created in the directory you start uvicorn from. Set `MONITOR_DB` to
put it somewhere else.

#### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app:app --reload
```

Open <http://localhost:8000>.

#### Linux & macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app:app --reload
```

Open <http://localhost:8000>.

### Telegram notifications

Optional. Without these variables the alerts go to the log instead of a chat.

| Variable             | Where it comes from                 |
| -------------------- | ----------------------------------- |
| `TELEGRAM_BOT_TOKEN` | `@BotFather` in Telegram            |
| `TELEGRAM_CHAT_ID`   | chat with the bot: your Telegram id |

### Tests

```bash
pytest
```

`pytest` is installed from `requirements-dev.txt`.
The Docker image doesn't have it.

## The files

- `app.py` — FastAPI: routes, forms, lifespan with the scheduler
- `state.py` — core, state transition function, writes incidents, and sends alert with `notifier`
- `checker.py` — the scheduler + HTTP/TCP probes, writes results to `checks` and processes `forced_down`
- `notifier.py` — Telegram notifications using Bot API, otherwise alert goes to log
- `dashboard.py` — gathers data for `/` and `/api/dashboard`
- `static/dashboard.js` — every 3 seconds fetches `/api/dashboard` and renders the page
- `templates/` — base, monitors, monitor_form, dashboard
- `static/` — `style.css`, `chart.umd.min.js` + `VENDOR.md`
- `schema.sql` — 3 tables: "monitors", "checks", "incidents"
- `db.py` — database connection, Write-Ahead Logging, applies `schema.sql` every start
- `config.py` — every environment variable in one place
- `test_state.py` — 18 tests for the state machine
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `requirements*.txt`, `.env.example` — one image and service, dependencies and environment variables

Chart.js — third-party library (version and license — in `static/VENDOR.md`).

## Design choices

### One process, not a worker queue

Standard "production" stack is Celery with Redis. But here load is not distributed to spread it. Additionally, it would have meant two more containers. I rejected it.

The scheduler is an `asyncio` task instead of the Celery scheduler. It starts in the FastAPI lifespan so everything works in one process. Only one Uvicorn worker is pinned in the `Dockerfile`. More workers would mean many schedulers -> duplicate alerts.

### Blocking calls and the event loop

`sqlite3` is synchronous. The scheduler runs on the event loop and a slow query to the database would block all checks in flight. The request handlers are written as `def`. FastAPI executes them in separate threads by itself. The scheduler reads and writes through `asyncio.to_thread`, so `aiosqlite` is not needed.

### Timeouts are results, not errors

A timeout is an ordinary check result (`ok=False`), not an exception. Otherwise there would be no row in `checks`, the monitor would be stuck in its current state and the silent node would not be visible.

### The state machine, and what a restart does to it

A state changes only after N (`confirmation_threshold`) identical check results. An alert goes only once because `decide` compares the series of results with saved `monitors.state`. The counter is read from recent `checks` rows, not from memory, so a restart causes no false alerts. `pending` -> `up` is silent, `pending` -> `down` alerts.

### Uptime comes from `incidents`, not from `checks`

Counting from `checks`, uptime would depend on the frequency of checks. The same minute of downtime would return a different percentage with a 5-second and a 5-minute interval.
Incidents contain intervals of downtime. Uptime = 1 - downtime / monitoring time.

## Known limitations

- old `checks` rows are not removed
- no authentication
- container runs as root
- ICMP is not supported: raw socket and root are needed

## AI use

1. **What I used.** Claude (Anthropic) since it is allowed for the CS50x final project. Files containing code written by AI are marked with two lines at the top.
2. **Until 14 September.** Architecture, decisions and plan are mine. AI wrote draft code based on these decisions and I reviewed and accepted every file.
3. **From 14 September.** First understanding and then code: I write the code and AI shows where to change the code and explains why. Reason: AI gives a different answer every time but I control my own fixes.
4. **What I changed or rejected:**
   - Celery / Redis — redundant
   - the title of the "Edit" page was lost when a validation error occurred — fixed then simplified
   - latency graph grew taller when the window was resized — fixed
   - heartbeat (row of squares of last checks) — removed, kept the graph and incidents
5. **README.** Text is mine. AI compared facts with the code and made mechanical edits: removing, changing separate words, splitting one sentence into two. AI pointed out English mistakes and I corrected them.
