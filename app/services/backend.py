"""Typed HTTP client for the core backend.

Every call carries the `X-Service-Key` header. Backend error envelopes are
re-raised as this service's own domain errors, so a 409 from the backend
surfaces to the agent as a 409 with the same human-readable message rather
than an opaque 502.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ConflictError, NotFoundError, UpstreamError, ValidationError
from app.core.logging import log_event

logger = logging.getLogger("ecojindu.api.backend")

_STATUS_TO_ERROR = {
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
}


class BackendClient:
    def __init__(self, base_url: str | None = None, service_key: str | None = None):
        self.base_url = (base_url or settings.BACKEND_BASE_URL).rstrip("/")
        self.service_key = service_key or settings.SERVICE_API_KEY
        self.timeout = settings.BACKEND_TIMEOUT_SECONDS

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Service-Key": self.service_key, "Content-Type": "application/json"}

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method, url, json=json, params=params, headers=self._headers
                )
        except httpx.HTTPError as exc:
            log_event(logger, logging.ERROR, "backend unreachable", path=path, error=str(exc))
            raise UpstreamError(
                "The booking service is temporarily unreachable. Please try again shortly."
            ) from exc

        if response.status_code >= 400:
            try:
                body = response.json()
                err = body.get("error", {})
                message = err.get("message") or body.get("detail") or response.text
                code = err.get("code", "upstream_error")
                details = err.get("details", {})
            except Exception:  # noqa: BLE001 - non-JSON error body
                message, code, details = response.text[:300], "upstream_error", {}

            log_event(
                logger,
                logging.WARNING,
                "backend error",
                path=path,
                status=response.status_code,
                code=code,
            )
            error_cls = _STATUS_TO_ERROR.get(response.status_code)
            if error_cls:
                raise error_cls(message, code=code, details=details)
            if response.status_code in (400, 402):
                raise AppError(message, code=code, details=details)
            raise UpstreamError(message, code=code, details=details)

        if not response.content:
            return None
        return response.json()

    # ── Catalogue ─────────────────────────────────────────────

    async def list_routes(self) -> list[dict]:
        return await self._request("GET", "/v1/routes")

    async def get_route(self, route_id: str) -> dict:
        return await self._request("GET", f"/v1/routes/{route_id}")

    async def list_trips(
        self,
        *,
        route_id: str | None = None,
        service_date: str | None = None,
        seats: int = 1,
        origin: str | None = None,
        destination: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"seats": seats}
        if route_id:
            params["route_id"] = route_id
        if service_date:
            params["service_date"] = service_date
        if origin:
            params["origin"] = origin
        if destination:
            params["destination"] = destination
        return await self._request("GET", "/v1/trips", params=params)

    async def get_trip(self, trip_id: str) -> dict:
        return await self._request("GET", f"/v1/trips/{trip_id}")

    async def list_plans(self) -> list[dict]:
        return await self._request("GET", "/v1/plans")

    # ── Bookings ──────────────────────────────────────────────

    async def create_booking(
        self,
        *,
        trip_id: str,
        passenger_name: str,
        passenger_phone: str,
        passenger_email: str | None = None,
        seats: int = 1,
        source: str = "whatsapp",
        notes: str | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/v1/bookings/internal",
            json={
                "trip_id": trip_id,
                "passenger_name": passenger_name,
                "passenger_phone": passenger_phone,
                "passenger_email": passenger_email,
                "seats": seats,
                "source": source,
                "notes": notes,
            },
        )

    async def create_subscription_booking(
        self,
        *,
        phone: str,
        trip_id: str,
        seats: int = 1,
        passenger_name: str | None = None,
        passenger_email: str | None = None,
        source: str = "subscription",
    ) -> dict:
        return await self._request(
            "POST",
            "/v1/bookings/internal/subscription",
            json={
                "phone": phone,
                "trip_id": trip_id,
                "seats": seats,
                "passenger_name": passenger_name,
                "passenger_email": passenger_email,
                "source": source,
            },
        )

    async def get_booking(self, booking_ref: str) -> dict:
        return await self._request("GET", f"/v1/bookings/internal/{booking_ref}")

    async def cancel_booking(self, booking_ref: str, reason: str | None = None) -> dict:
        return await self._request(
            "POST", f"/v1/bookings/internal/{booking_ref}/cancel", json={"reason": reason}
        )

    async def resend_ticket(self, booking_ref: str, channels: list[str]) -> dict:
        return await self._request(
            "POST",
            f"/v1/bookings/internal/{booking_ref}/resend-ticket",
            json={"channels": channels},
        )

    # ── Subscriptions ─────────────────────────────────────────

    async def subscription_by_phone(self, phone: str) -> dict | None:
        return await self._request("GET", f"/v1/subscriptions/internal/by-phone/{phone}")

    # ── Payments ──────────────────────────────────────────────

    async def verify_payment(self, reference: str) -> dict:
        return await self._request("GET", f"/v1/payments/verify/{reference}")

    async def forward_paystack_webhook(self, raw_body: bytes, signature: str | None) -> dict:
        """Relay the *raw* bytes so the backend can verify the signature itself."""
        url = f"{self.base_url}/v1/payments/webhook/paystack"
        headers = {"Content-Type": "application/json", "X-Service-Key": self.service_key}
        if signature:
            headers["x-paystack-signature"] = signature
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, content=raw_body, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError("Could not relay the webhook to the backend.") from exc

        if response.status_code >= 400:
            raise UpstreamError(f"Backend rejected the webhook: {response.text[:200]}")
        return response.json()

    def ticket_image_url(self, booking_ref: str) -> str:
        """Public URL of the QR PNG — sent to WhatsApp as a media attachment."""
        return f"{self.base_url}/v1/tickets/{booking_ref}/qr.png"

    async def health(self) -> dict:
        return await self._request("GET", "/health")


backend = BackendClient()
