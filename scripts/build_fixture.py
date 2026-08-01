#!/usr/bin/env python3
"""Build the small, checked-in test fixture from the full TravelWhiz GTFS feed.

Extracts the SW-operated trips touching Barnes (BNS), Waterloo (WAT), Clapham
Junction (CLJ), and London Road Guildford (LRD) around the dates used in the
project's validated worked examples, so pytest's golden-path tests assert
against real, previously-verified scheduled times rather than invented ones.

The fixture is meant to grow over time (see PLAN.md/RESEARCH.md's Testing
Strategy) — re-run this script with an expanded TARGET_DATES / station list
as new use cases need covering, and commit the regenerated fixture files.

Usage:
    python scripts/build_fixture.py /path/to/full/gtfs/unzipped/dir
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

FIXTURE_STATIONS = ("BNS", "WAT", "CLJ", "LRD")
FIXTURE_AGENCY = "SW"  # South Western Railway — matches the worked examples

# Covers: Mon weekday (BNS<->WAT example), Sat with engineering-works
# exception (2026-08-15) and a normal Sat (2026-08-22) for the CLJ<->LRD
# example, plus a Sun/Tue for day-of-week boundary coverage.
TARGET_DATES = {
    "20260815": "saturday",
    "20260816": "sunday",
    "20260817": "monday",
    "20260818": "tuesday",
    "20260822": "saturday",
}

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "gtfs"


def active_service_ids(calendar: pd.DataFrame, calendar_dates: pd.DataFrame, date: str, dow: str) -> set[str]:
    removed = set(
        calendar_dates.loc[
            (calendar_dates["date"] == date) & (calendar_dates["exception_type"] == 2),
            "service_id",
        ]
    )
    added = set(
        calendar_dates.loc[
            (calendar_dates["date"] == date) & (calendar_dates["exception_type"] == 1),
            "service_id",
        ]
    )
    base = set(
        calendar.loc[
            (calendar[dow] == 1)
            & (calendar["start_date"] <= date)
            & (calendar["end_date"] >= date),
            "service_id",
        ]
    )
    return (base - removed) | added


def main(gtfs_dir: str) -> None:
    src = Path(gtfs_dir)

    stops = pd.read_csv(src / "stops.txt", dtype=str)
    routes = pd.read_csv(src / "routes.txt", dtype=str)
    trips = pd.read_csv(src / "trips.txt", dtype=str)
    calendar = pd.read_csv(src / "calendar.txt", dtype=str)
    calendar_dates = pd.read_csv(src / "calendar_dates.txt", dtype=str)
    agency = pd.read_csv(src / "agency.txt", dtype=str)

    day_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    calendar[day_cols] = calendar[day_cols].astype(int)
    calendar_dates["exception_type"] = calendar_dates["exception_type"].astype(int)

    all_active: set[str] = set()
    for date, dow in TARGET_DATES.items():
        all_active |= active_service_ids(calendar, calendar_dates, date, dow)

    fixture_stop_ids = set(
        stops.loc[stops["stop_code"].str.upper().isin(FIXTURE_STATIONS), "stop_id"]
    )

    sw_route_ids = set(routes.loc[routes["agency_id"] == FIXTURE_AGENCY, "route_id"])
    candidate_trips = trips[
        trips["service_id"].isin(all_active) & trips["route_id"].isin(sw_route_ids)
    ]

    # Only load stop_times for candidate trips — full file is 247MB, too big to
    # read wholesale here. Chunk through it filtering by trip_id.
    candidate_trip_ids = set(candidate_trips["trip_id"])
    keep_trip_ids: set[str] = set()
    stop_times_chunks = []
    for chunk in pd.read_csv(src / "stop_times.txt", dtype=str, chunksize=500_000):
        matched = chunk[chunk["trip_id"].isin(candidate_trip_ids)]
        touches_fixture_station = matched["trip_id"][
            matched["stop_id"].isin(fixture_stop_ids)
        ].unique()
        keep_trip_ids.update(touches_fixture_station)
        stop_times_chunks.append(matched)
    stop_times = pd.concat(stop_times_chunks, ignore_index=True)

    # Keep full stop_times (all intermediate stops) only for trips that
    # actually call at one of the fixture stations.
    stop_times = stop_times[stop_times["trip_id"].isin(keep_trip_ids)]
    fixture_trips = candidate_trips[candidate_trips["trip_id"].isin(keep_trip_ids)]
    fixture_service_ids = set(fixture_trips["service_id"])
    fixture_route_ids = set(fixture_trips["route_id"])
    all_touched_stop_ids = set(stop_times["stop_id"])

    fixture_stops = stops[stops["stop_id"].isin(all_touched_stop_ids)]
    fixture_routes = routes[routes["route_id"].isin(fixture_route_ids)]
    fixture_calendar = calendar[calendar["service_id"].isin(fixture_service_ids)]
    fixture_calendar_dates = calendar_dates[
        calendar_dates["service_id"].isin(fixture_service_ids)
    ]
    fixture_agency = agency[agency["agency_id"] == FIXTURE_AGENCY]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fixture_stops.to_csv(OUT_DIR / "stops.txt", index=False)
    fixture_routes.to_csv(OUT_DIR / "routes.txt", index=False)
    fixture_trips.to_csv(OUT_DIR / "trips.txt", index=False)
    stop_times.to_csv(OUT_DIR / "stop_times.txt", index=False)
    fixture_calendar.to_csv(OUT_DIR / "calendar.txt", index=False)
    fixture_calendar_dates.to_csv(OUT_DIR / "calendar_dates.txt", index=False)
    fixture_agency.to_csv(OUT_DIR / "agency.txt", index=False)

    print(f"Fixture written to {OUT_DIR}")
    print(f"  stops: {len(fixture_stops)}")
    print(f"  routes: {len(fixture_routes)}")
    print(f"  trips: {len(fixture_trips)}")
    print(f"  stop_times: {len(stop_times)}")
    print(f"  calendar: {len(fixture_calendar)}")
    print(f"  calendar_dates: {len(fixture_calendar_dates)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
