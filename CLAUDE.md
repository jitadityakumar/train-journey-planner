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
- Same-day requests are the common case, but `date` can be any date within the loaded GTFS
  feed's range (feed request: `GET /api/journeys`... on an out-of-range date returns `400`).
- **Past dates/times are allowed, not rejected** (changed 2026-08-01): a query for a
  date/time already behind the current Europe/London wall-clock time still returns real
  scheduled results, as long as it falls within the loaded feed's coverage — the feed's
  own `min_date` is often in the past relative to "now" (it's a rolling ~1-year window,
  refreshed daily), so this is genuinely useful, not just a leftover-from-yesterday case.
  Every `DirectTripOut` and `JourneyOut` carries its own `is_past: bool` (**not** a single
  response-level field — see "API field changes" below) so callers can tell which specific
  results have already departed without doing their own clock math, even within a single
  window that straddles "now". Only dates outside the feed's actual min/max coverage
  (`DateOutOfRangeError`) still `400`.
- Data source is a nightly-refreshed real GTFS feed (TravelWhiz), not synthetic — results
  reflect actual scheduled services, including calendar exceptions (engineering works, etc).
- This app only models scheduled/timetabled journeys — it has no live delay/disruption data
  (that's `rail-disruption-monitor`'s job) and no real per-station minimum connection time
  (interchange journeys use a flat 5-minute MCT, capped at a 90-minute layover — see
  `MIN_CONNECTION_TIME_MINUTES`/`MAX_CONNECTION_TIME_MINUTES` in `app/config.py`).
- Routing covers **direct journeys and single-interchange (1 change) journeys** natively via
  `/api/direct`/`/api/journeys` (own SQLite GTFS index). As of GitHub issue #26, a **separate
  2-5 change fallback tier** exists at `GET /api/journeys/multi-change`, backed by an
  OpenTripPlanner sidecar rather than this app's own index — it's a distinct endpoint, not
  merged into `/api/journeys`' response, and callers must call it explicitly (see that
  endpoint's own section below for when it's meaningful to). It's a genuinely separate tier
  (own health-gated availability, own weaker degradation guarantees), not just "the rest of
  the results" — don't assume `/api/journeys` alone is now complete for 2+ change queries.
- Both `/api/journeys` and, as of 2026-08-02 (issue #19), `/api/direct` by default apply
  Pareto-dominance filtering: a trip/journey is dropped if another candidate departs no
  later, arrives no earlier, and needs no more changes, with at least one of those strictly
  better. This means the result list is not "every scheduled service" (unlike a typical
  human journey planner) — it's already been pruned to non-dominated options. `/api/direct`
  accepts `include_dominated=true` to opt out and get every scheduled trip in the window,
  unfiltered (its old, pre-issue-#19 default). Filtering is computed against a window
  slightly wider than the requested one, then trimmed back to it, so a faster trip departing
  just outside the requested window can still correctly dominate a slower one inside it —
  invisible to callers except as somewhat higher latency on wide windows.

## Known data gaps in the TravelWhiz feed

Found while extending #11's accuracy comparison against live Darwin data (larger sample,
1943 live trains across 3 time-of-day runs). Unlike #11's PAD/PDX and LST/LSX split (fixed
app-side via station aliasing), **these are not fixable in this app's code** — the
underlying data is genuinely absent from TravelWhiz's feed, not just filed under an
unexpected CRS code. An empty or short result for these cases means missing data, not
necessarily "no service exists":

- **Farringdon (`ZFD`) has no Elizabeth line data at all.** Its Thameslink service is fully
  present, but it has zero trips to/from any Elizabeth line-only station (`PDX`, `LSX`,
  `BDS`, etc.) in either direction, all day — even though neighboring Elizabeth line
  stations (Bond Street, Tottenham Court Road, Whitechapel, Canary Wharf) return 330+ trips
  to `PDX`/`LSX` on the same date. Live Darwin data confirms real Elizabeth line services
  call at Farringdon; the GTFS feed has no counterpart for any of them.
- **Oxford Parkway (`OXP`) isn't in the feed at all** — a real, open station, entirely
  absent from `GET /api/stations` and `stops.txt`. Any query naming it 400s with
  `unknown station code: OXP`.
