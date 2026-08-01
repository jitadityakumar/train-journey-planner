"""Direct-route journey planning queries against the SQLite GTFS index.

Algorithm (Phase 1, direct routes only — see RESEARCH.md §3):
1. Resolve stop_id for each CRS code.
2. Compute which service_ids are active on the requested date (calendar
   day-of-week pattern, with calendar_dates.txt exceptions layered on top).
3. Self-join stop_times on trip_id: one row at the origin, one at the
   destination, with origin's stop_sequence before destination's, on an
   active service, with origin departure_time inside the requested window.
4. For each match, pull the full ordered stop_times slice between origin and
   destination for the intermediate-stops display.

Post-midnight handling: GTFS represents a trip that runs past physical
midnight using hours >= 24 on the *same* service day, rather than rolling
over to the next day's service_id (so a service still tagged as "Monday"
can have a 24:30:00 departure, which is 00:30 Tuesday in real clock time).
A query whose window starts right after physical midnight (e.g. date=Tuesday,
time=00:00) therefore has to also look at *Monday's* active services with
the window shifted forward by 24h — otherwise those trips are invisible
(found by code review, 2026-08-01).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field

DAY_COLUMNS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

SECONDS_PER_DAY = 24 * 3600

# Selects service_ids active on :date, with calendar_dates.txt exceptions
# applied. Left as a subquery (not materialized into Python + a giant IN
# list) so SQLite can plan/index it directly — a day can have thousands of
# active services on the real feed, which risks running into SQLite's bound
# parameter limit if inlined as literals.
_ACTIVE_SERVICE_IDS_SQL = """
    SELECT service_id FROM calendar
    WHERE {day_col} = 1 AND start_date <= :date AND end_date >= :date
    AND service_id NOT IN (
        SELECT service_id FROM calendar_dates WHERE date = :date AND exception_type = 2
    )
    UNION
    SELECT service_id FROM calendar_dates WHERE date = :date AND exception_type = 1
