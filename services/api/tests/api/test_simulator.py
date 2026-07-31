"""Simulator endpoints that need no database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.analytics.whatif import default_scenarios
from app.core.config import Environment, Settings
from app.core.ids import uuid7
from app.infrastructure.db.models.identity import User
from app.interfaces.http.app import create_app
from app.interfaces.http.deps import get_current_user, get_session

PREFIX = "/api/v1/simulator"


AUTH: dict[str, str] = {}


@pytest.fixture
def client() -> TestClient:
    """A client whose identity is injected rather than authenticated.

    `get_current_user` resolves a Clerk principal against the users table, so an
    authenticated endpoint normally needs both a token and a database. Overriding the
    dependency lets the request-validation paths — which run before any query — be
    tested without either.
    """
    settings = Settings(environment=Environment.TEST, log_json=False, auth_dev_bypass=True)
    app = create_app(settings)

    user = User(id=uuid7(), clerk_user_id="user_simulator_tests", email="s@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: None

    return TestClient(app, raise_server_exceptions=False)


def test_the_standard_sweep_is_published(client: TestClient) -> None:
    payload = client.get(f"{PREFIX}/scenarios").json()

    assert len(payload["scenarios"]) == len(default_scenarios())
    assert all(item["label"] for item in payload["scenarios"])


def test_the_response_explains_why_a_long_sweep_is_worse(client: TestClient) -> None:
    """A caller adding scenarios needs to know it makes each one harder to establish."""
    note = client.get(f"{PREFIX}/scenarios").json()["note"]

    assert "raises the significance bar" in note


def test_a_scenario_that_changes_nothing_is_rejected(client: TestClient) -> None:
    """Otherwise it burns a slot in the family correction while testing nothing."""
    response = client.post(
        f"{PREFIX}/sweep",
        json={"scenarios": [{"label": "does nothing"}]},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert "does nothing" in response.json()["error"]["details"]["empty_scenarios"]


def test_too_many_scenarios_are_refused(client: TestClient) -> None:
    """A statistical limit, not a performance one."""
    response = client.post(
        f"{PREFIX}/sweep",
        json={
            "scenarios": [
                {"label": f"target {index}", "target_r": str(index)}
                for index in range(1, 20)
            ]
        },
        headers=AUTH,
    )

    assert response.status_code == 422


def test_an_inverted_date_range_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/sweep",
        json={"session_from": "2026-06-01", "session_to": "2026-01-01"},
        headers=AUTH,
    )

    assert response.status_code == 400
