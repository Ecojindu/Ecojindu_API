"""WhatsApp conversation-state store.

The `whatsapp_sessions` table is owned (and migrated) by the backend; this
service declares a thin mapping over the same table so it can read and write
conversation state without an HTTP round-trip on every inbound message.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import settings


class Base(DeclarativeBase):
    pass


class WhatsAppSession(Base):
    """Mirrors `app.models.notification.WhatsAppSession` in the backend."""

    __tablename__ = "whatsapp_sessions"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(48), default="idle", nullable=False)
    context: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def load_session(db: AsyncSession, phone: str) -> WhatsAppSession:
    """Fetch or create the conversation, resetting it if it went cold.

    A session idle for longer than WHATSAPP_SESSION_TIMEOUT_MINUTES is wiped back
    to `idle`, so someone returning next week starts from the greeting rather than
    halfway through last week's booking.
    """
    session = (
        await db.execute(select(WhatsAppSession).where(WhatsAppSession.phone == phone))
    ).scalar_one_or_none()

    if session is None:
        session = WhatsAppSession(phone=phone, state="idle", context={}, last_message_at=_now())
        db.add(session)
        await db.flush()
        return session

    timeout = timedelta(minutes=settings.WHATSAPP_SESSION_TIMEOUT_MINUTES)
    if session.last_message_at and _now() - session.last_message_at > timeout:
        session.state = "idle"
        session.context = {}

    session.last_message_at = _now()
    return session


async def save_session(
    db: AsyncSession, session: WhatsAppSession, *, state: str, context: dict | None = None
) -> None:
    session.state = state
    # Reassign rather than mutate — JSONB change tracking needs a new object.
    session.context = dict(context or {})
    session.last_message_at = _now()
    await db.flush()
    await db.commit()


async def reset_session(db: AsyncSession, session: WhatsAppSession) -> None:
    await save_session(db, session, state="idle", context={})


async def find_session_by_payment_reference(
    db: AsyncSession, reference: str
) -> WhatsAppSession | None:
    """Used by the payment webhook to find whom to WhatsApp the ticket to."""
    stmt = select(WhatsAppSession).where(
        WhatsAppSession.context["payment_reference"].astext == reference
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_session_by_booking_ref(db: AsyncSession, booking_ref: str) -> WhatsAppSession | None:
    stmt = select(WhatsAppSession).where(
        WhatsAppSession.context["booking_ref"].astext == booking_ref
    )
    return (await db.execute(stmt)).scalar_one_or_none()
