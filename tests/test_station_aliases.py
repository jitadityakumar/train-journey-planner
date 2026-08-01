"""Regression tests for GitHub issue #11: TravelWhiz's GTFS conversion
models Paddington and Liverpool Street's Elizabeth line platforms as
separate pseudo-CRS stations (PDX, LSX) distinct from the mainline code
riders/Darwin actually use (PAD, LST) — see STATION_ALIASES in
app/config.py. A synthetic feed isolates this from the real checked-in
fixture, which doesn't model the split.
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
        {"stop_id": "S_HAN", "stop_code": "HAN", "stop_name": "Hanwell"},
        {"stop_id": "S_CWX", "stop_code": "CWX", "stop_name": "Canary Wharf"},
        {"stop_id": "S_PAD", "stop_code": "PAD", "stop_name": "London Paddington"},
        {"stop_id": "S_PDX", "stop_code": "PDX", "stop_name": "Paddington"},
        {"stop_id": "S_ABW", "stop_code": "ABW", "stop_name": "Abbey Wood"},
        {"stop_id": "S_OOO", "stop_code": "OOO", "stop_name": "Oldstead"},
    ]
)
ROUTES = pd.DataFrame(
    [
        {"route_id": f"R{i}", "agency_id": "TT", "route_short_name": "", "route_long_name": f"Route {i}"}
        for i in range(1, 5)
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
def alias_db(tmp_path) -> Path:
    """Two independent physical services sharing the Paddington complex:

    - T_LIZ: an Elizabeth line service CWX (08:05) -> PDX (08:30) -> ABW (09:00).
    - T_MAIN: a mainline service HAN (08:00) -> PAD (08:20) -> OOO (08:40).

    Neither trip literally calls at both PAD and PDX, matching how the real
    feed models them as unconnected physical infrastructure.
    """
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    trips = [
        {"route_id": "R1", "service_id": "SVC", "trip_id": "T_LIZ", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC", "trip_id": "T_MAIN", "trip_headsign": ""},
    ]
    stop_times = [
        _stop_time("T_LIZ", "S_CWX", 1, "08:05:00"),
        _stop_time("T_LIZ", "S_PDX", 2, "08:30:00"),
        _stop_time("T_LIZ", "S_ABW", 3, "09:00:00"),
        _stop_time("T_MAIN", "S_HAN", 1, "08:00:00"),
        _stop_time("T_MAIN", "S_PAD", 2, "08:20:00"),
        _stop_time("T_MAIN", "S_OOO", 3, "08:40:00"),
    ]
    calendar = [_calendar_row("SVC", "monday", "20260803", "20260803")]
    _write_feed(gtfs_dir, trips, stop_times, calendar)
    db_path = tmp_path / "gtfs.db"
    build_database(gtfs_dir, db_path)
    return db_path


def test_direct_search_finds_trip_that_literally_departs_the_alias_code(alias_db):
    """CWX -> PAD should find T_LIZ (which actually calls at PDX, not PAD)
    as a direct trip — the exact bug reported in issue #11."""
    conn = get_connection(alias_db)
    origin = queries.get_station(conn, "CWX")
    destination = queries.get_station(conn, "PAD")

    trips = queries.find_direct_trips(conn, origin, destination, dt.date(2026, 8, 3), dt.time(8, 0), 60)

    assert len(trips) == 1
    assert trips[0].trip_id == "T_LIZ"
    assert trips[0].departure_time == "08:05:00"
    assert trips[0].arrival_time == "08:30:00"


def test_direct_search_is_symmetric_in_the_alias_code_itself(alias_db):
    """Querying the alias code directly (PDX, not PAD) still works — the
    alias merge widens searches, it doesn't replace the original code."""
    conn = get_connection(alias_db)
    origin = queries.get_station(conn, "CWX")
    destination = queries.get_station(conn, "PDX")

    trips = queries.find_direct_trips(conn, origin, destination, dt.date(2026, 8, 3), dt.time(8, 0), 60)

    assert len(trips) == 1
    assert trips[0].trip_id == "T_LIZ"


def test_list_stations_hides_alias_codes_but_keeps_primary(alias_db):
    conn = get_connection(alias_db)
    codes = {s.stop_code for s in queries.list_stations(conn)}

    assert "PAD" in codes
    assert "PDX" not in codes, "PDX is folded into PAD's search automatically, not a code riders search for"


def test_get_station_still_resolves_the_alias_code_directly(alias_db):
    """Hiding PDX from the station list (autocomplete) shouldn't break a
    caller that already knows the code, e.g. a bookmarked API URL."""
    conn = get_connection(alias_db)
    stop = queries.get_station(conn, "PDX")
    assert stop.stop_code == "PDX"


