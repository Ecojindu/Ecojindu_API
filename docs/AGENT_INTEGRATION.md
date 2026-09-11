# Ecojindu Shuttle — AI Agent Integration Guide

**For the Google ADK agent.** Everything you need to book a passenger onto an
Ecojindu Shuttle departure, end to end, without asking anyone a question.

* **Base URL (local):** `http://localhost:8001`
* **Base URL (production):** `https://api.ecojindu.ng` *(set by ops at deploy time)*
* **Interactive reference:** `<base>/docs` — live, try-it-out OpenAPI
* **Machine-readable spec:** `<base>/openapi.json`

---

## 1. Authentication

Every `/agent/v1/**` request needs an API key in the **`X-API-Key`** header.

```http
GET /agent/v1/routes HTTP/1.1
Host: localhost:8001
X-API-Key: dev-agent-key-change-me
```

```python
import httpx

client = httpx.AsyncClient(
    base_url="http://localhost:8001/agent/v1",
    headers={"X-API-Key": AGENT_API_KEY},
    timeout=30,
)
```

Keys live in the gateway's `AGENT_API_KEYS` env var (comma-separated), so you get
your own key and it can be rotated without touching anyone else's.

**Health check at start-up** — cheap, and tells you whether the backend behind the
gateway is reachable too:

```
GET /agent/v1/health  →  {"status":"ok","gateway":"ok","backend":"ok"}
```

**Rate limit:** 120 requests/minute per key by default. Exceeding it returns `429`
with `error.details.retry_after_seconds`.

---

## 2. Conventions you need to know

| Thing | Rule |
|---|---|
| **Money** | Always **kobo**. ₦15,000 is `1500000`. Every money field also has a `_naira` twin (`fare_naira: 15000.0`) so you never have to divide. |
| **Time** | ISO-8601 with offset. Local time is **Africa/Lagos** (UTC+1, no DST). `departure_time` is a plain local `"09:00"` for reading aloud. |
| **Phone numbers** | Send them however the caller says them — `08154471570`, `8154471570`, `+234 815 447 1570` all work. Normalised server-side to E.164. |
| **Booking reference** | `EJS-XXXXX`, using an alphabet with no `0`/`O`/`1`/`I` so it survives being read over a phone line. Case-insensitive on input; the `EJS-` prefix is added if you omit it. |
| **`message` / `summary`** | Every response has one. It is written to be **said or sent verbatim** — don't compose your own prose from raw fields. |

### Error shape

Every failure — 4xx and 5xx alike — returns:

```json
{
  "error": {
    "code": "seats_unavailable",
    "message": "Only 1 seat(s) left on that departure.",
    "details": { "seats_available": 1, "seats_requested": 3 },
    "request_id": "a1b2c3d4e5f6"
  }
}
```

`error.message` is always passenger-safe — relay it directly. Quote `request_id`
when reporting a problem to the backend team.

| Status | `code` | What to do |
|---|---|---|
| 401 | `missing_api_key`, `invalid_api_key` | Your key is wrong — don't retry |
| 404 | `not_found` | Reference or trip doesn't exist; ask the caller to re-read it |
| 409 | `seats_unavailable` | Offer another departure from `/availability` |
| 409 | `insufficient_credits` | Fall back to a paid booking via `POST /bookings` |
| 409 | `conflict` | Sold out, past the cut-off, or already travelled — read the message |
| 422 | `validation_error` | Bad input; `details.fields[]` says which |
| 429 | `rate_limited` | Back off `details.retry_after_seconds` |
| 502 | `upstream_error` | Booking service is down. Say so and offer the +234 815 447 1570 line |

---

## 3. The endpoints

### 3.1 `GET /agent/v1/routes`

Every active route with its fare and pickup points. Call this first to turn a spoken
place name into a `route_id`.

```bash
curl -s http://localhost:8001/agent/v1/routes -H "X-API-Key: $KEY"
```

