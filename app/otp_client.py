"""HTTP client for the OTP sidecar (GitHub issue #26) — the 2-5 change
fallback journey search, called by app/main.py only when the existing
direct/1-change SQL search (app/queries.py) returns zero results.

The sidecar runs OpenTripPlanner as a separate network service on
jk-server-ccu (see ../otp-sidecar/ and OTP_SIDECAR_PLAN.md), reached over
Tailscale. This module never touches the SQLite GTFS index — it only calls
the sidecar's GraphQL API and maps its response onto this app's own
DirectTrip-shaped fields (see OTP_SIDECAR_PLAN.md's field-mapping table,
confirmed via the 2026-08-12 spike against a live instance).

Query shape note: the exact `planConnection` argument names below follow
OTP 2.x's documented GraphQL GTFS API (the same query family verified live
during the spike) but weren't preserved verbatim from that spike session —
re-verify field/argument names against the actually-deployed sidecar
(`/otp/gtfs/v1`, GraphiQL IDE available there) before relying on this in
production, and adjust `_PLAN_CONNECTION_QUERY` if anything's drifted.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

from app import config
from app.queries import Stop

logger = logging.getLogger("train_journey_planner.otp_client")

LONDON_TZ = ZoneInfo("Europe/London")


class SidecarUnavailableError(RuntimeError):
    """Raised when the sidecar can't be reached or returns an error — callers
    should treat this the same as a health-check failure (degrade, don't
    500)."""


@dataclass
class MultiChangeLeg:
    origin: Stop
    destination: Stop
    agency_name: str | None
    agency_id: str | None
    route_description: str | None
    headsign: str | None
    departure_time: str  # "HH:MM:SS", London wall-clock
    arrival_time: str
    departure_next_day: bool
    arrival_next_day: bool
    duration_minutes: int
    # National Rail headcode — reuses issue #23's same-physical-train
    # detection signal. Not consumed by the first version of this tier (see
    # OTP_SIDECAR_PLAN.md), kept for a future "no change needed" refinement.
    trip_short_name: str | None = None


@dataclass
class MultiChangeJourney:
    legs: list[MultiChangeLeg] = field(default_factory=list)
    departure_time: str = ""
    departure_next_day: bool = False
    arrival_time: str = ""
    arrival_next_day: bool = False
    duration_minutes: int = 0

    @property
    def num_changes(self) -> int:
        return max(0, len(self.legs) - 1)


_PLAN_CONNECTION_QUERY = """
query PlanConnection(
  $originStopId: String!
  $destinationStopId: String!
  $earliestDeparture: OffsetDateTime!
  $maximumTransfers: Int!
) {
  planConnection(
    origin: { location: { stopLocation: { stopLocationId: $originStopId } } }
    destination: { location: { stopLocation: { stopLocationId: $destinationStopId } } }
    dateTime: { earliestDeparture: $earliestDeparture }
    preferences: { transit: { transfer: { maximumTransfers: $maximumTransfers } } }
  ) {
    edges {
      node {
        legs {
          mode
          start { scheduledTime }
          end { scheduledTime }
          from { stop { code name } }
          to { stop { code name } }
          agency { name gtfsId }
          route { shortName longName }
          trip { tripHeadsign tripShortName }
        }
      }
    }
  }
}
"""

# A leg with this mode isn't a train service — OTP can inject a WALK leg for
# an in-station platform transfer even on this app's transit-only graph (no
# OSM/street edges, per the 2026-08-12 spike, but OTP can still synthesize a
# short walk between colocated stops using default transfer distances).
# Found in Opus review, 2026-08-12: without this filter, a walk leg both
# inflates num_changes (defeating the "2-5 change" floor/ceiling below) and
# renders as a blank leg in the UI (multi_change.js — no agency/route/trip
# on a walk leg, so operator/route_description/headsign are all null).
_NON_TRANSIT_MODES = frozenset({"WALK"})


def _graphql_url() -> str:
    return config.OTP_SIDECAR_URL.rstrip("/") + config.OTP_GRAPHQL_PATH


def _health_url() -> str:
    return config.OTP_SIDECAR_URL.rstrip("/") + config.OTP_HEALTH_PATH


def _gtfs_id(stop_id: str) -> str:
    return f"{config.OTP_FEED_ID}:{stop_id}"


def _strip_feed_prefix(gtfs_id: str | None) -> str | None:
    if gtfs_id is None:
        return None
    prefix = f"{config.OTP_FEED_ID}:"
    return gtfs_id[len(prefix):] if gtfs_id.startswith(prefix) else gtfs_id


def _to_london(iso_timestamp: str) -> dt.datetime:
    """Parses an OTP `scheduledTime` and converts to Europe/London wall-clock.
    Found in Opus review, 2026-08-12: the app's own display/next-day logic
    (`strftime`/`.date()` below) is offset-blind, so without this, a sidecar
    that serialises OffsetDateTime as UTC rather than +01:00/+00:00 would
    silently show every summer time as an hour early — a wrong-but-plausible
    result, not an error. A naive timestamp (no offset at all) is assumed to
    already be London wall-clock, matching this app's own GTFS convention
    (see validation.py's module docstring)."""
    parsed = dt.datetime.fromisoformat(iso_timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LONDON_TZ)
    return parsed.astimezone(LONDON_TZ)


# Bounds concurrent sidecar calls (see config.OTP_MAX_CONCURRENT_SIDECAR_REQUESTS's
# docstring) — a plain threading.Semaphore, not asyncio, since route
# handlers run in Starlette's threadpool (GitHub issue #20's established
# concurrency model for this app).
_sidecar_request_semaphore = threading.Semaphore(config.OTP_MAX_CONCURRENT_SIDECAR_REQUESTS)


def plan_multi_change_journeys(
    origin: Stop,
    destination: Stop,
    date: dt.date,
    time: dt.time,
    window_minutes: int,
) -> list[MultiChangeJourney]:
    """Calls the sidecar's planConnection query for a 2-5 change itinerary
    search. Raises SidecarUnavailableError on any network/HTTP/GraphQL
    error, malformed response, or concurrency-cap timeout — callers must
    catch this and degrade rather than 500 (see OTP_SIDECAR_PLAN.md decision
    #2)."""
    if not config.OTP_SIDECAR_URL:
        raise SidecarUnavailableError("OTP_SIDECAR_URL is not configured")

    if not _sidecar_request_semaphore.acquire(timeout=config.OTP_SIDECAR_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS):
        raise SidecarUnavailableError(
            f"OTP sidecar concurrency cap ({config.OTP_MAX_CONCURRENT_SIDECAR_REQUESTS}) reached"
        )

    try:
        earliest_departure = dt.datetime.combine(date, time, tzinfo=LONDON_TZ).isoformat()
        variables = {
            "originStopId": _gtfs_id(origin.stop_id),
            "destinationStopId": _gtfs_id(destination.stop_id),
            "earliestDeparture": earliest_departure,
            "maximumTransfers": config.OTP_MAXIMUM_TRANSFERS,
        }

        # Broad by design, not just httpx.HTTPError: a malformed/unexpected
        # response (non-JSON body from a proxy error page despite HTTP 200, a
        # missing/renamed GraphQL field, an unparseable timestamp) must degrade
        # the same way a network failure does — see SidecarUnavailableError's
        # docstring and OTP_SIDECAR_PLAN.md decision #2 ("never a hard
        # failure"). The GraphQL query shape is explicitly unverified against a
        # live sidecar (see this module's docstring), so this boundary is doing
        # real work, not just defensive padding.
        try:
            response = httpx.post(
                _graphql_url(),
                json={"query": _PLAN_CONNECTION_QUERY, "variables": variables},
                timeout=config.OTP_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()

            if "errors" in payload:
                raise SidecarUnavailableError(f"OTP sidecar returned GraphQL errors: {payload['errors']}")

            edges = payload.get("data", {}).get("planConnection", {}).get("edges", [])
            window_end = dt.datetime.combine(date, time, tzinfo=LONDON_TZ) + dt.timedelta(minutes=window_minutes)

            journeys = []
            for edge in edges:
                legs_raw = edge.get("node", {}).get("legs", [])
                if not legs_raw:
                    continue
                journey = _build_journey(legs_raw, date)
                # Floor of 2 changes, matching the OTP_MAXIMUM_TRANSFERS=5
                # ceiling: this tier only exists because the direct/1-change
                # SQL search already came back empty, but OTP's own routing
                # model (different transfer-time/walking assumptions) can
                # legitimately surface a 0/1-change itinerary the SQL search
                # missed — showing that here would misrepresent this as a
                # "2-5 change" result. Computed from journey.legs, which
                # already excludes non-transit (e.g. WALK) legs.
                if journey.num_changes < 2:
                    continue
                first_leg_departure = _to_london(legs_raw[0]["start"]["scheduledTime"])
                if first_leg_departure >= window_end:
                    continue
                journeys.append(journey)
        except httpx.HTTPError as exc:
            raise SidecarUnavailableError(f"OTP sidecar request failed: {exc}") from exc
        except SidecarUnavailableError:
            raise
        except Exception as exc:
            raise SidecarUnavailableError(f"OTP sidecar returned an unexpected/malformed response: {exc}") from exc
    finally:
        _sidecar_request_semaphore.release()

    return journeys


def _build_journey(legs_raw: list[dict], query_date: dt.date) -> MultiChangeJourney:
    transit_legs_raw = [leg for leg in legs_raw if leg.get("mode") not in _NON_TRANSIT_MODES] or legs_raw
    legs = [_build_leg(leg, query_date) for leg in transit_legs_raw]
    # Overall departure/arrival still spans the *full* itinerary (including
    # any leading/trailing walk leg) — only the displayed/counted `legs`
    # list is transit-only.
    first_start = _to_london(legs_raw[0]["start"]["scheduledTime"])
    last_end = _to_london(legs_raw[-1]["end"]["scheduledTime"])
    return MultiChangeJourney(
        legs=legs,
        departure_time=first_start.strftime("%H:%M:%S"),
        departure_next_day=first_start.date() > query_date,
        arrival_time=last_end.strftime("%H:%M:%S"),
        arrival_next_day=last_end.date() > query_date,
        duration_minutes=round((last_end - first_start).total_seconds() / 60),
    )


def _build_leg(leg: dict, query_date: dt.date) -> MultiChangeLeg:
    start = _to_london(leg["start"]["scheduledTime"])
    end = _to_london(leg["end"]["scheduledTime"])
    from_stop = leg.get("from", {}).get("stop") or {}
    to_stop = leg.get("to", {}).get("stop") or {}
    agency = leg.get("agency") or {}
    route = leg.get("route") or {}
    trip = leg.get("trip") or {}
    return MultiChangeLeg(
        origin=Stop(stop_id="", stop_code=from_stop.get("code", ""), stop_name=from_stop.get("name", "")),
        destination=Stop(stop_id="", stop_code=to_stop.get("code", ""), stop_name=to_stop.get("name", "")),
        agency_name=agency.get("name"),
        agency_id=_strip_feed_prefix(agency.get("gtfsId")),
        route_description=route.get("shortName") or route.get("longName"),
        headsign=trip.get("tripHeadsign"),
        departure_time=start.strftime("%H:%M:%S"),
        arrival_time=end.strftime("%H:%M:%S"),
        departure_next_day=start.date() > query_date,
        arrival_next_day=end.date() > query_date,
        duration_minutes=round((end - start).total_seconds() / 60),
        trip_short_name=trip.get("tripShortName"),
    )


# --- Health check -----------------------------------------------------
#
# A simple in-process flag, refreshed periodically by a background job (see
# start_health_check_scheduler) — /api/journeys reads is_sidecar_healthy()
# rather than making a live health-check call on every request, so a slow or
# hanging sidecar can't add latency to the common (direct/1-change found)
# case. Thread-locked because the health-check job and request-handling
# threads both touch it (this app runs its route handlers in Starlette's
# threadpool — see GitHub issue #20).

_health_lock = threading.Lock()
_sidecar_healthy = False


def is_sidecar_healthy() -> bool:
    with _health_lock:
        return _sidecar_healthy


def check_sidecar_health() -> bool:
    """Hits the sidecar's actuator health endpoint once and updates the
    cached flag. Called both by the periodic background job and directly by
    tests — any failure (unreachable, non-200, unconfigured URL, or a
    malformed OTP_SIDECAR_URL that httpx can't even build a request from)
    counts as unhealthy, never raises. Broad except, not just httpx.HTTPError
    (found in Opus review, 2026-08-12: httpx.InvalidURL — e.g. a value with
    no scheme, exactly the kind of thing a hand-edited config can produce —
    isn't an HTTPError subclass, and APScheduler would otherwise swallow it
    into its own unconfigured logger, silently freezing the flag forever)."""
    global _sidecar_healthy
    healthy = False
    if config.OTP_SIDECAR_URL:
        try:
            response = httpx.get(_health_url(), timeout=config.OTP_HEALTH_CHECK_TIMEOUT_SECONDS)
            healthy = response.status_code == 200
        except Exception:
            healthy = False
    with _health_lock:
        _sidecar_healthy = healthy
    if not healthy:
        logger.warning("OTP sidecar health check failed — /api/journeys will degrade to direct + 1-change only")
    return healthy


def start_health_check_scheduler() -> BackgroundScheduler:
    """Mirrors app.refresh.start_scheduler's pattern: one BackgroundScheduler
    per process, fine under this app's single-uvicorn-worker configuration
    (see refresh.py's module docstring for the same caveat if that ever
    changes). Runs an immediate check on startup via an explicit
    next_run_time=now — NOT APScheduler's IntervalTrigger default, which is
    now + interval, not now (found and empirically verified in Opus review,
    2026-08-12: the flag was silently False, and the multi-change tier
    unnecessarily degraded, for a full OTP_HEALTH_CHECK_INTERVAL_MINUTES
    after every process start/redeploy — the exact bug this comment used to
    claim was already handled)."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        check_sidecar_health,
        trigger="interval",
        minutes=config.OTP_HEALTH_CHECK_INTERVAL_MINUTES,
        id="otp_sidecar_health_check",
        replace_existing=True,
        next_run_time=dt.datetime.now(),
    )
    scheduler.start()
    return scheduler
