"""Builds a queryable SQLite index from raw GTFS CSV files.

Raw GTFS text files are too large to scan per-request (an unindexed scan of
stop_times.txt takes ~9s even for a single query — see RESEARCH.md's Data
Validation section). Ingesting once into SQLite with the right indexes makes
every subsequent journey-planning query a fast indexed lookup instead.

Deliberately hand-rolled rather than using `partridge` at query time: SQLite
indexing on stop_id/trip_id/service_id/date fully covers the performance
problem partridge's date-pruning was meant to solve, without adding a runtime
dependency or its optional geopandas warnings. Calendar/exception resolution
is implemented directly in SQL (see queries.py) instead.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pandas as pd

from app.config import REVERSAL_MAX_DWELL_MINUTES, REVERSAL_MAX_HEADCODE_GROUP_SIZE

logger = logging.getLogger("train_journey_planner.db")

REQUIRED_FILES = (
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "agency.txt",
)


def _time_to_seconds(series: pd.Series) -> pd.Series:
    """Convert HH:MM:SS GTFS time strings to seconds-since-midnight.

    GTFS allows hours >= 24 for trips that run past midnight relative to
    their service day, so this is a plain arithmetic parse, not a time-of-day
    parser.
    """
    parts = series.str.split(":", expand=True).astype(int)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_readonly_connection(db_path: Path) -> sqlite3.Connection:
    """A connection opened read-only, for request-serving code that should
    never write. Also avoids a journal file appearing next to a database
    that the refresh job may `os.replace` out from under it.

    `check_same_thread=False`: this is opened by a sync FastAPI dependency
    generator (`get_db()` in app/main.py), whose setup/endpoint-body/teardown
    are each dispatched as separate `run_in_threadpool` calls — AnyIO doesn't
    guarantee those land on the same OS thread, so the default
    (`check_same_thread=True`, pinning the connection to its creating thread)
    raised `sqlite3.ProgrammingError` under concurrent load (GitHub issue
    #20; confirmed via scripts/concurrency_repro.py against a container with
    exception logging, 2026-08-02). Safe to disable here specifically
    because each connection is opened fresh per request, used only within
    that request's handling, and never shared or cached across requests —
    the thread-affinity check exists to catch concurrent cross-thread reuse
    of one connection, which this per-request-connection pattern never does
    even when a single request's own three threadpool calls land on
    different threads.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def build_database(gtfs_dir: Path, db_path: Path) -> None:
    """Read raw GTFS CSVs from `gtfs_dir` and write an indexed SQLite DB to `db_path`.

    Writes to `db_path` directly — callers that need atomicity (e.g. the
    refresh job swapping in new data without disturbing a running app) should
    build into a temp path and rename it into place afterwards.
    """
    gtfs_dir = Path(gtfs_dir)
    missing = [f for f in REQUIRED_FILES if not (gtfs_dir / f).exists()]
    if missing:
        raise FileNotFoundError(f"GTFS feed at {gtfs_dir} is missing required files: {missing}")

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _load_stops(gtfs_dir, conn)
        _load_agency(gtfs_dir, conn)
        _load_routes(gtfs_dir, conn)
        duplicate_service_ids = _find_duplicate_calendar_service_ids(gtfs_dir)
        excluded_trip_ids = _load_trips(gtfs_dir, conn, duplicate_service_ids)
        _load_stop_times(gtfs_dir, conn, excluded_trip_ids)
        _build_trip_continuations(conn)
        _load_calendar(gtfs_dir, conn, duplicate_service_ids)
        _load_calendar_dates(gtfs_dir, conn, duplicate_service_ids)
        conn.commit()
    finally:
        conn.close()


def _find_duplicate_calendar_service_ids(gtfs_dir: Path) -> set[str]:
    """`calendar.txt`'s `service_id` must be unique per the GTFS spec, but a
    malformed upstream feed can violate this (GitHub issue #32 — TravelWhiz's
    feed emitted the same `service_id` for two entirely unrelated services
    from different operators, apparently a collision in their ID generation,
    not a revision of the same service — their date ranges genuinely
    overlapped with different day-patterns, so there was no principled way
    to pick a "correct" row).

    Trips referencing a duplicate service_id are excluded from this build
    entirely (see `_load_trips`/`_load_stop_times`) rather than guessing
    which calendar row should apply to them — a wrong guess would silently
    misschedule real trips, which is worse than the trips being temporarily
    absent. This self-heals with no code change once the upstream duplicate
    is gone.
    """
    service_ids = pd.read_csv(gtfs_dir / "calendar.txt", dtype=str, usecols=["service_id"])["service_id"]
    counts = service_ids.value_counts()
    duplicates = set(counts[counts > 1].index)
    if duplicates:
        logger.warning(
            "_find_duplicate_calendar_service_ids: calendar.txt has %d duplicate "
            "service_id(s), violating the GTFS spec's uniqueness requirement — "
            "excluding every trip referencing them from this build: %s",
            len(duplicates), sorted(duplicates),
        )
    return duplicates


