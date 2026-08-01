# UK Train Journey Planner (GTFS)

Given a departure CRS code, arrival CRS code, a date, and a time, returns direct and
single-interchange trains departing within a 1-hour window — via a JSON API and a minimal web
form/results page.

Data source: [TravelWhiz's `gb-nationalrail.gtfs.zip`](https://storage.travelwhiz.app/generated-gtfs/gb-nationalrail.gtfs.zip),
refreshed daily. See the project's planning docs (kept outside this repo) for full background
and the phased roadmap.

## Status

**Phase 1 + 2: direct routes and single-interchange routes**, plus a Pareto-dominance filter
that prunes clearly-worse journeys from `/api/journeys` results. Multi-interchange
(2+ changes) routing is planned for a later phase.

See `CLAUDE.md` for a full API reference (endpoints, query params, response schemas) intended
for other agents/apps consuming this API programmatically.

## Running locally

```bash
docker compose up
```

First start downloads and indexes the GTFS feed (~1-2 minutes); subsequent starts reuse the
Docker volume. Then visit http://localhost:8000 for the web form (search stations by name or
CRS code, with autocomplete), or query the API directly:

```bash
# Direct trains only
curl "http://localhost:8000/api/direct?from=BNS&to=WAT&date=2026-08-17&time=09:00"

# Direct + single-interchange journeys, merged, ranked, and dominance-filtered
# (what the web form uses)
curl "http://localhost:8000/api/journeys?from=BNS&to=LRD&date=2026-08-17&time=09:00"

# Same, but direct trains only and a wider 120-minute search window
curl "http://localhost:8000/api/journeys?from=BNS&to=WAT&date=2026-08-17&time=09:00&direct_only=true&window_minutes=120"

# Full station list (CRS code + name) — also powers the web form's autocomplete
curl "http://localhost:8000/api/stations"
```

`/api/journeys` accepts `window_minutes` (default 60, max 180) and `direct_only` (default
false) query params; the web form exposes both as a search-window dropdown and an
all-trains/direct-only radio group. Interchange journeys use a flat 5-minute minimum
connection time (not real per-station data — see the planning docs' MCT section) and cap the
layover at 90 minutes.

Each direct trip in API/UI output includes the real train operating company (`agency_name`,
from GTFS `agency.txt`, e.g. "South Western Railway") alongside the existing route-pattern
description. Journey durations render as "1h6m" rather than a bare minute count.

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
