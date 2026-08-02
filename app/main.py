from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config, queries, validation
from app.db import get_readonly_connection
from app.refresh import refresh_if_missing, start_scheduler
from app.schemas import (
    DirectJourneyResponse,
    DirectTripOut,
    IntermediateStopOut,
    InterchangeTripOut,
    JourneyOut,
    JourneysResponse,
    StationOut,
)

logger = logging.getLogger("train_journey_planner.main")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def format_duration(total_minutes: int) -> str:
    """Renders a duration in minutes as "1h6m" (or "45m" under an hour,
    "2h" for an exact number of hours) instead of a bare minute count."""
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}m"
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h{minutes}m"


templates.env.filters["duration"] = format_duration


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fire-and-forget in a thread rather than awaiting: a cold start (fresh
    # volume) means a full download + ~30s build, and blocking here would
    # block the event loop — including /health, which is exactly the signal
    # a deploy/orchestrator uses to know the app isn't ready yet. get_db()
    # already returns 503 while the dataset is missing.
    asyncio.create_task(asyncio.to_thread(refresh_if_missing))
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="UK Train Journey Planner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# GitHub issue #20: bounds how many DB-touching requests run concurrently
# (see config.MAX_CONCURRENT_DB_REQUESTS for how the default was sized).
# A plain asyncio.Semaphore, not anyio.to_thread's shared thread limiter —
# that limiter is global to the whole process, including the lifespan's
# cold-start `asyncio.to_thread(refresh_if_missing)` build, and a small cap
# there risks stalling startup behind API traffic or vice versa. Scoping the
# semaphore to this middleware keeps it independent of that.
_DB_ROUTE_PATHS = frozenset({"/api/direct", "/api/journeys", "/api/stations", "/results"})
_db_request_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_DB_REQUESTS)


