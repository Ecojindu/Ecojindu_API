"""The agent surface must be closed by default and open only to configured keys."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import reset_rate_limits, verify_paystack_signature
from app.main import app


@pytest.fixture(autouse=True)
def _clear_limits():
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


AGENT_ENDPOINTS = [
    ("GET", "/agent/v1/routes"),
    ("GET", "/agent/v1/availability?route_id=x"),
    ("POST", "/agent/v1/quote"),
    ("POST", "/agent/v1/bookings"),
    ("GET", "/agent/v1/bookings/EJS-ABCDE"),
    ("POST", "/agent/v1/bookings/EJS-ABCDE/cancel"),
    ("GET", "/agent/v1/subscriptions/08090001122"),
    ("POST", "/agent/v1/subscriptions/08090001122/book"),
    ("POST", "/agent/v1/tickets/EJS-ABCDE/resend"),
]


@pytest.mark.parametrize("method,path", AGENT_ENDPOINTS)
def test_every_agent_endpoint_requires_a_key(client, method, path):
    response = client.request(method, path, json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


@pytest.mark.parametrize("method,path", AGENT_ENDPOINTS)
def test_wrong_key_is_rejected(client, method, path):
    response = client.request(method, path, json={}, headers={"X-API-Key": "definitely-not-it"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_configured_key_gets_past_auth(client):
    key = next(iter(settings.agent_api_keys))
    response = client.get("/agent/v1/routes", headers={"X-API-Key": key})
    # 200 with the backend up, 502 with it down — either way, not a 401.
    assert response.status_code != 401


def test_rate_limit_kicks_in(client, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MINUTE", 3)
    key = next(iter(settings.agent_api_keys))
    headers = {"X-API-Key": key}

    statuses = [client.get("/agent/v1/health", headers=headers).status_code for _ in range(5)]
    assert 429 in statuses, f"expected a 429 after 3 requests, got {statuses}"

    limited = client.get("/agent/v1/health", headers=headers)
    assert limited.json()["error"]["code"] == "rate_limited"
    assert "retry_after_seconds" in limited.json()["error"]["details"]


def test_health_is_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


#: The templated paths as OpenAPI declares them (AGENT_ENDPOINTS uses concrete values).
DOCUMENTED_PATHS = [
    "/agent/v1/routes",
    "/agent/v1/availability",
    "/agent/v1/quote",
    "/agent/v1/bookings",
    "/agent/v1/bookings/{ref}",
    "/agent/v1/bookings/{ref}/cancel",
    "/agent/v1/subscriptions/{phone}",
    "/agent/v1/subscriptions/{phone}/book",
    "/agent/v1/tickets/{ref}/resend",
]


def test_openapi_documents_the_agent_surface(client):
    spec = client.get("/openapi.json").json()
    for path in DOCUMENTED_PATHS:
        assert path in spec["paths"], f"{path} missing from the OpenAPI document"


def test_openapi_endpoints_carry_descriptions_for_the_agent_author(client):
    """The ADK teammate reads these — an undocumented endpoint is a support ticket."""
    spec = client.get("/openapi.json").json()
    for path in DOCUMENTED_PATHS:
        for method, operation in spec["paths"][path].items():
            assert operation.get("summary"), f"{method.upper()} {path} has no summary"


def test_paystack_signature_verification(monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", "sk_test_known_secret")
    body = b'{"event":"charge.success","data":{"reference":"EJSBK-1"}}'
    good = hmac.new(b"sk_test_known_secret", body, hashlib.sha512).hexdigest()

    assert verify_paystack_signature(body, good)
    assert not verify_paystack_signature(body, "deadbeef")
    assert not verify_paystack_signature(body + b" ", good)  # body tampered
    assert not verify_paystack_signature(body, "")


def test_unsigned_webhook_rejected_when_not_mocking(client, monkeypatch):
    monkeypatch.setattr(settings, "PAYSTACK_MOCK", False)
    response = client.post(
        "/webhooks/paystack",
        json={"event": "charge.success", "data": {"reference": "EJSBK-1"}},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
