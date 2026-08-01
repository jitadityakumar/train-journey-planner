from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

from app import queries

# GTFS times in this feed are UK National Rail schedule times, i.e. always
# Europe/London wall-clock (not UTC) — the "is this time in the past"
# check must compare against London time regardless of the server's own
# timezone (the Docker image sets none, so it runs UTC — a naive
# datetime.now() would be off by an hour during BST).
LONDON_TZ = ZoneInfo("Europe/London")


def validate_query(
    conn: sqlite3.Connection,
    from_crs: str,
    to_crs: str,
    date: dt.date,
    time: dt.time,
) -> tuple[queries.Stop, queries.Stop]:
    """Runs all Phase 1 validation and returns the resolved (origin, destination) stops.

    Raises UnknownStationError, SameStationError, or DateOutOfRangeError.
    Past dates/times are allowed as long as they fall within the loaded
    feed's coverage — see `is_in_past` for flagging them to callers.
    """
    origin = queries.get_station(conn, from_crs)
    destination = queries.get_station(conn, to_crs)
    if origin.stop_id == destination.stop_id:
        raise queries.SameStationError(origin.stop_code)
    queries.validate_date_in_range(conn, date)

    return origin, destination


def is_in_past(date: dt.date, time: dt.time, *, now: dt.datetime | None = None) -> bool:
    """True if the requested date/time is already behind the current
    Europe/London wall-clock time — used to flag (not reject) past
    searches, since the feed's coverage can genuinely include past dates."""
    now = now or dt.datetime.now(LONDON_TZ)
    return dt.datetime.combine(date, time, tzinfo=LONDON_TZ) < now
