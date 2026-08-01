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

from app.config import MAX_CONNECTION_TIME_MINUTES, MIN_CONNECTION_TIME_MINUTES

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


@dataclass
class InterchangeTrip:
    leg1: DirectTrip
    leg2: DirectTrip
    interchange: Stop
    connection_minutes: int
    total_duration_minutes: int


@dataclass
class Journey:
    """A direct or single-interchange result, flattened enough for the two
    kinds to be merged and sorted together (see find_journeys)."""

    kind: str  # "direct" or "interchange"
    departure_time: str
    departure_next_day: bool
    arrival_time: str
    arrival_next_day: bool
    duration_minutes: int
    direct: DirectTrip | None = None
    interchange: InterchangeTrip | None = None


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

    rows: list[tuple[sqlite3.Row, int]] = []
    for bucket_date, offset in _day_buckets(date):
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


def find_interchange_trips(
    conn: sqlite3.Connection,
    origin: Stop,
    destination: Stop,
    date: dt.date,
    window_start: dt.time,
    window_minutes: int,
    *,
    min_connection_minutes: int = MIN_CONNECTION_TIME_MINUTES,
    max_connection_minutes: int = MAX_CONNECTION_TIME_MINUTES,
) -> list[InterchangeTrip]:
    """All single-interchange journeys origin -> C -> destination, where leg 1
    departs origin within the window and leg 2 departs the interchange stop C
    between `min_connection_minutes` and `max_connection_minutes` after leg 1
    arrives (flat minimum connection time — see RESEARCH.md's MCT section;
    real per-station values aren't available without a CIF-native source).

    Leg 1 (origin -> any downstream stop C) is a bespoke query, since C isn't
    known in advance. Leg 2 (C -> destination) reuses find_direct_trips
    directly — a plain direct-route search anchored at the interchange's real
    arrival time — which gets the post-midnight handling for free (both the
    interchange's own day-boundary, and the leg-2 window's own).

    Trips that already reach `destination` directly (without changing) are
    excluded from leg 1's candidates — Phase 1's direct search already finds
    those, and offering a needless interchange onto a different train would
    never be a better option, so this is also this function's answer to
    "de-duplicate against direct results" (see RESEARCH.md §3).
    """
    if origin.stop_id == destination.stop_id:
        raise SameStationError(origin.stop_code)

    window_start_secs = _time_to_seconds(window_start)
    window_end_secs = window_start_secs + window_minutes * 60

    leg1_rows: list[tuple[sqlite3.Row, dt.date, int]] = []
    for bucket_date, offset in _day_buckets(date):
        bucket_rows = _query_leg1_candidates(
            conn,
            origin.stop_id,
            destination.stop_id,
            DAY_COLUMNS[bucket_date.weekday()],
            _format_gtfs_date(bucket_date),
            window_start_secs + offset,
            window_end_secs + offset,
        )
        leg1_rows.extend((row, bucket_date, offset) for row in bucket_rows)

    results: list[InterchangeTrip] = []
    for row, bucket_date, offset in leg1_rows:
        leg1_dep_date = _bucket_real_date(bucket_date, offset, row["dep_secs"])
        leg1_departure_dt = dt.datetime.combine(leg1_dep_date, dt.time()) + dt.timedelta(
            seconds=row["dep_secs"] % SECONDS_PER_DAY
        )
        arrival_date = _bucket_real_date(bucket_date, offset, row["arr_secs"])
        arrival_dt = dt.datetime.combine(arrival_date, dt.time()) + dt.timedelta(
            seconds=row["arr_secs"] % SECONDS_PER_DAY
        )

        dep_time, dep_next_day = _normalize_clock(row["dep_secs"], offset)
        arr_time, arr_next_day = _normalize_clock(row["arr_secs"], offset)
        leg1 = DirectTrip(
            trip_id=row["trip_id"],
            route_short_name=row["route_short_name"] or None,
            route_long_name=row["route_long_name"] or None,
            trip_headsign=row["trip_headsign"] or None,
            departure_time=dep_time,
            arrival_time=arr_time,
            departure_next_day=dep_next_day,
            arrival_next_day=arr_next_day,
            duration_minutes=round((row["arr_secs"] - row["dep_secs"]) / 60),
            intermediate_stops=_intermediate_stops(conn, row["trip_id"], row["origin_seq"], row["interchange_seq"]),
        )
        interchange_stop = Stop(
            stop_id=row["interchange_stop_id"],
            stop_code=row["interchange_stop_code"],
            stop_name=row["interchange_stop_name"],
        )

        leg2_window_start_dt = arrival_dt + dt.timedelta(minutes=min_connection_minutes)
        leg2_window_minutes = max_connection_minutes - min_connection_minutes
        leg2_candidates = find_direct_trips(
            conn,
            interchange_stop,
            destination,
            leg2_window_start_dt.date(),
            leg2_window_start_dt.time(),
            leg2_window_minutes,
        )

        for leg2 in leg2_candidates:
            if leg2.trip_id == leg1.trip_id:
                # Same physical trip re-appearing at the interchange stop
                # (a loop-line service can call at one station twice — see
                # RESEARCH.md's Data Validation section) isn't a real change.
                continue

            leg2_anchor_date = leg2_window_start_dt.date()
            leg2_departure_dt = _combine_with_next_day(
                leg2_anchor_date, leg2.departure_time, leg2.departure_next_day
            )
            leg2_arrival_dt = _combine_with_next_day(
                leg2_anchor_date, leg2.arrival_time, leg2.arrival_next_day
            )

            results.append(
                InterchangeTrip(
                    leg1=leg1,
                    leg2=leg2,
                    interchange=interchange_stop,
                    connection_minutes=round((leg2_departure_dt - arrival_dt).total_seconds() / 60),
                    total_duration_minutes=round(
                        (leg2_arrival_dt - leg1_departure_dt).total_seconds() / 60
                    ),
                )
            )

    return results


