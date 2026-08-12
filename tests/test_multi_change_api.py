"""Tests for the /api/journeys/multi-change endpoint and the /results
page's two-stage integration point (GitHub issue #26). The OTP sidecar
itself is always mocked via app.otp_client — no live sidecar involved."""

from __future__ import annotations

import pytest

from app import otp_client
from app.otp_client import MultiChangeJourney, MultiChangeLeg, SidecarUnavailableError
from app.queries import Stop


@pytest.fixture(autouse=True)
def reset_sidecar_health():
    """otp_client's health flag is module-level global state, shared across
    the whole test process — reset it after each test so one test's
    monkeypatched "healthy" state can't leak into the next."""
    yield
    otp_client._sidecar_healthy = False


def _fake_journey() -> MultiChangeJourney:
    leg1 = MultiChangeLeg(
        origin=Stop(stop_id="", stop_code="BNS", stop_name="Barnes"),
        destination=Stop(stop_id="", stop_code="CLJ", stop_name="Clapham Junction"),
        agency_name="Southern",
        agency_id="SN",
        route_description="Barnes - Horsham",
        headsign="Horsham",
        departure_time="09:06:00",
        arrival_time="09:15:00",
        departure_next_day=False,
        arrival_next_day=False,
        duration_minutes=9,
    )
    leg2 = MultiChangeLeg(
        origin=Stop(stop_id="", stop_code="CLJ", stop_name="Clapham Junction"),
        destination=Stop(stop_id="", stop_code="PUL", stop_name="Pulborough"),
        agency_name="Southern",
        agency_id="SN",
        route_description="Horsham - Pulborough",
        headsign="Pulborough",
        departure_time="09:30:00",
        arrival_time="10:00:00",
        departure_next_day=False,
        arrival_next_day=False,
        duration_minutes=30,
    )
    return MultiChangeJourney(
        legs=[leg1, leg2],
        departure_time="09:06:00",
        departure_next_day=False,
        arrival_time="10:00:00",
        arrival_next_day=False,
        duration_minutes=54,
    )


def test_multi_change_endpoint_returns_degraded_when_sidecar_unhealthy(client, monkeypatch):
    monkeypatch.setattr(otp_client, "_sidecar_healthy", False)
    r = client.get(
        "/api/journeys/multi-change",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sidecar_healthy"] is False
    assert body["journeys"] == []


def test_multi_change_endpoint_returns_journeys_when_sidecar_healthy(client, monkeypatch):
    monkeypatch.setattr(otp_client, "_sidecar_healthy", True)
    monkeypatch.setattr(otp_client, "plan_multi_change_journeys", lambda *a, **k: [_fake_journey()])

    r = client.get(
        "/api/journeys/multi-change",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sidecar_healthy"] is True
    assert len(body["journeys"]) == 1
    journey = body["journeys"][0]
    assert journey["num_changes"] == 1
    assert len(journey["legs"]) == 2
    assert journey["legs"][0]["origin"]["crs_code"] == "BNS"
    assert journey["legs"][1]["destination"]["crs_code"] == "PUL"


def test_multi_change_endpoint_degrades_on_sidecar_call_failure(client, monkeypatch):
    monkeypatch.setattr(otp_client, "_sidecar_healthy", True)

    def broken_plan(*a, **k):
        raise SidecarUnavailableError("boom")

    monkeypatch.setattr(otp_client, "plan_multi_change_journeys", broken_plan)

    r = client.get(
        "/api/journeys/multi-change",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sidecar_healthy"] is False
    assert body["journeys"] == []


def test_multi_change_endpoint_validates_stations(client, monkeypatch):
    monkeypatch.setattr(otp_client, "_sidecar_healthy", True)
    r = client.get(
        "/api/journeys/multi-change",
        params={"from": "ZZZ", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 400


def test_api_journeys_reports_sidecar_healthy_field(client, monkeypatch):
    monkeypatch.setattr(otp_client, "_sidecar_healthy", True)
    r = client.get(
        "/api/journeys",
        params={"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert r.json()["sidecar_healthy"] is True


def test_results_page_embeds_sidecar_health_for_js_second_stage(client, monkeypatch):
    """When the first pass finds nothing, the page should embed enough data
    for multi_change.js to decide whether to fetch the second stage or show
    the degraded banner immediately — verified at the data-attribute level
    since TestClient doesn't execute JS."""
    monkeypatch.setattr(otp_client, "_sidecar_healthy", False)
    r = client.get(
        "/results",
        params={"from_": "BNS", "to": "WAT", "date": "2026-08-17", "time": "03:00", "window_minutes": "5"},
    )
    assert r.status_code == 200
    assert 'id="multi-change-root"' in r.text
    assert 'data-sidecar-healthy="false"' in r.text
    assert "multi_change.js" in r.text