def test_interchange_across_the_alias_still_applies_minimum_connection_time(alias_db):
    """HAN -> OOO has no direct route. HAN -> PAD (mainline, T_MAIN) then
    PAD -> ABW should find an interchange via T_LIZ, which actually departs
    from PDX — confirming leg 2's own alias merge (inherited from
    find_direct_trips) picks it up, with the normal MCT still enforced
    between T_MAIN's 08:20 PAD arrival and T_LIZ's 08:30 PDX departure
    (10 minutes — comfortably above the 5-minute minimum, not skipped)."""
    conn = get_connection(alias_db)
    origin = queries.get_station(conn, "HAN")
    destination = queries.get_station(conn, "ABW")

    results = queries.find_interchange_trips(conn, origin, destination, dt.date(2026, 8, 3), dt.time(7, 55), 30)

    assert len(results) == 1
    result = results[0]
    assert result.leg1.trip_id == "T_MAIN"
    assert result.leg2.trip_id == "T_LIZ"
    assert result.interchange.stop_code == "PAD"
    assert result.connection_minutes == 10


def test_interchange_does_not_offer_the_alias_of_the_destination_as_a_fake_interchange_stop(alias_db):
    """HAN -> PAD: T_MAIN reaches PAD directly (Phase 1's job). It must not
    also show up as a 1-change journey via some candidate that's really
    just PAD's own alias group — that would be a bogus duplicate of the
    direct result found via find_direct_trips' own alias merge."""
    conn = get_connection(alias_db)
    origin = queries.get_station(conn, "HAN")
    destination = queries.get_station(conn, "PAD")

    results = queries.find_interchange_trips(conn, origin, destination, dt.date(2026, 8, 3), dt.time(7, 55), 30)

    assert results == []


def test_same_station_error_still_raised_for_the_literal_same_code(alias_db):
    conn = get_connection(alias_db)
    stop = queries.get_station(conn, "PAD")
    with pytest.raises(queries.SameStationError):
        queries.find_direct_trips(conn, stop, stop, dt.date(2026, 8, 3), dt.time(8, 0), 60)


def test_same_station_error_also_raised_across_an_alias_pair(alias_db):
    """PAD and PDX have different stop_ids but are the same physical
    station complex — a query between them is logically a same-station
    query too, and should be rejected the same way, not silently return an
    empty 'no journeys found' result."""
    conn = get_connection(alias_db)
    pad = queries.get_station(conn, "PAD")
    pdx = queries.get_station(conn, "PDX")

    with pytest.raises(queries.SameStationError):
        queries.find_direct_trips(conn, pad, pdx, dt.date(2026, 8, 3), dt.time(8, 0), 60)
    with pytest.raises(queries.SameStationError):
        queries.find_interchange_trips(conn, pdx, pad, dt.date(2026, 8, 3), dt.time(8, 0), 60)


def test_interchange_excludes_the_origins_own_alias_group_as_a_candidate_stop(tmp_path):
    """Symmetric to the destination-side exclusion above: starting a
    journey *from* PAD shouldn't offer PDX as a candidate interchange stop
    either (self-interchange within the same physical complex). Without the
    origin-alias exclusion, T_FROM_PAD's PDX stop would look like a valid
    interchange point, and T_LIZ2 (a genuinely separate service departing
    PDX) would produce a spurious 'change at PDX' journey."""
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    trips = [
        {"route_id": "R1", "service_id": "SVC", "trip_id": "T_FROM_PAD", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC", "trip_id": "T_LIZ2", "trip_headsign": ""},
    ]
    stop_times = [
        _stop_time("T_FROM_PAD", "S_PAD", 1, "08:00:00"),
        _stop_time("T_FROM_PAD", "S_PDX", 2, "08:05:00"),
        _stop_time("T_FROM_PAD", "S_OOO", 3, "08:30:00"),
        _stop_time("T_LIZ2", "S_PDX", 1, "08:15:00"),
        _stop_time("T_LIZ2", "S_ABW", 2, "08:45:00"),
    ]
    calendar = [_calendar_row("SVC", "monday", "20260803", "20260803")]
    _write_feed(gtfs_dir, trips, stop_times, calendar)
    db_path = tmp_path / "gtfs.db"
    build_database(gtfs_dir, db_path)

    conn = get_connection(db_path)
    origin = queries.get_station(conn, "PAD")
    destination = queries.get_station(conn, "ABW")

    results = queries.find_interchange_trips(conn, origin, destination, dt.date(2026, 8, 3), dt.time(7, 55), 30)

    assert all(r.interchange.stop_code not in {"PAD", "PDX"} for r in results), (
        "PDX is in the origin's own alias group — offering it as an interchange stop "
        "would be a bogus self-interchange within the same physical complex"
    )
