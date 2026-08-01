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

from app.config import REVERSAL_MAX_DWELL_MINUTES

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
    that the refresh job may `os.replace` out from under it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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
        _load_trips(gtfs_dir, conn)
        _load_stop_times(gtfs_dir, conn)
        _build_trip_continuations(conn)
        _load_calendar(gtfs_dir, conn)
        _load_calendar_dates(gtfs_dir, conn)
        conn.commit()
    finally:
        conn.close()


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


def _load_trips(gtfs_dir: Path, conn: sqlite3.Connection) -> None:
    df = pd.read_csv(
        gtfs_dir / "trips.txt",
        dtype=str,
        usecols=["trip_id", "route_id", "service_id", "trip_headsign", "trip_short_name"],
    )
    df.to_sql("trips", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_trips_trip_id ON trips(trip_id)")
    conn.execute("CREATE INDEX idx_trips_service_id ON trips(service_id)")
    # Backs the reversal-continuation join below (trip_short_name is the
    # National Rail headcode — this feed has no block_id, GTFS's usual
    # same-vehicle marker; see GitHub issue #15).
    conn.execute("CREATE INDEX idx_trips_short_name_service ON trips(trip_short_name, service_id)")


def _load_stop_times(gtfs_dir: Path, conn: sqlite3.Connection) -> None:
    df = pd.read_csv(
        gtfs_dir / "stop_times.txt",
        dtype=str,
        usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    )
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
    conn: sqlite3.Connection, max_dwell_minutes: int = REVERSAL_MAX_DWELL_MINUTES
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
    `block_id`, GTFS's usual same-vehicle marker) and `service_id`, run
    under the same agency, and the second departs the first's terminus
    within `max_dwell_minutes` of it arriving (a vehicle-dwell value, not a
    passenger-transfer one — see REVERSAL_MAX_DWELL_MINUTES in config.py).
    `service_id` alone is a calendar/running-days-pattern key, not a
    per-specific-service one, so trip_short_name + service_id + agency
    together are what's actually being trusted here, not any single field.

    A terminating trip matched by more than one departure, or a departure
    matched by more than one terminating trip, is ambiguous — skipped
    rather than guessed at, since a fabricated "direct" journey that doesn't
    really exist is worse than the miss this is meant to fix.

    Scoped to single (2-leg) reversals only: a genuine double reversal would
    need a further pass chaining synthesized trips together, not implemented
    here.
    """
    max_gap_secs = max_dwell_minutes * 60
    candidates = conn.execute(
        """
        WITH termini AS (
            SELECT st.trip_id, st.stop_id, st.arrival_secs
            FROM stop_times st
            JOIN (
                SELECT trip_id, MAX(stop_sequence) AS max_seq FROM stop_times GROUP BY trip_id
            ) m ON m.trip_id = st.trip_id AND m.max_seq = st.stop_sequence
        ),
        origins AS (
            SELECT st.trip_id, st.stop_id, st.departure_secs
            FROM stop_times st
            JOIN (
                SELECT trip_id, MIN(stop_sequence) AS min_seq FROM stop_times GROUP BY trip_id
            ) m ON m.trip_id = st.trip_id AND m.min_seq = st.stop_sequence
        )
        SELECT term.trip_id AS trip_id_1, orig.trip_id AS trip_id_2, term.stop_id AS reversal_stop_id
        FROM termini term
        JOIN trips t1 ON t1.trip_id = term.trip_id
        JOIN routes r1 ON r1.route_id = t1.route_id
        JOIN origins orig ON orig.stop_id = term.stop_id AND orig.trip_id != term.trip_id
        JOIN trips t2 ON t2.trip_id = orig.trip_id
        JOIN routes r2 ON r2.route_id = t2.route_id
        WHERE t1.service_id = t2.service_id
          AND t1.trip_short_name IS NOT NULL AND TRIM(t1.trip_short_name) != ''
          AND t1.trip_short_name = t2.trip_short_name
          AND r1.agency_id = r2.agency_id
          AND orig.departure_secs - term.arrival_secs BETWEEN 0 AND :max_gap
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
            "(a terminating trip matched more than one departure, or vice versa)",
            ambiguous,
        )
    # Logged at INFO rather than just asserted in a test — this is the
    # actual count needed to empirically sweep REVERSAL_MAX_DWELL_MINUTES
    # against the real production feed (see PLAN.md's review notes), which
    # isn't available in this dev/test environment.
    logger.info("_build_trip_continuations: synthesized %d reversal-continuation trip(s)", len(pairs))

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
        _synthesize_continuation_trip(conn, row["trip_id_1"], row["trip_id_2"], row["reversal_stop_id"])

    conn.execute("CREATE INDEX idx_synthesized_trips_leg1 ON synthesized_trips(leg1_trip_id)")
    conn.execute("CREATE INDEX idx_synthesized_trips_leg2 ON synthesized_trips(leg2_trip_id)")


def _synthesize_continuation_trip(
    conn: sqlite3.Connection, trip_id_1: str, trip_id_2: str, reversal_stop_id: str
) -> None:
    """Inserts one combined trip (trips + stop_times + synthesized_trips
    rows) covering `trip_id_1` then `trip_id_2`. Skips (rather than
    inserting a corrupt row) if either leg's stop_times are missing, or if
    leg 2's first departure would be before leg 1's last arrival — the
    caller's gap filter already guarantees this can't happen for a
    genuinely matched pair, but this stays a hard invariant rather than a
    trusted precondition."""
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
        "SELECT stop_id, arrival_time, departure_time, arrival_secs, departure_secs "
        "FROM stop_times WHERE trip_id = ? ORDER BY stop_sequence",
        (trip_id_2,),
    ).fetchall()
    if not leg1_stops or not leg2_stops:
        return
    if leg2_stops[0]["departure_secs"] < leg1_stops[-1]["arrival_secs"]:
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
    # 2's real departure (the dwell), keeping leg 1's arrival untouched.
    merged_rows[-1]["departure_time"] = leg2_stops[0]["departure_time"]
    merged_rows[-1]["departure_secs"] = leg2_stops[0]["departure_secs"]
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
        for i, s in enumerate(leg2_stops[1:], start=1)
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


def _load_calendar(gtfs_dir: Path, conn: sqlite3.Connection) -> None:
    df = pd.read_csv(gtfs_dir / "calendar.txt", dtype=str)
    day_cols = [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    ]
    df[day_cols] = df[day_cols].astype(int)
    df.to_sql("calendar", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_calendar_service_id ON calendar(service_id)")


def _load_calendar_dates(gtfs_dir: Path, conn: sqlite3.Connection) -> None:
    df = pd.read_csv(gtfs_dir / "calendar_dates.txt", dtype=str)
    df["exception_type"] = df["exception_type"].astype(int)
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
