"""Job endpoints that need no database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.core.ids import uuid7
from app.infrastructure.db.models.identity import User
from app.interfaces.http.app import create_app
from app.interfaces.http.deps import get_current_user, get_session
from app.interfaces.http.routers.jobs import ENQUEUEABLE
from app.jobs.handlers import HANDLERS

PREFIX = "/api/v1/jobs"


@pytest.fixture
def client() -> TestClient:
    settings = Settings(environment=Environment.TEST, log_json=False, auth_dev_bypass=True)
    app = create_app(settings)

    user = User(id=uuid7(), clerk_user_id="user_job_tests", email="j@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: None

    return TestClient(app, raise_server_exceptions=False)


def test_a_kind_that_is_not_enqueueable_is_rejected(client: TestClient) -> None:
    """Broker sync is driven by the sync pipeline. Letting a client queue it directly
    would let them schedule broker traffic outside the rate limiter."""
    response = client.post(f"{PREFIX}", json={"kind": "sync_broker"})

    assert response.status_code == 400
    assert "cannot be queued directly" in response.json()["error"]["message"]
    assert "detect_patterns" in response.json()["error"]["details"]["enqueueable"]


def test_an_unknown_kind_fails_validation(client: TestClient) -> None:
    response = client.post(f"{PREFIX}", json={"kind": "make_coffee"})

    assert response.status_code == 422


def test_every_enqueueable_kind_has_a_handler(client: TestClient) -> None:
    """Otherwise a client could queue work that will only ever dead-letter.

    Asserted against the dispatch table rather than a restated list, so adding a kind to
    one and not the other fails here.
    """
    assert set(ENQUEUEABLE) <= set(HANDLERS)


def test_an_unknown_report_type_is_rejected_before_queueing(client: TestClient) -> None:
    """Validating at enqueue time rather than in the handler.

    A bad payload that reaches the queue fails five times with backoff and then
    dead-letters — a 400 now is the same information, immediately.
    """
    response = client.post(
        f"{PREFIX}",
        json={"kind": "generate_reports", "payload": {"report_types": ["fortnightly"]}},
    )

    assert response.status_code == 400
    assert "unknown report type" in response.json()["error"]["message"]


def test_an_over_long_idempotency_key_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}",
        json={"kind": "detect_patterns", "idempotency_key": "x" * 500},
    )

    assert response.status_code == 422


def test_a_valid_request_reaches_the_database_layer(client: TestClient) -> None:
    """Validation runs before any query, so a legitimate request must get past it.

    With the session overridden to ``None`` the request fails inside the queue, which is
    the evidence it was not rejected earlier.
    """
    response = client.post(f"{PREFIX}", json={"kind": "detect_patterns"})

    assert response.status_code not in (400, 422)


def test_the_listing_limit_is_bounded(client: TestClient) -> None:
    assert client.get(f"{PREFIX}?limit=5000").status_code == 422
