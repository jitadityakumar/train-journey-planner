"""Direct and single-interchange journey planning queries against the
SQLite GTFS index.

Direct-route algorithm (Phase 1 — see RESEARCH.md §3):
1. Resolve stop_id for each CRS code.
2. Compute which service_ids are active on the requested date (calendar
   day-of-week pattern, with calendar_dates.txt exceptions layered on top).
3. Self-join stop_times on trip_id: one row at the origin, one at the
   destination, with origin's stop_sequence before destination's, on an
   active service, with origin departure_time inside the requested window.
4. For each match, pull the full ordered stop_times slice between origin and
   destination for the intermediate-stops display.

Single-interchange algorithm (Phase 2): leg 1 (origin -> some interchange
stop C, not known in advance) is a bespoke query; leg 2 (C -> destination)
reuses the direct-route search unmodified, anchored at leg 1's real arrival
time plus the minimum connection time. See find_interchange_trips.

Post-midnight handling: GTFS represents a trip that runs past physical
midnight using hours >= 24 on the *same* service day, rather than rolling
over to the next day's service_id (so a service still tagged as "Monday"
can have a 24:30:00 departure, which is 00:30 Tuesday in real clock time).
A query window can therefore need to look at up to three service days:
`date` itself, `date - 1` (whose >=24:00:00 continuation can land inside
today's window if the window starts soon after physical midnight), and
`date + 1` (whose own early trips can be the *other* valid way to reach a
window that itself extends past physical midnight — e.g. a window from
23:30 to 00:30 needs both `date`'s own >=24:00:00 trips *and* `date + 1`'s
plain <24:00:00 ones; missing the latter was found in code review,
2026-08-01, once Phase 2's leg-2 windows started being anchored at
arbitrary arrival times rather than only at round query times).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from app.config import (
    DOMINANCE_FETCH_BUFFER_MAX_MINUTES,
    DOMINANCE_FETCH_BUFFER_MIN_MINUTES,
    MAX_CONNECTION_TIME_MINUTES,
    MIN_CONNECTION_TIME_MINUTES,
    STATION_ALIASES,
)

DAY_COLUMNS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

SECONDS_PER_DAY = 24 * 3600


def _dominance_fetch_buffer_minutes(window_minutes: int) -> int:
    """How much wider than the requested display window to fetch dominance-
    filtering candidates from (see DOMINANCE_FETCH_BUFFER_MIN/MAX_MINUTES's
    docstring in config.py) — floor-and-cap, not a flat proportion of
    `window_minutes`, so this stays sane across this app's full range of
    window sizes (1 minute up to /api/direct's 24h cap)."""
    return min(max(window_minutes, DOMINANCE_FETCH_BUFFER_MIN_MINUTES), DOMINANCE_FETCH_BUFFER_MAX_MINUTES)

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
    agency_name: str | None
    agency_id: str | None
    route_short_name: str | None
    route_long_name: str | None
    trip_headsign: str | None
    departure_time: str
    arrival_time: str
    departure_next_day: bool
    arrival_next_day: bool
    duration_minutes: int
    intermediate_stops: list[IntermediateStop] = field(default_factory=list)
    # Set when this trip is a synthesized reversal-continuation (see
    # db._build_trip_continuations / GitHub issue #15) and the requested
    # journey genuinely spans both legs — the stop where the physical train
    # terminates and reverses direction under a new trip_id, not an ordinary
    # intermediate stop. None for every plain, single-trip_id result.
    reverses_at: Stop | None = None


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


_ALIAS_TO_PRIMARY: dict[str, str] = {
    alias: primary for primary, aliases in STATION_ALIASES.items() for alias in aliases
}
# Codes that shouldn't be offered as their own searchable station (see
# list_stations) — they're folded into their primary code's search
# automatically via _alias_codes/_alias_stop_ids instead.
HIDDEN_ALIAS_CODES: frozenset[str] = frozenset(_ALIAS_TO_PRIMARY)


def _alias_codes(crs_code: str) -> frozenset[str]:
    """The full group of CRS-like codes representing the same physical
    station complex as `crs_code` for direct-route search purposes (see
    STATION_ALIASES in config.py) — just `{crs_code}` for the overwhelming
    majority of stations that have no known split."""
    primary = _ALIAS_TO_PRIMARY.get(crs_code, crs_code)
    return frozenset({primary, *STATION_ALIASES.get(primary, ())})


def is_same_station_group(a: Stop, b: Stop) -> bool:
    """True if `a` and `b` are literally the same stop, or are the two
    (mainline/Elizabeth-line) sides of the same alias group (e.g. PAD and
    PDX — see STATION_ALIASES in config.py). find_direct_trips now treats
    both sides of an alias group as "the same physical station complex" for
    search purposes, so a query between them (e.g. from=PAD&to=PDX) is
    logically a same-station query too, and should be rejected the same way
    a literal PAD-to-PAD query is — not silently fall through to an empty
    "no journeys found" result (found in code review, 2026-08-01)."""
    return a.stop_id == b.stop_id or b.stop_code in _alias_codes(a.stop_code)


def _alias_stop_ids(conn: sqlite3.Connection, stop: Stop) -> list[str]:
    """All stop_ids matching `stop`'s alias group (see _alias_codes),
    resolved with the same lowest-rowid tie-break get_station/list_stations
    use, so results are consistent with what a plain single-code lookup
    would return. Used to widen a direct-route search so e.g. a query
    against PAD also matches trips that literally depart/arrive at PDX
    (Paddington's separately-coded Elizabeth line platforms) — see
    STATION_ALIASES's docstring in config.py. A code missing from this
    particular feed is skipped rather than erroring, since alias membership
    is a static list independent of what a given feed snapshot contains."""
    codes = _alias_codes(stop.stop_code)
    if len(codes) == 1:
        return [stop.stop_id]
    ids = []
    for code in codes:
        if code == stop.stop_code:
            ids.append(stop.stop_id)
            continue
        row = conn.execute(
            "SELECT stop_id FROM stops WHERE stop_code = ? ORDER BY rowid LIMIT 1",
            (code,),
        ).fetchone()
        if row is not None:
            ids.append(row["stop_id"])
    return ids


def _constituent_trip_ids(conn: sqlite3.Connection, trip_id: str) -> frozenset[str]:
    """The full set of real physical trip_ids `trip_id` represents — just
    `{trip_id}` for an ordinary trip that's never part of a reversal, or the
    complete "reversal family" of physical legs it belongs to (see
    db._build_trip_continuations / GitHub issue #15). Used to detect "same
    physical train" when comparing two trip_ids that might not be literally
    equal but do share a leg.

    Reversal-continuation synthesis is scoped to pairs only (no chained 3+
    leg trips are synthesized as a single trip_id), but a physical trip can
    still be *part of* more than one pair — e.g. C2 is leg2 of C1+C2 and
    also leg1 of C2+C3 for a train that reverses twice. Walking the
    synthesized_trips graph (by leg1_trip_id/leg2_trip_id, not just by
    trip_id primary key) finds the full connected family in that case —
    {C1, C2, C3} — rather than stopping at whichever single pair `trip_id`
    happens to be a member or constituent of, which would otherwise let
    e.g. C1+C2 (as leg1) and the plain trip C3 (as leg2) pass this "same
    train" check even though C1-C2-C3 is one physical reversing train
    (found in code review, 2026-08-01)."""
    physical_ids: set[str] = set()
    row = conn.execute(
        "SELECT leg1_trip_id, leg2_trip_id FROM synthesized_trips WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    frontier = {row["leg1_trip_id"], row["leg2_trip_id"]} if row is not None else {trip_id}

    while frontier:
        current = frontier.pop()
        if current in physical_ids:
            continue
        physical_ids.add(current)
        for pair in conn.execute(
            "SELECT leg1_trip_id, leg2_trip_id FROM synthesized_trips "
            "WHERE leg1_trip_id = ? OR leg2_trip_id = ?",
            (current, current),
        ).fetchall():
            frontier.add(pair["leg1_trip_id"])
            frontier.add(pair["leg2_trip_id"])

    return frozenset(physical_ids)


def _in_clause(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    """Builds a parameterized `(:p0, :p1, ...)` SQL fragment for an IN
    clause over `values` — alias groups are tiny (currently at most 2), so
    this stays well clear of SQLite's bound-parameter limit even though the
    rest of this module deliberately avoids inlining dynamic IN lists (see
    _ACTIVE_SERVICE_IDS_SQL's docstring)."""
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return "(" + ", ".join(f":{k}" for k in params) + ")", params


def list_stations(conn: sqlite3.Connection) -> list[Stop]:
    """All stations in the feed with a real CRS code, one row per code (a
    code can back multiple stop_id platform records — picks the
    lowest-rowid row per code, same tie-break get_station's unordered
    LIMIT 1 effectively uses, so a station's name is consistent between the
    two). Stops with no CRS code (parent stations, non-rail stops) are
    excluded rather than collapsed into one bogus NULL-code group.

    Codes in HIDDEN_ALIAS_CODES (e.g. PDX, LSX — see STATION_ALIASES in
    config.py) are excluded too: they're the same physical station complex
    as their primary code (PAD, LST), already folded into that code's
    search automatically, and not a code an ordinary rider would think to
    search for — see GitHub issue #11."""
    rows = conn.execute(
        """
        SELECT stop_id, stop_code, stop_name FROM stops s1
        WHERE stop_code IS NOT NULL AND TRIM(stop_code) != ''
          AND rowid = (SELECT MIN(rowid) FROM stops s2 WHERE s2.stop_code = s1.stop_code)
        ORDER BY stop_name
        """
    ).fetchall()
    return [
        Stop(stop_id=r["stop_id"], stop_code=r["stop_code"], stop_name=r["stop_name"])
        for r in rows
        if r["stop_code"] not in HIDDEN_ALIAS_CODES
    ]


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


def _fetch_direct_trip_rows(
    conn: sqlite3.Connection,
    origin: Stop,
    destination: Stop,
    date: dt.date,
    window_start: dt.time,
    window_minutes: int,
) -> list[tuple[sqlite3.Row, dt.date]]:
    """Raw (row, bucket_date) pairs for direct-route candidates departing
    origin -> destination within the window on `date`, sorted by real-world
    departure order — the shared fetch step behind find_direct_trips and
    dominant_direct_trips below. Deliberately doesn't build DirectTrip
    objects or run the per-row _intermediate_stops query: a widened fetch
    that's going to be dominance-filtered and trimmed back down shouldn't pay
    for that on every candidate, only the survivors (see dominant_direct_trips)."""
    if is_same_station_group(origin, destination):
        raise SameStationError(origin.stop_code)

    origin_stop_ids = _alias_stop_ids(conn, origin)
    destination_stop_ids = _alias_stop_ids(conn, destination)

    window_start_secs = _time_to_seconds(window_start)
    window_end_secs = window_start_secs + window_minutes * 60

    rows: list[tuple[sqlite3.Row, dt.date]] = []
    for bucket_date, offset in _day_buckets(date, window_start_secs, window_end_secs):
        bucket_rows = _query_direct_trips(
            conn,
            origin_stop_ids,
            destination_stop_ids,
            DAY_COLUMNS[bucket_date.weekday()],
            _format_gtfs_date(bucket_date),
            window_start_secs + offset,
            window_end_secs + offset,
        )
        rows.extend((row, bucket_date) for row in bucket_rows)

    # Sort by real-world departure order.
    rows.sort(key=lambda pair: _absolute_datetime(pair[1], pair[0]["dep_secs"]))
    return rows


def _build_direct_trip(conn: sqlite3.Connection, row: sqlite3.Row, bucket_date: dt.date, date: dt.date) -> DirectTrip:
    intermediate = _intermediate_stops(conn, row["trip_id"], row["origin_seq"], row["dest_seq"])
    duration_minutes = round((row["arr_secs"] - row["dep_secs"]) / 60)
    dep_dt = _absolute_datetime(bucket_date, row["dep_secs"])
    arr_dt = _absolute_datetime(bucket_date, row["arr_secs"])
    return DirectTrip(
        trip_id=row["trip_id"],
        agency_name=row["agency_name"] or None,
        agency_id=row["agency_id"] or None,
        route_short_name=row["route_short_name"] or None,
        route_long_name=row["route_long_name"] or None,
        trip_headsign=row["trip_headsign"] or None,
        departure_time=dep_dt.strftime("%H:%M:%S"),
        arrival_time=arr_dt.strftime("%H:%M:%S"),
        departure_next_day=dep_dt.date() > date,
        arrival_next_day=arr_dt.date() > date,
        duration_minutes=duration_minutes,
        intermediate_stops=intermediate,
        reverses_at=(
            Stop(
                stop_id=row["reversal_stop_id"],
                stop_code=row["reversal_stop_code"],
                stop_name=row["reversal_stop_name"],
            )
            if row["reversal_stop_id"] is not None
            else None
        ),
    )


def _row_departure_arrival_secs(row: sqlite3.Row, bucket_date: dt.date, date: dt.date) -> tuple[int, int]:
    """(departure, arrival) in seconds relative to `date`'s midnight — the
    same domain find_direct_trips'/find_interchange_trips' own
    window_start_secs/window_end_secs bounds use, so it's directly
    comparable to a window cutoff regardless of which service-day bucket the
    row actually matched (see _day_buckets)."""
    base = dt.datetime.combine(date, dt.time())
    dep = int((_absolute_datetime(bucket_date, row["dep_secs"]) - base).total_seconds())
    arr = int((_absolute_datetime(bucket_date, row["arr_secs"]) - base).total_seconds())
    return dep, arr


def find_direct_trips(
    conn: sqlite3.Connection,
    origin: Stop,
    destination: Stop,
    date: dt.date,
    window_start: dt.time,
    window_minutes: int,
) -> list[DirectTrip]:
    """All direct trips origin -> destination departing within the window on
    `date`. Also matches trips at either station's alias stop_ids (e.g. a
    search against PAD also matches PDX, Paddington's separately-coded
    Elizabeth line platforms — see STATION_ALIASES in config.py / GitHub
    issue #11) — a rider at either platform is at the same physical station
    complex, so this needs no interchange/MCT, unlike find_interchange_trips'
    genuine interchange search below.

    Plain, unwidened, undominated — the window here is exactly what's
    fetched and returned, no dominance filtering or trimming applied. This
    is deliberate: find_interchange_trips' leg-2 anchoring reuses this
    function with a connection-feasibility window (not a display window),
    and widening/filtering it would both change /api/journeys' existing
    behavior and multiply leg-2 query cost (see GitHub issue #19). Top-level
    callers that want dominance filtering against a display window should
    use dominant_direct_trips instead."""
    rows = _fetch_direct_trip_rows(conn, origin, destination, date, window_start, window_minutes)
    return [_build_direct_trip(conn, row, bucket_date, date) for row, bucket_date in rows]


def dominant_direct_trips(
    conn: sqlite3.Connection,
    origin: Stop,
    destination: Stop,
    date: dt.date,
    window_start: dt.time,
    window_minutes: int,
) -> list[DirectTrip]:
    """Direct trips departing in [window_start, window_start + window_minutes],
    Pareto-filtered against a wider fetch so a faster trip departing just
    after the display window can correctly dominate a slower one inside it
    (GitHub issue #19's window-boundary follow-up — see
    _dominance_fetch_buffer_minutes). Dominance is decided on the cheap
    (departure_secs, arrival_secs) key before paying for the per-row
    _intermediate_stops query, so the wider fetch mostly only costs extra for
    candidates that actually survive to be displayed.

    Used by /api/direct's own handler. NOT used by find_interchange_trips'
    leg-2 anchoring — see find_direct_trips' docstring for why that has to
    stay untouched."""
    buffer_minutes = _dominance_fetch_buffer_minutes(window_minutes)
    rows = _fetch_direct_trip_rows(conn, origin, destination, date, window_start, window_minutes + buffer_minutes)

    def key(pair: tuple[sqlite3.Row, dt.date]) -> tuple[int, int, int]:
        return _row_departure_arrival_secs(pair[0], pair[1], date) + (0,)

    survivors = _drop_dominated(rows, key)

    window_end_secs = _time_to_seconds(window_start) + window_minutes * 60
    trimmed = [pair for pair in survivors if key(pair)[0] < window_end_secs]

    return [_build_direct_trip(conn, row, bucket_date, date) for row, bucket_date in trimmed]


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
    arrival time — which gets the post-midnight handling for free.

    A candidate C is only excluded when it's literally the origin or the
    destination (self-interchange, or reproducing what a direct trip already
    does at C=destination) — a trip that reaches the destination directly is
    *not* excluded from being used to reach some other, more useful
    interchange point, since a slow direct service can legitimately lose to
    changing onto a faster one (found in code review, 2026-08-01: the
    original filter answered a different, wrong question — "did this trip
    ever reach the destination" — rather than "is this specific interchange
    pointless"). The one thing that *is* always pointless is landing leg 2
    back on the exact same physical trip_id as leg 1 (possible on loop-line
    services revisiting a stop), which is filtered explicitly below.
    """
    if is_same_station_group(origin, destination):
        raise SameStationError(origin.stop_code)

    origin_stop_ids = _alias_stop_ids(conn, origin)
    destination_stop_ids = _alias_stop_ids(conn, destination)

    window_start_secs = _time_to_seconds(window_start)
    window_end_secs = window_start_secs + window_minutes * 60

    leg1_rows: list[tuple[sqlite3.Row, dt.date]] = []
    for bucket_date, offset in _day_buckets(date, window_start_secs, window_end_secs):
        bucket_rows = _query_leg1_candidates(
            conn,
            origin_stop_ids,
            destination_stop_ids,
            DAY_COLUMNS[bucket_date.weekday()],
            _format_gtfs_date(bucket_date),
            window_start_secs + offset,
            window_end_secs + offset,
        )
        leg1_rows.extend((row, bucket_date) for row in bucket_rows)

    # _constituent_trip_ids does a small BFS query against synthesized_trips
    # — cheap once, but this loop calls it for the same leg1/leg2 trip_id
    # combination many times over (once per leg1 row x leg2 candidate), so
    # memoize rather than repeat the lookup on every iteration.
    constituent_cache: dict[str, frozenset[str]] = {}

    def constituents(trip_id: str) -> frozenset[str]:
        cached = constituent_cache.get(trip_id)
        if cached is None:
            cached = _constituent_trip_ids(conn, trip_id)
            constituent_cache[trip_id] = cached
        return cached

    candidates: list[InterchangeTrip] = []
    for row, bucket_date in leg1_rows:
        leg1_departure_dt = _absolute_datetime(bucket_date, row["dep_secs"])
        arrival_dt = _absolute_datetime(bucket_date, row["arr_secs"])

        leg1 = DirectTrip(
            trip_id=row["trip_id"],
            agency_name=row["agency_name"] or None,
            agency_id=row["agency_id"] or None,
            route_short_name=row["route_short_name"] or None,
            route_long_name=row["route_long_name"] or None,
            trip_headsign=row["trip_headsign"] or None,
            departure_time=leg1_departure_dt.strftime("%H:%M:%S"),
            arrival_time=arrival_dt.strftime("%H:%M:%S"),
            departure_next_day=leg1_departure_dt.date() > date,
            arrival_next_day=arrival_dt.date() > date,
            duration_minutes=round((row["arr_secs"] - row["dep_secs"]) / 60),
            intermediate_stops=_intermediate_stops(conn, row["trip_id"], row["origin_seq"], row["interchange_seq"]),
            reverses_at=(
                Stop(
                    stop_id=row["reversal_stop_id"],
                    stop_code=row["reversal_stop_code"],
                    stop_name=row["reversal_stop_name"],
                )
                if row["reversal_stop_id"] is not None
                else None
            ),
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

        leg1_constituents = constituents(leg1.trip_id)
        for leg2 in leg2_candidates:
            if leg1_constituents & constituents(leg2.trip_id):
                # Same physical trip re-appearing at the interchange stop
                # (a loop-line service can call at one station twice — see
                # RESEARCH.md's Data Validation section) isn't a real
                # change. Comparing constituent trip_ids rather than raw
                # trip_id equality also catches the case where either leg
                # is a synthesized reversal-continuation trip (see issue
                # #15) that embeds the other leg's actual physical train —
                # a plain `==` check would miss that and offer the same
                # physical train as a fake "interchange".
                continue

            # Rebase leg 2's next-day flags onto the *original* query date —
            # find_direct_trips reports them relative to whatever date it
            # was anchored at (leg2_window_start_dt.date(), i.e. potentially
            # already a day or more after `date`), not the caller's `date`.
            leg2_anchor_date = leg2_window_start_dt.date()
            leg2_departure_dt = _combine_with_next_day(
                leg2_anchor_date, leg2.departure_time, leg2.departure_next_day
            )
            leg2_arrival_dt = _combine_with_next_day(
                leg2_anchor_date, leg2.arrival_time, leg2.arrival_next_day
            )
            leg2_rebased = _rebase_next_day(
                leg2,
                departure_next_day=leg2_departure_dt.date() > date,
                arrival_next_day=leg2_arrival_dt.date() > date,
            )

            candidates.append(
                InterchangeTrip(
                    leg1=leg1,
                    leg2=leg2_rebased,
                    interchange=interchange_stop,
                    connection_minutes=round((leg2_departure_dt - arrival_dt).total_seconds() / 60),
                    total_duration_minutes=round(
                        (leg2_arrival_dt - leg1_departure_dt).total_seconds() / 60
                    ),
                )
            )

    # Keep only one journey per (leg1, leg2) trip pair — when the same two
    # physical trains cross paths at more than one shared stop (e.g. leg 1
    # calls at both CLJ and VXH before terminating, and leg 2 also calls at
    # both on its way out), every shared stop is a technically-valid
    # interchange point, but they all produce the *same* overall journey:
    # leg 1's trip fixes the departure time and leg 2's trip fixes the
    # arrival time regardless of which shared stop you get off/on at, so
    # departure, arrival, and total duration are guaranteed identical across
    # all of them — there's no real choice being offered, just noise (found
    # from a real user-reported case, 2026-08-01: BNS->LRD showed the same
    # journey twice, once changing at CLJ and once at VXH). Keep whichever
    # lets the rider change at the earliest possible station — the first
    # point leg 1 reaches a stop leg 2 also calls at — rather than whichever
    # happens to have the shortest wait, since a passenger has no reason to
    # stay on leg 1 past the first valid opportunity to change onto leg 2.
    # This also naturally covers a loop-line trip calling at the same
    # interchange stop twice (found in code review, 2026-08-01): the first
    # occurrence is definitionally the earliest opportunity to change.
    best_by_key: dict[tuple[str, str], InterchangeTrip] = {}
    for candidate in candidates:
        key = (candidate.leg1.trip_id, candidate.leg2.trip_id)
        existing = best_by_key.get(key)
        if existing is None or _absolute_seconds(
            candidate.leg1.arrival_time, candidate.leg1.arrival_next_day
        ) < _absolute_seconds(existing.leg1.arrival_time, existing.leg1.arrival_next_day):
            best_by_key[key] = candidate

    return list(best_by_key.values())


def find_journeys(
    conn: sqlite3.Connection,
    origin: Stop,
    destination: Stop,
    date: dt.date,
    window_start: dt.time,
    window_minutes: int,
    direct_only: bool = False,
) -> list[Journey]:
    """Direct and single-interchange journeys, merged and Pareto-filtered
    (see `_drop_dominated_journeys` — drops journeys no rider has a reason
    to prefer over some other candidate in the set), then ranked by
    departure time (then duration) — see RESEARCH.md §3's ranking rule.

    `direct_only=True` skips the interchange search entirely (not just
    filtering interchange results back out afterward) — also avoids the
    per-leg1-candidate leg2 queries in find_interchange_trips, which are the
    most expensive part of this function.

    Both legs are fetched over a widened window and dominance-filtered
    together as one merged set, then trimmed back down to the real display
    window at the very end — not before the merge (GitHub issue #19's
    window-boundary follow-up). A direct trip departing just after the
    display window can legitimately dominate an in-window interchange
    journey on change-count alone (_dominates only requires
    `a_changes <= b_changes`), so trimming either leg's fetch before this
    merge would let that interchange survive when it shouldn't. Widening
    find_interchange_trips' own leg-1 fetch this way is safe: it only
    affects the leg-1 departure window, not leg 2's MCT-bounded connection
    search, which find_interchange_trips anchors independently off each
    leg-1 candidate's actual arrival time."""
    buffer_minutes = _dominance_fetch_buffer_minutes(window_minutes)
    fetch_window_minutes = window_minutes + buffer_minutes

    direct_trips = find_direct_trips(conn, origin, destination, date, window_start, fetch_window_minutes)
    interchange_trips = (
        []
        if direct_only
        else find_interchange_trips(conn, origin, destination, date, window_start, fetch_window_minutes)
    )

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
    journeys = _drop_dominated_journeys(journeys)

    window_end_secs = _time_to_seconds(window_start) + window_minutes * 60
    journeys = [j for j in journeys if _absolute_seconds(j.departure_time, j.departure_next_day) < window_end_secs]

    journeys.sort(key=lambda j: (_absolute_minutes(j.departure_time, j.departure_next_day), j.duration_minutes))
    return journeys


def _num_changes(journey: Journey) -> int:
    """A wrong value here doesn't crash — it silently changes which
    journeys the dominance filter below deletes — so an unrecognized kind
    (e.g. a future 2+-interchange journey type, see the README's future
    multi-interchange phase) raises instead of quietly defaulting to 1 and
    letting a 2-change journey masquerade as a 1-change one that could then
    dominate, and delete, a genuinely better single-change result."""
    if journey.kind == "direct":
        return 0
    if journey.kind == "interchange":
        return 1
    raise ValueError(f"don't know how to count changes for journey kind {journey.kind!r}")


def _absolute_seconds(time_str: str, next_day: bool) -> int:
    """Like _absolute_minutes but to the second — the feed's own times
    (this app's golden interchange examples included) commonly fall on a
    :30, and minute-level truncation can make two journeys 30s apart on both
    ends look tied when one genuinely departs/arrives earlier than the
    other; that only mattered for sort stability before, but the dominance
    filter below now uses it to decide what to delete."""
    t = dt.time.fromisoformat(time_str)
    seconds = t.hour * 3600 + t.minute * 60 + t.second
    return seconds + (SECONDS_PER_DAY if next_day else 0)


def _dominates(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    """True if candidate `a` (departure_secs, arrival_secs, num_changes)
    makes `b` pointless to offer: `a` departs no earlier, arrives no later,
    and requires no more changes than `b` — and is strictly better on at
    least one of those, so nothing about `b` gives a rider a reason to
    prefer it. Ties (identical on all three) don't dominate each other;
    both are kept, since e.g. two direct trains at the same time are
    genuinely distinct services (see the interchange dedupe key's own
    docstring above for the same "don't collapse distinct real choices"
    principle)."""
    a_dep, a_arr, a_changes = a
    b_dep, b_arr, b_changes = b
    at_least_as_good = a_dep >= b_dep and a_arr <= b_arr and a_changes <= b_changes
    strictly_better = a_dep > b_dep or a_arr < b_arr or a_changes < b_changes
    return at_least_as_good and strictly_better


def _drop_dominated_journeys(journeys: list[Journey]) -> list[Journey]:
    """Pareto-filters the merged direct+interchange candidate list (2026-08-01
    UX review — cuts routes like BNS->WAT down from 1000+ raw candidates,
    almost all interchange combinations a direct train already beats, to a
    handful of genuinely distinct choices). Applied uniformly across direct
    and interchange together, not as a separate "prefer direct" rule: a
    journey survives only if no other candidate departs at least as late,
    arrives at least as early, and needs no more changes, while being
    strictly better on at least one of those — mirroring the dominance
    RAPTOR computes round-by-round internally (direct/0-change journeys are
    round 1; a round-2 interchange only survives if it beats what round 1
    already found), without needing to adopt RAPTOR itself yet (see the
    README's future multi-interchange phase). This deliberately departs
    from how e.g. SWR's own journey planner behaves — it lists every
    scheduled direct train, dominated or not — because this app's whole
    problem here is candidate-count explosion, and a strictly slower same-
    or-later-departing direct offers nothing an earlier-arriving one
    doesn't (ticket/operator/calling-pattern preferences aside, which this
    filter doesn't model).

    Comparison keys are computed once per journey up front rather than
    reparsed on every pairwise comparison — with O(n^2) comparisons and a
    real-feed candidate count that can reach several thousand at
    MAX_JOURNEYS_WINDOW_MINUTES, reparsing each time made this measurably
    slow (multi-second) on a wide, mostly-non-dominated result set;
    precomputed plain-int comparisons don't (~0.2s at ~7000 candidates,
    measured against the real feed)."""
    return _drop_dominated(
        journeys,
        lambda j: (
            _absolute_seconds(j.departure_time, j.departure_next_day),
            _absolute_seconds(j.arrival_time, j.arrival_next_day),
            _num_changes(j),
        ),
    )


T = TypeVar("T")


def _drop_dominated(items: list[T], key_fn: Callable[[T], tuple[int, int, int]]) -> list[T]:
    """Generic Pareto filter shared by _drop_dominated_journeys (num_changes
    varies per journey, via _num_changes' deliberate raise-on-unknown-kind)
    and dominant_direct_trips (num_changes fixed at 0 for every candidate,
    since they're all direct) — same O(n^2) all-pairs comparison, keyed by a
    caller-supplied `(departure_secs, arrival_secs, num_changes)` tuple per
    item."""
    keys = [key_fn(item) for item in items]
    return [item for i, item in enumerate(items) if not any(_dominates(keys[k], keys[i]) for k in range(len(items)) if k != i)]


def _query_leg1_candidates(
    conn: sqlite3.Connection,
    origin_stop_ids: list[str],
    destination_stop_ids: list[str],
    day_col: str,
    date_str: str,
    lo_secs: int,
    hi_secs: int,
) -> list[sqlite3.Row]:
    """Every (trip, later stop) pair from `origin_stop_ids`, excluding the
    origin's own alias group (self-interchange on a loop service) and the
    destination's alias group (that's just the direct route, already found —
    with alias merging of its own — by find_direct_trips) as candidate
    interchange stops. Excluding the *whole* alias group, not just the
    literal id passed in, matters here: without it, a leg 1 arriving at PDX
    when the destination is PAD would look like a "genuine" interchange
    candidate distinct from the direct search, producing a bogus duplicate
    of a journey find_direct_trips already reports as direct (see
    STATION_ALIASES in config.py / GitHub issue #11). Naturally bounded by
    real service patterns (the handful of trips departing in the window,
    times however many stops each one makes) rather than needing a curated
    list of major stations."""
    active_sql = _ACTIVE_SERVICE_IDS_SQL.format(day_col=day_col)
    origin_clause, origin_params = _in_clause("orig", origin_stop_ids)
    dest_clause, dest_params = _in_clause("dest", destination_stop_ids)
    sql = f"""
        SELECT
            t.trip_id, t.trip_headsign,
            r.route_short_name, r.route_long_name, ag.agency_name, ag.agency_id,
            a.stop_sequence AS origin_seq, a.departure_secs AS dep_secs,
            c.stop_id AS interchange_stop_id, c.stop_sequence AS interchange_seq,
            c.arrival_secs AS arr_secs,
            cs.stop_code AS interchange_stop_code, cs.stop_name AS interchange_stop_name,
            syn.reversal_stop_id AS reversal_stop_id,
            rs.stop_code AS reversal_stop_code, rs.stop_name AS reversal_stop_name
        FROM trips t
        JOIN stop_times a ON a.trip_id = t.trip_id AND a.stop_id IN {origin_clause}
        JOIN stop_times c ON c.trip_id = t.trip_id AND c.stop_sequence > a.stop_sequence
        JOIN stops cs ON cs.stop_id = c.stop_id
        JOIN routes r ON r.route_id = t.route_id
        LEFT JOIN agency ag ON ag.agency_id = r.agency_id
        LEFT JOIN synthesized_trips syn ON syn.trip_id = t.trip_id
        LEFT JOIN stops rs ON rs.stop_id = syn.reversal_stop_id
        WHERE a.departure_secs >= :lo AND a.departure_secs < :hi
          AND c.stop_id NOT IN {dest_clause}
          AND c.stop_id NOT IN {origin_clause}
          AND t.service_id IN ({active_sql})
          -- Same duplicate-suppression as _query_direct_trips above: a
          -- synthesized reversal-continuation trip also contains both of
          -- its constituent legs' stops individually, so without this an
          -- interchange candidate wholly inside one leg would duplicate
          -- what searching that leg's own plain trip_id already finds.
          AND (
            syn.trip_id IS NULL
            OR (
              a.stop_sequence < syn.reversal_seq AND c.stop_sequence > syn.reversal_seq
              -- Same retracing-route protection as _query_direct_trips
              -- above — require the closest matching origin/candidate pair,
              -- not a later re-visit of either stop (found in code review,
              -- 2026-08-01).
              AND NOT EXISTS (
                SELECT 1 FROM stop_times dup_o
                WHERE dup_o.trip_id = t.trip_id AND dup_o.stop_id = a.stop_id
                  AND dup_o.stop_sequence > a.stop_sequence AND dup_o.stop_sequence < c.stop_sequence
              )
              AND NOT EXISTS (
                SELECT 1 FROM stop_times dup_c
                WHERE dup_c.trip_id = t.trip_id AND dup_c.stop_id = c.stop_id
                  AND dup_c.stop_sequence > a.stop_sequence AND dup_c.stop_sequence < c.stop_sequence
              )
            )
          )
    """
    params = {
        **origin_params,
        **dest_params,
        "date": date_str,
        "lo": lo_secs,
        "hi": hi_secs,
    }
    return conn.execute(sql, params).fetchall()


def _day_buckets(date: dt.date, window_start_secs: int, window_end_secs: int) -> list[tuple[dt.date, int]]:
    """Which service-day(s) to query, and by how much to shift the window's
    seconds bounds to search each one's own local (0, 24h+) raw-seconds
    storage. Always includes `date` itself (offset 0) and `date - 1`
    (offset +86400, to catch its >=24:00:00 post-midnight continuation
    landing inside today's window). Also includes as many forward days
    (`date + 1`, `date + 2`, ...) as `window_end_secs` actually reaches —
    those early trips are plain, normally-tagged following-day services, not
    `date`'s own >=24:00:00 notation, so they're invisible without a forward
    bucket for each day the window spans (see module docstring). A plain
    query window never needs more than one forward day, but a dominance
    fetch widened past the display window (GitHub issue #19) can, e.g. a
    24h /api/direct window starting late in the evening plus the dominance
    buffer — generalized to N days rather than hardcoding a single forward
    bucket so that case isn't silently missed."""
    buckets = [(date, 0), (date - dt.timedelta(days=1), SECONDS_PER_DAY)]
    forward_days = max(0, (window_end_secs - 1) // SECONDS_PER_DAY)
    buckets.extend(
        (date + dt.timedelta(days=n), -n * SECONDS_PER_DAY) for n in range(1, forward_days + 1)
    )
    return buckets


def _absolute_datetime(bucket_date: dt.date, raw_secs: int) -> dt.datetime:
    """The real calendar datetime a raw (possibly >=24h, or — for the
    `date + 1` bucket — effectively negative-shifted-back-to-normal)
    GTFS seconds value represents, given which service day (bucket_date)
    it was matched against. `raw_secs // SECONDS_PER_DAY` naturally
    generalizes to any magnitude: 0 for a plain <24h value, 1 for a single
    post-midnight day, etc. — no special-casing per bucket needed."""
    extra_days, wall_secs = divmod(raw_secs, SECONDS_PER_DAY)
    return dt.datetime.combine(bucket_date, dt.time()) + dt.timedelta(days=extra_days, seconds=wall_secs)


def _combine_with_next_day(anchor_date: dt.date, time_str: str, next_day: bool) -> dt.datetime:
    combined = dt.datetime.combine(anchor_date, dt.time.fromisoformat(time_str))
    return combined + dt.timedelta(days=1) if next_day else combined


def _absolute_minutes(time_str: str, next_day: bool) -> int:
    t = dt.time.fromisoformat(time_str)
    minutes = t.hour * 60 + t.minute
    return minutes + (24 * 60 if next_day else 0)


def _rebase_next_day(trip: DirectTrip, *, departure_next_day: bool, arrival_next_day: bool) -> DirectTrip:
    """Returns a copy of `trip` with the next-day flags overridden — used to
    rebase a leg found via find_direct_trips (which reports them relative to
    its own anchor date) onto the caller's original query date."""
    return DirectTrip(
        trip_id=trip.trip_id,
        agency_name=trip.agency_name,
        agency_id=trip.agency_id,
        route_short_name=trip.route_short_name,
        route_long_name=trip.route_long_name,
        trip_headsign=trip.trip_headsign,
        departure_time=trip.departure_time,
        arrival_time=trip.arrival_time,
        departure_next_day=departure_next_day,
        arrival_next_day=arrival_next_day,
        duration_minutes=trip.duration_minutes,
        intermediate_stops=trip.intermediate_stops,
        reverses_at=trip.reverses_at,
    )


def _query_direct_trips(
    conn: sqlite3.Connection,
    origin_stop_ids: list[str],
    destination_stop_ids: list[str],
    day_col: str,
    date_str: str,
    lo_secs: int,
    hi_secs: int,
) -> list[sqlite3.Row]:
    active_sql = _ACTIVE_SERVICE_IDS_SQL.format(day_col=day_col)
    origin_clause, origin_params = _in_clause("orig", origin_stop_ids)
    dest_clause, dest_params = _in_clause("dest", destination_stop_ids)
    sql = f"""
        SELECT
            t.trip_id, t.trip_headsign,
            r.route_short_name, r.route_long_name, ag.agency_name, ag.agency_id,
            st1.stop_sequence AS origin_seq, st1.departure_secs AS dep_secs,
            st2.stop_sequence AS dest_seq, st2.arrival_secs AS arr_secs,
            syn.reversal_stop_id AS reversal_stop_id,
            rs.stop_code AS reversal_stop_code, rs.stop_name AS reversal_stop_name
        FROM trips t
        JOIN stop_times st1 ON st1.trip_id = t.trip_id AND st1.stop_id IN {origin_clause}
        JOIN stop_times st2 ON st2.trip_id = t.trip_id AND st2.stop_id IN {dest_clause}
        JOIN routes r ON r.route_id = t.route_id
        LEFT JOIN agency ag ON ag.agency_id = r.agency_id
        LEFT JOIN synthesized_trips syn ON syn.trip_id = t.trip_id
        LEFT JOIN stops rs ON rs.stop_id = syn.reversal_stop_id
        WHERE st1.stop_sequence < st2.stop_sequence
          AND st1.departure_secs >= :lo AND st1.departure_secs < :hi
          AND t.service_id IN ({active_sql})
          -- A synthesized reversal-continuation trip (see
          -- db._build_trip_continuations / issue #15) also contains every
          -- stop of its two constituent legs individually — without this,
          -- a query that lies entirely within one leg would match both the
          -- plain constituent trip and this synthesized copy of it,
          -- showing the same physical train twice. Only keep a synthesized
          -- match when the journey genuinely straddles the reversal point.
          AND (
            syn.trip_id IS NULL
            OR (
              st1.stop_sequence < syn.reversal_seq AND st2.stop_sequence > syn.reversal_seq
              -- A reversal that retraces its own route (e.g. A -> B ->
              -- terminus -> reverses -> back through B -> Z) makes the
              -- origin and/or destination stop_id appear more than once in
              -- the synthesized trip. Without these, a plain A -> B query
              -- would additionally match the *later*, post-reversal
              -- occurrence of B — a real but nonsensical "ride past B,
              -- reverse, come back to B" answer nobody would want when the
              -- plain, un-synthesized trip already gets there directly.
              -- Require the chosen origin/destination pair to be the
              -- closest one to each other (no other matching-stop row of
              -- either lies strictly between them) — found in code review,
              -- 2026-08-01.
              AND NOT EXISTS (
                SELECT 1 FROM stop_times dup_o
                WHERE dup_o.trip_id = t.trip_id AND dup_o.stop_id = st1.stop_id
                  AND dup_o.stop_sequence > st1.stop_sequence AND dup_o.stop_sequence < st2.stop_sequence
              )
              AND NOT EXISTS (
                SELECT 1 FROM stop_times dup_d
                WHERE dup_d.trip_id = t.trip_id AND dup_d.stop_id = st2.stop_id
                  AND dup_d.stop_sequence > st1.stop_sequence AND dup_d.stop_sequence < st2.stop_sequence
              )
            )
          )
    """
    params = {
        **origin_params,
        **dest_params,
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
            arrival_time=_wall_clock(row["arrival_secs"]),
            departure_time=_wall_clock(row["departure_secs"]),
        )
        for row in rows
    ]


def _wall_clock(secs: int) -> str:
    wrapped = secs % SECONDS_PER_DAY
    h, rem = divmod(wrapped, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _time_to_seconds(t: dt.time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _parse_gtfs_date(date_str: str) -> dt.date:
    return dt.datetime.strptime(date_str, "%Y%m%d").date()


def _format_gtfs_date(date: dt.date) -> str:
    return date.strftime("%Y%m%d")
