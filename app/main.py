"""Ecojindu Shuttle — integrations & agent gateway."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.agent.router import router as agent_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import log_event, new_request_id, request_id_ctx, setup_logging
from app.services.backend import backend
from app.services.sessions import engine
from app.webhooks.router import router as webhooks_router
from app.whatsapp.router import router as whatsapp_router

setup_logging()
logger = logging.getLogger("ecojindu.api")

DESCRIPTION = """
Everything **Ecojindu Shuttle** exposes to the outside world.

* **WhatsApp booking bot** — a stateful, rule-based flow over Twilio that takes a
  passenger from greeting to paid QR ticket without leaving the chat.
* **AI agent gateway** (`/agent/v1`) — a clean, API-key-protected REST surface
  mapping 1:1 to what a booking agent needs. See `docs/AGENT_INTEGRATION.md`.
* **Webhooks** — Paystack (signature-verified, idempotent) and Twilio status callbacks.

This service holds no business logic of its own: it authenticates callers, shapes
requests, and delegates to `ecojindu-backend` over an internal service key.

**Auth**

| Surface | Header |
|---|---|
| `/agent/v1/**` | `X-API-Key: <your agent key>` |
| `/webhooks/paystack` | `x-paystack-signature` (HMAC-SHA512 of the raw body) |
| `/webhooks/twilio/**` | `X-Twilio-Signature` when `TWILIO_VALIDATE_SIGNATURE=true` |

Money is always in **kobo** (₦1 = 100 kobo). Local time is Africa/Lagos.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        db_state = "ok"
    except Exception as exc:  # noqa: BLE001 - start up anyway, report degraded
        db_state = f"unreachable ({exc.__class__.__name__})"

    log_event(
        logger,
        logging.INFO,
        "gateway starting",
        environment=settings.ENVIRONMENT,
        backend=settings.BACKEND_BASE_URL,
        database=db_state,
        twilio_mock=settings.TWILIO_MOCK,
        agent_keys=len(settings.agent_api_keys),
    )
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("gateway stopped")


app = FastAPI(
    title=settings.APP_NAME,
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "Ecojindu Shuttle", "email": settings.COMPANY_EMAIL},
    openapi_tags=[
        {"name": "AI Agent", "description": "The surface the ADK agent consumes. Auth: `X-API-Key`."},
        {"name": "WhatsApp", "description": "Twilio inbound messages and delivery callbacks."},
        {"name": "Webhooks", "description": "Paystack payment events, relayed to the backend."},
        {"name": "Health", "description": "Liveness and dependency checks."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or new_request_id()
    token = request_id_ctx.set(rid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)

    response.headers["X-Request-ID"] = rid
    if not request.url.path.startswith(("/health", "/docs", "/openapi")):
        log_event(
            logger,
            logging.INFO,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    return response


register_exception_handlers(app)
app.include_router(agent_router)
app.include_router(whatsapp_router)
app.include_router(webhooks_router)


@app.get("/", tags=["Health"], summary="Service banner")
async def root() -> dict:
    return {
        "service": "ecojindu-api",
        "version": "1.0.0",
        "surfaces": {
            "agent": "/agent/v1",
            "whatsapp_webhook": "/webhooks/twilio/whatsapp",
            "paystack_webhook": "/webhooks/paystack",
        },
        "docs": "/docs",
        "agent_integration_guide": "docs/AGENT_INTEGRATION.md",
    }


@app.get("/health", tags=["Health"], summary="Liveness plus dependency checks")
async def health() -> dict:
    db_ok = True
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    backend_ok = True
    try:
        await backend.health()
    except Exception:  # noqa: BLE001
        backend_ok = False

    healthy = db_ok and backend_ok
    return {
        "status": "ok" if healthy else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "backend": "ok" if backend_ok else "unreachable",
        "twilio": "mock" if settings.TWILIO_MOCK else "live",
        "environment": settings.ENVIRONMENT,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.PORT, reload=settings.DEBUG)
