"""API-key auth for the agent surface, plus webhook signature verification."""
# NOTE: no `from __future__ import annotations` — FastAPI cannot resolve deferred
# annotations on a class-instance dependency's __call__.

import base64
import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque
from typing import Annotated, Optional
from urllib.parse import urlencode

from fastapi import Header, Request

from app.core.config import settings
from app.core.errors import AuthError, RateLimitError


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


# ── Agent API keys ───────────────────────────────────────────


async def require_agent_key(
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> str:
    """Every /agent/v1 endpoint sits behind this.

    Keys are compared in constant time against the configured allow-list, so a
    rotated key stops working the moment it leaves AGENT_API_KEYS.
    """
    if not x_api_key:
        raise AuthError(
            "Missing API key. Send it in the `X-API-Key` header.", code="missing_api_key"
        )
    if not any(constant_time_equals(x_api_key, k) for k in settings.agent_api_keys):
        raise AuthError("That API key is not recognised.", code="invalid_api_key")
    return x_api_key


AgentKey = Annotated[str, Header()]


# ── Simple per-key rate limiting ─────────────────────────────


class _Buckets:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - window:
                q.popleft()
            if len(q) >= limit:
                return False, int(q[0] + window - now) + 1
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_buckets = _Buckets()


def reset_rate_limits() -> None:
    _buckets.reset()


class AgentRateLimit:
    """Per-API-key throttle so one misbehaving agent can't crowd out the others."""

    def __init__(self, limit: Optional[int] = None, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds

    async def __call__(
        self,
        request: Request,
        x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
    ) -> None:
        limit = self.limit or settings.AGENT_RATE_LIMIT_PER_MINUTE
        identity = x_api_key or (request.client.host if request.client else "anonymous")
        key = hashlib.sha256(identity.encode()).hexdigest()[:16]
        allowed, retry_after = _buckets.hit(key, limit, self.window_seconds)
        if not allowed:
            raise RateLimitError(
                f"Rate limit of {limit} requests/minute exceeded.",
                details={"retry_after_seconds": retry_after},
            )


# ── Webhook signatures ───────────────────────────────────────


def verify_paystack_signature(raw_body: bytes, header_signature: str) -> bool:
    """Paystack signs with HMAC-SHA512 keyed by the secret key."""
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, header_signature or "")


def verify_twilio_signature(url: str, params: dict, header_signature: str) -> bool:
    """Twilio signs `url + sorted(k+v for form fields)` with HMAC-SHA1, base64'd.

    See https://www.twilio.com/docs/usage/security#validating-requests
    """
    if not settings.TWILIO_AUTH_TOKEN:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(
        settings.TWILIO_AUTH_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header_signature or "")


def build_absolute_url(path: str, query: Optional[dict] = None) -> str:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    url = f"{base}{path}"
    if query:
        url += "?" + urlencode(query)
    return url
