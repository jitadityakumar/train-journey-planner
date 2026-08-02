#!/usr/bin/env python3
"""Concurrent-request repro script for GitHub issue #20.

Fires N simultaneous requests at /api/direct against a running instance of
the app and reports the status code (and, for non-2xx, the response body) of
each. Used to capture a real traceback via app/main.py's catch-all exception
handler/logging (Step 0 of issue #20's plan) rather than debugging blind —
run this against a container built from this branch and check `docker logs`
for the traceback.

Usage:
    python scripts/concurrency_repro.py [base_url] [concurrency] [rounds]

Defaults to http://localhost:8010, concurrency 5, 10 rounds (so 50 requests
total, 5 in flight at a time).
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

DEFAULT_BASE_URL = "http://localhost:8010"
DEFAULT_CONCURRENCY = 5
DEFAULT_ROUNDS = 10

# BNS -> WAT is one of this project's validated worked examples (see
# scripts/build_fixture.py) — a real, frequently-served route in the checked-in
# test fixture, but any date/time works fine here since this script only
# cares about the HTTP status code, not the journey content.
QUERY_PARAMS = {
    "from": "BNS",
    "to": "WAT",
    "date": "2026-08-10",
    "time": "08:00",
    "window_minutes": "60",
}


def fire_one(base_url: str, i: int) -> tuple[int, int, str]:
    try:
        resp = httpx.get(f"{base_url}/api/direct", params=QUERY_PARAMS, timeout=30)
        body = "" if resp.is_success else resp.text[:500]
        return i, resp.status_code, body
    except httpx.HTTPError as exc:
        return i, -1, str(exc)


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_CONCURRENCY
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_ROUNDS

    total = concurrency * rounds
    status_counts: dict[int, int] = {}
    failures: list[tuple[int, int, str]] = []

    print(f"Firing {total} requests at {base_url}/api/direct ({concurrency} concurrent x {rounds} rounds)...")

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for round_num in range(rounds):
            futures = [pool.submit(fire_one, base_url, round_num * concurrency + j) for j in range(concurrency)]
            for future in as_completed(futures):
                i, status, body = future.result()
                status_counts[status] = status_counts.get(status, 0) + 1
                if status != 200:
                    failures.append((i, status, body))

    print(f"\nStatus code counts: {status_counts}")
    if failures:
        print(f"\n{len(failures)} non-200 response(s):")
        for i, status, body in failures[:10]:
            print(f"  request {i}: status={status} body={body!r}")
    else:
        print("\nNo failures — could not reproduce at this concurrency/round count.")


if __name__ == "__main__":
    main()
