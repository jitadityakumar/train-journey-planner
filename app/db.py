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

import sqlite3
from pathlib import Path

import pandas as pd

REQUIRED_FILES = (
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
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
    try:
        _load_stops(gtfs_dir, conn)
        _load_routes(gtfs_dir, conn)
        _load_trips(gtfs_dir, conn)
        _load_stop_times(gtfs_dir, conn)
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
        usecols=["trip_id", "route_id", "service_id", "trip_headsign"],
    )
    df.to_sql("trips", conn, if_exists="replace", index=False)
    conn.execute("CREATE UNIQUE INDEX idx_trips_trip_id ON trips(trip_id)")
    conn.execute("CREATE INDEX idx_trips_service_id ON trips(service_id)")


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
    conn.execute("CREATE INDEX idx_stop_times_stop_id ON stop_times(stop_id)")


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