def _load_stops(gtfs_dir: Path, conn: sqlite3.Connection) -> None:
    df = pd.read_csv(
        gtfs_dir / "stops.txt",
        dtype=str,
        usecols=["stop_id", "stop_code", "stop_name"],
    )
    df["stop_code"] = df["stop_code"].str.upper()
    df.to_sql("stops", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_stops_stop_id ON stops(stop_id)")
    conn.execute("CREATE INDEX idx_stops_stop_code ON stops(stop_code)")


def _load_agency(gtfs_dir: Path, conn: sqlite3.Connection) -> None:
    df = pd.read_csv(
        gtfs_dir / "agency.txt",
        dtype=str,
        usecols=["agency_id", "agency_name"],
    )
    df.to_sql("agency", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_agency_agency_id ON agency(agency_id)")


def _load_routes(gtfs_dir: Path, conn: sqlite3.Connection) -> None:
    df = pd.read_csv(
        gtfs_dir / "routes.txt",
        dtype=str,
        usecols=["route_id", "agency_id", "route_short_name", "route_long_name"],
    )
    df.to_sql("routes", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_routes_route_id ON routes(route_id)")


def _load_trips(gtfs_dir: Path, conn: sqlite3.Connection, excluded_service_ids: set[str]) -> pd.Series:
    """Returns the trip_ids dropped for referencing a duplicate service_id,
    so `_load_stop_times` can remove only those rows — not a blanket
    trips-membership filter, which would also silently swallow any
    unrelated orphaned `stop_times.txt` row (a different feed problem this
    fix isn't meant to mask, and previously loaded as-is)."""
    df = pd.read_csv(
        gtfs_dir / "trips.txt",
        dtype=str,
        usecols=["trip_id", "route_id", "service_id", "trip_headsign", "trip_short_name"],
    )
    excluded_trip_ids = df.loc[df["service_id"].isin(excluded_service_ids), "trip_id"]
    if len(excluded_trip_ids):
        df = df[~df["trip_id"].isin(excluded_trip_ids)]
        logger.warning(
            "_load_trips: dropped %d trip(s) referencing a duplicate calendar.txt "
            "service_id (see _find_duplicate_calendar_service_ids)", len(excluded_trip_ids),
        )
    df.to_sql("trips", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_trips_trip_id ON trips(trip_id)")
    conn.execute("CREATE INDEX idx_trips_service_id ON trips(service_id)")
    return excluded_trip_ids


def _load_stop_times(gtfs_dir: Path, conn: sqlite3.Connection, excluded_trip_ids: pd.Series) -> None:
    df = pd.read_csv(
        gtfs_dir / "stop_times.txt",
        dtype=str,
        usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    )
    if len(excluded_trip_ids):
        df = df[~df["trip_id"].isin(excluded_trip_ids)]
    df["stop_sequence"] = df["stop_sequence"].astype(int)
    df["arrival_secs"] = _time_to_seconds(df["arrival_time"])
    df["departure_secs"] = _time_to_seconds(df["departure_time"])
    df.to_sql("stop_times", conn, if_exists="replace", index=False)
    conn.execute("CREATE INDEX idx_stop_times_trip_id ON stop_times(trip_id, stop_sequence)")
    # Composite on (stop_id, departure_secs), not just stop_id: every direct/
    # leg query filters on both (queries.py's _query_direct_trips and
    # _query_leg1_candidates), and the plain stop_id prefix still serves
    # lookups that don't filter on time — found in code review, 2026-08-01,
    # as the main cost behind Phase 2's per-candidate leg-2 queries scanning
    # every stop_times row for a hub station instead of a narrow range seek.
    conn.execute("CREATE INDEX idx_stop_times_stop_id ON stop_times(stop_id, departure_secs)")


def _build_trip_continuations(
    conn: sqlite3.Connection,
    max_dwell_minutes: int = REVERSAL_MAX_DWELL_MINUTES,
    max_headcode_group_size: int = REVERSAL_MAX_HEADCODE_GROUP_SIZE,
) -> None:
    """Detects branch-line reversals — a physical train that terminates at a
    station and continues under a *different* trip_id shortly after (see
    GitHub issue #15, e.g. Tadworth -> Purley -> London Bridge) — and
    synthesizes one combined trip per reversal, so the existing direct-route
    query machinery (find_direct_trips, the dominance filter, _num_changes)
    can find the real seamless journey with zero interchange/MCT applied,
    without any special-casing at query time.

    Join key: two trips are the same physical train reversing if they share
    `trip_short_name` (the National Rail headcode — this feed has no
    `block_id`, GTFS's usual same-vehicle marker), `service_id`, and agency,
    and the continuing trip *calls at* (not necessarily originates from) the
    first trip's terminus within `max_dwell_minutes` of it arriving (a
    vehicle-dwell value, not a passenger-transfer one — see
    REVERSAL_MAX_DWELL_MINUTES in config.py). Matching on "calls at" rather
    than "originates from" is required, not an optional broadening: both of
    issue #15's real confirmed cases (Tadworth/Purley, Earlswood/Redhill) are
    portion joins where the continuing service actually starts a stop or two
    earlier and just calls at the reversal stop mid-trip — an
    origin-required match found precisely zero of the real feed's reversals
    when checked against production data (2026-08-02).

    Real headcodes are unreliable in isolation: this feed carries a
    placeholder trip_short_name ("0B00") on ~17% of all trips, spanning
    every operator, which would otherwise form enormous same-headcode/
    same-service groups with no real reversal relationship at all. Capping
    at `max_headcode_group_size` (a real reversing train's own group is
    small — the working plus its reversals) is what actually keeps this
    safe; the terminating/departing-trip ambiguity check below catches
    incidental clashes within an otherwise-small group, not the 0B00 noise
    (removing the cap was measured to add ~16,500 bogus synthesized trips
    against the real feed).

    Reality check against the real feed (2026-08-02): ~98% of kept pairs are
    a portion *attach* rather than a reversal in the literal sense — the
    continuing unit is often already standing at the platform (or arrives
    after) rather than turning the same physical train around. "Reversal" is
    kept as the name/vocabulary throughout (matching issue #15 and the
    existing `reverses_at`/`reversal_stop_id`/`reversal_seq` fields) because
    the rider-facing effect is identical either way: a seamless, zero-change
    continuation. A continuation is only ever allowed to join *before* its
    own terminus (see `reversal_calls` below) — it must have at least one
    further stop after the join point, or there is nothing to append and no
    reason to synthesize anything.

    If a continuing trip calls at the reversal stop more than once within
    the dwell window (e.g. an out-and-back working), every such call
    produces its own candidate row, all keyed to the same trip_id_2 — the
    ambiguity check below then discards all of them, since there is no
    principled way to prefer one call over another. This is a deliberate
    conservative miss, not an arbitrary pick of the "wrong" occurrence.

    Query shape: built as real temporary tables with an explicit index,
    not CTEs. A CTE-based version (join termini/origins driven from a
    headcode-grouped CTE) was tried and measured live against the real
    production feed (2026-08-01) to still take the wide, combinatorial
    stop_id-first join plan regardless of CTE nesting order — SQLite
    flattens/reorders CTEs, so the drive order in the SQL text isn't
    actually honoured. Materializing into temp tables with a real index
    forces the intended plan (verified via EXPLAIN QUERY PLAN against the
    real feed, 2026-08-02: single index seek per candidate row, ~5s total).

    A terminating trip matched by more than one continuation, or a
    continuation call matched by more than one terminating trip, is
    ambiguous — skipped rather than guessed at, since a fabricated "direct"
    journey that doesn't really exist is worse than the miss this is meant
    to fix.

    Scoped to single (2-leg) reversals only: a genuine double reversal would
    need a further pass chaining synthesized trips together, not implemented
    here.
    """
    max_gap_secs = max_dwell_minutes * 60
    # Individual DROPs, not executescript() — executescript implicitly
    # commits, which would otherwise split build_database's single-commit
    # build into two transactions for no reason. Defensive against a second
    # call reusing this connection (the only current caller always passes a
    # freshly-opened one, but temp tables otherwise persist for the
    # connection's lifetime).
    conn.execute("DROP TABLE IF EXISTS temp.headcode_group_counts")
    conn.execute("DROP TABLE IF EXISTS temp.eligible_trips")
    conn.execute("DROP TABLE IF EXISTS temp.eligible_max_seq")
    conn.execute("DROP TABLE IF EXISTS temp.reversal_termini")
    conn.execute("DROP TABLE IF EXISTS temp.reversal_calls")

    # Trips whose (trip_short_name, service_id) group is small enough to
    # plausibly be one physical train's own reversal(s), not headcode noise
    # like 0B00. A full scan of `trips` (no index backs this pair — nothing
    # else needs one, and everything downstream only ever touches this
    # pre-filtered set) is fine at ~2s against the real feed's ~360k trips.
    conn.execute(
        """
        CREATE TEMP TABLE headcode_group_counts AS
        SELECT trip_short_name, service_id, COUNT(*) AS n
        FROM trips
        WHERE trip_short_name IS NOT NULL AND TRIM(trip_short_name) != ''
        GROUP BY trip_short_name, service_id
        HAVING COUNT(*) BETWEEN 2 AND :max_group
        """,
        {"max_group": max_headcode_group_size},
    )
    conn.execute(
        """
        CREATE TEMP TABLE eligible_trips AS
        SELECT t.trip_id, t.trip_short_name, t.service_id, r.agency_id
        FROM trips t
        JOIN routes r ON r.route_id = t.route_id
        JOIN headcode_group_counts g
          ON g.trip_short_name = t.trip_short_name AND g.service_id = t.service_id
        """
    )

    # Each eligible trip's own last stop_sequence — computed once and reused
    # by both tables below, rather than a per-row correlated subquery.
    conn.execute(
        """
        CREATE TEMP TABLE eligible_max_seq AS
        SELECT e.trip_id, MAX(st.stop_sequence) AS max_seq
        FROM eligible_trips e
        JOIN stop_times st ON st.trip_id = e.trip_id
        GROUP BY e.trip_id
        """
    )
    conn.execute("CREATE INDEX temp.idx_eligible_max_seq_trip_id ON eligible_max_seq(trip_id)")

    # One row per eligible trip: its own terminus (last stop).
    conn.execute(
        """
        CREATE TEMP TABLE reversal_termini AS
        SELECT e.trip_id, e.trip_short_name, e.service_id, e.agency_id,
               st.stop_id, st.arrival_secs
        FROM eligible_trips e
        JOIN eligible_max_seq m ON m.trip_id = e.trip_id
        JOIN stop_times st ON st.trip_id = e.trip_id AND st.stop_sequence = m.max_seq
        """
    )
    # Every call *before* an eligible trip's own terminus — a continuation
    # only needs to call at the reversal stop, not originate there, but it
    # must call there strictly before its own last stop: a "continuation"
    # whose matched call is its own terminus has nothing left to append
    # (an empty tail), and worse, would occupy that trip's slot in the
    # ambiguity check below for a match that adds nothing — found in code
    # review, 2026-08-02, confirmed live against the real feed (3 of 1,557
    # kept pairs were this shape, all no-ops today only because
    # reversal_seq's own-terminus value can never satisfy queries.py's
    # `stop_sequence > reversal_seq` guard, but one feed revision away from
    # silently starving a genuine reversal's ambiguity budget instead).
    conn.execute(
        """
        CREATE TEMP TABLE reversal_calls AS
        SELECT e.trip_id, e.trip_short_name, e.service_id, e.agency_id,
               st.stop_id, st.stop_sequence, st.departure_secs
        FROM eligible_trips e
        JOIN eligible_max_seq m ON m.trip_id = e.trip_id
        JOIN stop_times st ON st.trip_id = e.trip_id AND st.stop_sequence < m.max_seq
        """
    )
    # The index that actually makes the join below a single seek per
    # candidate row (confirmed via EXPLAIN QUERY PLAN against the real feed).
    conn.execute(
        "CREATE INDEX temp.idx_reversal_calls_join "
        "ON reversal_calls(trip_short_name, service_id, agency_id, stop_id, departure_secs)"
    )

    candidates = conn.execute(
        """
        SELECT term.trip_id AS trip_id_1, cal.trip_id AS trip_id_2,
               term.stop_id AS reversal_stop_id, cal.stop_sequence AS leg2_join_seq
        FROM reversal_termini term
        JOIN reversal_calls cal
          ON cal.trip_short_name = term.trip_short_name
         AND cal.service_id = term.service_id
         AND cal.agency_id = term.agency_id
         AND cal.stop_id = term.stop_id
         AND cal.departure_secs >= term.arrival_secs
         AND cal.departure_secs <= term.arrival_secs + :max_gap
        WHERE cal.trip_id != term.trip_id
        """,
        {"max_gap": max_gap_secs},
    ).fetchall()

    leg1_counts: dict[str, int] = {}
    leg2_counts: dict[str, int] = {}
    for row in candidates:
        leg1_counts[row["trip_id_1"]] = leg1_counts.get(row["trip_id_1"], 0) + 1
        leg2_counts[row["trip_id_2"]] = leg2_counts.get(row["trip_id_2"], 0) + 1
    pairs = [
        row
        for row in candidates
        if leg1_counts[row["trip_id_1"]] == 1 and leg2_counts[row["trip_id_2"]] == 1
    ]
    ambiguous = len(candidates) - len(pairs)
    if ambiguous:
        logger.warning(
            "_build_trip_continuations: skipped %d ambiguous reversal candidate(s) "
            "(a terminating trip matched more than one continuation call, or vice versa — "
            "including a continuing trip that calls at the reversal stop more than once "
            "within the dwell window, which is deliberately never disambiguated)",
            ambiguous,
        )
    if not pairs:
        logger.warning(
            "_build_trip_continuations: synthesized 0 reversal-continuation trips — check "
            "trip_short_name/agency_id data quality in this feed if that's unexpected "
            "(the join requires both to be populated and consistent)"
        )
    else:
        logger.info("_build_trip_continuations: synthesized %d reversal-continuation trip(s)", len(pairs))

    try:
        conn.execute(
            """
            CREATE TABLE synthesized_trips (
                trip_id TEXT PRIMARY KEY,
                leg1_trip_id TEXT NOT NULL,
                leg2_trip_id TEXT NOT NULL,
                reversal_stop_id TEXT NOT NULL,
                reversal_seq INTEGER NOT NULL
            )
            """
        )

        for row in pairs:
            _synthesize_continuation_trip(
                conn, row["trip_id_1"], row["trip_id_2"], row["reversal_stop_id"], row["leg2_join_seq"]
            )

        conn.execute("CREATE INDEX idx_synthesized_trips_leg1 ON synthesized_trips(leg1_trip_id)")
        conn.execute("CREATE INDEX idx_synthesized_trips_leg2 ON synthesized_trips(leg2_trip_id)")
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.headcode_group_counts")
        conn.execute("DROP TABLE IF EXISTS temp.eligible_trips")
        conn.execute("DROP TABLE IF EXISTS temp.eligible_max_seq")
        conn.execute("DROP TABLE IF EXISTS temp.reversal_termini")
        conn.execute("DROP TABLE IF EXISTS temp.reversal_calls")


def _synthesize_continuation_trip(
    conn: sqlite3.Connection,
    trip_id_1: str,
    trip_id_2: str,
    reversal_stop_id: str,
    leg2_join_seq: int,
) -> None:
    """Inserts one combined trip (trips + stop_times + synthesized_trips
    rows) covering `trip_id_1` then `trip_id_2`. `leg2_join_seq` is leg 2's
    own stop_sequence at the reversal stop — leg 2 is not required to
    *originate* there (both of issue #15's real cases are portion joins
    where it doesn't), so anything leg 2 called at *before* that point is
    dropped; only the join stop onward is appended after leg 1. Skips
    (rather than inserting a corrupt row) if either leg's stop_times are
    missing, if leg 2 has no row at `leg2_join_seq`, or if leg 2's departure
    from the join point would be before leg 1's last arrival — the caller's
    gap filter already guarantees this can't happen for a genuinely matched
    pair, but this stays a hard invariant rather than a trusted
    precondition."""
    leg1_trip = conn.execute(
        "SELECT service_id, trip_short_name FROM trips WHERE trip_id = ?", (trip_id_1,)
    ).fetchone()
    leg2_trip = conn.execute(
        "SELECT route_id, trip_headsign, trip_short_name FROM trips WHERE trip_id = ?", (trip_id_2,)
    ).fetchone()
    leg1_stops = conn.execute(
        "SELECT stop_id, arrival_time, departure_time, arrival_secs, departure_secs "
        "FROM stop_times WHERE trip_id = ? ORDER BY stop_sequence",
        (trip_id_1,),
    ).fetchall()
    leg2_stops = conn.execute(
        "SELECT stop_id, stop_sequence, arrival_time, departure_time, arrival_secs, departure_secs "
        "FROM stop_times WHERE trip_id = ? ORDER BY stop_sequence",
        (trip_id_2,),
    ).fetchall()
    if not leg1_stops or not leg2_stops:
        return

    join_index = next(
        (i for i, s in enumerate(leg2_stops) if s["stop_sequence"] == leg2_join_seq), None
    )
    if join_index is None:
        return
    join_stop = leg2_stops[join_index]
    if join_stop["departure_secs"] < leg1_stops[-1]["arrival_secs"]:
        return

    synthetic_trip_id = f"{trip_id_1}+{trip_id_2}"
    reversal_seq = len(leg1_stops)

    merged_rows = [
        {
            "trip_id": synthetic_trip_id,
            "stop_id": s["stop_id"],
            "stop_sequence": i,
            "arrival_time": s["arrival_time"],
            "departure_time": s["departure_time"],
            "arrival_secs": s["arrival_secs"],
            "departure_secs": s["departure_secs"],
        }
        for i, s in enumerate(leg1_stops, start=1)
    ]
    # The reversal stop's departure is leg 1's own value at this point (its
    # terminus — it never departs again as that trip); overwrite with leg
    # 2's real departure from the join stop (the dwell), keeping leg 1's
    # arrival untouched.
    merged_rows[-1]["departure_time"] = join_stop["departure_time"]
    merged_rows[-1]["departure_secs"] = join_stop["departure_secs"]
    merged_rows.extend(
        {
            "trip_id": synthetic_trip_id,
            "stop_id": s["stop_id"],
            "stop_sequence": reversal_seq + i,
            "arrival_time": s["arrival_time"],
            "departure_time": s["departure_time"],
            "arrival_secs": s["arrival_secs"],
            "departure_secs": s["departure_secs"],
        }
        for i, s in enumerate(leg2_stops[join_index + 1 :], start=1)
    )

    conn.execute(
        "INSERT INTO trips (trip_id, route_id, service_id, trip_headsign, trip_short_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            synthetic_trip_id,
            leg2_trip["route_id"],
            leg1_trip["service_id"],
            leg2_trip["trip_headsign"],
            leg2_trip["trip_short_name"],
        ),
    )
    conn.executemany(
        "INSERT INTO stop_times "
        "(trip_id, stop_id, stop_sequence, arrival_time, departure_time, arrival_secs, departure_secs) "
        "VALUES (:trip_id, :stop_id, :stop_sequence, :arrival_time, :departure_time, :arrival_secs, :departure_secs)",
        merged_rows,
    )
    conn.execute(
        "INSERT INTO synthesized_trips (trip_id, leg1_trip_id, leg2_trip_id, reversal_stop_id, reversal_seq) "
        "VALUES (?, ?, ?, ?, ?)",
        (synthetic_trip_id, trip_id_1, trip_id_2, reversal_stop_id, reversal_seq),
    )


def _load_calendar(gtfs_dir: Path, conn: sqlite3.Connection, excluded_service_ids: set[str]) -> None:
    df = pd.read_csv(gtfs_dir / "calendar.txt", dtype=str)
    day_cols = [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    ]
    df[day_cols] = df[day_cols].astype(int)
    if excluded_service_ids:
        # Every row for a duplicate service_id is dropped, not just all-but-one —
        # no trip references these service_ids any more (see _load_trips), and
        # keeping a row here with nothing pointing at it is dead weight that
        # would also make the UNIQUE index below fail again.
        df = df[~df["service_id"].isin(excluded_service_ids)]
    df.to_sql("calendar", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_calendar_service_id ON calendar(service_id)")


def _load_calendar_dates(gtfs_dir: Path, conn: sqlite3.Connection, excluded_service_ids: set[str]) -> None:
    df = pd.read_csv(gtfs_dir / "calendar_dates.txt", dtype=str)
    df["exception_type"] = df["exception_type"].astype(int)
    if excluded_service_ids:
        # Same dead-weight cleanup as _load_calendar: no trip references
        # these service_ids any more, so any exception rows for them here
        # are inert but pointless to keep.
        df = df[~df["service_id"].isin(excluded_service_ids)]
    df.to_sql("calendar_dates", conn, if_exists="replace", index=False)
    conn.execute(
        "CREATE INDEX idx_calendar_dates_lookup ON calendar_dates(service_id, date)"
    )
    # Queries filter primarily by date (see queries.py's active-service SQL),
    # which idx_calendar_dates_lookup above can't serve efficiently since
    # service_id is its leading column — this covers that access pattern.
    conn.execute(
        "CREATE INDEX idx_calendar_dates_date ON calendar_dates(date, exception_type, service_id)"
    )
