"""The WhatsApp booking conversation.

A deliberately simple, rule-based state machine — no AI. The ADK agent will
later sit *alongside* this over the same `/agent/v1` endpoints, so this flow
stays as the always-works fallback.

    idle ──MENU──▶ choosing_route ──▶ choosing_date ──▶ choosing_trip
         ──▶ choosing_seats ──▶ collecting_name ──▶ awaiting_payment ──▶ idle

Global commands work in any state: MENU, HELP, CANCEL, MY BOOKING <ref>.
Everything is forgiving: numbers, partial names and common typos all resolve,
and anything unrecognised re-prompts with the options rather than dead-ending.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.errors import AppError
from app.services.backend import backend

logger = logging.getLogger("ecojindu.api.whatsapp")

LAGOS = ZoneInfo("Africa/Lagos")

# ── States ───────────────────────────────────────────────────
IDLE = "idle"
CHOOSING_ROUTE = "choosing_route"
CHOOSING_DATE = "choosing_date"
CHOOSING_TRIP = "choosing_trip"
CHOOSING_SEATS = "choosing_seats"
COLLECTING_NAME = "collecting_name"
COLLECTING_EMAIL = "collecting_email"
AWAITING_PAYMENT = "awaiting_payment"
CONFIRMING_CANCEL = "confirming_cancel"


@dataclass
class Reply:
    """What the flow decided to say back."""

    text: str
    state: str
    context: dict = field(default_factory=dict)
    media_url: str | None = None


def naira(kobo: int | None) -> str:
    return "₦{:,.0f}".format((kobo or 0) / 100)


def _fmt_dt(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LAGOS)
    return dt.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _fmt_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LAGOS)
    return dt.strftime("%I:%M %p").lstrip("0")


def _today() -> date:
    return datetime.now(LAGOS).date()


# ── Copy ─────────────────────────────────────────────────────

GREETING = (
    "🌿 *Ecojindu Shuttle*\n"
    "_Bridging Cities, Powering Green Mobility_\n\n"
    "Welcome! I can book you a seat on our zero-emission airport shuttle."
)

HELP_TEXT = (
    "🌿 *Ecojindu Shuttle — Help*\n\n"
    "*What I can do*\n"
    "• Book a seat on any scheduled departure\n"
    "• Check an existing booking\n"
    "• Cancel a booking\n\n"
    "*Commands — type these any time*\n"
    "*MENU* — start a new booking\n"
    "*MY BOOKING EJS-XXXXX* — check a booking\n"
    "*CANCEL* — stop what we're doing\n"
    "*HELP* — show this message\n\n"
    f"Prefer a human? Call {settings.COMPANY_PHONE}.\n"
    f"Or book online: {settings.WEB_BASE_URL}"
)


def _fallback(message: str) -> str:
    return f"{message}\n\n_Type *MENU* to start over or *HELP* for options._"


# ── Input parsing ────────────────────────────────────────────

_DATE_WORDS = {
    "today": 0, "tod": 0,
    "tomorrow": 1, "tmrw": 1, "tomorow": 1, "tomorrw": 1, "2moro": 1, "tmr": 1,
    "day after tomorrow": 2, "overmorrow": 2,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "a": 1, "an": 1, "single": 1, "couple": 2, "pair": 2,
}


def parse_date(text: str) -> date | None:
    """Understand 'today', 'tmrw', 'friday', '14/08', '2026-08-14', '14 aug'."""
    cleaned = text.strip().lower()
    today = _today()

    if cleaned in _DATE_WORDS:
        return today + timedelta(days=_DATE_WORDS[cleaned])

    for word, offset in _WEEKDAYS.items():
        if cleaned == word or cleaned == f"next {word}":
            ahead = (offset - today.weekday()) % 7
            if ahead == 0 or cleaned.startswith("next"):
                ahead = ahead or 7
            return today + timedelta(days=ahead)

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d %b", "%d %B", "%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        if parsed.year == 1900:  # day/month only — assume the next occurrence
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        return parsed

    # Bare "14" or "14/08" meaning day (of this or next month).
    m = re.fullmatch(r"(\d{1,2})(?:[/\-](\d{1,2}))?", cleaned)
    if m:
        day = int(m.group(1))
        month = int(m.group(2)) if m.group(2) else today.month
        if 1 <= day <= 31 and 1 <= month <= 12:
            year = today.year
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            if candidate < today:
                try:
                    candidate = date(year + 1, month, day)
                except ValueError:
                    return None
            return candidate
    return None


def parse_count(text: str, maximum: int) -> int | None:
    """Digits win; otherwise look for a number word anywhere ('a couple of seats')."""
    cleaned = text.strip().lower()

    m = re.search(r"\d+", cleaned)
    if m:
        value = int(m.group())
        return value if 1 <= value <= maximum else None

    if cleaned in _NUMBER_WORDS:
        value = _NUMBER_WORDS[cleaned]
    else:
        words = re.findall(r"[a-z]+", cleaned)
        # "a"/"an" are only a count when they're the whole message, otherwise
        # "a seat for my wife and I" would silently book one.
        value = next(
            (_NUMBER_WORDS[w] for w in words if w in _NUMBER_WORDS and w not in {"a", "an"}),
            None,
        )
        if value is None:
            return None

    return value if 1 <= value <= maximum else None


def parse_choice(text: str, count: int) -> int | None:
    """A 1-based menu pick, tolerating '2.', 'option 2', '#2'."""
    m = re.search(r"\d+", text.strip())
    if not m:
        return None
    value = int(m.group())
    return value - 1 if 1 <= value <= count else None


def looks_like_name(text: str) -> bool:
    cleaned = text.strip()
    return 2 <= len(cleaned) <= 80 and bool(re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", cleaned))


def parse_email(text: str) -> str | None:
    cleaned = text.strip()
    if cleaned.lower() in {"skip", "no", "none", "-", "nil"}:
        return None
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", cleaned):
        return cleaned.lower()
    return "invalid"


BOOKING_REF_RE = re.compile(r"\b(EJS[- ]?[A-Z0-9]{4,6})\b", re.IGNORECASE)


def extract_booking_ref(text: str) -> str | None:
    m = BOOKING_REF_RE.search(text)
    if not m:
        return None
    ref = m.group(1).upper().replace(" ", "-")
    return ref if ref.startswith("EJS-") else "EJS-" + ref.removeprefix("EJS")


# ── Screen builders ──────────────────────────────────────────


async def _route_menu(context: dict) -> Reply:
    routes = [r for r in await backend.list_routes() if r.get("is_active", True)]
    if not routes:
        return Reply(
            _fallback("No routes are published right now. Please try again shortly."), IDLE, {}
        )

    lines = [GREETING, "", "*Where are you travelling?*", ""]
    for i, r in enumerate(routes, 1):
        lines.append(f"*{i}.* {r['origin_terminal'].split(',')[0]} → {r['destination'].split(',')[0]}")
        lines.append(f"     {naira(r['base_fare_kobo'])} per seat · {r['duration_mins']} mins")
    lines += ["", "_Reply with a number (e.g. *1*)._"]

    context = {**context, "routes": [{"id": r["id"], "name": r["name"], "fare": r["base_fare_kobo"]} for r in routes]}
    return Reply("\n".join(lines), CHOOSING_ROUTE, context)


def _date_prompt(route_name: str, context: dict) -> Reply:
    today = _today()
    return Reply(
        f"✅ *{route_name}*\n\n"
        "*What date do you want to travel?*\n\n"
        f"• *today* ({today:%a %d %b})\n"
        f"• *tomorrow* ({today + timedelta(days=1):%a %d %b})\n"
        "• a weekday, e.g. *friday*\n"
        f"• or a date, e.g. *{today + timedelta(days=3):%d/%m/%Y}*",
        CHOOSING_DATE,
        context,
    )


async def _trip_menu(route_id: str, service_date: date, context: dict) -> Reply:
    trips = await backend.list_trips(route_id=route_id, service_date=service_date.isoformat(), seats=1)
    bookable = [t for t in trips if t.get("is_bookable")]

    if not bookable:
        return Reply(
            f"😕 No seats left on *{service_date:%a %d %b}*.\n\n"
            "Try another date — just reply with one (e.g. *tomorrow*), "
            "or type *MENU* to change route.",
            CHOOSING_DATE,
            context,
        )

    lines = [f"🚐 *Departures — {service_date:%a %d %b %Y}*", ""]
    for i, t in enumerate(bookable, 1):
        left = t["seats_available"]
        badge = "🟢" if left > 6 else ("🟡" if left > 2 else "🔴")
        lines.append(f"*{i}.* {_fmt_time(t['departure_datetime'])}  ·  {naira(t['fare_kobo'])}")
        lines.append(f"     {badge} {left} seat{'s' if left != 1 else ''} left")
    lines += ["", "_Reply with a number to pick a departure._"]

    context = {
        **context,
        "service_date": service_date.isoformat(),
        "trips": [
            {
                "id": t["id"],
                "departure": t["departure_datetime"],
                "fare": t["fare_kobo"],
                "seats_available": t["seats_available"],
            }
            for t in bookable
        ],
    }
    return Reply("\n".join(lines), CHOOSING_TRIP, context)


async def _booking_status_reply(ref: str) -> str:
    try:
        booking = await backend.get_booking(ref)
    except AppError:
        return _fallback(
            f"I couldn't find booking *{ref}*.\n\n"
            "Double-check the reference — it looks like *EJS-8K3F2*."
        )

    trip = booking.get("trip") or {}
    status_label = {
        "pending_payment": "⏳ Awaiting payment",
        "confirmed": "✅ Confirmed",
        "checked_in": "🎫 Checked in",
        "completed": "🏁 Completed",
        "cancelled": "❌ Cancelled",
    }.get(booking["status"], booking["status"])

    lines = [
        f"*Booking {booking['booking_ref']}*",
        "",
        f"Status: {status_label}",
        f"Passenger: {booking['passenger_name']}",
        f"Route: {trip.get('route_name', '—')}",
        f"Departs: {_fmt_dt(trip['departure_datetime']) if trip.get('departure_datetime') else '—'}",
        f"Seats: {booking['seats']}"
        + (f" ({', '.join(booking['seat_numbers'])})" if booking.get("seat_numbers") else ""),
        f"Amount: {naira(booking['amount_kobo']) if booking['amount_kobo'] else 'Paid with ride credits'}",
    ]
    if booking["status"] == "confirmed":
        lines += ["", "Your QR ticket was emailed and texted to you.", "Arrive 20 minutes before departure."]
    elif booking["status"] == "pending_payment":
        lines += ["", "⚠️ Payment isn't complete — your seats are only held for a short while."]
    lines += ["", "_Type *MENU* to make another booking._"]
    return "\n".join(lines)


# ── Main entry point ─────────────────────────────────────────


async def handle_message(phone: str, text: str, state: str, context: dict) -> Reply:
    """Advance the conversation by one message.

    `phone` is E.164 without the `whatsapp:` prefix. Never raises — any upstream
    failure is turned into a friendly message so the passenger is never stranded.
    """
    body = (text or "").strip()
    upper = body.upper()
    context = dict(context or {})

    # ── Global commands, valid in every state ──
    if upper in {"HELP", "?", "INFO", "HLP"}:
        return Reply(HELP_TEXT, state, context)

    if upper in {"MENU", "START", "HI", "HELLO", "HEY", "BOOK", "RESTART", "0", "GOOD MORNING", "GOOD AFTERNOON", "GOOD EVENING"}:
        return await _route_menu({})

    ref_in_message = extract_booking_ref(body)
    if upper.startswith("MY BOOKING") or upper.startswith("STATUS") or (ref_in_message and state in {IDLE, AWAITING_PAYMENT}):
        if not ref_in_message:
            return Reply(
                "Send the booking reference like this:\n*MY BOOKING EJS-8K3F2*", state, context
            )
        return Reply(await _booking_status_reply(ref_in_message), IDLE, {})

    if upper == "CANCEL":
        if context.get("booking_ref"):
            return Reply(
                f"Cancel booking *{context['booking_ref']}*?\n\n"
                "Reply *YES* to cancel it, or *NO* to keep it.",
                CONFIRMING_CANCEL,
                context,
            )
        return Reply(
            "No problem — I've cleared that.\n\n_Type *MENU* whenever you're ready to book._",
            IDLE,
            {},
        )

    # ── State machine ──
    try:
        if state == IDLE:
            return await _route_menu({})

        if state == CHOOSING_ROUTE:
            routes = context.get("routes", [])
            index = parse_choice(body, len(routes))
            if index is None:
                return Reply(
                    f"Please reply with a number between *1* and *{len(routes)}*.",
                    CHOOSING_ROUTE,
                    context,
                )
            chosen = routes[index]
            context.update(route_id=chosen["id"], route_name=chosen["name"], fare=chosen["fare"])
            return _date_prompt(chosen["name"], context)

        if state == CHOOSING_DATE:
            travel_date = parse_date(body)
            if travel_date is None:
                return Reply(
                    "I didn't catch that date. Try *today*, *tomorrow*, *friday*, "
                    f"or *{(_today() + timedelta(days=2)):%d/%m/%Y}*.",
                    CHOOSING_DATE,
                    context,
                )
            if travel_date < _today():
                return Reply("That date has already passed. Which day would you like?", CHOOSING_DATE, context)
            if travel_date > _today() + timedelta(days=60):
                return Reply(
                    "We only publish departures 60 days ahead. Please pick an earlier date.",
                    CHOOSING_DATE,
                    context,
                )
            return await _trip_menu(context["route_id"], travel_date, context)

        if state == CHOOSING_TRIP:
            trips = context.get("trips", [])
            index = parse_choice(body, len(trips))
            if index is None:
                travel_date = parse_date(body)
                if travel_date and travel_date >= _today():
                    return await _trip_menu(context["route_id"], travel_date, context)
                return Reply(
                    f"Please reply with a number between *1* and *{len(trips)}* to pick a departure.",
                    CHOOSING_TRIP,
                    context,
                )
            chosen = trips[index]
            context.update(
                trip_id=chosen["id"],
                departure=chosen["departure"],
                fare=chosen["fare"],
                seats_available=chosen["seats_available"],
            )
            cap = min(chosen["seats_available"], settings.WHATSAPP_MAX_SEATS)
            return Reply(
                f"🕐 *{_fmt_dt(chosen['departure'])}*\n"
                f"{naira(chosen['fare'])} per seat\n\n"
                f"*How many seats do you need?* (1–{cap})",
                CHOOSING_SEATS,
                context,
            )

        if state == CHOOSING_SEATS:
            cap = min(context.get("seats_available", 1), settings.WHATSAPP_MAX_SEATS)
            seats = parse_count(body, cap)
            if seats is None:
                return Reply(
                    f"Please reply with a number between *1* and *{cap}*.", CHOOSING_SEATS, context
                )
            context["seats"] = seats
            return Reply(
                f"👤 *What name should the ticket be in?*\n\n"
                "_Please send the passenger's full name._",
                COLLECTING_NAME,
                context,
            )

        if state == COLLECTING_NAME:
            if not looks_like_name(body):
                return Reply(
                    "That doesn't look like a name. Please send the passenger's full name, "
                    "e.g. *Chinedu Okafor*.",
                    COLLECTING_NAME,
                    context,
                )
            context["passenger_name"] = body.strip().title()
            return Reply(
                "📧 *What email should we send the ticket to?*\n\n"
                "_Reply *SKIP* if you'd rather only get it here and by SMS._",
                COLLECTING_EMAIL,
                context,
            )

        if state == COLLECTING_EMAIL:
            email = parse_email(body)
            if email == "invalid":
                return Reply(
                    "That email doesn't look right. Please send it again, or reply *SKIP*.",
                    COLLECTING_EMAIL,
                    context,
                )
            context["passenger_email"] = email
            return await _create_booking(phone, context)

        if state == AWAITING_PAYMENT:
            ref = context.get("booking_ref")
            if ref:
                return Reply(await _booking_status_reply(ref), AWAITING_PAYMENT, context)
            return await _route_menu({})

        if state == CONFIRMING_CANCEL:
            if upper in {"YES", "Y", "YEAH", "YEP", "CONFIRM", "OK", "OKAY"}:
                ref = context.get("booking_ref")
                try:
                    await backend.cancel_booking(ref, reason="Cancelled by passenger on WhatsApp")
                    return Reply(
                        f"❌ Booking *{ref}* has been cancelled.\n\n"
                        "Any payment will be refunded to your original method within 3–5 working days.\n\n"
                        "_Type *MENU* to book again._",
                        IDLE,
                        {},
                    )
                except AppError as exc:
                    return Reply(_fallback(f"I couldn't cancel that: {exc.message}"), IDLE, {})
            return Reply(
                "👍 Kept. Your booking is unchanged.\n\n_Type *MENU* to book another trip._",
                IDLE,
                context,
            )

    except AppError as exc:
        logger.warning("flow error for %s: %s", phone, exc.message)
        return Reply(_fallback(f"⚠️ {exc.message}"), IDLE, {})
    except Exception:  # noqa: BLE001 - a bug must not strand the passenger
        logger.exception("unexpected flow error for %s", phone)
        return Reply(
            _fallback("⚠️ Something went wrong on our side. Nothing was charged."), IDLE, {}
        )

    # Unknown state — reset rather than loop.
    return await _route_menu({})


async def _create_booking(phone: str, context: dict) -> Reply:
    """Place the hold and quote the fare with a payment link.

    Subscribers with credits skip payment entirely.
    """
    seats = context["seats"]
    name = context["passenger_name"]
    email = context.get("passenger_email")

    subscription = None
    try:
        subscription = await backend.subscription_by_phone(phone)
    except AppError:
        subscription = None

    if subscription and subscription.get("credits_remaining", 0) >= seats:
        result = await backend.create_subscription_booking(
            phone=phone,
            trip_id=context["trip_id"],
            seats=seats,
            passenger_name=name,
            passenger_email=email,
            source="whatsapp",
        )
        booking = result["booking"]
        remaining = subscription["credits_remaining"] - seats
        return Reply(
            f"✅ *Booked with your ride credits*\n\n"
            f"Reference: *{booking['booking_ref']}*\n"
            f"{context['route_name']}\n"
            f"{_fmt_dt(context['departure'])}\n"
            f"Seats: {seats} ({', '.join(booking['seat_numbers'])})\n"
            f"Credits left: *{remaining}*\n\n"
            "Your QR ticket is attached — show it at the terminal. "
            "We've also emailed and texted it to you.\n\n"
            "_Arrive 20 minutes before departure._",
            IDLE,
            {"booking_ref": booking["booking_ref"]},
            media_url=backend.ticket_image_url(booking["booking_ref"]),
        )

    result = await backend.create_booking(
        trip_id=context["trip_id"],
        passenger_name=name,
        passenger_phone=phone,
        passenger_email=email,
        seats=seats,
        source="whatsapp",
    )
    booking = result["booking"]
    payment = result.get("payment") or {}
    total = booking["amount_kobo"]

    context.update(
        booking_ref=booking["booking_ref"],
        payment_reference=payment.get("reference"),
        amount_kobo=total,
    )

    return Reply(
        f"🧾 *Almost there — here's your quote*\n\n"
        f"Reference: *{booking['booking_ref']}*\n"
        f"{context['route_name']}\n"
        f"{_fmt_dt(context['departure'])}\n"
        f"Passenger: {name}\n"
        f"Seats: {seats} × {naira(context['fare'])}\n"
        f"*Total: {naira(total)}*\n\n"
        f"💳 Pay securely here:\n{payment.get('authorization_url', '')}\n\n"
        f"⏱️ Your seats are held for *15 minutes*. Once payment lands I'll send your "
        "QR ticket right here.\n\n"
        "_Type *CANCEL* to release the seats._",
        AWAITING_PAYMENT,
        context,
    )


async def build_ticket_message(booking: dict) -> tuple[str, str]:
    """Copy + media URL for the ticket WhatsApp sent after payment settles."""
    trip = booking.get("trip") or {}
    seat_labels = ", ".join(booking.get("seat_numbers") or [])
    text = (
        f"🎫 *Payment confirmed — you're on board!*\n\n"
        f"Reference: *{booking['booking_ref']}*\n"
        f"{trip.get('route_name', '')}\n"
        f"{_fmt_dt(trip['departure_datetime']) if trip.get('departure_datetime') else ''}\n"
        f"Passenger: {booking['passenger_name']}\n"
        f"Seats: {booking['seats']}" + (f" ({seat_labels})" if seat_labels else "") + "\n"
        f"Paid: {naira(booking['amount_kobo'])}\n\n"
        "📎 Your QR boarding pass is attached — show it at the terminal gate.\n"
        "We've also emailed and texted it to you.\n\n"
        "🕒 Please arrive *20 minutes* before departure.\n"
        f"Need help? Call {settings.COMPANY_PHONE}."
    )
    return text, backend.ticket_image_url(booking["booking_ref"])
