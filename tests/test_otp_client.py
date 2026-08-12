"""Tests for app/otp_client.py (GitHub issue #26) — the OTP sidecar HTTP
client and health-check flag. All network access is mocked (httpx.post/
httpx.get monkeypatched); no live sidecar is used or required."""

from __future__ import annotations

import datetime as dt
import threading
import time

import httpx
import pytest

from app import config, otp_client
from app.queries import Stop

BNS = Stop(stop_id="9100BNS", stop_code="BNS", stop_name="Barnes")
PUL = Stop(stop_id="9100PUL", stop_code="PUL", stop_name="Pulborough")


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_data


def _leg(
    dep: str,
    arr: str,
    from_code: str,
    to_code: str,
    from_name: str,
    to_name: str,
    agency_name="Southern",
    agency_gtfs_id="1:SN",
    route_name="Barnes - Pulborough",
    headsign="Pulborough",
    trip_short_name="2306",
    mode="RAIL",
) -> dict:
    return {
        "mode": mode,
        "start": {"scheduledTime": dep},
        "end": {"scheduledTime": arr},
        "from": {"stop": {"code": from_code, "name": from_name}},
        "to": {"stop": {"code": to_code, "name": to_name}},
        "agency": {"name": agency_name, "gtfsId": agency_gtfs_id},
        "route": {"shortName": None, "longName": route_name},
        "trip": {"tripHeadsign": headsign, "tripShortName": trip_short_name},
    }


@pytest.fixture(autouse=True)
def sidecar_url(monkeypatch):
    monkeypatch.setattr(config, "OTP_SIDECAR_URL", "http://otp.example:8080")
    yield


def _three_leg_journey_edge() -> dict:
    """2-change (3-leg) itinerary — the minimum this tier is meant to
    surface (see the num_changes < 2 floor in plan_multi_change_journeys)."""
    return {
        "node": {
            "legs": [
                _leg(
                    "2026-08-12T10:00:00+01:00", "2026-08-12T10:09:00+01:00",
                    "BNS", "CLJ", "Barnes", "Clapham Junction",
                ),
                _leg(
                    "2026-08-12T10:15:00+01:00", "2026-08-12T10:35:00+01:00",
                    "CLJ", "HRH", "Clapham Junction", "Horsham",
                ),
                _leg(
                    "2026-08-12T10:40:00+01:00", "2026-08-12T10:55:00+01:00",
                    "HRH", "PUL", "Horsham", "Pulborough",
                ),
            ]
        }
    }


def test_plan_multi_change_journeys_maps_three_leg_response(monkeypatch):
    payload = {"data": {"planConnection": {"edges": [_three_leg_journey_edge()]}}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))

    journeys = otp_client.plan_multi_change_journeys(
        BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 90
    )

    assert len(journeys) == 1
    journey = journeys[0]
    assert journey.num_changes == 2
    assert journey.departure_time == "10:00:00"
    assert journey.arrival_time == "10:55:00"
    assert journey.duration_minutes == 55
    assert len(journey.legs) == 3
    assert journey.legs[0].origin.stop_code == "BNS"
    assert journey.legs[0].destination.stop_code == "CLJ"
    assert journey.legs[2].destination.stop_code == "PUL"
    assert journey.legs[0].agency_name == "Southern"
    assert journey.legs[0].agency_id == "SN"  # "1:" feed prefix stripped
    assert journey.legs[0].route_description == "Barnes - Pulborough"
    assert journey.legs[0].headsign == "Pulborough"
    assert journey.legs[0].trip_short_name == "2306"


def test_plan_multi_change_journeys_excludes_fewer_than_two_changes(monkeypatch):
    payload = {
        "data": {
            "planConnection": {
                "edges": [
                    {
                        "node": {
                            "legs": [
                                _leg(
                                    "2026-08-12T10:00:00+01:00", "2026-08-12T10:09:00+01:00",
                                    "BNS", "CLJ", "Barnes", "Clapham Junction",
                                ),
                                _leg(
                                    "2026-08-12T10:15:00+01:00", "2026-08-12T10:45:00+01:00",
                                    "CLJ", "PUL", "Clapham Junction", "Pulborough",
                                ),
                            ]
                        }
                    },  # 1-change itinerary — should be filtered out
                    _three_leg_journey_edge(),  # 2-change — should survive
                ]
            }
        }
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))

    journeys = otp_client.plan_multi_change_journeys(
        BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 90
    )

    assert len(journeys) == 1
    assert journeys[0].num_changes == 2


