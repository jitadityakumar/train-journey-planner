from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class StationOut(BaseModel):
    crs_code: str
    name: str


class IntermediateStopOut(BaseModel):
    stop_name: str
    stop_code: str
    arrival_time: str
    departure_time: str


class DirectTripOut(BaseModel):
    trip_id: str
    operator: str | None
    operator_code: str | None
    route_description: str | None
    headsign: str | None
    departure_time: str
    arrival_time: str
    departure_next_day: bool
    arrival_next_day: bool
    duration_minutes: int
    is_past: bool
    intermediate_stops: list[IntermediateStopOut]
    # Set when this is a synthesized reversal-continuation trip (a physical
    # train that terminates, reverses, and continues under a different
    # trip_id — see GitHub issue #15) and the journey genuinely spans both
    # legs: the stop where the reversal happens, not an ordinary change.
    reverses_at: StationOut | None = None


class InterchangeTripOut(BaseModel):
    leg1: DirectTripOut
    leg2: DirectTripOut
    interchange: StationOut
    connection_minutes: int
    total_duration_minutes: int


class JourneyOut(BaseModel):
    kind: Literal["direct", "interchange"]
    departure_time: str
    departure_next_day: bool
    arrival_time: str
    arrival_next_day: bool
    duration_minutes: int
    is_past: bool
    direct: DirectTripOut | None = None
    interchange: InterchangeTripOut | None = None


class MultiChangeLegOut(BaseModel):
    """A single leg of a 2-5 change journey from the OTP sidecar tier (see
    app/otp_client.py, GitHub issue #26). Deliberately a smaller shape than
    DirectTripOut — no trip_id/intermediate_stops/reverses_at, since those
    aren't sourced from this app's own GTFS index and aren't needed for a
    first version (see OTP_SIDECAR_PLAN.md)."""

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


class MultiChangeJourneyOut(BaseModel):
    legs: list[MultiChangeLegOut]
    departure_time: str
    departure_next_day: bool
    arrival_time: str
    arrival_next_day: bool
    duration_minutes: int
    num_changes: int
    is_past: bool


class MultiChangeJourneysResponse(BaseModel):
    """Response for the second-stage /api/journeys/multi-change endpoint —
    only ever called by the frontend after the first-pass /api/journeys
    response comes back empty (two-stage UI, never blended — see
    OTP_SIDECAR_PLAN.md decision #1)."""

    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    sidecar_healthy: bool
    journeys: list[MultiChangeJourneyOut]


class DirectJourneyResponse(BaseModel):
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    # Mirrors JourneysResponse.direct_only's pattern: makes the response
    # self-describing about which mode produced it (see GitHub issue #19 —
    # dominance filtering is applied by default as of that issue).
    filter_dominated: bool
    trips: list[DirectTripOut]


class JourneysResponse(BaseModel):
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    direct_only: bool
    journeys: list[JourneyOut]
    # Whether the OTP sidecar was healthy as of the last background check
    # (see app/otp_client.py) — lets the frontend decide whether to attempt
    # the second-stage multi-change search at all when this list is empty,
    # or go straight to a "deeper search unavailable" state (GitHub issue
    # #26, OTP_SIDECAR_PLAN.md decision #2).
    sidecar_healthy: bool


class ErrorResponse(BaseModel):
    detail: str
