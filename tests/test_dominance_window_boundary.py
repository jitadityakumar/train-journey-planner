"""Tests for GitHub issue #19: dominance filtering on /api/direct, and the
window-boundary blind spot that would otherwise reproduce on it (a slow
trip near the tail of the display window can survive the Pareto filter
purely because the faster trip that would dominate it departs just after
the window closes — see the widen-then-trim fix in dominant_direct_trips
and find_journeys, app/queries.py).

Purpose-built synthetic feeds (following tests/test_midnight_interchange.py's
pattern) — the real checked-in fixture doesn't naturally land trips at the
exact boundary needed to exercise this.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from app.db import build_database, get_connection
from app import queries

STOPS = pd.DataFrame(
    [
        {"stop_id": "S_AAA", "stop_code": "AAA", "stop_name": "Alpha"},
        {"stop_id": "S_BBB", "stop_code": "BBB", "stop_name": "Bravo"},
        {"stop_id": "S_CCC", "stop_code": "CCC", "stop_name": "Charlie"},
    ]
)
ROUTES = pd.DataFrame(
    [
        {"route_id": f"R{i}", "agency_id": "TT", "route_short_name": "", "route_long_name": f"Route {i}"}
        for i in range(1, 8)
    ]
)
AGENCY = pd.DataFrame([{"agency_id": "TT", "agency_name": "Test Trains"}])


def _write_feed(gtfs_dir: Path, trips, stop_times, calendar):
    STOPS.to_csv(gtfs_dir / "stops.txt", index=False)
    AGENCY.to_csv(gtfs_dir / "agency.txt", index=False)
    ROUTES.to_csv(gtfs_dir / "routes.txt", index=False)
    trips_df = pd.DataFrame(trips)
    # No reversal-continuation trips in these fixtures — trip_short_name is a
    # required column (see app/db.py's _load_trips) but left blank here so
    # _build_trip_continuations finds nothing to synthesize.
    if "trip_short_name" not in trips_df.columns:
        trips_df["trip_short_name"] = ""
    trips_df.to_csv(gtfs_dir / "trips.txt", index=False)
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


def _build_db(tmp_path, trips, stop_times, calendar) -> Path:
    gtfs_dir = tmp_path / "gtfs"
    gtfs_dir.mkdir()
    _write_feed(gtfs_dir, trips, stop_times, calendar)
    db_path = tmp_path / "gtfs.db"
    build_database(gtfs_dir, db_path)
    return db_path


def test_dominant_direct_trips_drops_a_slow_trip_dominated_just_outside_the_display_window(tmp_path):
    """Mirrors the real BNS->WAT repro from issue #19's window-boundary
    follow-up comment: a slow trip survives a fixed-window fetch purely
    because the faster trip that dominates it departs a few minutes after
    the window closes."""
    trips = [
        {"route_id": "R1", "service_id": "SVC", "trip_id": "SLOW", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC", "trip_id": "FAST", "trip_headsign": ""},
    ]
    stop_times = [
        _stop_time("SLOW", "S_AAA", 1, "08:57:00"),
        _stop_time("SLOW", "S_BBB", 2, "10:00:00"),
        _stop_time("FAST", "S_AAA", 1, "09:06:00"),
        _stop_time("FAST", "S_BBB", 2, "09:26:00"),
    ]
    calendar = [_calendar_row("SVC", "monday", "20260803", "20260803")]
    conn = get_connection(_build_db(tmp_path, trips, stop_times, calendar))

    origin = queries.get_station(conn, "AAA")
    destination = queries.get_station(conn, "BBB")
    date = dt.date(2026, 8, 3)

    raw = queries.find_direct_trips(conn, origin, destination, date, dt.time(8, 0), 60)
    assert {t.trip_id for t in raw} == {"SLOW"}, "the plain, unwidened fetch can't even see FAST at this window size"

    filtered = queries.dominant_direct_trips(conn, origin, destination, date, dt.time(8, 0), 60)
    assert {t.trip_id for t in filtered} == set(), (
        "SLOW departs inside the display window but is dominated by FAST, which departs "
        "just after the window closes — the widened fetch must find FAST to drop SLOW, "
        "then trim FAST itself back out since it's outside the display window"
    )


def test_find_journeys_lets_an_out_of_window_direct_dominate_an_in_window_interchange(tmp_path):
    """The widen-then-trim fix must happen once, at find_journeys' top
    level, not inside find_direct_trips itself: trimming a dominating direct
    trip away before the merge would let a worse interchange survive when it
    shouldn't (see issue #19's implementation notes on why leg trimming
    can't happen per-leg)."""
    trips = [
        {"route_id": "R1", "service_id": "SVC", "trip_id": "LEG1_DOMINATED", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC", "trip_id": "LEG2_DOMINATED", "trip_headsign": ""},
        {"route_id": "R3", "service_id": "SVC", "trip_id": "DIRECT_OUTSIDE_WINDOW", "trip_headsign": ""},
        {"route_id": "R4", "service_id": "SVC", "trip_id": "LEG1_SURVIVOR", "trip_headsign": ""},
        {"route_id": "R5", "service_id": "SVC", "trip_id": "LEG2_SURVIVOR", "trip_headsign": ""},
    ]
    stop_times = [
        _stop_time("LEG1_DOMINATED", "S_AAA", 1, "09:05:00"),
        _stop_time("LEG1_DOMINATED", "S_CCC", 2, "09:10:00"),
        _stop_time("LEG2_DOMINATED", "S_CCC", 1, "09:15:00"),
        _stop_time("LEG2_DOMINATED", "S_BBB", 2, "10:00:00"),
        _stop_time("DIRECT_OUTSIDE_WINDOW", "S_AAA", 1, "09:50:00"),
        _stop_time("DIRECT_OUTSIDE_WINDOW", "S_BBB", 2, "09:55:00"),
        _stop_time("LEG1_SURVIVOR", "S_AAA", 1, "09:10:00"),
        _stop_time("LEG1_SURVIVOR", "S_CCC", 2, "09:20:00"),
        _stop_time("LEG2_SURVIVOR", "S_CCC", 1, "09:25:00"),
        _stop_time("LEG2_SURVIVOR", "S_BBB", 2, "09:35:00"),
    ]
    calendar = [_calendar_row("SVC", "monday", "20260803", "20260803")]
    conn = get_connection(_build_db(tmp_path, trips, stop_times, calendar))

    origin = queries.get_station(conn, "AAA")
    destination = queries.get_station(conn, "BBB")

    journeys = queries.find_journeys(conn, origin, destination, dt.date(2026, 8, 3), dt.time(9, 0), 30)

    kinds_and_departures = {(j.kind, j.departure_time) for j in journeys}
    assert kinds_and_departures == {("interchange", "09:10:00")}, (
        "the dominated 09:05 interchange must be dropped — DIRECT_OUTSIDE_WINDOW, which "
        "departs after the 30-minute display window closes, dominates it on change-count "
        "alone — and DIRECT_OUTSIDE_WINDOW itself must not appear since it's outside the "
        "display window; only the genuinely undominated 09:10 interchange should survive"
    )


def test_dominant_direct_trips_widening_crosses_midnight_to_find_a_dominating_next_day_trip(tmp_path):
    """Widening the fetch window for dominance purposes can push it past
    physical midnight even when the plain display window doesn't — this
    must correctly pull in the extra day-bucket (see _day_buckets) rather
    than silently missing the trip that lives there, or duplicating results."""
    trips = [
        {"route_id": "R1", "service_id": "SVC_MON", "trip_id": "SLOW", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC_TUE", "trip_id": "FAST", "trip_headsign": ""},
    ]
    stop_times = [
        # SLOW crosses midnight itself (GTFS >=24:00:00 notation, still tagged Monday).
        _stop_time("SLOW", "S_AAA", 1, "23:05:00"),
        _stop_time("SLOW", "S_BBB", 2, "24:50:00"),
        # FAST is a plainly-tagged Tuesday trip, only visible via the forward
        # day-bucket that widening triggers (see module docstring in queries.py).
        _stop_time("FAST", "S_AAA", 1, "00:09:00"),
        _stop_time("FAST", "S_BBB", 2, "00:19:00"),
    ]
    calendar = [
        _calendar_row("SVC_MON", "monday", "20260803", "20260803"),
        _calendar_row("SVC_TUE", "tuesday", "20260804", "20260804"),
    ]
    conn = get_connection(_build_db(tmp_path, trips, stop_times, calendar))

    origin = queries.get_station(conn, "AAA")
    destination = queries.get_station(conn, "BBB")
    date = dt.date(2026, 8, 3)

    raw = queries.find_direct_trips(conn, origin, destination, date, dt.time(23, 0), 30)
    assert {t.trip_id for t in raw} == {"SLOW"}, "the plain window doesn't cross midnight, so FAST isn't fetched"

    filtered = queries.dominant_direct_trips(conn, origin, destination, date, dt.time(23, 0), 30)
    assert {t.trip_id for t in filtered} == set(), (
        "the widened fetch must cross into Tuesday's bucket to find FAST, which departs "
        "later and arrives earlier than SLOW in real elapsed time, dominating it — SLOW is "
        "then dropped and FAST itself is trimmed back out as outside the display window, "
        "with no duplicate or missed candidate along the way"
    )


def test_dominant_direct_trips_widening_can_reach_two_days_forward(tmp_path):
    """_day_buckets must generalize past a single forward day: a near-24h
    /api/direct window starting late in the evening, plus the dominance
    buffer, can push the widened fetch's end past *two* midnight boundaries
    — missing the second one would silently reintroduce a version of the
    window-boundary bug this issue fixes, just one day further out (found
    in code review)."""
    trips = [
        {"route_id": "R1", "service_id": "SVC_MON", "trip_id": "SLOW", "trip_headsign": ""},
        {"route_id": "R2", "service_id": "SVC_WED", "trip_id": "FAST", "trip_headsign": ""},
    ]
    stop_times = [
        # SLOW departs 23:50 Monday (inside the display window) and takes an
        # implausibly long time — a deliberately bad service, arriving
        # 23:50 *two* days later (Wednesday), so a same-Wednesday dominating
        # trip is actually possible to construct.
        _stop_time("SLOW", "S_AAA", 1, "23:50:00"),
        _stop_time("SLOW", "S_BBB", 2, "71:50:00"),
        # FAST is a plainly-tagged Wednesday trip (two calendar days after
        # the Monday query date) departing just before the widened fetch's
        # end and arriving well before SLOW — only reachable if _day_buckets
        # extends to a second forward day.
        _stop_time("FAST", "S_AAA", 1, "00:05:00"),
        _stop_time("FAST", "S_BBB", 2, "00:15:00"),
    ]
    calendar = [
        _calendar_row("SVC_MON", "monday", "20260803", "20260803"),
        _calendar_row("SVC_WED", "wednesday", "20260805", "20260805"),
    ]
    conn = get_connection(_build_db(tmp_path, trips, stop_times, calendar))

    origin = queries.get_station(conn, "AAA")
    destination = queries.get_station(conn, "BBB")
    date = dt.date(2026, 8, 3)

    # A near-24h window starting late in the evening: window_minutes=1439
    # (matching /api/direct's real 24h cap) plus the dominance buffer pushes
    # window_end_secs past two full days.
    filtered = queries.dominant_direct_trips(conn, origin, destination, date, dt.time(23, 50), 1439)
    assert {t.trip_id for t in filtered} == set(), (
        "FAST (Wednesday, two days after the query date) dominates SLOW and must be found "
        "by the widened fetch to drop it, then be trimmed back out itself as outside the "
        "display window — if _day_buckets only reached one day forward, FAST would be "
        "invisible and SLOW would wrongly survive"
    )