def test_plan_multi_change_journeys_excludes_departures_past_window(monkeypatch):
    # Reuse the 2-change fixture edge, shifted so its first leg departs
    # 11:30 — outside a 09:00-10:00 window — so this genuinely exercises the
    # window filter rather than incidentally passing via the num_changes floor.
    late_edge = _three_leg_journey_edge()
    for leg in late_edge["node"]["legs"]:
        leg["start"]["scheduledTime"] = leg["start"]["scheduledTime"].replace("T10:", "T13:").replace("T09:", "T12:")
        leg["end"]["scheduledTime"] = leg["end"]["scheduledTime"].replace("T10:", "T13:").replace("T09:", "T12:")
    payload = {"data": {"planConnection": {"edges": [late_edge]}}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))

    # Window is 09:00-10:00; the only itinerary departs 13:00, well outside it.
    journeys = otp_client.plan_multi_change_journeys(
        BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 60
    )
    assert journeys == []


def test_plan_multi_change_journeys_marks_next_day_departures(monkeypatch):
    next_day_edge = _three_leg_journey_edge()
    for leg in next_day_edge["node"]["legs"]:
        leg["start"]["scheduledTime"] = leg["start"]["scheduledTime"].replace("2026-08-12T10:", "2026-08-13T00:")
        leg["end"]["scheduledTime"] = leg["end"]["scheduledTime"].replace("2026-08-12T10:", "2026-08-13T00:")
    payload = {"data": {"planConnection": {"edges": [next_day_edge]}}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))

    journeys = otp_client.plan_multi_change_journeys(
        BNS, PUL, dt.date(2026, 8, 12), dt.time(23, 30), 60
    )
    assert len(journeys) == 1
    assert journeys[0].departure_next_day is True
    assert journeys[0].arrival_next_day is True


def test_plan_multi_change_journeys_raises_on_network_error(monkeypatch):
    def broken_post(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", broken_post)

    with pytest.raises(otp_client.SidecarUnavailableError):
        otp_client.plan_multi_change_journeys(BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 60)


def test_plan_multi_change_journeys_raises_on_graphql_errors(monkeypatch):
    payload = {"errors": [{"message": "boom"}]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))

    with pytest.raises(otp_client.SidecarUnavailableError):
        otp_client.plan_multi_change_journeys(BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 60)


def test_plan_multi_change_journeys_raises_when_url_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "OTP_SIDECAR_URL", "")

    with pytest.raises(otp_client.SidecarUnavailableError):
        otp_client.plan_multi_change_journeys(BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 60)


def test_check_sidecar_health_true_on_200(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(status_code=200))
    assert otp_client.check_sidecar_health() is True
    assert otp_client.is_sidecar_healthy() is True


def test_check_sidecar_health_false_on_error_status(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(status_code=503))
    assert otp_client.check_sidecar_health() is False
    assert otp_client.is_sidecar_healthy() is False


def test_check_sidecar_health_false_on_network_error(monkeypatch):
    def broken_get(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", broken_get)
    assert otp_client.check_sidecar_health() is False


def test_check_sidecar_health_false_when_url_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "OTP_SIDECAR_URL", "")
    assert otp_client.check_sidecar_health() is False


def test_check_sidecar_health_false_on_non_http_error(monkeypatch):
    """httpx.InvalidURL (e.g. from a scheme-less OTP_SIDECAR_URL) isn't an
    httpx.HTTPError subclass — must still degrade, not raise (Opus review,
    2026-08-12)."""

    def broken_get(*a, **k):
        raise httpx.InvalidURL("no scheme")

    monkeypatch.setattr(httpx, "get", broken_get)
    assert otp_client.check_sidecar_health() is False


