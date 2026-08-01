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
    route_description: str | None
    headsign: str | None
    departure_time: str
    arrival_time: str
    departure_next_day: bool
    arrival_next_day: bool
    duration_minutes: int
    intermediate_stops: list[IntermediateStopOut]


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
    direct: DirectTripOut | None = None
    interchange: InterchangeTripOut | None = None


class DirectJourneyResponse(BaseModel):
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    is_past: bool
    trips: list[DirectTripOut]


class JourneysResponse(BaseModel):
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    direct_only: bool
    is_past: bool
    journeys: list[JourneyOut]


class ErrorResponse(BaseModel):
    detail: str
