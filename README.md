# UK Train Journey Planner (GTFS)

Given a departure CRS code, arrival CRS code, a date, and a time, returns direct and
single-interchange trains departing within a 1-hour window — via a JSON API and a minimal web
form/results page.

Data source: [TravelWhiz's `gb-nationalrail.gtfs.zip`](https://storage.travelwhiz.app/generated-gtfs/gb-nationalrail.gtfs.zip),
refreshed daily. See the project's planning docs (kept outside this repo) for full background
and the phased roadmap.

## Status

**Phase 1 + 2: direct routes and single-interchange routes.** Multi-interchange (2+ changes)
routing is planned for a later phase.

## Running locally

```bash
docker compose up
```

First start downloads and indexes the GTFS feed (~1-2 minutes); subsequent starts reuse the
Docker volume. Then visit http://localhost:8000 for the web form, or query the API directly:

```bash
# Direct trains only
curl "http://localhost:8000/api/direct?from=BNS&to=WAT&date=2026-08-17&time=09:00"

# Direct + single-interchange journeys, merged and ranked (what the web form uses)
curl "http://localhost:8000/api/journeys?from=BNS&to=LRD&date=2026-08-17&time=09:00"
```

Interchange journeys use a flat 5-minute minimum connection time (not real per-station
data — see the planning docs' MCT section) and cap the layover at 90 minutes.

Interactive API docs: http://localhost:8000/docs

## Running tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests run against a small, checked-in GTFS fixture (`tests/fixtures/gtfs/`) built from real,
previously-verified station data — no network access or full dataset download required. See
`scripts/build_fixture.py` to regenerate/expand it.

## Dataset refresh

The GTFS feed is not committed to this repo (too large — ~80MB zipped). It's downloaded to a
Docker volume and refreshed automatically:

- **On startup**, if no dataset is present yet (fresh volume), it's fetched immediately.
- **Daily at 04:00** (configurable via `REFRESH_HOUR`/`REFRESH_MINUTE` env vars), matching
  TravelWhiz's nightly regeneration window.

Refreshes download to a temp path, validate structure, then atomically swap in — a failed or
corrupt refresh never disturbs a dataset already being served.