```json
[
  {
    "route_id": "8f14e45f-…",
    "name": "Umuahia → Sam Mbakwe Airport",
    "code": "UMU-OWA",
    "origin": "Nnenna Otti Bus Terminal, Umuahia",
    "destination": "Sam Mbakwe International Cargo Airport, Owerri",
    "distance_km": 72.0,
    "duration_mins": 90,
    "base_fare_kobo": 1500000,
    "base_fare_naira": 15000.0,
    "pickup_points": [
      "Nnenna Otti Bus Terminal, Umuahia",
      "Umuahia Tower Junction",
      "Umuahia–Owerri Road / Olokoro",
      "Naze Junction, Owerri"
    ]
  }
]
```

There are four routes: Umuahia ⇄ Airport and Aba ⇄ Airport, each direction its own
route. Match on `origin` / `destination` / `code`, never on array position.

---

### 3.2 `GET /agent/v1/availability`

Bookable departures with live seat counts, soonest first.

| Query param | Type | Notes |
|---|---|---|
| `route_id` | uuid | From `/routes`. Required unless you pass `origin`/`destination` |
| `date` | `YYYY-MM-DD` | Africa/Lagos. Omit to see everything upcoming |
| `seats` | int 1–14 | Only return departures with room for this many. Default 1 |
| `origin` | string | Partial match, e.g. `Umuahia` |
| `destination` | string | Partial match, e.g. `Airport` |

Sold-out, cancelled and past-cut-off departures are filtered out — everything you get
back is bookable right now.

```bash
curl -s "http://localhost:8001/agent/v1/availability?route_id=$RID&date=2026-08-14&seats=2" \
  -H "X-API-Key: $KEY"
```

```json
[
  {
    "trip_id": "3f2c8d1a-…",
    "route_id": "8f14e45f-…",
    "route_name": "Umuahia → Sam Mbakwe Airport",
    "origin": "Nnenna Otti Bus Terminal, Umuahia",
    "destination": "Sam Mbakwe International Cargo Airport, Owerri",
    "service_date": "2026-08-14",
    "departure_time": "09:00",
    "departure_datetime": "2026-08-14T09:00:00+01:00",
    "duration_mins": 90,
    "seats_available": 11,
    "seats_total": 14,
    "fare_kobo": 1500000,
    "fare_naira": 15000.0,
    "status": "scheduled",
    "is_bookable": true
  }
]
```

An empty array means nothing is available that day — offer the next day.

**Timetable:** 06:00, 09:00, 12:00 and 15:00 daily, each direction. Trips are
published 14 days ahead.

---

### 3.3 `POST /agent/v1/quote`

Price a journey before committing. Two modes:

```jsonc
// Preferred — exact departure, reports seats left
{ "trip_id": "3f2c8d1a-…", "seats": 2 }

// Indicative — caller hasn't picked a time yet
{ "route_id": "8f14e45f-…", "service_date": "2026-08-14", "seats": 2 }
```

```json
{
  "trip_id": "3f2c8d1a-…",
  "route_name": "Umuahia → Sam Mbakwe Airport",
  "departure_datetime": "2026-08-14T09:00:00+01:00",
  "seats": 2,
  "fare_per_seat_kobo": 1500000,
  "total_kobo": 3000000,
  "total_naira": 30000.0,
  "currency": "NGN",
  "seats_available": 11,
  "summary": "2 seats on the Friday 14 August at 9:00 AM departure from Nnenna Otti Bus Terminal to Sam Mbakwe International Cargo Airport comes to ₦30,000. 11 seats are still available."
}
```

Read `summary` out. It already handles singular/plural and currency formatting.

---

### 3.4 `POST /agent/v1/bookings`

Create a booking and get a payment link.

```json
{
  "trip_id": "3f2c8d1a-…",
  "passenger_name": "Chinedu Okafor",
  "passenger_phone": "08154471570",
  "passenger_email": "chinedu@example.com",
  "seats": 2,
  "notes": "Booked by phone agent"
}
```

