from __future__ import annotations

import datetime as dt
import re

import pytest

from app import validation
from app.main import _next_quarter_hour, format_duration


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (0, "0m"),
        (45, "45m"),
        (59, "59m"),
        (60, "1h"),
        (66, "1h6m"),
        (120, "2h"),
        (125, "2h5m"),
    ],
)
def test_format_duration(minutes, expected):
    assert format_duration(minutes) == expected


@pytest.mark.parametrize(
    "now,expected",
    [
        (dt.datetime(2026, 8, 17, 9, 0, 0), dt.datetime(2026, 8, 17, 9, 0, 0)),
        (dt.datetime(2026, 8, 17, 9, 1, 0), dt.datetime(2026, 8, 17, 9, 15, 0)),
        (dt.datetime(2026, 8, 17, 9, 14, 59), dt.datetime(2026, 8, 17, 9, 15, 0)),
        (dt.datetime(2026, 8, 17, 9, 15, 0), dt.datetime(2026, 8, 17, 9, 15, 0)),
        (dt.datetime(2026, 8, 17, 9, 16, 0), dt.datetime(2026, 8, 17, 9, 30, 0)),
        # Rolls over into the next calendar day.
        (dt.datetime(2026, 8, 17, 23, 50, 0), dt.datetime(2026, 8, 18, 0, 0, 0)),
    ],
)
def test_next_quarter_hour(now, expected):
    assert _next_quarter_hour(now) == expected


def test_form_page_defaults_date_and_time_to_next_quarter_hour(client):
    r = client.get("/")
    assert r.status_code == 200
    date_match = re.search(r'id="date"[^>]*value="(\d{4}-\d{2}-\d{2})"', r.text)
    time_match = re.search(r'id="time"[^>]*value="(\d{2}:\d{2})"', r.text)
    assert date_match is not None
    assert time_match is not None
    minute = int(time_match.group(1).split(":")[1])
    assert minute in (0, 15, 30, 45)


def test_api_direct_golden_path(client):
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["origin"]["crs_code"] == "BNS"
    assert body["destination"]["crs_code"] == "WAT"
    departures = {(t["departure_time"], t["arrival_time"]) for t in body["trips"]}
    assert ("09:06:00", "09:26:00") in departures
    assert ("09:35:00", "09:57:30") in departures


def test_api_direct_includes_operator(client):
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    fast_trip = next(t for t in body["trips"] if t["departure_time"] == "09:06:00")
    assert fast_trip["operator"] == "South Western Railway"


def test_api_direct_unknown_station_returns_400(client):
    r = client.get(
        "/api/direct",
        params={"from": "ZZZ", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 400
    assert "unknown station code" in r.json()["detail"]


def test_api_direct_same_station_returns_400(client):
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "BNS", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 400
    assert "same station" in r.json()["detail"]


def test_api_direct_date_out_of_range_returns_400(client):
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "2020-01-01", "time": "09:00"},
    )
    assert r.status_code == 400
    assert "outside the loaded feed's coverage" in r.json()["detail"]


def test_is_in_past_boundary_cases():
    now = dt.datetime(2026, 8, 17, 9, 0, 0, tzinfo=validation.LONDON_TZ)
    # A request for exactly "now" is not (yet) past.
    assert validation.is_in_past(dt.date(2026, 8, 17), dt.time(9, 0, 0), now=now) is False
    # One second in the future is not past; one second in the past is.
    assert validation.is_in_past(dt.date(2026, 8, 17), dt.time(9, 0, 1), now=now) is False
    assert validation.is_in_past(dt.date(2026, 8, 17), dt.time(8, 59, 59), now=now) is True


def test_api_direct_future_query_is_not_past(client):
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert r.json()["is_past"] is False


def test_api_direct_past_date_within_feed_range_returns_results_flagged_past(client):
    # 2026-06-01 is before "now" (the fixture's clock in these tests is real
    # wall-clock time, well past this date) but still inside the checked-in
    # fixture's calendar coverage (2026-05-17 onward) — past searches must be
    # served, not rejected, with is_past=True to flag them.
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "2026-06-01", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_past"] is True
    assert body["trips"], "a past date within the feed's range should still return real trips"


def test_api_journeys_past_date_within_feed_range_returns_results_flagged_past(client):
    r = client.get(
        "/api/journeys",
        params={"from": "BNS", "to": "WAT", "date": "2026-06-01", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_past"] is True
    assert body["journeys"]


def test_api_direct_date_before_feed_start_still_returns_400(client):
    # Before the feed's own coverage entirely (not just "in the past") must
    # still be rejected — only DateOutOfRangeError, not PastTimeError.
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "2020-01-01", "time": "09:00"},
    )
    assert r.status_code == 400
    assert "outside the loaded feed's coverage" in r.json()["detail"]


def test_results_page_shows_past_badge_for_past_date(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-06-01", "time": "09:00"},
    )
    assert r.status_code == 200
    assert 'class="badge badge-past"' in r.text
    assert "Past" in r.text


def test_results_page_omits_past_badge_for_future_date(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert 'class="badge badge-past"' not in r.text


def test_api_direct_malformed_date_returns_422(client):
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "not-a-date", "time": "09:00"},
    )
    assert r.status_code == 422


