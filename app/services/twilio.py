"""Twilio WhatsApp sender.

`TWILIO_MOCK=true` logs the exact outbound message instead of calling Twilio, so
the whole conversational flow is testable with no account.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger("ecojindu.api.twilio")


@dataclass(slots=True)
class SendResult:
    success: bool
    message_sid: str | None = None
    error: str | None = None


def normalise_whatsapp_address(phone: str) -> str:
    """`+2348154471570` → `whatsapp:+2348154471570` (idempotent)."""
    phone = phone.strip()
    return phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"


def strip_whatsapp_prefix(address: str) -> str:
    return address.replace("whatsapp:", "").strip()


class TwilioWhatsApp:
    def __init__(self) -> None:
        self.sid = settings.TWILIO_ACCOUNT_SID
        self.token = settings.TWILIO_AUTH_TOKEN
        self.sender = normalise_whatsapp_address(settings.TWILIO_WHATSAPP_FROM)
        self.mock = settings.TWILIO_MOCK or not (self.sid and self.token)

    async def send(
        self, to: str, body: str, media_url: str | None = None
    ) -> SendResult:
        to_address = normalise_whatsapp_address(to)

        if self.mock:
            log_event(
                logger,
                logging.INFO,
                "WhatsApp (mock)",
                to=to_address,
                media=media_url or "-",
                body=body.replace("\n", " ⏎ ")[:400],
            )
            return SendResult(True, message_sid=f"SM_mock_{abs(hash(body)) % 10**10}")

        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        form = {"To": to_address, "From": self.sender, "Body": body}
        if media_url:
            form["MediaUrl"] = media_url
        if settings.TWILIO_STATUS_CALLBACK_URL:
            form["StatusCallback"] = settings.TWILIO_STATUS_CALLBACK_URL

        try:
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                    data=form,
                    headers={"Authorization": f"Basic {auth}"},
                )
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - never let Twilio break the webhook
            log_event(logger, logging.ERROR, "WhatsApp send failed", error=str(exc)[:300])
            return SendResult(False, error=str(exc)[:300])

        if response.status_code < 300:
            return SendResult(True, message_sid=data.get("sid"))

        log_event(
            logger,
            logging.ERROR,
            "WhatsApp rejected",
            status=response.status_code,
            error=str(data)[:300],
        )
        return SendResult(False, error=str(data)[:300])


whatsapp = TwilioWhatsApp()


def twiml(message: str | None = None) -> str:
    """Twilio expects TwiML on the webhook response; empty means 'no auto-reply'."""
    if not message:
        return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    escaped = (
        message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>'