class ConcurrencyLimitMiddleware:
    """Raw ASGI middleware, not `@app.middleware("http")` (Starlette's
    `BaseHTTPMiddleware`) — the latter runs `call_next` inside an `anyio`
    task group, which collides with `ServerErrorMiddleware`'s re-raise-
    after-send behavior for unhandled exceptions (see the catch-all handler
    above) and surfaces as a stray unraisable exception even though the
    client still gets the right response. Confirmed via
    tests/test_concurrency.py: the BaseHTTPMiddleware version made that
    test fail with an ExceptionGroup even though the live app (verified
    against a running container) returned the correct 500 JSON either way.
    A plain ASGI callable doesn't wrap anything in a task group, so it
    doesn't have this problem."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in _DB_ROUTE_PATHS:
            await self.app(scope, receive, send)
            return
        # Captured once into a local rather than read twice by module-global
        # name (acquire, then release) — harmless today since the global is
        # never reassigned post-startup (only tests swap it, between
        # requests), but this removes even the possibility of acquire/
        # release ever targeting different Semaphore objects.
        semaphore = _db_request_semaphore
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=config.DB_REQUEST_ACQUIRE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            response = JSONResponse(
                status_code=503,
                content={"detail": "Server is at capacity — please retry shortly."},
                headers={"Retry-After": str(config.DB_REQUEST_ACQUIRE_TIMEOUT_SECONDS)},
            )
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            semaphore.release()


app.add_middleware(ConcurrencyLimitMiddleware)


@app.exception_handler(RequestValidationError)
async def results_validation_error(request: Request, exc: RequestValidationError):
    # /results is the HTML page (reachable with JS disabled/broken, or with
    # an autocomplete entry the client couldn't resolve to a CRS code) — it
    # should render the same styled error card as any other bad query,
    # not FastAPI's raw JSON blob. /api/* keeps the default JSON 422.
    if request.url.path != "/results":
        return await request_validation_exception_handler(request, exc)

    bad_fields = {str(e["loc"][-1]) for e in exc.errors() if e["loc"] and e["loc"][0] == "query"}
    if bad_fields & {"from_", "to"}:
        error = "Couldn't find that station — pick one from the dropdown, or enter its 3-letter CRS code."
    elif bad_fields & {"date", "time"}:
        error = "That date or time doesn't look right."
    else:
        error = "That search doesn't look right — please check the fields and try again."

    params = request.query_params
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "error": error,
            "result": None,
            "from_": params.get("from_", "").upper(),
            "to": params.get("to", "").upper(),
            "date": params.get("date", ""),
            "time": params.get("time", ""),
        },
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # GitHub issue #20: under concurrent load, requests were intermittently
    # hitting Starlette's bare (non-JSON) 500 with no logging anywhere in
    # the stack, leaving the actual traceback uncaptured. This doesn't fix
    # the underlying crash — it just makes it observable (full traceback in
    # logs) and gives callers FastAPI's normal JSON error shape instead of
    # Starlette's default HTML/plain-text one. HTTPException instances never
    # reach this handler — Starlette dispatches those to its own
    # more-specific default handler first.
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


def get_db():
    if not config.GTFS_DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="GTFS dataset is not loaded yet — try again shortly.",
        )
    conn = get_readonly_connection(config.GTFS_DB_PATH)
    try:
        # Schema-version guard: a DB built before the reversal-continuation
        # fix (issue #15) predates the synthesized_trips table every query
        # now LEFT JOINs against — without this check, every request would
        # 500 with a raw "no such table" sqlite error instead of a clear
        # message, until the next scheduled refresh happens to rebuild it.
        # Matches this project's existing precedent (see CLAUDE.md/context.md)
        # of schema changes needing a manual DB rebuild on deploy — this
        # just makes the failure mode legible instead of silent/opaque.
        has_synthesized_trips = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'synthesized_trips'"
        ).fetchone()
        if has_synthesized_trips is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "GTFS dataset was built with an older schema — delete the gtfs.db file "
                    "and restart to force a rebuild."
                ),
            )
        yield conn
    finally:
        conn.close()


def _validate_or_400(
    conn: sqlite3.Connection, from_crs: str, to_crs: str, date: dt.date, time: dt.time
) -> tuple[queries.Stop, queries.Stop]:
    try:
        return validation.validate_query(conn, from_crs, to_crs, date, time)
    except queries.UnknownStationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except queries.SameStationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except queries.DateOutOfRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _direct_trip_out(t: queries.DirectTrip, query_date: dt.date) -> DirectTripOut:
    return DirectTripOut(
        trip_id=t.trip_id,
        operator=t.agency_name,
        operator_code=t.agency_id,
        route_description=t.route_short_name or t.route_long_name,
        headsign=t.trip_headsign,
        departure_time=t.departure_time,
        arrival_time=t.arrival_time,
        departure_next_day=t.departure_next_day,
        arrival_next_day=t.arrival_next_day,
        duration_minutes=t.duration_minutes,
        is_past=validation.trip_is_in_past(query_date, t.departure_time, t.departure_next_day),
        intermediate_stops=[
            IntermediateStopOut(
                stop_name=s.stop_name,
                stop_code=s.stop_code,
                arrival_time=s.arrival_time,
                departure_time=s.departure_time,
            )
            for s in t.intermediate_stops
        ],
        reverses_at=(
            StationOut(crs_code=t.reverses_at.stop_code, name=t.reverses_at.stop_name)
            if t.reverses_at is not None
            else None
        ),
    )


def _run_direct_query(
    conn: sqlite3.Connection,
    from_crs: str,
    to_crs: str,
    date: dt.date,
    time: dt.time,
    window_minutes: int,
    filter_dominated: bool = True,
) -> DirectJourneyResponse:
    origin, destination = _validate_or_400(conn, from_crs, to_crs, date, time)
    finder = queries.dominant_direct_trips if filter_dominated else queries.find_direct_trips
    trips = finder(conn, origin, destination, date, time, window_minutes)

    return DirectJourneyResponse(
        origin=StationOut(crs_code=origin.stop_code, name=origin.stop_name),
        destination=StationOut(crs_code=destination.stop_code, name=destination.stop_name),
        date=date.isoformat(),
        window_start=time.isoformat(timespec="minutes"),
        window_minutes=window_minutes,
        filter_dominated=filter_dominated,
        trips=[_direct_trip_out(t, date) for t in trips],
    )


def _run_journeys_query(
    conn: sqlite3.Connection,
    from_crs: str,
    to_crs: str,
    date: dt.date,
    time: dt.time,
    window_minutes: int,
    direct_only: bool = False,
) -> JourneysResponse:
    origin, destination = _validate_or_400(conn, from_crs, to_crs, date, time)
    journeys = queries.find_journeys(conn, origin, destination, date, time, window_minutes, direct_only)

    journey_outs = []
    for j in journeys:
        interchange_out = None
        if j.interchange is not None:
            interchange_out = InterchangeTripOut(
                leg1=_direct_trip_out(j.interchange.leg1, date),
                leg2=_direct_trip_out(j.interchange.leg2, date),
                interchange=StationOut(
                    crs_code=j.interchange.interchange.stop_code,
                    name=j.interchange.interchange.stop_name,
                ),
                connection_minutes=j.interchange.connection_minutes,
                total_duration_minutes=j.interchange.total_duration_minutes,
            )
        journey_outs.append(
            JourneyOut(
                kind=j.kind,
                departure_time=j.departure_time,
                departure_next_day=j.departure_next_day,
                arrival_time=j.arrival_time,
                arrival_next_day=j.arrival_next_day,
                duration_minutes=j.duration_minutes,
                is_past=validation.trip_is_in_past(date, j.departure_time, j.departure_next_day),
                direct=_direct_trip_out(j.direct, date) if j.direct is not None else None,
                interchange=interchange_out,
            )
        )

    return JourneysResponse(
        origin=StationOut(crs_code=origin.stop_code, name=origin.stop_name),
        destination=StationOut(crs_code=destination.stop_code, name=destination.stop_name),
        date=date.isoformat(),
        window_start=time.isoformat(timespec="minutes"),
        window_minutes=window_minutes,
        direct_only=direct_only,
        journeys=journey_outs,
    )


@app.get("/api/direct", response_model=DirectJourneyResponse)
def api_direct(
    from_: str = Query(..., alias="from", min_length=3, max_length=3),
    to: str = Query(..., min_length=3, max_length=3),
    date: dt.date = Query(...),
    time: dt.time = Query(...),
    window_minutes: int = Query(config.DEFAULT_WINDOW_MINUTES, ge=1, le=24 * 60),
    include_dominated: bool = Query(
        False,
        description=(
            "If true, skip Pareto-dominance filtering and return every trip in the "
            "window, including ones no rider would ever prefer over another trip in "
            "the same response (matches this endpoint's pre-issue-#19 behavior)."
        ),
    ),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Direct trips only. Dominance-filtered by default (as of GitHub issue
    #19) — pass include_dominated=true to opt out and get every scheduled
    trip in the window, unfiltered."""
    filter_dominated = not include_dominated
    if filter_dominated and window_minutes > config.MAX_DOMINATED_DIRECT_WINDOW_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"window_minutes over {config.MAX_DOMINATED_DIRECT_WINDOW_MINUTES} requires "
                "include_dominated=true — dominance filtering's cost no longer qualifies for "
                "the larger unfiltered window cap (see GitHub issue #19)."
            ),
        )
    return _run_direct_query(conn, from_, to, date, time, window_minutes, filter_dominated=filter_dominated)


