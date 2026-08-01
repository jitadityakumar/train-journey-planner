"""Golden-path and validation tests for the direct-route query logic.

The golden-path times are real, previously-verified scheduled departures
(see RESEARCH.md's Data Validation section / PLAN.md's Testing Strategy) —
not invented — so these tests double as a regression guard against the
query logic silently returning wrong times.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import queries


def test_direct_bns_to_wat_golden_path(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")

    trips = queries.find_direct_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )

    departures = {(t.departure_time, t.arrival_time) for t in trips}
    assert ("09:06:00", "09:26:00") in departures
    assert ("09:35:00", "09:57:30") in departures


def test_direct_trip_includes_ordered_intermediate_stops(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")
    trips = queries.find_direct_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )
    fast_trip = next(t for t in trips if t.departure_time == "09:06:00")

    stop_codes = [s.stop_code for s in fast_trip.intermediate_stops]
    assert stop_codes == ["PUT", "WNT", "CLJ", "QRB", "VXH"]


def test_direct_trip_includes_agency_name(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")
    trips = queries.find_direct_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )
    fast_trip = next(t for t in trips if t.departure_time == "09:06:00")
    assert fast_trip.agency_name == "South Western Railway"


def test_window_excludes_departures_outside_range(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")
    trips = queries.find_direct_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )
    for t in trips:
        # Half-open window: [09:00, 10:00) — a departure at exactly 10:00:00
        # belongs to the *next* hour's window, not this one.
        assert dt.time(9, 0) <= dt.time.fromisoformat(t.departure_time[:8]) < dt.time(10, 0)


def test_post_midnight_trip_visible_from_next_calendar_day_query(conn):
    """Regression test for a real bug found in code review (2026-08-01): a
    trip stored under Saturday's service_id with a departure of 24:01:00
    (i.e. 00:01 Sunday in real clock time) was invisible when querying
    Sunday 00:00 directly, because the naive query only looked at Sunday's
    own active services — which don't include this trip at all, since GTFS
    represents its post-midnight continuation under the *previous* day.
    """
    origin = queries.get_station(conn, "CLJ")
    destination = queries.get_station(conn, "WAT")

    trips = queries.find_direct_trips(
        conn, origin, destination, dt.date(2026, 8, 16), dt.time(0, 0), 30
    )
    match = next(t for t in trips if t.trip_id == "L81910:20260815:O:SW:6400fd01")
    assert match.departure_time == "00:01:00"
    assert match.arrival_time == "00:10:00"
    # This trip's raw GTFS departure (24:01:00) sits on the day *before* the
    # query date, so relative to the query it's not a "next day" departure.
    assert match.departure_next_day is False
    assert match.arrival_next_day is False


def test_next_day_flag_set_when_window_itself_spans_midnight(conn):
    """The same trip as above, this time found via a same-day query whose
    window spans across midnight — here the departure genuinely does fall
    on the day after the requested date, so the +1 marker should show.
    """
    origin = queries.get_station(conn, "CLJ")
    destination = queries.get_station(conn, "WAT")

    trips = queries.find_direct_trips(
        conn, origin, destination, dt.date(2026, 8, 15), dt.time(23, 55), 30
    )
    match = next(t for t in trips if t.trip_id == "L81910:20260815:O:SW:6400fd01")
    assert match.departure_time == "00:01:00"
    assert match.departure_next_day is True
    assert match.arrival_next_day is True


def test_no_trips_returns_empty_list_not_error(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")
    # 03:00 window: no plausible passenger service at this hour.
    trips = queries.find_direct_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(3, 0), 60
    )
    assert trips == []


def test_unknown_station_code_raises(conn):
    with pytest.raises(queries.UnknownStationError):
        queries.get_station(conn, "ZZZ")


def test_station_lookup_is_case_insensitive(conn):
    upper = queries.get_station(conn, "BNS")
    lower = queries.get_station(conn, "bns")
    assert upper.stop_id == lower.stop_id


def test_date_outside_feed_coverage_raises(conn):
    with pytest.raises(queries.DateOutOfRangeError):
        queries.validate_date_in_range(conn, dt.date(2020, 1, 1))


def test_same_origin_and_destination_raises(conn):
    origin = queries.get_station(conn, "BNS")
    with pytest.raises(queries.SameStationError):
        queries.find_direct_trips(conn, origin, origin, dt.date(2026, 8, 17), dt.time(9, 0), 60)


def test_list_stations_returns_one_row_per_crs_code(conn):
    stations = queries.list_stations(conn)
    codes = [s.stop_code for s in stations]
    assert len(codes) == len(set(codes))
    assert "BNS" in codes and "WAT" in codes


def test_list_stations_excludes_blank_or_null_crs_codes(conn):
    # Parent stations / non-rail stops in a real feed can have no CRS code
    # at all; SQLite's GROUP BY collapses every NULL into one group, so
    # these must be filtered out rather than surfaced as a bogus station.
    conn.execute(
        "INSERT INTO stops (stop_id, stop_code, stop_name) VALUES (?, ?, ?)",
        ("PARENT1", None, "Some Parent Station"),
    )
    conn.execute(
        "INSERT INTO stops (stop_id, stop_code, stop_name) VALUES (?, ?, ?)",
        ("PARENT2", "", "Another Parent Station"),
    )
    stations = queries.list_stations(conn)
    assert all(s.stop_code for s in stations)
    assert "Some Parent Station" not in [s.stop_name for s in stations]
    assert "Another Parent Station" not in [s.stop_name for s in stations]
