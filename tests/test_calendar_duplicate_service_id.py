"""Regression test for GitHub issue #32: TravelWhiz's feed emitted the same
`service_id` twice in `calendar.txt`, for two entirely unrelated trips from
different operators (a collision in their ID generation, not a revision of
the same service — the two rows' date ranges genuinely overlapped with
different day-patterns, so there is no principled way to pick a "correct"
row). Previously this hard-failed the whole nightly refresh with
`sqlite3.IntegrityError: UNIQUE constraint failed: calendar.service_id`.

Chosen fix (Option B, see the issue): drop every trip referencing a
duplicate service_id from the build entirely, log a warning, and let the
rest of the refresh succeed — rather than guessing which calendar row
should apply to them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from app import queries
from app.db import build_database, get_connection

STOPS = pd.DataFrame(
    [
        {"stop_id": "S_WIG", "stop_code": "WIG", "stop_name": "Wigan Wallgate"},
        {"stop_id": "S_LDS", "stop_code": "LDS", "stop_name": "Leeds"},
        {"stop_id": "S_VIC", "stop_code": "VIC", "stop_name": "London Victoria"},
        {"stop_id": "S_GRV", "stop_code": "GRV", "stop_name": "Gravesend"},
        {"stop_id": "S_UNA", "stop_code": "UNA", "stop_name": "Unaffected A"},
        {"stop_id": "S_UNB", "stop_code": "UNB", "stop_name": "Unaffected B"},
    ]
)
ROUTES = pd.DataFrame(
    [
        {"route_id": "R_COLLIDE_1", "agency_id": "NT", "route_short_name": "", "route_long_name": "Wigan Wallgate - Leeds"},
        {"route_id": "R_COLLIDE_2", "agency_id": "SE", "route_short_name": "", "route_long_name": "London Victoria - Gravesend"},
        {"route_id": "R_UNAFFECTED", "agency_id": "NT", "route_short_name": "", "route_long_name": "Unaffected Route"},
    ]
)
AGENCY = pd.DataFrame(
    [
        {"agency_id": "NT", "agency_name": "Northern Trains"},
        {"agency_id": "SE", "agency_name": "Southeastern"},
    ]
)

MONDAY = dt.date(2026, 8, 3)


def _calendar_row(service_id, day, start, end):
    row = {d: 0 for d in queries.DAY_COLUMNS}
    row[day] = 1
    return {"service_id": service_id, "start_date": start, "end_date": end, **row}


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    trips = [
        # Colliding service_id "SVC_COLLIDE": two unrelated trips from
        # different operators, both referencing it — mirrors the real
        # feed's Northern Wigan->Leeds vs. Southeastern Victoria->Gravesend
        # collision.
        {"route_id": "R_COLLIDE_1", "service_id": "SVC_COLLIDE", "trip_id": "T_WIG_LDS", "trip_headsign": "Leeds", "trip_short_name": "1001"},
        {"route_id": "R_COLLIDE_2", "service_id": "SVC_COLLIDE", "trip_id": "T_VIC_GRV", "trip_headsign": "Gravesend", "trip_short_name": "2001"},
        # A trip on an unrelated, non-duplicated service_id — must survive
        # the build untouched.
        {"route_id": "R_UNAFFECTED", "service_id": "SVC_OK", "trip_id": "T_UNA_UNB", "trip_headsign": "Unaffected B", "trip_short_name": "3001"},
    ]
    stop_times = [
        {"trip_id": "T_WIG_LDS", "stop_id": "S_WIG", "stop_sequence": 1, "arrival_time": "16:49:00", "departure_time": "16:49:00"},
        {"trip_id": "T_WIG_LDS", "stop_id": "S_LDS", "stop_sequence": 2, "arrival_time": "19:04:00", "departure_time": "19:04:00"},
        {"trip_id": "T_VIC_GRV", "stop_id": "S_VIC", "stop_sequence": 1, "arrival_time": "17:33:00", "departure_time": "17:33:00"},
        {"trip_id": "T_VIC_GRV", "stop_id": "S_GRV", "stop_sequence": 2, "arrival_time": "18:38:00", "departure_time": "18:38:00"},
        {"trip_id": "T_UNA_UNB", "stop_id": "S_UNA", "stop_sequence": 1, "arrival_time": "10:00:00", "departure_time": "10:00:00"},
        {"trip_id": "T_UNA_UNB", "stop_id": "S_UNB", "stop_sequence": 2, "arrival_time": "10:30:00", "departure_time": "10:30:00"},
    ]
    calendar = [
        # Two conflicting rows for the same service_id, overlapping date
        # ranges, different day-patterns — same shape as the real feed.
        _calendar_row("SVC_COLLIDE", "monday", "20260518", "20261212"),
        _calendar_row("SVC_COLLIDE", "saturday", "20260523", "20261010"),
        _calendar_row("SVC_OK", "monday", "20260803", "20260803"),
    ]

    gtfs_dir = tmp_path_factory.mktemp("gtfs_duplicate_service_id")
    STOPS.to_csv(gtfs_dir / "stops.txt", index=False)
    AGENCY.to_csv(gtfs_dir / "agency.txt", index=False)
    ROUTES.to_csv(gtfs_dir / "routes.txt", index=False)
    pd.DataFrame(trips).to_csv(gtfs_dir / "trips.txt", index=False)
    pd.DataFrame(stop_times).to_csv(gtfs_dir / "stop_times.txt", index=False)
    pd.DataFrame(calendar).to_csv(gtfs_dir / "calendar.txt", index=False)
    pd.DataFrame(columns=["service_id", "date", "exception_type"]).to_csv(
        gtfs_dir / "calendar_dates.txt", index=False
    )

    db_path = gtfs_dir / "gtfs.db"
    build_database(gtfs_dir, db_path)
    return db_path


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_build_database_does_not_raise_on_duplicate_service_id(db_path):
    """The headline regression: this used to raise
    sqlite3.IntegrityError and abort the whole refresh."""
    assert db_path.exists()


def test_trips_referencing_the_duplicate_service_id_are_excluded(conn):
    rows = conn.execute("SELECT trip_id FROM trips ORDER BY trip_id").fetchall()
    trip_ids = {r["trip_id"] for r in rows}
    assert "T_WIG_LDS" not in trip_ids
    assert "T_VIC_GRV" not in trip_ids
    assert "T_UNA_UNB" in trip_ids


def test_stop_times_for_excluded_trips_are_also_dropped(conn):
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM stop_times WHERE trip_id IN ('T_WIG_LDS', 'T_VIC_GRV')"
    ).fetchone()
    assert rows["n"] == 0


def test_duplicate_service_id_is_absent_from_calendar_table(conn):
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM calendar WHERE service_id = 'SVC_COLLIDE'"
    ).fetchone()
    assert rows["n"] == 0


def test_unaffected_service_and_trip_survive_the_build(conn):
    origin = queries.get_station(conn, "UNA")
    destination = queries.get_station(conn, "UNB")
    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(9, 0), 120)
    assert [t.trip_id for t in results] == ["T_UNA_UNB"]


def test_excluded_trips_are_unqueryable(conn):
    origin = queries.get_station(conn, "WIG")
    destination = queries.get_station(conn, "LDS")
    assert queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(16, 0), 120) == []
