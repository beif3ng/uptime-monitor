# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""HTTP layer: the dashboard, monitor CRUD, and the scheduler's home.

Handlers are plain `def`, not `async def`. sqlite3 is synchronous, and FastAPI
runs sync handlers in a worker thread, so a slow query here cannot stall the
event loop that the scheduler will live on from stage 2 onwards.
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError, model_validator

import checker
import dashboard
import db

BASE_DIR = Path(__file__).parent


# Default values for form
DEFAULTS = {
    "name": "",
    "type": "http",
    "target": "",
    "interval_seconds": 10,
    "timeout_seconds": 5,
    "confirmation_threshold": 2,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the scheduler for exactly as long as the application runs.

    The scheduler is a task on the same event loop as the web server, not a
    second process: one process means one scheduler, and one scheduler means a
    monitor cannot be checked twice per interval. Hence a single Uvicorn worker.
    """
    db.init()
    task = asyncio.create_task(checker.scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


class MonitorForm(BaseModel):
    """Validation shared by create and edit.

    The database has its own CHECK constraints; these rules exist so that a
    mistake in the form comes back as a sentence rather than an IntegrityError.
    """

    name: str = Field(min_length=1, max_length=80)
    type: Literal["http", "tcp"]
    target: str = Field(min_length=1)
    interval_seconds: int = Field(ge=5, le=86400)
    timeout_seconds: float = Field(gt=0)
    confirmation_threshold: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def _check(self):
        if self.type == "http":
            parsed = urlparse(self.target)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(
                    "An HTTP target must be a full URL, "
                    "for example https://example.com/health"
                )
        else:
            host, _, port = self.target.rpartition(":")
            if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError(
                    "A TCP target must be host:port, for example db.example.com:5432"
                )
        # Without this a check could still be running when the next tick fires.
        if self.timeout_seconds >= self.interval_seconds:
            raise ValueError("Timeout must be shorter than the interval.")
        return self


def monitor_fields(
    name: str = Form(default=""),
    type: str = Form(default=""),
    target: str = Form(default=""),
    interval_seconds: str = Form(default=""),
    timeout_seconds: str = Form(default=""),
    confirmation_threshold: str = Form(default=""),
) -> dict:
    """Collect the form fields as raw strings.

    Two deliberate choices here, both so that every mistake comes back as the
    form the user was filling in rather than as FastAPI's own 422 JSON page.
    The numeric fields are not typed as int or float, and every field has a
    default: an empty input arrives as a missing value, which without a default
    would be rejected before this handler ever ran. Judging the values is
    MonitorForm's job alone.
    """
    return {
        "name": name.strip(),
        "type": type,
        "target": target.strip(),
        "interval_seconds": interval_seconds,
        "timeout_seconds": timeout_seconds,
        "confirmation_threshold": confirmation_threshold,
    }


def _message(exc: ValidationError) -> str:
    """Turn the first validation error into one sentence for the form.

    Rules that span several fields carry no location, so they are shown on
    their own rather than blamed on one input.
    """
    error = exc.errors()[0]
    text = error["msg"].removeprefix("Value error, ")
    if not error["loc"]:
        return text
    return f"{str(error['loc'][0]).replace('_', ' ').capitalize()}: {text}"


def _form_page(request, fields, action, error=None, status=200):
    return templates.TemplateResponse(
        request,
        "monitor_form.html",
        {"fields": fields, "action": action, "error": error},
        status_code=status,
    )


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request, "dashboard.html", {"window_minutes": dashboard.CHART_MINUTES}
    )


@app.get("/api/dashboard")
def dashboard_data():
    """One document per poll: cards, chart and incident feed together.

    Three endpoints would let the panels drift a few seconds apart, and the
    whole payload is a handful of kilobytes.
    """
    return dashboard.payload()


@app.get("/monitors")
def list_monitors(request: Request):
    with db.connect() as conn:
        # The subquery picks the newest check per monitor by id: checked_at has
        # a one-second resolution, so several checks can share a timestamp.
        monitors = conn.execute(
            """SELECT m.*, c.checked_at AS last_checked_at, c.ok AS last_ok,
                      c.latency_ms AS last_latency_ms, c.error AS last_error
                 FROM monitors m
                 LEFT JOIN checks c
                   ON c.id = (SELECT id FROM checks
                               WHERE monitor_id = m.id
                            ORDER BY id DESC LIMIT 1)
             ORDER BY m.name"""
        ).fetchall()
    return templates.TemplateResponse(request, "monitors.html", {"monitors": monitors})


@app.get("/monitors/new")
def new_monitor(request: Request):
    return _form_page(request, DEFAULTS, "/monitors/new")


@app.post("/monitors/new")
def create_monitor(request: Request, fields: Annotated[dict, Depends(monitor_fields)]):
    try:
        form = MonitorForm(**fields)
    except ValidationError as exc:
        return _form_page(request, fields, "/monitors/new", _message(exc), 400)

    try:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO monitors (name, type, target, interval_seconds,
                                         timeout_seconds, confirmation_threshold)
                   VALUES (:name, :type, :target, :interval_seconds,
                           :timeout_seconds, :confirmation_threshold)""",
                form.model_dump(),
            )
    except sqlite3.IntegrityError:
        # The only UNIQUE column is the name, so this is the duplicate case.
        return _form_page(
            request,
            fields,
            "/monitors/new",
            f"A monitor named {form.name} already exists.",
            400,
        )
    return RedirectResponse("/monitors", status_code=303)


@app.get("/monitors/{monitor_id}/edit")
def edit_monitor(request: Request, monitor_id: int):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "No such monitor")
    return _form_page(request, dict(row), f"/monitors/{monitor_id}/edit")


@app.post("/monitors/{monitor_id}/edit")
def update_monitor(
    request: Request,
    monitor_id: int,
    fields: Annotated[dict, Depends(monitor_fields)],
):
    action = f"/monitors/{monitor_id}/edit"
    try:
        form = MonitorForm(**fields)
    except ValidationError as exc:
        return _form_page(request, fields, action, _message(exc), 400)

    try:
        with db.connect() as conn:
            updated = conn.execute(
                """UPDATE monitors
                      SET name = :name, type = :type, target = :target,
                          interval_seconds = :interval_seconds,
                          timeout_seconds = :timeout_seconds,
                          confirmation_threshold = :confirmation_threshold
                    WHERE id = :id""",
                form.model_dump() | {"id": monitor_id},
            ).rowcount
    except sqlite3.IntegrityError:
        return _form_page(
            request,
            fields,
            action,
            f"A monitor named {form.name} already exists.",
            400,
        )
    if not updated:
        raise HTTPException(404, "No such monitor")
    return RedirectResponse("/monitors", status_code=303)


@app.post("/monitors/{monitor_id}/toggle-forced-down")
def toggle_forced_down(monitor_id: int):
    """Flip the demo kill switch. The checker reads the flag on the next tick."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE monitors SET forced_down = NOT forced_down WHERE id = ?",
            (monitor_id,),
        )
    return RedirectResponse("/monitors", status_code=303)


@app.post("/monitors/{monitor_id}/delete")
def delete_monitor(monitor_id: int):
    # Deleting the monitor drops its checks and incidents through the
    # ON DELETE CASCADE clauses in schema.sql.
    with db.connect() as conn:
        conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
    return RedirectResponse("/monitors", status_code=303)