- **A residual ~2.5% background rate of scattered single-train omissions.** Across the
  1943-train live sample, ~48 trains had no GTFS counterpart at all — not a
  window-boundary or aliasing artifact, spread across ~30 unrelated route pairs/operators
  with no obvious common cause. Looks like inherent noise in TravelWhiz's CIF→GTFS
  conversion rather than a fixable pattern.

None of these have an app-side fix — there's no alternate CRS code or query-logic change
that recovers the missing data (unlike #11). The only real fixes are upstream (TravelWhiz
correcting their conversion) or a data-source change (the Phase 3 own-CIF-pipeline
fallback, currently future-optional, not near-term work). See issue #13 for the full
investigation.

## Endpoints

### `GET /api/direct` — direct trains only

Query params:

| param            | type | required | notes                                              |
|-------------------|------|----------|-----------------------------------------------------|
| `from`            | str  | yes      | 3-letter CRS code                                   |
| `to`              | str  | yes      | 3-letter CRS code                                   |
| `date`            | date | yes      | `YYYY-MM-DD`                                        |
| `time`            | time | yes      | `HH:MM` (window start, London local time)           |
| `window_minutes`  | int  | no       | default 60, range 1–1440 (24h) for `include_dominated=true`; **1–180 when filtered (the default)** — see below |
| `include_dominated` | bool | no    | default `false`; if `true`, skips Pareto-dominance filtering and returns every trip in the window, including ones no rider would prefer over another trip in the same response (matches this endpoint's pre-2026-08-02/issue-#19 behavior) |

The larger 24h `window_minutes` cap only applies with `include_dominated=true` (a genuinely
O(n) plain range scan regardless of window size). The default, filtered path now runs the
same O(n²) dominance pass `/api/journeys` does, over a similarly widened fetch (see
`dominant_direct_trips` in `app/queries.py`), so it's capped the same way:
`window_minutes` over `MAX_DOMINATED_DIRECT_WINDOW_MINUTES` (180, `app/config.py`) with
filtering still on returns `400`. Pass `include_dominated=true` to use a window above 180
minutes.

Returns `DirectJourneyResponse`:

```json
{
  "origin": {"crs_code": "BNS", "name": "Barnes"},
  "destination": {"crs_code": "WAT", "name": "London Waterloo"},
  "date": "2026-08-17",
  "window_start": "09:00",
  "window_minutes": 60,
  "filter_dominated": true,
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

`JourneysResponse` also carries `sidecar_healthy: bool` (added GitHub issue #26) — whether the
OTP sidecar passed its last background health check (checked every
`OTP_HEALTH_CHECK_INTERVAL_MINUTES`, default 2 min; see `app/otp_client.py`). This is about
whether calling `/api/journeys/multi-change` is currently worthwhile, not about `/api/journeys`
itself, which never touches the sidecar.

**Any caller wanting 2-5 change coverage must implement a two-step call pattern itself** — there
is no single endpoint that transparently escalates. Call `/api/journeys` first; only if it
returns zero journeys (and, ideally, `sidecar_healthy: true`), call `/api/journeys/multi-change`
as a second, separate request. This app's own `/results` UI (`app/static/multi_change.js`)
follows exactly this pattern and is the reference implementation. This is a deliberate design
choice, not an oversight — see "Why not one endpoint?" below.

### `GET /api/journeys/multi-change` — 2-5 change fallback tier (OTP sidecar-backed)

Added GitHub issue #26. Meant to be called only as a second stage, after `/api/journeys`
comes back with zero results — this app's own UI (`/results`) follows exactly that pattern:
render the direct/1-change search first, and only fetch this endpoint client-side if that
came back empty (see `app/static/multi_change.js`). Calling it unconditionally works but
wastes a network round-trip to a separate host (jk-server-ccu) for journeys that
`/api/journeys` would already have found more cheaply.

Same `from`/`to`/`date`/`time`/`window_minutes` params as `/api/journeys` (no `direct_only` —
this tier is inherently multi-change). Routing itself is delegated to an OpenTripPlanner
instance running as a separate network service (`otp-sidecar/`, deployed to `jk-server-ccu`,
reached over Tailscale) — this app's own SQLite GTFS index is only used for the
origin/destination CRS-code lookup, not the route search.

**Never hard-fails.** If the sidecar is down, unreachable, or errors, this returns `200` with
`sidecar_healthy: false` and `journeys: []` rather than a `4xx`/`5xx` — callers should treat
that as "deeper search temporarily unavailable", not an error to retry aggressively.

**Concurrency: this endpoint has its own, separate cap from `/api/journeys`.** `/api/journeys`
(and `/api/direct`/`/api/stations`/`/results`) share `MAX_CONCURRENT_DB_REQUESTS` (default 4,
GitHub issue #20) — a local, SQLite/CPU-bound budget. This endpoint is deliberately excluded
from that gate (its cost is external, not local DB/CPU) and instead has its own
`OTP_MAX_CONCURRENT_SIDECAR_REQUESTS` (default 4, `threading.Semaphore` with a short acquire
timeout, degrades to `sidecar_healthy: false`/`journeys: []` rather than queuing). **A caller
pacing concurrent requests must budget the two endpoints separately** — e.g. 4 concurrent
`/api/journeys` calls and 4 concurrent `/api/journeys/multi-change` calls are each independently
fine, but neither cap protects against exhausting the other. Note the sidecar-call cap applies
per request to this endpoint, regardless of how many changes (2-5) the query actually resolves
to inside OTP — a caller doesn't need to know the change count in advance to budget correctly.

**Why not one endpoint?** Merging the two (e.g. an `include_multi_change=true` flag on
`/api/journeys` that transparently falls back server-side) was considered and rejected for now:
it would hide a real latency cliff behind a single call — `/api/journeys` is a local SQL query,
this endpoint is a network round-trip to a separate host (`jk-server-ccu`) that can take
0.8-1.8s even when healthy — and would take away a caller's ability to opt out of that slow
path. This app's own `/results` UI genuinely needs the two stages to be visible (it shows a
spinner only for the second stage), which is the main reason the split exists. A caller that
doesn't want to implement the two-step pattern can simply never call
`/api/journeys/multi-change` and only get direct/1-change results.

Returns `MultiChangeJourneysResponse`:

```json
{
  "origin": {"crs_code": "BNS", "name": "Barnes"},
  "destination": {"crs_code": "PUL", "name": "Pulborough"},
  "date": "2026-08-17",
  "window_start": "09:00",
  "window_minutes": 60,
  "sidecar_healthy": true,
  "journeys": [MultiChangeJourneyOut, ...]
}
```

`MultiChangeJourneyOut.legs` is a `list[MultiChangeLegOut]` of length `num_changes + 1` (3 to
6 legs for the 2-5 change range this tier targets — a floor of 2 changes is enforced
explicitly in `plan_multi_change_journeys`, since OTP's own routing model can legitimately
surface a 0/1-change itinerary the SQL search missed, which would misrepresent this tier if
shown; the ceiling comes from `OTP_MAXIMUM_TRANSFERS` — see `app/config.py`). Unlike
`DirectTripOut`, `MultiChangeLegOut` has no `trip_id`,
`intermediate_stops`, or `reverses_at` — those aren't sourced from this app's own GTFS index
for this tier and weren't included in the first version (see `OTP_SIDECAR_PLAN.md` in the
sibling `local-apps/train-journey-planner-gtfs` context directory for the full design).
Results in this tier are **not** Pareto-filtered against `/api/journeys`' own direct/
interchange results — the two tiers are never merged or compared against each other, only
shown one at a time in the UI (first pass, or this tier if the first pass was empty).

### `GET /api/stations` — full station list

No query params. Returns `list[StationOut]` — every station in the currently-loaded feed
(~2600 stations, real TravelWhiz feed). Powers the web form's name/CRS autocomplete; also the
simplest way for another agent/app to resolve a station name to its CRS code.

```json
[{"crs_code": "BNS", "name": "Barnes"}, {"crs_code": "WAT", "name": "London Waterloo"}, ...]
```

### `GET /api/gtfs/checksum`, `GET /api/gtfs/zip` — internal, not for journey queries

Added GitHub issue #28. Serve the sha256 (plain text) and raw bytes of the `gtfs.zip` this
app's own scheduled refresh last persisted (`app/refresh.py`, `config.GTFS_ZIP_PATH`/
`GTFS_ZIP_CHECKSUM_PATH`) — pulled by the OTP sidecar's `poll_and_build.sh` on `jk-server-ccu`
over plain HTTP (replacing an earlier SSH/SCP pull that Tailscale SSH check-mode could hang
indefinitely). Both `404` if no refresh has completed yet. Not useful to a journey-planning
caller — listed here only because they're part of this app's HTTP surface.

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
    operator_code: str | None      # GTFS agency_id short code, e.g. "SW" for South Western Railway; None if unresolvable
    route_description: str | None # route pattern description, e.g. "Alton - London Waterloo via Wimbledon" (route_short_name or route_long_name)
    headsign: str | None
    departure_time: str       # wall-clock "HH:MM:SS"
    arrival_time: str
    departure_next_day: bool  # true if this trip's departure is on the day after `date` (post-midnight service)
    arrival_next_day: bool
    duration_minutes: int
    is_past: bool              # true if THIS trip's own departure_time (+ departure_next_day) is already behind Europe/London "now" — computed per trip, not per response, since a window can straddle "now"
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
    is_past: bool              # true if this journey's own departure has already passed (same rule as DirectTripOut.is_past — for "interchange" journeys this mirrors interchange.leg1.is_past, since that's the journey's real departure)
    direct: DirectTripOut | None        # populated iff kind == "direct"
    interchange: InterchangeTripOut | None  # populated iff kind == "interchange"

class DirectJourneyResponse:
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    filter_dominated: bool  # true unless the request set include_dominated=true — see API field changes below
    trips: list[DirectTripOut]

class JourneysResponse:
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    direct_only: bool
    journeys: list[JourneyOut]
    sidecar_healthy: bool  # added issue #26 — whether /api/journeys/multi-change is currently worth calling

class MultiChangeLegOut:               # added issue #26 — a single leg within a MultiChangeJourneyOut
    origin: StationOut
    destination: StationOut
    operator: str | None
    operator_code: str | None
    route_description: str | None
    headsign: str | None
    departure_time: str
    arrival_time: str
    departure_next_day: bool
    arrival_next_day: bool
    duration_minutes: int

class MultiChangeJourneyOut:           # added issue #26
    legs: list[MultiChangeLegOut]      # length == num_changes + 1
    departure_time: str
    departure_next_day: bool
    arrival_time: str
    arrival_next_day: bool
    duration_minutes: int
    num_changes: int
    is_past: bool

class MultiChangeJourneysResponse:     # added issue #26 — GET /api/journeys/multi-change's response
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    sidecar_healthy: bool              # false means journeys is unconditionally [] — sidecar down/erroring, not "genuinely no journeys"
    journeys: list[MultiChangeJourneyOut]
```

## API field changes

- **2026-08-30 (issue #28): added `GET /api/gtfs/checksum` and `GET /api/gtfs/zip`.** Internal
  plumbing for the OTP sidecar's GTFS pull (see their own section above) — not relevant to
  journey-planning callers, purely additive.
- **2026-08-12 (issue #26): added `GET /api/journeys/multi-change`, `JourneysResponse.sidecar_healthy`,
  and the OTP-sidecar dependency described above.** Purely additive — no existing endpoint or
  field changed shape. The new endpoint depends on a separately-deployed OTP sidecar
  (`otp-sidecar/`, `jk-server-ccu`) being reachable and healthy; when it isn't, the endpoint
  degrades (see its own section) rather than erroring, so a caller polling it doesn't need
  special-case error handling for "sidecar down", only for checking `sidecar_healthy`.
- **2026-08-02 (issue #19): `/api/direct` now applies Pareto-dominance filtering by
  default**, matching `/api/journeys`' existing behavior — previously it returned every
  scheduled direct trip in the window, unfiltered, which meant the two endpoints could
  disagree about the same physical trip set for the same query. This is a **breaking change
  to the default response** (no deprecation window — the only known consumer,
  `london-commuter-stations`, is what motivated the fix): a caller relying on the old
  "every trip" behavior must now pass `include_dominated=true`. Also added
  `DirectJourneyResponse.filter_dominated: bool`, purely additive, mirroring
  `JourneysResponse.direct_only`'s pattern of making the response self-describing about
  which mode produced it. **Also lowered `/api/direct`'s effective `window_minutes` cap to
  180 whenever filtering is on** (found in code review, 2026-08-02): the filtered path now
  does the same O(n²) dominance pass over a similarly widened fetch that `/api/journeys`
  already pays for, so the 24h cap's original "plain indexed range scan regardless of window
  size" justification (see `app/config.py`) no longer holds for it — a request over 180
  minutes with filtering still on now `400`s rather than risking a slow, unbounded scan;
  `include_dominated=true` keeps the full 24h cap, since that path is still genuinely O(n).
- **2026-08-01: `operator` (real train-operating company, from GTFS `agency.txt`) and
  `route_description` (route pattern text, e.g. "Alton - London Waterloo via Wimbledon")
  were renamed from `agency_name`/`operator` respectively** — the old names had it backwards
  (`operator` used to hold the route-pattern text, not the operator). This was a breaking
  change to `DirectTripOut`; no backwards-compat aliases were kept.
- **2026-08-01: added `DirectTripOut.operator_code`** — the GTFS `agency_id` short code
  (e.g. `"SW"`) alongside the existing `operator` full name, from the same already-joined
  `agency` table. Purely additive, no existing field changed.
- **2026-08-01: `is_past` moved from response-level to per-trip/per-journey** — it used to
  be a single `is_past: bool` on `DirectJourneyResponse`/`JourneysResponse`, computed once
  from the search's own `date`/`time`. That was misleading whenever the search window
  straddled "now" (e.g. searching a 60-minute window starting a few minutes ago): some
  results in the same response have already departed and some haven't, but they all got the
  same flag. Both response-level fields were removed; `DirectTripOut.is_past` and
  `JourneyOut.is_past` were added instead, each computed from that specific trip/journey's
  own `departure_time`/`departure_next_day`. This is a **breaking change** — callers reading
  `body["is_past"]` at the top level must switch to reading it off each trip/journey; no
  backwards-compat alias was kept, matching the `operator`/`route_description` rename's
  precedent above.

## Error responses

All error responses use FastAPI's default `{"detail": "..."}` shape (see `ErrorResponse` in
`app/schemas.py`) on `/api/*` routes:

| status | cause                                                                  |
|--------|--------------------------------------------------------------------------|
| 400    | unknown CRS code, same origin/destination, date out of the feed's covered range (past dates/times within the feed's range are allowed — see `is_past` above, not an error), or `/api/direct`'s `window_minutes` over 180 without `include_dominated=true` (see `/api/direct`'s own section above) |
| 422    | request validation failure (e.g. malformed date/time, `from`/`to` not exactly 3 chars) — standard FastAPI shape, not the custom one below |
| 500    | unhandled server error — logged with a full traceback server-side (GitHub issue #20; previously these reached the client as Starlette's bare non-JSON 500 with no server-side logging at all) |
| 503    | either the GTFS dataset isn't loaded yet (cold start — retry shortly), or the server is at its concurrent-request cap (GitHub issue #20's `MAX_CONCURRENT_DB_REQUESTS`, default 4) and the request timed out waiting for a free slot — retry shortly; this case includes a `Retry-After` header (seconds) |

Note: `/results` (the HTML page, not `/api/*`) has its own 422 handling that renders a styled
error page instead of a JSON blob — not relevant to API consumers.

## Example calls

```bash
curl "http://localhost:8000/api/direct?from=BNS&to=WAT&date=2026-08-17&time=09:00"

curl "http://localhost:8000/api/journeys?from=BNS&to=LRD&date=2026-08-17&time=09:00&window_minutes=90"

curl "http://localhost:8000/api/journeys?from=BNS&to=WAT&date=2026-08-17&time=09:00&direct_only=true"

curl "http://localhost:8000/api/journeys/multi-change?from=BNS&to=PUL&date=2026-08-17&time=09:00"

curl "http://localhost:8000/api/stations"
```

## Keeping this file up to date

Update this file whenever `app/main.py`'s routes or `app/schemas.py`'s models change —
it's the contract other agents rely on. It intentionally does not duplicate `README.md`'s
setup/running/testing instructions, which are for humans running this app locally.
