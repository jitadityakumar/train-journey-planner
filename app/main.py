from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
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
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
        operator=t.route_short_name or t.route_long_name,
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
) -> JourneysResponse:
    origin, destination = _validate_or_400(conn, from_crs, to_crs, date, time)
    journeys = queries.find_journeys(conn, origin, destination, date, time, window_minutes)

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
    conn: sqlite3.Connection = Depends(get_db),
):
    """Direct and single-interchange journeys, merged and ranked. `/api/direct`
    stays direct-only and unchanged — this is the combined view (also what
    the web form/results page uses)."""
    return _run_journeys_query(conn, from_, to, date, time, window_minutes)


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
    conn: sqlite3.Connection = Depends(get_db),
):
    error = None
    result = None
    try:
        result = _run_journeys_query(conn, from_, to, date, time, window_minutes)
    except HTTPException as exc:
        error = exc.detail

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
        },
    )