def test_api_journeys_includes_direct_and_interchange(client):
    r = client.get(
        "/api/journeys",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    kinds = {j["kind"] for j in body["journeys"]}
    assert "direct" in kinds
    direct_departures = {j["departure_time"] for j in body["journeys"] if j["kind"] == "direct"}
    assert "09:06:00" in direct_departures


def test_api_journeys_golden_interchange(client):
    r = client.get(
        "/api/journeys",
        params={"from": "BNS", "to": "LRD", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    # 09:06:00's CLJ change used to be the documented worked example, but a
    # dominance-filtered response (2026-08-01 UX review) correctly drops it:
    # a 09:11:30 departure reaches the same interchange, on the same
    # connecting train, arriving at the same time — so 09:06:00 offers no
    # reason to leave earlier. 09:26:30 is the earliest surviving CLJ change
    # (nothing else departs later and still catches the 10:32:30 arrival).
    match = next(
        j
        for j in body["journeys"]
        if j["kind"] == "interchange"
        and j["interchange"]["leg1"]["departure_time"] == "09:26:30"
        and j["interchange"]["interchange"]["crs_code"] == "CLJ"
    )
    assert match["interchange"]["leg2"]["arrival_time"] == "10:32:30"
    assert not any(
        j["kind"] == "interchange" and j["interchange"]["leg1"]["departure_time"] == "09:06:00"
        for j in body["journeys"]
    ), "09:06:00's change is dominated by 09:11:30's identical-arrival connection and should be filtered out"


def test_api_journeys_direct_only_excludes_interchange_and_defaults_false(client):
    r = client.get(
        "/api/journeys",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.json()["direct_only"] is False

    r = client.get(
        "/api/journeys",
        params={"from": "BNS", "to": "LRD", "date": "2026-08-17", "time": "09:00", "direct_only": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["direct_only"] is True
    assert body["journeys"] == [], "BNS -> LRD has no direct route, so direct_only=true should return none"


def test_api_journeys_same_station_returns_400(client):
    r = client.get(
        "/api/journeys",
        params={"from": "BNS", "to": "BNS", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 400


def test_api_stations_lists_names_and_crs_codes(client):
    r = client.get("/api/stations")
    assert r.status_code == 200
    stations = r.json()
    by_crs = {s["crs_code"]: s["name"] for s in stations}
    assert by_crs["BNS"] == "Barnes"
    assert by_crs["WAT"] == "London Waterloo"
    assert len(stations) == len(by_crs)  # one row per CRS code, no duplicates


def test_health_reports_dataset_present(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "dataset_present": True}


def test_form_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<form" in r.text


def test_results_page_renders_golden_path(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "09:06" in r.text
    assert "09:35" in r.text


def test_results_page_heading_shows_full_station_names(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "Barnes (BNS) to London Waterloo (WAT)" in r.text


def test_results_page_heading_falls_back_to_codes_on_error(client):
    r = client.get(
        "/results",
        params={"from_": "ZZZ", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "ZZZ to WAT" in r.text


def test_results_page_shows_operator_name_above_route_description(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert '<div class="trip-operator">South Western Railway</div>' in r.text


def test_results_page_summary_shows_window_as_time_range(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "09:00 to 10:00" in r.text
    assert "+60min" not in r.text


def test_results_page_summary_marks_next_day_when_window_crosses_midnight(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-08-17", "time": "23:30"},
    )
    assert r.status_code == 200
    assert "23:30 to 00:30" in r.text
    assert "<sup>+1</sup>" in r.text


def test_results_page_direct_only_shows_no_results_message_for_interchange_only_route(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "LRD", "date": "2026-08-17", "time": "09:00", "direct_only": "true"},
    )
    assert r.status_code == 200
    assert "(direct trains only)" in r.text
    assert "No direct trains found in this window." in r.text


def test_results_page_renders_interchange_journey(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "LRD", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "1 change" in r.text
    assert "Clapham Junction" in r.text


def test_results_page_renders_durations_in_hours_and_minutes(client):
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "LRD", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "1h6m total" in r.text
    assert "<span>8m</span>" in r.text


def test_results_page_renders_validation_error(client):
    r = client.get(
        "/results",
        params={"from_": "ZZZ", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "unknown station code" in r.text


def test_results_page_renders_friendly_error_for_unresolved_free_text(client):
    # An autocomplete entry the client failed to resolve to a CRS code (or a
    # request with JS disabled) sends free text through query params that
    # are declared 3-char-only — this must render the same styled error
    # card, not FastAPI's raw JSON validation error.
    r = client.get(
        "/results",
        params={"from_": "London Waterloo", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert "find that station" in r.text


def test_results_page_friendly_error_is_tailored_to_bad_date(client):
    # A validation failure on date/time (not from_/to) should get its own
    # message, not the station-lookup wording.
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "not-a-date", "time": "09:00"},
    )
    assert r.status_code == 422
    assert "date or time" in r.text
    assert "station" not in r.text.lower()


def test_api_direct_still_returns_json_422_for_malformed_query(client):
    # The friendly-error handler is scoped to /results only — /api/* must
    # keep FastAPI's default JSON 422 body.
    r = client.get(
        "/api/direct",
        params={"from": "TOOLONG", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/json"
