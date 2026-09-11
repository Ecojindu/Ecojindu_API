"""Twilio inbound-message webhook."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AuthError
from app.core.logging import log_event
from app.core.security import verify_twilio_signature
from app.services.sessions import get_db, load_session, save_session
from app.services.twilio import strip_whatsapp_prefix, twiml, whatsapp
from app.whatsapp import flow

logger = logging.getLogger("ecojindu.api.whatsapp")

router = APIRouter(prefix="/webhooks/twilio", tags=["WhatsApp"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post(
    "/whatsapp",
    response_class=Response,
    summary="Twilio inbound WhatsApp message",
    description=(
        "Twilio posts an `application/x-www-form-urlencoded` body here for every "
        "inbound message. The reply is sent through the REST API (so it can carry "
        "the QR image as media) and an empty TwiML document is returned."
    ),
)
async def inbound_whatsapp(
    request: Request,
    db: DbSession,
    From: Annotated[str, Form()] = "",
    Body: Annotated[str, Form()] = "",
    x_twilio_signature: Annotated[str | None, Header(alias="X-Twilio-Signature")] = None,
) -> Response:
    if settings.TWILIO_VALIDATE_SIGNATURE:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if not verify_twilio_signature(str(request.url), params, x_twilio_signature or ""):
            raise AuthError("Invalid Twilio signature.", code="invalid_twilio_signature")

    phone = strip_whatsapp_prefix(From)
    if not phone:
        return Response(content=twiml(), media_type="application/xml")

    session = await load_session(db, phone)
    log_event(
        logger,
        logging.INFO,
        "whatsapp inbound",
        phone=phone,
        state=session.state,
        body=(Body or "")[:120],
    )

    reply = await flow.handle_message(phone, Body, session.state, session.context or {})
    await save_session(db, session, state=reply.state, context=reply.context)

    result = await whatsapp.send(phone, reply.text, media_url=reply.media_url)
    if not result.success:
        # Fall back to a TwiML reply so the passenger still hears something back.
        log_event(logger, logging.WARNING, "falling back to TwiML", phone=phone, error=result.error)
        return Response(content=twiml(reply.text), media_type="application/xml")

    return Response(content=twiml(), media_type="application/xml")


@router.post(
    "/status",
    response_class=Response,
    summary="Twilio delivery status callback",
    description="Records queued / sent / delivered / read / failed transitions for outbound messages.",
)
async def twilio_status_callback(
    MessageSid: Annotated[str, Form()] = "",
    MessageStatus: Annotated[str, Form()] = "",
    To: Annotated[str, Form()] = "",
    ErrorCode: Annotated[str | None, Form()] = None,
    ErrorMessage: Annotated[str | None, Form()] = None,
) -> Response:
    level = logging.WARNING if MessageStatus in {"failed", "undelivered"} else logging.INFO
    log_event(
        logger,
        level,
        "whatsapp delivery status",
        sid=MessageSid,
        status=MessageStatus,
        to=strip_whatsapp_prefix(To),
        error_code=ErrorCode or "-",
        error=ErrorMessage or "-",
    )
    return Response(status_code=204)