"""


class UnknownStationError(ValueError):
    def __init__(self, crs_code: str):
        self.crs_code = crs_code
        super().__init__(f"unknown station code: {crs_code}")


class SameStationError(ValueError):
    def __init__(self, crs_code: str):
        self.crs_code = crs_code
        super().__init__(f"origin and destination are the same station: {crs_code}")


class DateOutOfRangeError(ValueError):
    def __init__(self, requested: dt.date, min_date: dt.date, max_date: dt.date):
        self.requested = requested
        self.min_date = min_date
        self.max_date = max_date
        super().__init__(
            f"date {requested.isoformat()} is outside the loaded feed's coverage "
            f"({min_date.isoformat()} to {max_date.isoformat()})"
        )


@dataclass
class Stop:
    stop_id: str
    stop_code: str
    stop_name: str


@dataclass
class IntermediateStop:
    stop_name: str
    stop_code: str
    arrival_time: str
    departure_time: str


@dataclass
class DirectTrip:
    trip_id: str
    route_short_name: str | None
    route_long_name: str | None
    trip_headsign: str | None
    departure_time: str
    arrival_time: str
    departure_next_day: bool
    arrival_next_day: bool
    duration_minutes: int
    intermediate_stops: list[IntermediateStop] = field(default_factory=list)


def get_station(conn: sqlite3.Connection, crs_code: str) -> Stop:
    row = conn.execute(
        "SELECT stop_id, stop_code, stop_name FROM stops WHERE stop_code = ? LIMIT 1",
        (crs_code.strip().upper(),),
    ).fetchone()
    if row is None:
        raise UnknownStationError(crs_code)
    return Stop(stop_id=row["stop_id"], stop_code=row["stop_code"], stop_name=row["stop_name"])


def feed_date_range(conn: sqlite3.Connection) -> tuple[dt.date, dt.date]:
    # Union calendar.txt's start/end range with calendar_dates.txt's own
    # dates — a service_id can exist only via calendar_dates.txt (pure
    # additions), which calendar.txt's range alone would miss.
    row = conn.execute(
        """
        SELECT MIN(d) AS min_d, MAX(d) AS max_d FROM (
            SELECT start_date AS d FROM calendar
            UNION ALL SELECT end_date FROM calendar
            UNION ALL SELECT date FROM calendar_dates
        )
        """
    ).fetchone()
    if row is None or row["min_d"] is None:
        raise RuntimeError("calendar table is empty — GTFS feed did not load correctly")
    return _parse_gtfs_date(row["min_d"]), _parse_gtfs_date(row["max_d"])


def validate_date_in_range(conn: sqlite3.Connection, date: dt.date) -> None:
    min_date, max_date = feed_date_range(conn)
    if not (min_date <= date <= max_date):
        raise DateOutOfRangeError(date, min_date, max_date)


def active_service_ids(conn: sqlite3.Connection, date: dt.date) -> set[str]:
    """Public helper kept for callers/tests that want the resolved set
    directly; find_direct_trips itself uses the SQL fragment inline."""
    day_col = DAY_COLUMNS[date.weekday()]
    sql = _ACTIVE_SERVICE_IDS_SQL.format(day_col=day_col)
    rows = conn.execute(sql, {"date": _format_gtfs_date(date)}).fetchall()
    return {row["service_id"] for row in rows}


def find_direct_trips(
    conn: sqlite3.Connection,
    origin: Stop,
    destination: Stop,
    date: dt.date,
    window_start: dt.time,
    window_minutes: int,
) -> list[DirectTrip]:
    """All direct trips origin -> destination departing within the window on `date`."""
    if origin.stop_id == destination.stop_id:
        raise SameStationError(origin.stop_code)

    window_start_secs = _time_to_seconds(window_start)
    window_end_secs = window_start_secs + window_minutes * 60

    # Two service-day buckets: `date` itself (offset 0), and `date - 1 day`
    # shifted forward by 24h (offset 86400) to catch that previous day's
    # post-midnight (>=24:00:00) continuation trips landing inside today's
    # window — see module docstring.
    buckets = [
        (date, 0),
        (date - dt.timedelta(days=1), SECONDS_PER_DAY),
    ]

    rows: list[tuple[sqlite3.Row, int]] = []
    for bucket_date, offset in buckets:
        bucket_rows = _query_direct_trips(
            conn,
            origin.stop_id,
            destination.stop_id,
            DAY_COLUMNS[bucket_date.weekday()],
            _format_gtfs_date(bucket_date),
            window_start_secs + offset,
            window_end_secs + offset,
        )
        rows.extend((row, offset) for row in bucket_rows)

    # Sort by real-world departure order: subtracting each row's bucket
    # offset undoes the day-1 shift so both buckets compare on the same
    # (0, 24h) real-clock scale.
    rows.sort(key=lambda pair: pair[0]["dep_secs"] - pair[1])

    results = []
    for row, offset in rows:
        intermediate = _intermediate_stops(conn, row["trip_id"], row["origin_seq"], row["dest_seq"])
        duration_minutes = round((row["arr_secs"] - row["dep_secs"]) / 60)
        dep_time, dep_next_day = _normalize_clock(row["dep_secs"], offset)
        arr_time, arr_next_day = _normalize_clock(row["arr_secs"], offset)
        results.append(
            DirectTrip(
                trip_id=row["trip_id"],
                route_short_name=row["route_short_name"] or None,
                route_long_name=row["route_long_name"] or None,
                trip_headsign=row["trip_headsign"] or None,
                departure_time=dep_time,
                arrival_time=arr_time,
                departure_next_day=dep_next_day,
                arrival_next_day=arr_next_day,
                duration_minutes=duration_minutes,
                intermediate_stops=intermediate,
            )
        )
    return results


def _query_direct_trips(
    conn: sqlite3.Connection,
    origin_stop_id: str,
    destination_stop_id: str,
    day_col: str,
    date_str: str,
    lo_secs: int,
    hi_secs: int,
) -> list[sqlite3.Row]:
    active_sql = _ACTIVE_SERVICE_IDS_SQL.format(day_col=day_col)
    sql = f"""
        SELECT
            t.trip_id, t.trip_headsign,
            r.route_short_name, r.route_long_name,
            st1.stop_sequence AS origin_seq, st1.departure_secs AS dep_secs,
            st2.stop_sequence AS dest_seq, st2.arrival_secs AS arr_secs
        FROM trips t
        JOIN stop_times st1 ON st1.trip_id = t.trip_id AND st1.stop_id = :origin_id
        JOIN stop_times st2 ON st2.trip_id = t.trip_id AND st2.stop_id = :destination_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE st1.stop_sequence < st2.stop_sequence
          AND st1.departure_secs >= :lo AND st1.departure_secs < :hi
          AND t.service_id IN ({active_sql})
    """
    params = {
        "origin_id": origin_stop_id,
        "destination_id": destination_stop_id,
        "date": date_str,
        "lo": lo_secs,
        "hi": hi_secs,
    }
    return conn.execute(sql, params).fetchall()


def _intermediate_stops(
    conn: sqlite3.Connection, trip_id: str, origin_seq: int, dest_seq: int
) -> list[IntermediateStop]:
    rows = conn.execute(
        """
        SELECT s.stop_name, s.stop_code, st.arrival_secs, st.departure_secs
        FROM stop_times st
        JOIN stops s ON s.stop_id = st.stop_id
        WHERE st.trip_id = ? AND st.stop_sequence > ? AND st.stop_sequence < ?
        ORDER BY st.stop_sequence
        """,
        (trip_id, origin_seq, dest_seq),
    ).fetchall()
    return [
        IntermediateStop(
            stop_name=row["stop_name"],
            stop_code=row["stop_code"],
            arrival_time=_normalize_clock(row["arrival_secs"], 0)[0],
            departure_time=_normalize_clock(row["departure_secs"], 0)[0],
        )
        for row in rows
    ]


def _normalize_clock(secs: int, bucket_offset: int) -> tuple[str, bool]:
    """Wall-clock HH:MM:SS for a raw (possibly >=24h) GTFS seconds value,
    plus whether it falls on the day after the query's `date`.

    Raw seconds wrap to a real time-of-day via `% 86400` regardless of which
    day-bucket produced the row (see find_direct_trips docstring — a
    `date - 1 day` bucket match with raw secs >= 86400 is, by construction,
    exactly on `date`, not the day after). Only a same-day bucket match
    (`bucket_offset == 0`) with raw secs >= 86400 is genuinely one calendar
    day past the requested `date`.
    """
    wrapped = secs % SECONDS_PER_DAY
    h, rem = divmod(wrapped, 3600)
    m, s = divmod(rem, 60)
    next_day = bucket_offset == 0 and secs >= SECONDS_PER_DAY
    return f"{h:02d}:{m:02d}:{s:02d}", next_day


def _time_to_seconds(t: dt.time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _parse_gtfs_date(date_str: str) -> dt.date:
    return dt.datetime.strptime(date_str, "%Y%m%d").date()


def _format_gtfs_date(date: dt.date) -> str:
    return date.strftime("%Y%m%d")
