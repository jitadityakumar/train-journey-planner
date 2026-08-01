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


class PastTimeError(ValueError):
    def __init__(self, requested: dt.datetime, now: dt.datetime):
        self.requested = requested
        self.now = now
        super().__init__("requested time is in the past")


def validate_query(
    conn: sqlite3.Connection,
    from_crs: str,
    to_crs: str,
    date: dt.date,
    time: dt.time,
    *,
    now: dt.datetime | None = None,
) -> tuple[queries.Stop, queries.Stop]:
    """Runs all Phase 1 validation and returns the resolved (origin, destination) stops.

    Raises UnknownStationError, SameStationError, DateOutOfRangeError, or
    PastTimeError.
    """
    origin = queries.get_station(conn, from_crs)
    destination = queries.get_station(conn, to_crs)
    if origin.stop_id == destination.stop_id:
        raise queries.SameStationError(origin.stop_code)
    queries.validate_date_in_range(conn, date)

    now = now or dt.datetime.now(LONDON_TZ)
    if date == now.date() and time < now.time():
        requested = dt.datetime.combine(date, time)
        raise PastTimeError(requested, now)

    return origin, destination