`passenger_email` and `notes` are optional. `201 Created`:

```json
{
  "booking_ref": "EJS-8K3F2",
  "status": "pending_payment",
  "passenger_name": "Chinedu Okafor",
  "passenger_phone": "+2348154471570",
  "seats": 2,
  "seat_numbers": ["4", "5"],
  "amount_kobo": 3000000,
  "amount_naira": 30000.0,
  "route_name": "Umuahia → Sam Mbakwe Airport",
  "departure_datetime": "2026-08-14T09:00:00+01:00",
  "payment_url": "https://checkout.paystack.com/abc123xyz",
  "payment_reference": "EJSBK-3F9A21…",
  "hold_expires_at": "2026-08-12T14:15:00+00:00",
  "ticket_url": null,
  "paid_with_credits": false,
  "message": "Booking EJS-8K3F2 is held for 2 seats at ₦30,000. Pay within 15 minutes to confirm: https://checkout.paystack.com/abc123xyz"
}
```

> ### ⚠️ The single most important thing to get right
>
> **The seats are held, not sold.** `status` is `pending_payment` and the hold expires
> at `hold_expires_at` (15 minutes). If payment doesn't land by then, the seats go
> back on sale and the booking is cancelled.
>
> Tell the caller their seats are held for 15 minutes and send them `payment_url`.
> Once Paystack settles, everything else is automatic: the booking confirms, the QR
> ticket is issued, and it goes out by email and SMS. **You don't poll and you don't
> confirm anything.** If you want to check, `GET /agent/v1/bookings/{ref}` and look
> for `status: "confirmed"`.

---

### 3.5 `GET /agent/v1/bookings/{ref}`

Status lookup. `ref` is case-insensitive; `8K3F2` and `EJS-8K3F2` both work.

```json
{
  "booking_ref": "EJS-8K3F2",
  "status": "confirmed",
  "passenger_name": "Chinedu Okafor",
  "seats": 2,
  "seat_numbers": ["4", "5"],
  "amount_naira": 30000.0,
  "route_name": "Umuahia → Sam Mbakwe Airport",
  "departure_datetime": "2026-08-14T09:00:00+01:00",
  "ticket_url": "https://api.ecojindu.ng/v1/tickets/EJS-8K3F2/qr.png",
  "payment_url": null,
  "message": "This booking is confirmed and the QR ticket has been sent."
}
```

| `status` | Means |
|---|---|
| `pending_payment` | Seats held, awaiting payment |
| `confirmed` | Paid, QR ticket issued and sent |
| `checked_in` | Scanned at the terminal gate |
| `completed` | Trip travelled |
| `cancelled` | Released — by the passenger, ops, or an expired hold |

---

### 3.6 `POST /agent/v1/bookings/{ref}/cancel`

```json
{ "reason": "Caller's flight was moved" }
```

Releases the seats. A credit booking returns its ride credit to the subscriber's
balance; a paid booking is refunded to the original method. Returns `409` if the
passenger has already checked in or travelled.

---

### 3.7 `GET /agent/v1/subscriptions/{phone}`

**Call this early** whenever the caller sounds like a regular — it changes the whole
conversation. If `can_book_now` is `true`, there is nothing to pay.

```json
{
  "found": true,
  "phone": "08090001122",
  "subscriber_name": "Amaka Obi",
  "plan_name": "Ecojindu Commuter — Tier 1",
  "credits_total": 12,
  "credits_used": 2,
  "credits_remaining": 10,
  "expires_at": "2026-11-10T00:00:00+01:00",
  "status": "active",
  "can_book_now": true,
  "message": "Amaka Obi is on Ecojindu Commuter — Tier 1 with 10 ride credits remaining, valid until 10 November 2026."
}
```

When `found` is `false`, the `message` already contains the upsell copy for both tiers:

* **Tier 1** — ₦200,000 · 12 rides · 3 months
* **Tier 2** — ₦1,000,000 · 50 rides · 12 months