def find_journeys(
    conn: sqlite3.Connection,
    origin: Stop,
    destination: Stop,
    date: dt.date,
    window_start: dt.time,
    window_minutes: int,
) -> list[Journey]:
    """Direct and single-interchange journeys, merged and ranked by
    departure time (then duration) — see RESEARCH.md §3's ranking rule."""
    direct_trips = find_direct_trips(conn, origin, destination, date, window_start, window_minutes)
    interchange_trips = find_interchange_trips(conn, origin, destination, date, window_start, window_minutes)

    journeys = [
        Journey(
            kind="direct",
            departure_time=t.departure_time,
            departure_next_day=t.departure_next_day,
            arrival_time=t.arrival_time,
            arrival_next_day=t.arrival_next_day,
            duration_minutes=t.duration_minutes,
            direct=t,
        )
        for t in direct_trips
    ] + [
        Journey(
            kind="interchange",
            departure_time=i.leg1.departure_time,
            departure_next_day=i.leg1.departure_next_day,
            arrival_time=i.leg2.arrival_time,
            arrival_next_day=i.leg2.arrival_next_day,
            duration_minutes=i.total_duration_minutes,
            interchange=i,
        )
        for i in interchange_trips
    ]
    journeys.sort(key=lambda j: (_absolute_minutes(j.departure_time, j.departure_next_day), j.duration_minutes))
    return journeys


def _query_leg1_candidates(
    conn: sqlite3.Connection,
    origin_stop_id: str,
    destination_stop_id: str,
    day_col: str,
    date_str: str,
    lo_secs: int,
    hi_secs: int,
) -> list[sqlite3.Row]:
    """Every (trip, later stop) pair from `origin_stop_id`, for trips that do
    *not* also reach `destination_stop_id` directly — i.e. every plausible
    interchange candidate. Naturally bounded by real service patterns (the
    handful of trips departing in the window, times however many stops each
    one makes) rather than needing a curated list of major stations."""
    active_sql = _ACTIVE_SERVICE_IDS_SQL.format(day_col=day_col)
    sql = f"""
        SELECT
            t.trip_id, t.trip_headsign,
            r.route_short_name, r.route_long_name,
            a.stop_sequence AS origin_seq, a.departure_secs AS dep_secs,
            c.stop_id AS interchange_stop_id, c.stop_sequence AS interchange_seq,
            c.arrival_secs AS arr_secs,
            cs.stop_code AS interchange_stop_code, cs.stop_name AS interchange_stop_name
        FROM trips t
        JOIN stop_times a ON a.trip_id = t.trip_id AND a.stop_id = :origin_id
        JOIN stop_times c ON c.trip_id = t.trip_id AND c.stop_sequence > a.stop_sequence
        JOIN stops cs ON cs.stop_id = c.stop_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE a.departure_secs >= :lo AND a.departure_secs < :hi
          AND c.stop_id != :destination_id
          AND t.service_id IN ({active_sql})
          AND NOT EXISTS (
              SELECT 1 FROM stop_times bx
              WHERE bx.trip_id = t.trip_id AND bx.stop_id = :destination_id
                AND bx.stop_sequence > a.stop_sequence
          )
    """
    params = {
        "origin_id": origin_stop_id,
        "destination_id": destination_stop_id,
        "date": date_str,
        "lo": lo_secs,
        "hi": hi_secs,
    }
    return conn.execute(sql, params).fetchall()


def _day_buckets(date: dt.date) -> list[tuple[dt.date, int]]:
    # `date` itself (offset 0), and `date - 1 day` shifted forward by 24h
    # (offset 86400) to catch that previous day's post-midnight (>=24:00:00)
    # continuation trips landing inside today's window — see module
    # docstring.
    return [(date, 0), (date - dt.timedelta(days=1), SECONDS_PER_DAY)]


def _bucket_real_date(bucket_date: dt.date, offset: int, raw_secs: int) -> dt.date:
    """The real calendar date a raw (possibly >=24h) seconds value falls on,
    given which day-bucket (see _day_buckets) produced it."""
    if offset == 0:
        return bucket_date + dt.timedelta(days=1) if raw_secs >= SECONDS_PER_DAY else bucket_date
    return bucket_date + dt.timedelta(days=1)


def _combine_with_next_day(anchor_date: dt.date, time_str: str, next_day: bool) -> dt.datetime:
    combined = dt.datetime.combine(anchor_date, dt.time.fromisoformat(time_str))
    return combined + dt.timedelta(days=1) if next_day else combined


def _absolute_minutes(time_str: str, next_day: bool) -> int:
    t = dt.time.fromisoformat(time_str)
    minutes = t.hour * 60 + t.minute
    return minutes + (24 * 60 if next_day else 0)


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
