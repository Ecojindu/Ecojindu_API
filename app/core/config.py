"""Configuration for the integrations & agent gateway."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True
    APP_NAME: str = "Ecojindu Shuttle API Gateway"
    PORT: int = 8001
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # ── Core backend ──────────────────────────────────────────
    BACKEND_BASE_URL: str = "http://localhost:8000"
    SERVICE_API_KEY: str = "dev-service-key-change-me"
    BACKEND_TIMEOUT_SECONDS: int = 25

    # ── Shared database ───────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ecojindu"

    # ── Agent auth ────────────────────────────────────────────
    AGENT_API_KEYS: str = "dev-agent-key-change-me"
    AGENT_RATE_LIMIT_PER_MINUTE: int = 120

    # ── Twilio ────────────────────────────────────────────────
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = "whatsapp:+14155238886"
    TWILIO_STATUS_CALLBACK_URL: str = ""
    TWILIO_VALIDATE_SIGNATURE: bool = False
    TWILIO_MOCK: bool = True
    PUBLIC_BASE_URL: str = "http://localhost:8001"

    # ── Paystack ──────────────────────────────────────────────
    PAYSTACK_SECRET_KEY: str = "sk_test_placeholder"
    PAYSTACK_MOCK: bool = True

    # ── WhatsApp conversation ─────────────────────────────────
    WHATSAPP_SESSION_TIMEOUT_MINUTES: int = 30
    WHATSAPP_MAX_SEATS: int = 6

    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    COMPANY_NAME: str = "Ecojindu Shuttle"
    COMPANY_EMAIL: str = "jinduinc@gmail.com"
    COMPANY_PHONE: str = "+2348154471570"
    WEB_BASE_URL: str = "http://localhost:3000"

    @field_validator("DATABASE_URL")
    @classmethod
    def _force_async_driver(cls, v: str) -> str:
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def agent_api_keys(self) -> set[str]:
        return {k.strip() for k in self.AGENT_API_KEYS.split(",") if k.strip()}

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
