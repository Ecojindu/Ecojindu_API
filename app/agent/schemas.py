"""Request/response contracts for the AI-agent surface.

These are the shapes the ADK agent's tool definitions should mirror. Every money
value is in **kobo** (₦1 = 100 kobo). Every timestamp is ISO-8601 with an offset;
local time is Africa/Lagos (UTC+1, no DST).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class RouteOut(BaseModel):
    route_id: str = Field(description="Pass this to /availability and /quote")
    name: str = Field(examples=["Umuahia → Sam Mbakwe Airport"])
    code: str = Field(examples=["UMU-OWA"])
    origin: str = Field(examples=["Nnenna Otti Bus Terminal, Umuahia"])
    destination: str = Field(examples=["Sam Mbakwe International Cargo Airport, Owerri"])
    distance_km: float
    duration_mins: int
    base_fare_kobo: int = Field(description="Fare per seat, in kobo", examples=[1_500_000])
    base_fare_naira: float = Field(examples=[15000.0])
    pickup_points: list[str] = Field(description="Stops where a passenger may board")


class TripOut(BaseModel):
    trip_id: str = Field(description="Pass this to /quote and /bookings")
    route_id: str
    route_name: str
    origin: str
    destination: str
    service_date: date
    departure_time: str = Field(description="Local HH:MM in Africa/Lagos", examples=["09:00"])
    departure_datetime: str = Field(description="ISO-8601 with offset")
    duration_mins: int
    seats_available: int
    seats_total: int
    fare_kobo: int
    fare_naira: float
    status: str = Field(examples=["scheduled"])
    is_bookable: bool = Field(description="False when sold out, cancelled or past the cut-off")


class QuoteRequest(BaseModel):
    trip_id: str | None = Field(
        default=None, description="Preferred. Quote a specific departure."
    )
    route_id: str | None = Field(
        default=None, description="Use with `service_date` when no trip has been chosen yet."
    )
    service_date: date | None = None
    seats: int = Field(default=1, ge=1, le=14)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"trip_id": "3f2c…", "seats": 2},
                {"route_id": "9a1b…", "service_date": "2026-08-14", "seats": 1},
            ]
        }
    }


class QuoteOut(BaseModel):
    trip_id: str | None
    route_name: str
    departure_datetime: str | None
    seats: int
    fare_per_seat_kobo: int
    total_kobo: int
    total_naira: float
    currency: str = "NGN"
    seats_available: int | None = None
    summary: str = Field(description="One-line, speakable summary the agent can read out")


class CreateBookingRequest(BaseModel):
    trip_id: str
    passenger_name: str = Field(min_length=2, max_length=160, examples=["Chinedu Okafor"])
    passenger_phone: str = Field(
        description="Nigerian number in any common format — 08154471570, +2348154471570",
        examples=["08154471570"],
    )
    passenger_email: EmailStr | None = None
    seats: int = Field(default=1, ge=1, le=14)
    notes: str | None = Field(default=None, max_length=500)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "trip_id": "3f2c8d1a-…",
                    "passenger_name": "Chinedu Okafor",
                    "passenger_phone": "08154471570",
                    "passenger_email": "chinedu@example.com",
                    "seats": 2,
                }
            ]
        }
    }


class BookingOut(BaseModel):
    booking_ref: str = Field(examples=["EJS-8K3F2"])
    status: str = Field(
        description="pending_payment | confirmed | checked_in | completed | cancelled"
    )
    passenger_name: str
    passenger_phone: str
    passenger_email: str | None = None
    seats: int
    seat_numbers: list[str] = []
    amount_kobo: int
    amount_naira: float
    route_name: str | None = None
    departure_datetime: str | None = None
    payment_url: str | None = Field(
        default=None, description="Paystack checkout link. Null once paid or for credit bookings."
    )
    payment_reference: str | None = None
    hold_expires_at: str | None = Field(
        default=None, description="Seats are released after this instant if payment hasn't landed"
    )
    ticket_url: str | None = Field(default=None, description="QR PNG, once confirmed")
    paid_with_credits: bool = False
    message: str = Field(description="Passenger-facing sentence the agent can say verbatim")


class SubscriptionOut(BaseModel):
    found: bool
    phone: str
    subscriber_name: str | None = None
    plan_name: str | None = None
    credits_total: int | None = None
    credits_used: int | None = None
    credits_remaining: int | None = None
    expires_at: str | None = None
    status: str | None = None
    can_book_now: bool = False
    message: str


class SubscriptionBookingRequest(BaseModel):
    trip_id: str
    seats: int = Field(default=1, ge=1, le=14)
    passenger_name: str | None = Field(
        default=None, description="Defaults to the subscriber's own name"
    )
    passenger_email: EmailStr | None = None


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ResendRequest(BaseModel):
    channels: list[Literal["email", "sms", "whatsapp"]] = Field(
        default=["email", "sms"], description="Where to send the ticket again"
    )


class SimpleResult(BaseModel):
    ok: bool
    message: str
    booking_ref: str | None = None
