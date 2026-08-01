"""Regression tests for two post-midnight bugs found in code review
(2026-08-01) that the main checked-in fixture doesn't naturally exercise:

1. Leg 2's next-day flags were computed relative to leg 2's own anchor date
   (which can already be a day past the user's query date, once leg 1
   itself crosses midnight) instead of the original query date.
2. A leg-2 search window that itself extends past physical midnight only
   ever looked *backward* one service day (Phase 1's original fix), never
   *forward* — so a plainly-tagged, following-day trip (not GTFS's
   >=24:00:00 notation, just an ordinary early-morning departure under the
   next day's own service_id) was invisible.

Both need a purpose-built synthetic feed — the real checked-in fixture has
no trip that happens to land in either exact situation.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from app.db import build_database, get_connection
from app import queries

STOPS = pd.DataFrame(
    [
        {"stop_id": "S_AAA", "stop_code": "AAA", "stop_name": "Alpha"},
        {"stop_id": "S_CCC", "stop_code": "CCC", "stop_name": "Charlie"},
        {"stop_id": "S_BBB", "stop_code": "BBB", "stop_name": "Bravo"},
    ]
)
ROUTES = pd.DataFrame(
    [
        {"route_id": f"R{i}", "agency_id": "TT", "route_short_name": "", "route_long_name": f"Route {i}"}
        for i in range(1, 4)
    ]
)
AGENCY = pd.DataFrame([{"agency_id": "TT", "agency_name": "Test Trains"}])


def _write_feed(gtfs_dir: Path, trips, stop_times, calendar):
    STOPS.to_csv(gtfs_dir / "stops.txt", index=False)
    AGENCY.to_csv(gtfs_dir / "agency.txt", index=False)
    ROUTES.to_csv(gtfs_dir / "routes.txt", index=False)
    pd.DataFrame(trips).to_csv(gtfs_dir / "trips.txt", index=False)
    pd.DataFrame(stop_times).to_csv(gtfs_dir / "stop_times.txt", index=False)
    pd.DataFrame(calendar).to_csv(gtfs_dir / "calendar.txt", index=False)
    pd.DataFrame(columns=["service_id", "date", "exception_type"]).to_csv(
        gtfs_dir / "calendar_dates.txt", index=False
    )


def _stop_time(trip_id, stop_id, seq, hms):
    return {
        "trip_id": trip_id,
        "stop_id": stop_id,
        "stop_sequence": seq,
        "arrival_time": hms,
        "departure_time": hms,
    }


def _calendar_row(service_id, day, start, end):
    row = {d: 0 for d in queries.DAY_COLUMNS}
    row[day] = 1
    return {"service_id": service_id, "start_date": start, "end_date": end, **row}


@pytest.fixture
def leg1_crosses_midnight_db(tmp_path) -> Path:
    """Leg 1 (AAA -> CCC) departs 23:50 Monday and arrives 00:20 Tuesday
    (GTFS 24:20:00 notation). Leg 2 (CCC -> BBB) is a plain Tuesday-tagged
    trip departing 00:30 — found via leg 2's own inner same-day bucket
    regardless of the fix, so this isolates finding #1 (next-day
    rebasing) specifically."""
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    trips = [
        {"route_id": "R1", "service_id": "SVC_MON", "trip_id": "T1", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC_TUE", "trip_id": "T2", "trip_headsign": ""},
    ]
    stop_times = [
        _stop_time("T1", "S_AAA", 1, "23:50:00"),
        _stop_time("T1", "S_CCC", 2, "24:20:00"),
        _stop_time("T2", "S_CCC", 1, "00:30:00"),
        _stop_time("T2", "S_BBB", 2, "00:45:00"),
    ]
    calendar = [
        _calendar_row("SVC_MON", "monday", "20260803", "20260803"),
        _calendar_row("SVC_TUE", "tuesday", "20260804", "20260804"),
    ]
    _write_feed(gtfs_dir, trips, stop_times, calendar)
    db_path = tmp_path / "gtfs.db"
    build_database(gtfs_dir, db_path)
    return db_path


def test_leg2_next_day_flags_rebased_onto_query_date(leg1_crosses_midnight_db):
    conn = get_connection(leg1_crosses_midnight_db)
    origin = queries.get_station(conn, "AAA")
    destination = queries.get_station(conn, "BBB")

    results = queries.find_interchange_trips(
        conn, origin, destination, dt.date(2026, 8, 3), dt.time(23, 30), 60
    )
    assert len(results) == 1
    result = results[0]

    assert result.leg1.departure_time == "23:50:00"
    assert result.leg1.departure_next_day is False
    assert result.leg1.arrival_time == "00:20:00"
    assert result.leg1.arrival_next_day is True, "leg 1 arrives on the day after the query date"

    assert result.leg2.departure_time == "00:30:00"
    assert result.leg2.departure_next_day is True, (
        "leg 2 departs Tuesday, one day after the Monday query date — "
        "previously computed relative to leg 2's own anchor date instead, "
        "which happened to already be Tuesday, silently reading as False"
    )
    assert result.leg2.arrival_time == "00:45:00"
    assert result.leg2.arrival_next_day is True
    assert result.connection_minutes == 10


@pytest.fixture
def leg2_window_crosses_midnight_db(tmp_path) -> Path:
    """Leg 1 (AAA -> CCC) departs 23:20 and arrives 23:50 Monday — no
    midnight crossing on leg 1 itself. Leg 2's search window (23:55 + 85min)
    extends past physical midnight. Two competing leg-2 candidates exist:
    T2, Monday's own >=24:00:00 continuation (findable via the pre-existing
    backward-looking bucket), and T3, a plainly-tagged Tuesday trip at the
    same real clock time (only findable via the new forward-looking
    bucket) — isolates finding #2."""
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    trips = [
        {"route_id": "R1", "service_id": "SVC_MON", "trip_id": "T1", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC_MON", "trip_id": "T2", "trip_headsign": ""},
        {"route_id": "R3", "service_id": "SVC_TUE", "trip_id": "T3", "trip_headsign": ""},
    ]
    stop_times = [
        _stop_time("T1", "S_AAA", 1, "23:20:00"),
        _stop_time("T1", "S_CCC", 2, "23:50:00"),
        _stop_time("T2", "S_CCC", 1, "24:20:00"),
        _stop_time("T2", "S_BBB", 2, "24:35:00"),
        _stop_time("T3", "S_CCC", 1, "00:40:00"),
        _stop_time("T3", "S_BBB", 2, "00:55:00"),
    ]
    calendar = [
        _calendar_row("SVC_MON", "monday", "20260803", "20260803"),
        _calendar_row("SVC_TUE", "tuesday", "20260804", "20260804"),
    ]
    _write_feed(gtfs_dir, trips, stop_times, calendar)
    db_path = tmp_path / "gtfs.db"
    build_database(gtfs_dir, db_path)
    return db_path


def test_leg2_window_finds_trips_tagged_on_the_following_service_day(leg2_window_crosses_midnight_db):
    conn = get_connection(leg2_window_crosses_midnight_db)
    origin = queries.get_station(conn, "AAA")
    destination = queries.get_station(conn, "BBB")

    results = queries.find_interchange_trips(
        conn, origin, destination, dt.date(2026, 8, 3), dt.time(23, 0), 60
    )
    leg2_departures = {r.leg2.trip_id: r.leg2.departure_time for r in results}

    assert leg2_departures.get("T2") == "00:20:00", "Monday's own >=24:00:00 continuation"
    assert leg2_departures.get("T3") == "00:40:00", (
        "a plainly-tagged Tuesday trip at the same real time — previously invisible, "
        "since the leg-2 window only ever looked backward one service day, never forward"
    )
