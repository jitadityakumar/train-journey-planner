from __future__ import annotations

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
    headsign: str | None
    departure_time: str
    arrival_time: str
    departure_next_day: bool
    arrival_next_day: bool
    duration_minutes: int
    intermediate_stops: list[IntermediateStopOut]


class DirectJourneyResponse(BaseModel):
    origin: StationOut
    destination: StationOut
    date: str
    window_start: str
    window_minutes: int
    trips: list[DirectTripOut]


class ErrorResponse(BaseModel):
    detail: str
