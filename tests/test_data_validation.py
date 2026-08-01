"""Phase 0 retrofit: turns the one-off manual feed validation into a regression
guard against the fixture's (and by extension, TravelWhiz's) structure
silently changing shape.
"""

from __future__ import annotations

import datetime as dt


def test_feed_loads_with_expected_tables(conn):
    for table in ("stops", "routes", "trips", "stop_times", "calendar", "calendar_dates", "agency"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count > 0, f"{table} table is empty"


def test_agency_name_resolves_for_south_western_railway(conn):
    row = conn.execute("SELECT agency_name FROM agency WHERE agency_id = 'SW'").fetchone()
    assert row["agency_name"] == "South Western Railway"


def test_known_crs_codes_resolve(conn):
    codes = {
        row["stop_code"]
        for row in conn.execute(
            "SELECT stop_code FROM stops WHERE stop_code IN ('BNS','WAT','CLJ','LRD')"
        )
    }
    assert codes == {"BNS", "WAT", "CLJ", "LRD"}


def test_calendar_date_range_covers_worked_examples(conn):
    from app import queries

    min_date, max_date = queries.feed_date_range(conn)
    for worked_example_date in (
        dt.date(2026, 8, 15),
        dt.date(2026, 8, 17),
        dt.date(2026, 8, 22),
    ):
        assert min_date <= worked_example_date <= max_date