def test_start_health_check_scheduler_fires_immediately(monkeypatch):
    """APScheduler's IntervalTrigger defaults next_run_time to now + interval,
    not now — start_health_check_scheduler must override that explicitly so
    the flag isn't stuck False for a full interval after every process start
    (Opus review, 2026-08-12)."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(status_code=200))
    monkeypatch.setattr(otp_client, "_sidecar_healthy", False)

    scheduler = otp_client.start_health_check_scheduler()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not otp_client.is_sidecar_healthy():
            time.sleep(0.05)
        assert otp_client.is_sidecar_healthy() is True
    finally:
        scheduler.shutdown(wait=False)


def test_plan_multi_change_journeys_normalizes_non_london_offset(monkeypatch):
    """OTP could serialize scheduledTime as UTC ("+00:00") rather than
    Europe/London's own offset — display times/next-day flags must still
    come out in London wall-clock (Opus review, 2026-08-12). First leg
    departs 10:00 Europe/London (BST, +01:00) == 09:00 UTC."""
    utc_edge = {
        "node": {
            "legs": [
                _leg(
                    "2026-08-12T09:00:00+00:00", "2026-08-12T09:09:00+00:00",
                    "BNS", "CLJ", "Barnes", "Clapham Junction",
                ),
                _leg(
                    "2026-08-12T09:15:00+00:00", "2026-08-12T09:35:00+00:00",
                    "CLJ", "HRH", "Clapham Junction", "Horsham",
                ),
                _leg(
                    "2026-08-12T09:40:00+00:00", "2026-08-12T09:55:00+00:00",
                    "HRH", "PUL", "Horsham", "Pulborough",
                ),
            ]
        }
    }
    payload = {"data": {"planConnection": {"edges": [utc_edge]}}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))

    journeys = otp_client.plan_multi_change_journeys(
        BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 90
    )

    assert len(journeys) == 1
    # 09:00 UTC on 2026-08-12 (BST) is 10:00 London wall-clock, not 09:00.
    assert journeys[0].departure_time == "10:00:00"
    assert journeys[0].arrival_time == "10:55:00"


def test_plan_multi_change_journeys_excludes_walk_legs_from_count_and_display(monkeypatch):
    """A WALK leg (in-station transfer) must not count toward num_changes or
    appear in the rendered leg list (Opus review, 2026-08-12) — otherwise a
    genuine 1-change journey with one walking transfer would read as
    2-change and sail through the floor filter, and the walk leg would
    render blank (no agency/route/trip)."""
    edge_with_walk = {
        "node": {
            "legs": [
                _leg(
                    "2026-08-12T10:00:00+01:00", "2026-08-12T10:09:00+01:00",
                    "BNS", "CLJ", "Barnes", "Clapham Junction",
                ),
                _leg(
                    "2026-08-12T10:10:00+01:00", "2026-08-12T10:12:00+01:00",
                    "CLJ", "CLJ", "Clapham Junction", "Clapham Junction",
                    mode="WALK",
                ),
                _leg(
                    "2026-08-12T10:15:00+01:00", "2026-08-12T10:45:00+01:00",
                    "CLJ", "PUL", "Clapham Junction", "Pulborough",
                ),
            ]
        }
    }
    payload = {"data": {"planConnection": {"edges": [edge_with_walk]}}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(payload))

    # This is a 1-change itinerary once the walk leg is excluded — must be
    # filtered out by the num_changes < 2 floor, not counted as 2-change.
    journeys = otp_client.plan_multi_change_journeys(
        BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 90
    )
    assert journeys == []


def test_plan_multi_change_journeys_respects_concurrency_cap(monkeypatch):
    monkeypatch.setattr(config, "OTP_MAX_CONCURRENT_SIDECAR_REQUESTS", 1)
    monkeypatch.setattr(config, "OTP_SIDECAR_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(otp_client, "_sidecar_request_semaphore", threading.Semaphore(1))

    otp_client._sidecar_request_semaphore.acquire()  # simulate an in-flight request
    try:
        with pytest.raises(otp_client.SidecarUnavailableError):
            otp_client.plan_multi_change_journeys(BNS, PUL, dt.date(2026, 8, 12), dt.time(9, 0), 60)
    finally:
        otp_client._sidecar_request_semaphore.release()
