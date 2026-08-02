from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from starlette.testclient import TestClient

from app import config, queries
from app import main as main_module

DIRECT_PARAMS = {"from": "BNS", "to": "WAT", "date": "2026-08-17", "time": "09:00"}


def test_unhandled_exception_returns_json_500_and_logs_traceback(client, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(queries, "dominant_direct_trips", boom)

    # A fresh TestClient with raise_server_exceptions=False, not the shared
    # `client` fixture: TestClient stores that flag on its transport at
    # construction time, so mutating client.raise_server_exceptions after
    # the fact has no effect — the app's lifespan is already running (the
    # `client` fixture entered it), so this reuses the same app instance
    # without triggering it a second time.
    raw_client = TestClient(main_module.app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="train_journey_planner.main"):
        r = raw_client.get("/api/direct", params=DIRECT_PARAMS)

    assert r.status_code == 500
    assert r.json() == {"detail": "Internal Server Error"}
    assert any("Unhandled exception" in rec.message for rec in caplog.records)
    assert any(rec.exc_info is not None for rec in caplog.records)


def test_api_route_still_returns_normal_error_for_http_exceptions(client):
    # HTTPException (e.g. an unknown station -> 400) must still reach its
    # own handling, not get swallowed into a generic 500 by the catch-all.
    r = client.get("/api/direct", params={**DIRECT_PARAMS, "from": "ZZZ"})
    assert r.status_code == 400


def test_concurrency_middleware_returns_503_with_retry_after_when_saturated(client, monkeypatch):
    # Hold the one available slot with a slow request, then confirm a second
    # concurrent request is rejected with 503 + Retry-After rather than
    # queueing indefinitely (GitHub issue #20's bounded-concurrency piece).
    monkeypatch.setattr(main_module, "_db_request_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(config, "DB_REQUEST_ACQUIRE_TIMEOUT_SECONDS", 0.2)

    real_dominant_direct_trips = queries.dominant_direct_trips

    def slow_dominant_direct_trips(*args, **kwargs):
        time.sleep(0.6)
        return real_dominant_direct_trips(*args, **kwargs)

    monkeypatch.setattr(queries, "dominant_direct_trips", slow_dominant_direct_trips)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.get, "/api/direct", params=DIRECT_PARAMS)
        time.sleep(0.1)  # let `first` acquire the sole slot before firing the second
        second = pool.submit(client.get, "/api/direct", params=DIRECT_PARAMS)

        first_resp = first.result()
        second_resp = second.result()

    assert first_resp.status_code == 200
    assert second_resp.status_code == 503
    assert second_resp.headers["retry-after"] == "0.2"


def test_health_stays_exempt_from_concurrency_limit(client, monkeypatch):
    # /health must never queue behind API traffic — docker-compose's
    # healthcheck polls it, and it doesn't touch the DB dependency at all.
    monkeypatch.setattr(main_module, "_db_request_semaphore", asyncio.Semaphore(0))

    r = client.get("/health")
    assert r.status_code == 200
