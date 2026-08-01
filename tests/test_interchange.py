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

    # Pins the full surviving set (not just "these two are present") so a
    # future change that over-prunes directs against each other — e.g. a
    # faster later departure wrongly deleting an earlier one it doesn't
    # actually dominate — would be caught here, not just a change that
    # under-prunes.
    direct_departures = {j.departure_time for j in journeys if j.kind == "direct"}
    assert direct_departures == {
        "09:06:00",
        "09:11:30",
        "09:19:30",
        "09:26:30",
        "09:35:00",
        "09:41:30",
        "09:49:30",
        "09:56:30",
    }


def test_dominated_journeys_are_filtered_from_bns_wat(conn):
    """2026-08-01 UX review: BNS->WAT has frequent direct service, so every
    single-interchange candidate is beaten by some direct train that departs
    at least as late and arrives at least as early — none should survive."""
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")
    journeys = queries.find_journeys(conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60)

    assert journeys, "expected at least the direct BNS->WAT trips"
    assert all(j.kind == "direct" for j in journeys), "no interchange should beat this route's direct service"


def test_find_journeys_direct_only_excludes_interchange_results(conn):
    """BNS -> LRD has no direct route at all (see the module's other tests) —
    with direct_only=True the interchange search shouldn't even run, so the
    result should be empty rather than falling back to interchange results."""
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "LRD")
    journeys = queries.find_journeys(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60, direct_only=True
    )
    assert journeys == []


def test_find_journeys_direct_only_keeps_direct_results(conn):
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "WAT")
    all_journeys = queries.find_journeys(conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60)
    direct_only_journeys = queries.find_journeys(
        conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60, direct_only=True
    )
    assert all(j.kind == "direct" for j in direct_only_journeys)
    assert direct_only_journeys == [j for j in all_journeys if j.kind == "direct"]


def test_dominated_interchange_dropped_when_a_later_departure_reaches_the_same_arrival(conn):
    """The 09:06:00 CLJ change used to be the documented worked example, but
    it's dominated: 09:11:30 reaches the same interchange, on the same
    connecting train, arriving at the same time (10:32:30) — so there's no
    reason to ever leave at 09:06:00 for this journey. Only the latest
    departure that still catches a given arrival should survive."""
    origin = queries.get_station(conn, "BNS")
    destination = queries.get_station(conn, "LRD")
    journeys = queries.find_journeys(conn, origin, destination, dt.date(2026, 8, 17), dt.time(9, 0), 60)

    clj_arrivals_at_10_32_30 = [
        j
        for j in journeys
        if j.kind == "interchange"
        and j.interchange.interchange.stop_code == "CLJ"
        and j.arrival_time == "10:32:30"
    ]
    assert len(clj_arrivals_at_10_32_30) == 1, "only the latest-departing, non-dominated option should remain"
    assert clj_arrivals_at_10_32_30[0].departure_time == "09:26:30"


def _journey(kind, departure_time, arrival_time, *, departure_next_day=False, arrival_next_day=False):
    return queries.Journey(
        kind=kind,
        departure_time=departure_time,
        departure_next_day=departure_next_day,
        arrival_time=arrival_time,
        arrival_next_day=arrival_next_day,
        duration_minutes=1,
    )


def test_dominance_filter_keeps_ties_on_departure_arrival_and_changes():
    """Two candidates identical on departure, arrival, and change count are
    genuinely different real choices (e.g. two different interchange
    stations offering the same overall trip) — a tie doesn't dominate."""
    a = _journey("interchange", "09:00:00", "10:00:00")
    b = _journey("interchange", "09:00:00", "10:00:00")
    assert queries._drop_dominated_journeys([a, b]) == [a, b]


def test_dominance_filter_drops_a_strictly_worse_journey():
    # Same departure, but the direct arrives earlier with fewer changes —
    # dominates the interchange outright.
    direct = _journey("direct", "09:00:00", "09:30:00")
    interchange = _journey("interchange", "09:00:00", "09:45:00")
    assert queries._drop_dominated_journeys([direct, interchange]) == [direct]


def test_dominance_filter_isolates_the_changes_axis():
    # Same departure and arrival — only the change count differs — so this
    # can't pass by accident of the time-based axes alone.
    direct = _journey("direct", "09:00:00", "09:30:00")
    interchange = _journey("interchange", "09:00:00", "09:30:00")
    assert queries._drop_dominated_journeys([direct, interchange]) == [direct]


def test_dominance_filter_can_drop_a_direct_journey():
    # A deliberate departure from how e.g. SWR's own planner behaves (it
    # lists every scheduled direct train): applied uniformly, a later
    # direct that also arrives earlier makes an earlier, slower direct
    # pointless to offer too, not just interchanges.
    slower_earlier = _journey("direct", "09:00:00", "09:50:00")
    faster_later = _journey("direct", "09:10:00", "09:40:00")
    assert queries._drop_dominated_journeys([slower_earlier, faster_later]) == [faster_later]


def test_dominance_filter_keeps_both_across_midnight_when_neither_dominates():
    # 23:50 same-day vs 00:10 next-day: 1430 vs 1450 absolute minutes —
    # the later one is genuinely later, not wrapped back around to "earlier"
    # by the day boundary.
    late_tonight = _journey("direct", "23:50:00", "23:59:00")
    early_tomorrow = _journey("direct", "00:10:00", "00:20:00", departure_next_day=True, arrival_next_day=True)
    result = queries._drop_dominated_journeys([late_tonight, early_tomorrow])
    assert result == [late_tonight, early_tomorrow]


def test_num_changes_raises_on_unrecognized_journey_kind():
    # A future journey kind (e.g. multi-interchange) must not silently
    # collapse to a wrong change count and dominate/delete real results.
    unknown = _journey("double-interchange", "09:00:00", "10:00:00")
    with pytest.raises(ValueError):
        queries._num_changes(unknown)


def test_dominance_filter_drops_across_midnight_when_dominated():
    # A same-day-late departure that arrives after a next-day-early one that
    # departs even later still — the next-day journey wins on both axes.
    dominated = _journey("direct", "23:50:00", "00:40:00", arrival_next_day=True)
    dominant = _journey("direct", "23:55:00", "00:30:00", departure_next_day=False, arrival_next_day=True)
    assert queries._drop_dominated_journeys([dominated, dominant]) == [dominant]


def test_dominance_filter_keeps_journeys_that_trade_off_departure_and_arrival():
    # Neither dominates: the first departs earlier but also arrives earlier.
    earlier = _journey("direct", "09:00:00", "09:30:00")
    later = _journey("direct", "09:10:00", "09:50:00")
    assert queries._drop_dominated_journeys([earlier, later]) == [earlier, later]
