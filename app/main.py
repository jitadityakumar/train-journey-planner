from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
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


def get_db():
    if not config.GTFS_DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="GTFS dataset is not loaded yet — try again shortly.",
        )
    conn = get_readonly_connection(config.GTFS_DB_PATH)
    try:
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
    except validation.PastTimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _direct_trip_out(t: queries.DirectTrip) -> DirectTripOut:
    return DirectTripOut(
        trip_id=t.trip_id,
        operator=t.agency_name,
        route_description=t.route_short_name or t.route_long_name,
        headsign=t.trip_headsign,
        departure_time=t.departure_time,
        arrival_time=t.arrival_time,
        departure_next_day=t.departure_next_day,
        arrival_next_day=t.arrival_next_day,
        duration_minutes=t.duration_minutes,
        intermediate_stops=[
            IntermediateStopOut(
                stop_name=s.stop_name,
                stop_code=s.stop_code,
                arrival_time=s.arrival_time,
                departure_time=s.departure_time,
            )
            for s in t.intermediate_stops
        ],
    )


def _run_direct_query(
    conn: sqlite3.Connection,
    from_crs: str,
    to_crs: str,
    date: dt.date,
    time: dt.time,
    window_minutes: int,
) -> DirectJourneyResponse:
    origin, destination = _validate_or_400(conn, from_crs, to_crs, date, time)
    trips = queries.find_direct_trips(conn, origin, destination, date, time, window_minutes)

    return DirectJourneyResponse(
        origin=StationOut(crs_code=origin.stop_code, name=origin.stop_name),
        destination=StationOut(crs_code=destination.stop_code, name=destination.stop_name),
        date=date.isoformat(),
        window_start=time.isoformat(timespec="minutes"),
        window_minutes=window_minutes,
        trips=[_direct_trip_out(t) for t in trips],
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
                leg1=_direct_trip_out(j.interchange.leg1),
                leg2=_direct_trip_out(j.interchange.leg2),
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
                direct=_direct_trip_out(j.direct) if j.direct is not None else None,
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
    conn: sqlite3.Connection = Depends(get_db),
):
    return _run_direct_query(conn, from_, to, date, time, window_minutes)


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

    window_end = dt.datetime.combine(date, time) + dt.timedelta(minutes=window_minutes)

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
        },
    )
