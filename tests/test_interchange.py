"""Golden-path, MCT-boundary, dedupe, and merge/ranking tests for the
single-interchange finder (Phase 2). The golden-path times are real,
previously-verified scheduled times (RESEARCH.md's Data Validation
section) — not invented.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import config, queries


def test_interchange_bns_clj_lrd_golden_path(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "LRD")

    results = queries.find_interchange_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )

    def find(leg1_dep, connection_minutes):
        return next(
            r
            for r in results
            if r.interchange.stop_code == "CLJ"
            and r.leg1.departure_time == leg1_dep
            and r.connection_minutes == connection_minutes
        )

    first = find("09:06:00", 28)
    assert first.leg1.arrival_time == "09:14:00"
    assert first.leg2.departure_time == "09:42:00"
    assert first.leg2.arrival_time == "10:32:30"
    assert first.total_duration_minutes == 86

    second = find("09:35:00", 19)
    assert second.leg1.arrival_time == "09:43:00"
    assert second.leg2.departure_time == "10:02:00"
    assert second.leg2.arrival_time == "10:57:30"
    assert second.total_duration_minutes == 82


def test_mct_boundary_excludes_connections_under_the_minimum(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "LRD")

    # The golden 28-minute CLJ connection (09:14 arrival -> 09:42 departure)
    # from the test above, used to pin an exact boundary.
    with_28_min_minimum = queries.find_interchange_trips(
        conn,
        origin,
        destination,
        dt.date(2026, 8, 17),
        dt.time(9, 0),
        60,
        min_connection_minutes=28,
    )
    assert any(
        r.interchange.stop_code == "CLJ" and r.leg1.departure_time == "09:06:00" and r.connection_minutes == 28
        for r in with_28_min_minimum
    ), "a connection exactly at the minimum should be included"

    with_29_min_minimum = queries.find_interchange_trips(
        conn,
        origin,
        destination,
        dt.date(2026, 8, 17),
        dt.time(9, 0),
        60,
        min_connection_minutes=29,
    )
    assert not any(
        r.interchange.stop_code == "CLJ" and r.leg1.departure_time == "09:06:00" and r.connection_minutes == 28
        for r in with_29_min_minimum
    ), "a connection one minute under the minimum should be excluded"


def test_default_min_connection_time_is_five_minutes():
    assert config.MIN_CONNECTION_TIME_MINUTES == 5


def test_interchange_allows_leg1_trips_that_also_reach_destination_directly(conn):
    """Corrected dedupe rule (2026-08-01 code review): a trip reaching the
    destination directly is *not* automatically excluded from being used as
    leg 1 to some other interchange point — a slow direct service can
    legitimately lose to changing onto a faster one, so excluding by "does
    trip1 ever reach the destination" was answering the wrong question. It's
    only ever pointless when leg 2 would land back on that exact same
    trip_id — that guard is tested separately below."""
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")

    direct_trip_ids = {
        t.trip_id
        for t in queries.find_direct_trips(conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60)
    }
    interchange_results = queries.find_interchange_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )
    leg1_trip_ids = {r.leg1.trip_id for r in interchange_results}
    assert direct_trip_ids & leg1_trip_ids, (
        "expected at least one trip that reaches WAT directly to also appear "
        "as a leg-1 candidate toward some other interchange stop"
    )


def test_interchange_keeps_distinct_results_for_different_interchange_stops(conn):
    """Regression test (2026-08-01): an earlier version of the dedupe step
    keyed only on (leg1.trip_id, leg2.trip_id), which silently dropped the
    documented CLJ worked example — the same two physical trains also cross
    paths at Waterloo, and deduping without the interchange stop in the key
    collapsed the CLJ result into the shorter-wait Waterloo one, even though
    they're genuinely different stations a passenger could change at."""
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "LRD")
    results = queries.find_interchange_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )
    stops_for_this_trip_pair = {
        r.interchange.stop_code
        for r in results
        if r.leg1.departure_time == "09:06:00" and r.leg2.arrival_time == "10:32:30"
    }
    assert "CLJ" in stops_for_this_trip_pair
    assert "WAT" in stops_for_this_trip_pair


def test_interchange_excludes_same_trip_as_both_legs(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "LRD")
    results = queries.find_interchange_trips(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60
    )
    assert all(r.leg1.trip_id != r.leg2.trip_id for r in results)


def test_interchange_same_station_raises(conn):
    origin = queries.get_station(conn, "BNS")
    with pytest.raises(queries.SameStationError):
        queries.find_interchange_trips(conn, origin, origin, dt.date(2026, 8, 17), dt.time(9, 0), 60)


def test_find_journeys_merges_direct_and_interchange_sorted_by_departure(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "LRD")
    journeys = queries.find_journeys(conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60)

    assert journeys, "expected at least one interchange journey for BNS -> LRD"
    assert all(j.kind == "interchange" for j in journeys), "no direct BNS->LRD route exists in this data"

    def sort_key(j):
        t = dt.time.fromisoformat(j.departure_time)
        minutes = t.hour * 60 + t.minute
        return minutes + (24 * 60 if j.departure_next_day else 0)

    keys = [sort_key(j) for j in journeys]
    assert keys == sorted(keys)


def test_find_journeys_includes_direct_trips_for_bns_wat(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")
    journeys = queries.find_journeys(conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60)

    direct_departures = {j.departure_time for j in journeys if j.kind == "direct"}
    assert "09:06:00" in direct_departures
    assert "09:35:00" in direct_departures
