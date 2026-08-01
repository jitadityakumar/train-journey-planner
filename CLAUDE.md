# train-journey-planner — API reference for agents

This file exists so other agents/apps (e.g. sibling local-apps like `direct-train-summary`
and `rail-disruption-monitor`) can consume this app's JSON API without reading the source.
Human-facing setup/running docs are in `README.md`.

## Base URL

Local dev: `http://localhost:8000` (via `docker compose up`). Deployed instance (this
machine, `jkumar-server`, over Tailscale): `http://100.71.231.39:8010`.

Interactive OpenAPI docs are always available at `/docs` on whichever base URL is running.

## Data notes agents should know before calling this API

- Stations are identified by 3-letter CRS code (e.g. `BNS`, `WAT`, `CLJ`, `LRD`), National
  Rail's standard station code — not GTFS `stop_id`.
- All times are UK National Rail schedule times, i.e. Europe/London wall-clock, not UTC.
  A request with `date`/`time` in the past (London time) returns `400`.
- Same-day requests are the common case, but `date` can be any date within the loaded GTFS
  feed's range (feed request: `GET /api/journeys`... on an out-of-range date returns `400`).
- Data source is a nightly-refreshed real GTFS feed (TravelWhiz), not synthetic — results
  reflect actual scheduled services, including calendar exceptions (engineering works, etc).
- This app only models scheduled/timetabled journeys — it has no live delay/disruption data
  (that's `rail-disruption-monitor`'s job) and no real per-station minimum connection time
  (interchange journeys use a flat 5-minute MCT, capped at a 90-minute layover — see
  `MIN_CONNECTION_TIME_MINUTES`/`MAX_CONNECTION_TIME_MINUTES` in `app/config.py`).
- Routing currently covers **direct journeys and single-interchange (1 change) journeys
  only**. Journeys needing 2+ changes are not found or returned (planned as a future phase,
  not yet built) — if a query returns few/no journeys, that may be scope, not absence of
  service.
- `/api/journeys` applies Pareto-dominance filtering: a journey is dropped if another
  candidate departs no later, arrives no earlier, and needs no more changes, with at least
  one of those strictly better. This means the result list is not "every scheduled service"
  (unlike a typical human journey planner) — it's already been pruned to non-dominated
  options. `/api/direct` does not filter — it returns every direct trip in the window.

## Endpoints

### `GET /api/direct` — direct trains only

Query params:

| param            | type | required | notes                                              |
|-------------------|------|----------|-----------------------------------------------------|
| `from`            | str  | yes      | 3-letter CRS code                                   |
| `to`              | str  | yes      | 3-letter CRS code                                   |
| `date`            | date | yes      | `YYYY-MM-DD`                                        |
| `time`            | time | yes      | `HH:MM` (window start, London local time)           |
| `window_minutes`  | int  | no       | default 60, range 1–1440 (24h)                      |

Returns `DirectJourneyResponse`:

```json
{
  "origin": {"crs_code": "BNS", "name": "Barnes"},
  "destination": {"crs_code": "WAT", "name": "London Waterloo"},
  "date": "2026-08-17",
  "window_start": "09:00",
  "window_minutes": 60,
  "trips": [DirectTripOut, ...]
}
```

### `GET /api/journeys` — direct + single-interchange, merged, ranked, dominance-filtered

This is the combined view (also what the web form/results page uses). Same query params as
`/api/direct`, plus:

| param          | type | required | notes                                                                  |
|-----------------|------|----------|--------------------------------------------------------------------------|
| `window_minutes`| int  | no       | default 60, range 1–180 (lower cap than `/api/direct` — see `app/config.py`) |
| `direct_only`   | bool | no       | default `false`; if `true`, skips the interchange search entirely (equivalent to `/api/direct` but in the `JourneysResponse` shape) |

Returns `JourneysResponse`:

```json
{
  "origin": {"crs_code": "BNS", "name": "Barnes"},
  "destination": {"crs_code": "LRD", "name": "Leatherhead"},
  "date": "2026-08-17",
  "window_start": "09:00",
  "window_minutes": 60,
  "direct_only": false,
  "journeys": [JourneyOut, ...]
}
```

`JourneyOut.kind` is `"direct"` or `"interchange"`; exactly one of `direct` (a `DirectTripOut`)
/ `interchange` (an `InterchangeTripOut`) is populated depending on `kind`.

