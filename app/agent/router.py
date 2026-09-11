"""`/agent/v1` — the API-key-protected surface the ADK booking agent consumes.

Design notes for whoever wires the agent:

* One endpoint per user intent, so each maps cleanly to a single agent tool.
* Every response carries a `message` / `summary` field written to be spoken or
  sent verbatim — the agent shouldn't have to compose prose from raw fields.
* Errors come back as `{"error": {"code", "message", ...}}` with a
  passenger-safe `message`, so the agent can relay it directly.
* Phone numbers are accepted in any common Nigerian format and normalised server-side.

See `docs/AGENT_INTEGRATION.md` for worked flows.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Path, Query

from app.core.config import settings
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.logging import log_event
from app.core.security import AgentRateLimit, require_agent_key
from app.agent.schemas import (
    BookingOut,
    CancelRequest,
    CreateBookingRequest,
    QuoteOut,
    QuoteRequest,
    ResendRequest,
    RouteOut,
    SimpleResult,
    SubscriptionBookingRequest,
    SubscriptionOut,
    TripOut,
)
from app.services.backend import backend

logger = logging.getLogger("ecojindu.api.agent")

LAGOS = ZoneInfo("Africa/Lagos")

router = APIRouter(
    prefix="/agent/v1",
    tags=["AI Agent"],
    dependencies=[Depends(require_agent_key), Depends(AgentRateLimit())],
    responses={
        401: {"description": "Missing or invalid `X-API-Key`"},
        429: {"description": "Rate limit exceeded"},
        502: {"description": "The core booking service is unreachable"},
    },
)


def naira(kobo: int | None) -> float:
    return round((kobo or 0) / 100, 2)


def _fmt_naira(kobo: int | None) -> str:
    return "₦{:,.0f}".format((kobo or 0) / 100)


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LAGOS)


def _speak_dt(iso: str) -> str:
    dt = _local(iso)
    return dt.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")


def _route_out(r: dict) -> RouteOut:
    return RouteOut(
        route_id=r["id"],
        name=r["name"],
        code=r["code"],
        origin=r["origin_terminal"],
        destination=r["destination"],
        distance_km=r["distance_km"],
        duration_mins=r["duration_mins"],
        base_fare_kobo=r["base_fare_kobo"],
        base_fare_naira=naira(r["base_fare_kobo"]),
        pickup_points=[s["name"] for s in r.get("stops", []) if s.get("pickup_allowed")],
    )


def _trip_out(t: dict) -> TripOut:
    return TripOut(
        trip_id=t["id"],
        route_id=t["route_id"],
        route_name=t["route_name"],
        origin=t["origin_terminal"],
        destination=t["destination"],
        service_date=t["service_date"],
        departure_time=_local(t["departure_datetime"]).strftime("%H:%M"),
        departure_datetime=_local(t["departure_datetime"]).isoformat(),
        duration_mins=t["duration_mins"],
        seats_available=t["seats_available"],
        seats_total=t["seats_total"],
        fare_kobo=t["fare_kobo"],
        fare_naira=naira(t["fare_kobo"]),
        status=t["status"],
        is_bookable=t["is_bookable"],
    )


def _booking_out(payload: dict, *, message: str | None = None) -> BookingOut:
    booking = payload.get("booking", payload)
    payment = payload.get("payment") or {}
    trip = booking.get("trip") or {}
    confirmed = booking["status"] in {"confirmed", "checked_in", "completed"}

    return BookingOut(
        booking_ref=booking["booking_ref"],
        status=booking["status"],
        passenger_name=booking["passenger_name"],
        passenger_phone=booking["passenger_phone"],
        passenger_email=booking.get("passenger_email"),
        seats=booking["seats"],
        seat_numbers=booking.get("seat_numbers") or [],
        amount_kobo=booking["amount_kobo"],
        amount_naira=naira(booking["amount_kobo"]),
        route_name=trip.get("route_name"),
        departure_datetime=_local(trip["departure_datetime"]).isoformat()
        if trip.get("departure_datetime")
        else None,
        payment_url=payment.get("authorization_url") or None,
        payment_reference=payment.get("reference"),
        hold_expires_at=payload.get("hold_expires_at") or booking.get("hold_expires_at"),
        ticket_url=backend.ticket_image_url(booking["booking_ref"]) if confirmed else None,
        paid_with_credits=bool(booking.get("subscription_id")) and booking["amount_kobo"] == 0,
        message=message or payload.get("message") or "Booking retrieved.",
    )


# ══════════════════════════════════════════════════════════════
#  Discovery
# ══════════════════════════════════════════════════════════════


@router.get(
    "/routes",
    response_model=list[RouteOut],
    summary="List every active route with its fare and pickup points",
    description=(
        "Call this first when a caller hasn't said where they're going, or to map a "
        "spoken place name ('Umuahia', 'the airport') onto a `route_id`."
    ),
)
async def list_routes() -> list[RouteOut]:
    routes = await backend.list_routes()
    return [_route_out(r) for r in routes if r.get("is_active", True)]


@router.get(
    "/availability",
    response_model=list[TripOut],
    summary="Departures with live seat availability",
    description=(
        "Returns only future, bookable departures, soonest first. Filter by "
        "`route_id` plus `date`, or search loosely with `origin` / `destination`."
    ),
)
async def availability(
    route_id: Annotated[str | None, Query(description="From /agent/v1/routes")] = None,
    date_: Annotated[date | None, Query(alias="date", description="YYYY-MM-DD, Africa/Lagos")] = None,
    seats: Annotated[int, Query(ge=1, le=14, description="Only show departures with room for this many")] = 1,
    origin: Annotated[str | None, Query(description="Partial place name, e.g. 'Umuahia'")] = None,
    destination: Annotated[str | None, Query(description="Partial place name, e.g. 'Airport'")] = None,
) -> list[TripOut]:
    if not route_id and not origin and not destination:
        raise ValidationError(
            "Provide `route_id`, or `origin` and/or `destination`, so I know which corridor to search."
        )
    trips = await backend.list_trips(
        route_id=route_id,
        service_date=date_.isoformat() if date_ else None,
        seats=seats,
        origin=origin,
        destination=destination,
    )
    return [_trip_out(t) for t in trips if t.get("is_bookable")]


@router.post(
    "/quote",
    response_model=QuoteOut,
    summary="Price a journey before booking",
    description=(
        "Give either a `trip_id` (preferred — quotes that exact departure and reports "
        "seats left) or a `route_id` + `service_date` for an indicative price."
    ),
)
async def quote(payload: QuoteRequest) -> QuoteOut:
    if payload.trip_id:
        trip = await backend.get_trip(payload.trip_id)
        total = trip["fare_kobo"] * payload.seats
        seat_word = "seat" if payload.seats == 1 else "seats"
        return QuoteOut(
            trip_id=trip["id"],
            route_name=trip["route_name"],
            departure_datetime=_local(trip["departure_datetime"]).isoformat(),
            seats=payload.seats,
            fare_per_seat_kobo=trip["fare_kobo"],
            total_kobo=total,
            total_naira=naira(total),
            seats_available=trip["seats_available"],
            summary=(
                f"{payload.seats} {seat_word} on the {_speak_dt(trip['departure_datetime'])} "
                f"departure from {trip['origin_terminal'].split(',')[0]} to "
                f"{trip['destination'].split(',')[0]} comes to {_fmt_naira(total)}. "
                f"{trip['seats_available']} seats are still available."
            ),
        )

    if not payload.route_id:
        raise ValidationError("Provide either `trip_id`, or `route_id` with `service_date`.")

    route = await backend.get_route(payload.route_id)
    total = route["base_fare_kobo"] * payload.seats
    seat_word = "seat" if payload.seats == 1 else "seats"
    when = f" on {payload.service_date:%A %d %B}" if payload.service_date else ""
    return QuoteOut(
        trip_id=None,
        route_name=route["name"],
        departure_datetime=None,
        seats=payload.seats,
        fare_per_seat_kobo=route["base_fare_kobo"],
        total_kobo=total,
        total_naira=naira(total),
        summary=(
            f"{payload.seats} {seat_word} on {route['name']}{when} is "
            f"{_fmt_naira(total)} — {_fmt_naira(route['base_fare_kobo'])} per seat."
        ),
    )


# ══════════════════════════════════════════════════════════════
#  Booking
# ══════════════════════════════════════════════════════════════


@router.post(
    "/bookings",
    response_model=BookingOut,
    status_code=201,
    summary="Create a booking and get a Paystack payment link",
    description=(
        "Holds the seats for 15 minutes and returns `payment_url`. Read that link to "
        "the caller (or send it over WhatsApp/SMS); the booking confirms automatically "
        "once Paystack settles, and the passenger is emailed and texted their QR ticket.\n\n"
        "**Seats are not secured until payment lands.** If the hold lapses the seats "
        "are released and the booking is cancelled.\n\n"
        "For a subscriber with credits, use `/subscriptions/{phone}/book` instead — "
        "that confirms instantly with no payment."
    ),
    responses={
        409: {"description": "Sold out, past the cut-off, or the trip was cancelled"},
    },
)
async def create_booking(payload: CreateBookingRequest) -> BookingOut:
    result = await backend.create_booking(
        trip_id=payload.trip_id,
        passenger_name=payload.passenger_name,
        passenger_phone=payload.passenger_phone,
        passenger_email=payload.passenger_email,
        seats=payload.seats,
        source="agent",
        notes=payload.notes,
    )
    booking = result["booking"]
    payment = result.get("payment") or {}
    log_event(
        logger, logging.INFO, "agent booking created", ref=booking["booking_ref"], seats=payload.seats
    )

    return _booking_out(
        result,
        message=(
            f"Booking {booking['booking_ref']} is held for {payload.seats} "
            f"{'seat' if payload.seats == 1 else 'seats'} at "
            f"{_fmt_naira(booking['amount_kobo'])}. Pay within 15 minutes to confirm: "
            f"{payment.get('authorization_url', '')}"
        ),
    )


@router.get(
    "/bookings/{ref}",
    response_model=BookingOut,
    summary="Look up a booking by reference",
    description="Use when a caller says 'what's happening with EJS-8K3F2?'",
    responses={404: {"description": "No booking with that reference"}},
)
async def get_booking(
    ref: Annotated[str, Path(description="Booking reference, e.g. EJS-8K3F2", examples=["EJS-8K3F2"])],
) -> BookingOut:
    booking = await backend.get_booking(ref)
    status_message = {
        "pending_payment": "Payment hasn't completed yet — the seats are only held briefly.",
        "confirmed": "This booking is confirmed and the QR ticket has been sent.",
        "checked_in": "This passenger has already been checked in at the terminal.",
        "completed": "This trip has been travelled.",
        "cancelled": "This booking was cancelled.",
    }.get(booking["status"], "Booking found.")
    return _booking_out({"booking": booking}, message=status_message)


@router.post(
    "/bookings/{ref}/cancel",
    response_model=SimpleResult,
    summary="Cancel a booking",
    description=(
        "Releases the seats and, for a credit booking, returns the ride credit to the "
        "subscriber's balance. Paid bookings are refunded to the original method."
    ),
    responses={409: {"description": "Already travelled or checked in"}},
)
async def cancel_booking(ref: str, payload: CancelRequest) -> SimpleResult:
    await backend.cancel_booking(ref, reason=payload.reason or "Cancelled via AI agent")
    log_event(logger, logging.INFO, "agent cancelled booking", ref=ref)
    return SimpleResult(
        ok=True,
        booking_ref=ref.upper(),
        message=(
            f"Booking {ref.upper()} has been cancelled and the seats released. "
            "Any payment is refunded to the original method within 3–5 working days."
        ),
    )


@router.post(
    "/tickets/{ref}/resend",
    response_model=SimpleResult,
    summary="Resend the QR ticket",
    description="For 'I can't find my ticket'. Only works on confirmed bookings.",
)
async def resend_ticket(ref: str, payload: ResendRequest) -> SimpleResult:
    channels = [c for c in payload.channels if c in {"email", "sms"}] or ["email", "sms"]
    await backend.resend_ticket(ref, channels)

    if "whatsapp" in payload.channels:
        try:
            booking = await backend.get_booking(ref)
            from app.services.twilio import whatsapp as wa
            from app.whatsapp.flow import build_ticket_message

            text, media = await build_ticket_message(booking)
            await wa.send(booking["passenger_phone"], text, media_url=media)
            channels.append("whatsapp")
        except AppError as exc:
            log_event(logger, logging.WARNING, "whatsapp resend failed", ref=ref, error=exc.message)

    return SimpleResult(
        ok=True,
        booking_ref=ref.upper(),
        message=f"Ticket {ref.upper()} has been resent by {' and '.join(channels)}.",
    )


# ══════════════════════════════════════════════════════════════
#  Subscriptions
# ══════════════════════════════════════════════════════════════


@router.get(
    "/subscriptions/{phone}",
    response_model=SubscriptionOut,
    summary="Check a subscriber's remaining ride credits",
    description=(
        "Call this **before** quoting a price when the caller sounds like a regular — "
        "if `can_book_now` is true, book through `/subscriptions/{phone}/book` and "
        "charge nothing."
    ),
)
async def subscription_lookup(
    phone: Annotated[str, Path(description="Any common Nigerian format", examples=["08090001122"])],
) -> SubscriptionOut:
    try:
        sub = await backend.subscription_by_phone(phone)
    except NotFoundError:
        sub = None

    if not sub:
        return SubscriptionOut(
            found=False,
            phone=phone,
            can_book_now=False,
            message=(
                "That number has no active subscription. Quote the normal fare, or offer "
                f"our plans: Tier 1 at ₦200,000 for 12 rides over 3 months, or Tier 2 at "
                f"₦1,000,000 for 50 rides over a year — {settings.WEB_BASE_URL}/subscriptions"
            ),
        )

    remaining = sub["credits_remaining"]
    expires = sub.get("expires_at")
    return SubscriptionOut(
        found=True,
        phone=phone,
        subscriber_name=sub.get("subscriber_name"),
        plan_name=sub.get("plan_name"),
        credits_total=sub["credits_total"],
        credits_used=sub["credits_used"],
        credits_remaining=remaining,
        expires_at=expires,
        status=sub["status"],
        can_book_now=remaining > 0 and sub["status"] == "active",
        message=(
            f"{sub.get('subscriber_name') or 'This subscriber'} is on {sub.get('plan_name')} "
            f"with {remaining} ride credit{'s' if remaining != 1 else ''} remaining"
            + (f", valid until {_local(expires):%d %B %Y}." if expires else ".")
        ),
    )


@router.post(
    "/subscriptions/{phone}/book",
    response_model=BookingOut,
    status_code=201,
    summary="Book using ride credits — no payment",
    description=(
        "Deducts one credit per seat atomically and confirms immediately. The QR ticket "
        "goes out by email and SMS straight away, so there is no payment link to read out.\n\n"
        "Returns **409** if the balance is short — fall back to `/bookings` in that case."
    ),
    responses={
        404: {"description": "No account registered against that phone number"},
        409: {"description": "Not enough credits, or the departure is sold out"},
    },
)
async def book_with_credits(phone: str, payload: SubscriptionBookingRequest) -> BookingOut:
    result = await backend.create_subscription_booking(
        phone=phone,
        trip_id=payload.trip_id,
        seats=payload.seats,
        passenger_name=payload.passenger_name,
        passenger_email=payload.passenger_email,
        source="agent",
    )
    booking = result["booking"]
    log_event(logger, logging.INFO, "agent credit booking", ref=booking["booking_ref"], phone=phone)

    return _booking_out(
        result,
        message=(
            f"Confirmed. Booking {booking['booking_ref']} used {payload.seats} ride "
            f"{'credit' if payload.seats == 1 else 'credits'} — nothing to pay. "
            "The QR ticket has been emailed and texted to the passenger."
        ),
    )


@router.get(
    "/health",
    summary="Gateway and backend reachability",
    description="Cheap check the agent can call at start-up to confirm it is wired up correctly.",
)
async def agent_health() -> dict:
    try:
        upstream = await backend.health()
        return {"status": "ok", "gateway": "ok", "backend": upstream.get("status", "unknown")}
    except AppError as exc:
        return {"status": "degraded", "gateway": "ok", "backend": "unreachable", "detail": exc.message}
