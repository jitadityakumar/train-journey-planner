from __future__ import annotations


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


def test_api_direct_malformed_date_returns_422(client):
    r = client.get(
        "/api/direct",
        params={"from": "BNS", "to": "WAT", "date": "not-a-date", "time": "09:00"},
    )
    assert r.status_code == 422


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


def test_results_page_renders_validation_error(client):
    r = client.get(
        "/results",
        params={"from_": "ZZZ", "to": "WAT", "date": "2026-08-17", "time": "09:00"},
    )
    assert r.status_code == 200
    assert "unknown station code" in r.text