### `GET /api/stations` — full station list

No query params. Returns `list[StationOut]` — every station in the currently-loaded feed
(~2600 stations, real TravelWhiz feed). Powers the web form's name/CRS autocomplete; also the
simplest way for another agent/app to resolve a station name to its CRS code.

```json
[{"crs_code": "BNS", "name": "Barnes"}, {"crs_code": "WAT", "name": "London Waterloo"}, ...]
```

### `GET /health`

`{"status": "ok", "dataset_present": true}`. `dataset_present: false` during a cold start
before the first GTFS download/index completes — `/api/*` endpoints return `503` in that
window rather than `dataset_present: false` propagating an error some other way.

### `GET /`, `GET /results`

Server-rendered HTML (web form + results page). Not intended for programmatic use — use the
`/api/*` endpoints above instead.

## Schemas (Pydantic models, `app/schemas.py`)

```python
class StationOut:
    crs_code: str
    name: str

class IntermediateStopOut:
    stop_name: str
    stop_code: str
    arrival_time: str      # "HH:MM" or "HH:MM:SS", may exceed 24:00:00 pre-normalization upstream — always already normalized in API output
    departure_time: str

class DirectTripOut:
    trip_id: str
    operator: str | None           # real train-operating company, e.g. "South Western Railway" (from GTFS agency.txt); None if unresolvable
    route_description: str | None # route pattern description, e.g. "Alton - London Waterloo via Wimbledon" (route_short_name or route_long_name)
    headsign: str | None
    departure_time: str       # wall-clock "HH:MM:SS"
    arrival_time: str
    departure_next_day: bool  # true if this trip's departure is on the day after `date` (post-midnight service)
    arrival_next_day: bool
    duration_minutes: int
    intermediate_stops: list[IntermediateStopOut]

class InterchangeTripOut:
    leg1: DirectTripOut
    leg2: DirectTripOut
    interchange: StationOut          # where the change happens
    connection_minutes: int          # wait time at the interchange station
    total_duration_minutes: int      # leg1 + connection + leg2

class JourneyOut:
    kind: Literal["direct", "interchange"]
    departure_time: str
    departure_next_day: bool
    arrival_time: str
    arrival_next_day: bool
    duration_minutes: int
    direct: DirectTripOut | None        # populated iff kind == "direct"
    interchange: InterchangeTripOut | None  # populated iff kind == "interchange"

class DirectJourneyResponse:
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    trips: list[DirectTripOut]

class JourneysResponse:
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    direct_only: bool
    journeys: list[JourneyOut]
```

`operator` (real train-operating company, from GTFS `agency.txt`) and `route_description`
(route pattern text, e.g. "Alton - London Waterloo via Wimbledon") were renamed from
`agency_name`/`operator` respectively on 2026-08-01 — the old names had it backwards
(`operator` used to hold the route-pattern text, not the operator). This was a breaking
change to `DirectTripOut`; no backwards-compat aliases were kept.

## Error responses

All error responses use FastAPI's default `{"detail": "..."}` shape (see `ErrorResponse` in
`app/schemas.py`) on `/api/*` routes:

| status | cause                                                                  |
|--------|--------------------------------------------------------------------------|
| 400    | unknown CRS code, same origin/destination, date out of the feed's covered range, or requested date/time in the past (London time) |
| 422    | request validation failure (e.g. malformed date/time, `from`/`to` not exactly 3 chars) — standard FastAPI shape, not the custom one below |
| 503    | GTFS dataset not loaded yet (cold start) — retry shortly              |

Note: `/results` (the HTML page, not `/api/*`) has its own 422 handling that renders a styled
error page instead of a JSON blob — not relevant to API consumers.

## Example calls

```bash
curl "http://localhost:8000/api/direct?from=BNS&to=WAT&date=2026-08-17&time=09:00"

curl "http://localhost:8000/api/journeys?from=BNS&to=LRD&date=2026-08-17&time=09:00&window_minutes=90"

curl "http://localhost:8000/api/journeys?from=BNS&to=WAT&date=2026-08-17&time=09:00&direct_only=true"

curl "http://localhost:8000/api/stations"
```

## Keeping this file up to date

Update this file whenever `app/main.py`'s routes or `app/schemas.py`'s models change —
it's the contract other agents rely on. It intentionally does not duplicate `README.md`'s
setup/running/testing instructions, which are for humans running this app locally.