@app.get("/api/journeys", response_model=JourneysResponse)
def api_journeys(
    from_: str = Query(..., alias="from", min_length=3, max_length=3),
    to: str = Query(..., min_length=3, max_length=3),
    date: dt.date = Query(...),
    time: dt.time = Query(...),
    window_minutes: int = Query(config.DEFAULT_WINDOW_MINUTES, ge=1, le=config.MAX_JOURNEYS_WINDOW_MINUTES),
    direct_only: bool = Query(False, description="If true, return only direct trains (no single-change journeys)."),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Direct and single-interchange journeys, merged and ranked. `/api/direct`
    stays direct-only and unchanged — this is the combined view (also what
    the web form/results page uses)."""
    return _run_journeys_query(conn, from_, to, date, time, window_minutes, direct_only)


@app.get("/api/stations", response_model=list[StationOut])
def api_stations(conn: sqlite3.Connection = Depends(get_db)):
    """All stations in the loaded feed — powers the search-by-name/CRS
    autocomplete on the web form."""
    return [StationOut(crs_code=s.stop_code, name=s.stop_name) for s in queries.list_stations(conn)]


@app.get("/health")
def health():
    return {"status": "ok", "dataset_present": config.GTFS_DB_PATH.exists()}


def _next_quarter_hour(now: dt.datetime) -> dt.datetime:
    """Rounds up to the next 15-minute slot (00/15/30/45); if `now` already
    sits exactly on one, keeps it rather than jumping to the next."""
    floored = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    if floored < now:
        floored += dt.timedelta(minutes=15)
    return floored


@app.get("/", response_class=HTMLResponse)
def form(request: Request):
    default_dt = _next_quarter_hour(dt.datetime.now(validation.LONDON_TZ))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "default_date": default_dt.date().isoformat(),
            "default_time": default_dt.strftime("%H:%M"),
            "default_window_minutes": config.DEFAULT_WINDOW_MINUTES,
        },
    )


@app.get("/results", response_class=HTMLResponse)
def results(
    request: Request,
    from_: str = Query(..., min_length=3, max_length=3),
    to: str = Query(..., min_length=3, max_length=3),
    date: dt.date = Query(...),
    time: dt.time = Query(...),
    window_minutes: int = Query(config.DEFAULT_WINDOW_MINUTES, ge=1, le=config.MAX_JOURNEYS_WINDOW_MINUTES),
    direct_only: bool = Query(False),
    conn: sqlite3.Connection = Depends(get_db),
):
    error = None
    result = None
    try:
        result = _run_journeys_query(conn, from_, to, date, time, window_minutes, direct_only)
    except HTTPException as exc:
        error = exc.detail

    window_start = dt.datetime.combine(date, time)
    window_end = window_start + dt.timedelta(minutes=window_minutes)

    def _shifted_url(shift: dt.timedelta) -> str:
        shifted = window_start + shift
        params = {
            "from_": from_.upper(),
            "to": to.upper(),
            "date": shifted.date().isoformat(),
            "time": shifted.strftime("%H:%M"),
            "window_minutes": window_minutes,
            "direct_only": str(direct_only).lower(),
        }
        return f"/results?{urlencode(params)}"

    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "error": error,
            "result": result,
            "from_": from_.upper(),
            "to": to.upper(),
            "date": date.isoformat(),
            "time": time.isoformat(timespec="minutes"),
            "window_end_time": window_end.strftime("%H:%M"),
            "window_end_next_day": window_end.date() > date,
            "direct_only": direct_only,
            "prev_url": _shifted_url(-dt.timedelta(minutes=window_minutes)),
            "next_url": _shifted_url(dt.timedelta(minutes=window_minutes)),
        },
    )
