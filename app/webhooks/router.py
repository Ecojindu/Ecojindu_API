"""Paystack webhook receiver.

Either service may be the one Paystack is pointed at. When it lands here we:

1. Verify the signature (HMAC-SHA512 over the raw body).
2. Relay the raw bytes to the backend, which does the authoritative, idempotent
   processing — confirm the booking, issue the QR, send email + SMS.
3. If the booking came from WhatsApp, push the QR ticket back into the chat.

Step 3 is the only thing this service adds, and it is guarded by the session's
`ticket_sent` flag so a duplicate delivery doesn't re-send the ticket.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, AuthError
from app.core.logging import log_event
from app.core.security import verify_paystack_signature
from app.services.backend import backend
from app.services.sessions import find_session_by_payment_reference, get_db, save_session
from app.services.twilio import whatsapp
from app.whatsapp.flow import build_ticket_message

logger = logging.getLogger("ecojindu.api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post(
    "/paystack",
    summary="Paystack webhook (signature-verified, idempotent)",
    description=(
        "Point the Paystack dashboard at either this endpoint or the backend's — "
        "processing converges either way because the backend keys every delivery "
        "in its `webhook_events` ledger."
    ),
)
async def paystack_webhook(
    request: Request,
    db: DbSession,
    x_paystack_signature: Annotated[str | None, Header(alias="x-paystack-signature")] = None,
) -> dict:
    raw = await request.body()

    if not settings.PAYSTACK_MOCK:
        if not verify_paystack_signature(raw, x_paystack_signature or ""):
            log_event(logger, logging.WARNING, "rejected unsigned paystack webhook")
            raise AuthError("Invalid webhook signature.", code="invalid_signature")

    body = await request.json()
    event = body.get("event", "unknown")
    reference = (body.get("data") or {}).get("reference")

    relay = await backend.forward_paystack_webhook(raw, x_paystack_signature)
    log_event(logger, logging.INFO, "relayed to backend", event=event, reference=reference, result=relay.get("status"))

    whatsapp_delivery = "not_applicable"
    if event == "charge.success" and reference:
        whatsapp_delivery = await _deliver_whatsapp_ticket(db, reference)

    return {"status": relay.get("status", "processed"), "reference": reference, "whatsapp": whatsapp_delivery}


async def _deliver_whatsapp_ticket(db: AsyncSession, reference: str) -> str:
    """Send the QR ticket into the chat that started this booking."""
    session = await find_session_by_payment_reference(db, reference)
    if session is None:
        return "no_whatsapp_session"

    context = dict(session.context or {})
    if context.get("ticket_sent"):
        return "already_sent"

    booking_ref = context.get("booking_ref")
    if not booking_ref:
        return "no_booking_ref"

    try:
        booking = await backend.get_booking(booking_ref)
    except AppError as exc:
        log_event(logger, logging.WARNING, "ticket lookup failed", ref=booking_ref, error=exc.message)
        return "lookup_failed"

    if booking.get("status") not in {"confirmed", "checked_in", "completed"}:
        return "booking_not_confirmed"

    text, media_url = await build_ticket_message(booking)
    result = await whatsapp.send(session.phone, text, media_url=media_url)

    if result.success:
        context["ticket_sent"] = True
        await save_session(db, session, state="idle", context=context)
        log_event(logger, logging.INFO, "whatsapp ticket delivered", ref=booking_ref, phone=session.phone)
        return "sent"

    log_event(logger, logging.ERROR, "whatsapp ticket failed", ref=booking_ref, error=result.error)
    return "send_failed"


@router.post(
    "/paystack/replay/{reference}",
    summary="Re-attempt WhatsApp ticket delivery for a settled payment",
    description="Operational escape hatch for when Twilio was down at settlement time.",
)
async def replay_whatsapp_ticket(reference: str, db: DbSession) -> dict:
    session = await find_session_by_payment_reference(db, reference)
    if session is not None:
        context = dict(session.context or {})
        context.pop("ticket_sent", None)
        await save_session(db, session, state=session.state, context=context)
    return {"reference": reference, "whatsapp": await _deliver_whatsapp_ticket(db, reference)}