---

### 3.8 `POST /agent/v1/subscriptions/{phone}/book`

Book with ride credits. **No payment, no link to read out.**

```json
{ "trip_id": "3f2c8d1a-…", "seats": 1, "passenger_name": "Amaka Obi" }
```

`201 Created` — already confirmed:

```json
{
  "booking_ref": "EJS-P4M2X",
  "status": "confirmed",
  "amount_kobo": 0,
  "amount_naira": 0.0,
  "paid_with_credits": true,
  "ticket_url": "https://api.ecojindu.ng/v1/tickets/EJS-P4M2X/qr.png",
  "payment_url": null,
  "message": "Confirmed. Booking EJS-P4M2X used 1 ride credit — nothing to pay. The QR ticket has been emailed and texted to the passenger."
}
```

One credit is deducted per seat, atomically — two simultaneous requests for the last
credit cannot both win. On `409 insufficient_credits`, fall back to `POST /bookings`.

---

### 3.9 `POST /agent/v1/tickets/{ref}/resend`

For "I can't find my ticket".

```json
{ "channels": ["email", "sms", "whatsapp"] }
```

Defaults to `["email", "sms"]`. `whatsapp` sends the QR image into the passenger's
WhatsApp thread. Only works on confirmed bookings.

---

## 4. Worked flows

### 4.1 Standard paid booking

```
Caller: "I need to get to the airport from Umuahia on Friday, two of us."

1. GET  /agent/v1/routes
        → match origin "Umuahia", destination "Airport" → route_id

2. GET  /agent/v1/subscriptions/{caller_phone}
        → found: false  ⇒ this is a paid booking

3. GET  /agent/v1/availability?route_id={route_id}&date=2026-08-14&seats=2
        → offer the times: "We have 6am, 9am, midday and 3pm."

   Caller: "9am."

4. POST /agent/v1/quote  {"trip_id": "...", "seats": 2}
        → say `summary` verbatim: "…comes to ₦30,000. 11 seats are still available."

   Caller: "Go ahead."

5. POST /agent/v1/bookings
        {"trip_id":"...","passenger_name":"Chinedu Okafor",
         "passenger_phone":"08154471570","passenger_email":"chinedu@example.com","seats":2}
        → booking_ref EJS-8K3F2, payment_url

6. Say: "You're EJS-8K3F2, ₦30,000 for two seats. I've held them for 15 minutes —
         I'm sending the payment link now. Your QR ticket arrives by email and SMS
         the moment it goes through."

   (Nothing further to do. Confirmation, ticket and notifications are automatic.)
```

### 4.2 Subscriber — no payment at all

```
1. GET  /agent/v1/subscriptions/08090001122
        → can_book_now: true, credits_remaining: 10

   Say: "You've got 10 rides left on your Commuter plan — this one's covered."

2. GET  /agent/v1/availability?route_id={route_id}&date=2026-08-14
3. POST /agent/v1/subscriptions/08090001122/book  {"trip_id": "...", "seats": 1}
        → status "confirmed" immediately, ticket already sent

   Say the `message` verbatim.
```

### 4.3 Sold out — recover gracefully

```
POST /agent/v1/bookings → 409
{"error":{"code":"seats_unavailable",
          "message":"Only 1 seat(s) left on that departure.",
          "details":{"seats_available":1,"seats_requested":3}}}

→ Say the message, then re-query availability for the same day and offer the
  alternatives. Never retry the same booking blindly.
```

### 4.4 Status check

```
Caller: "It's E-J-S dash 8-K-3-F-2."
GET /agent/v1/bookings/EJS-8K3F2  →  say `message`, then the departure details.
```

---

## 5. Suggested ADK tool definitions

Nine endpoints, nine tools. Names that map to caller intent:

| Tool | Endpoint | When |
|---|---|---|
| `list_routes` | `GET /routes` | Caller names a place |
| `check_availability` | `GET /availability` | Caller names a date |
| `get_fare_quote` | `POST /quote` | Before asking for a decision |
| `create_booking` | `POST /bookings` | Caller says yes, no credits |
| `get_booking_status` | `GET /bookings/{ref}` | Caller gives a reference |
| `cancel_booking` | `POST /bookings/{ref}/cancel` | Caller wants out |
| `check_subscription` | `GET /subscriptions/{phone}` | **Early, always** |
| `book_with_credits` | `POST /subscriptions/{phone}/book` | `can_book_now` is true |
| `resend_ticket` | `POST /tickets/{ref}/resend` | "I lost my ticket" |

### Prompt guidance worth encoding

1. **Check the subscription first.** It's one call and it can remove the entire
   payment conversation.
2. **Never invent a fare.** Always `/quote` — fares can be overridden per departure.
3. **Never promise a seat is secured** before `status` is `confirmed`. Say "held for
   15 minutes".
4. **Read `message` and `summary` verbatim.** They handle plurals, currency and dates.
5. **Relay `error.message` as-is.** It is already written for passengers.
6. **Read references back phonetically** — "E-J-S dash 8-K-3-F-2".
7. **Escalate on 502.** Give the human line: +234 815 447 1570.

### Minimal tool implementation

```python
import httpx
from typing import Any

BASE = "http://localhost:8001/agent/v1"
HEADERS = {"X-API-Key": AGENT_API_KEY}


async def _call(method: str, path: str, **kw) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=30) as c:
        r = await c.request(method, path, **kw)
    body = r.json()
    if r.status_code >= 400:
        err = body["error"]
        # Surface the passenger-safe message; let the model decide how to recover.
        return {"ok": False, "code": err["code"], "message": err["message"],
                "details": err.get("details", {})}
    return {"ok": True, **(body if isinstance(body, dict) else {"items": body})}


async def check_subscription(phone: str) -> dict:
    """Check whether a caller has prepaid ride credits. Call this early."""
    return await _call("GET", f"/subscriptions/{phone}")


async def check_availability(route_id: str, date: str, seats: int = 1) -> dict:
    """List bookable departures on a route for a date (YYYY-MM-DD)."""
    return await _call("GET", "/availability",
                       params={"route_id": route_id, "date": date, "seats": seats})


async def create_booking(trip_id: str, passenger_name: str, passenger_phone: str,
                         seats: int = 1, passenger_email: str | None = None) -> dict:
    """Hold seats and get a Paystack payment link. Seats are NOT secured until paid."""
    return await _call("POST", "/bookings", json={
        "trip_id": trip_id, "passenger_name": passenger_name,
        "passenger_phone": passenger_phone, "seats": seats,
        "passenger_email": passenger_email,
    })
```

---

## 6. Testing locally

Start both services (see each README), then:

```bash
export KEY=dev-agent-key-change-me

curl -s localhost:8001/agent/v1/health -H "X-API-Key: $KEY"
curl -s localhost:8001/agent/v1/routes -H "X-API-Key: $KEY"
```

With `PAYSTACK_MOCK=true` the `payment_url` points at a local mock checkout page —
open it, click **Pay**, and the booking confirms, the QR is issued and the
notifications fire exactly as in production. That gives you a complete, offline,
zero-credential loop to build against.

Seeded test data:

| | |
|---|---|
| Subscriber with credits | `08090001122` (Amaka Obi, Tier 1) |
| Number with no subscription | any other, e.g. `08011112222` |
| Routes | Umuahia ⇄ Airport, Aba ⇄ Airport |
| Departures | 06:00, 09:00, 12:00, 15:00 daily, 14 days ahead |

---

## 7. Questions

If something here doesn't match what the API does, the API is right and this document
is stale — check `<base>/docs`, then tell the backend team with the `request_id` from
the response.

`jinduinc@gmail.com` · +234 815 447 1570 · @ecojindu.ng
