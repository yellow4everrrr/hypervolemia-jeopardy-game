"""Report endpoints that need no database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.core.ids import uuid7
from app.infrastructure.db.models.identity import User
from app.interfaces.http.app import create_app
from app.interfaces.http.deps import get_current_user, get_session
from app.reports.comparison import COMPARED_METRICS
from app.reports.periods import PERIODIC_TYPES

PREFIX = "/api/v1/reports"


@pytest.fixture
def client() -> TestClient:
    settings = Settings(environment=Environment.TEST, log_json=False, auth_dev_bypass=True)
    app = create_app(settings)

    user = User(id=uuid7(), clerk_user_id="user_report_tests", email="r@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: None

    return TestClient(app, raise_server_exceptions=False)


def test_the_definition_publishes_the_report_types(client: TestClient) -> None:
    payload = client.get(f"{PREFIX}/definition").json()

    assert payload["periods"] == [item.value for item in PERIODIC_TYPES]
    assert payload["version"] >= 1


def test_the_definition_publishes_the_compared_metrics(client: TestClient) -> None:
    """A trader who sees "no change established" needs to know what was tested."""
    payload = client.get(f"{PREFIX}/definition").json()

    assert len(payload["compared_metrics"]) == len(COMPARED_METRICS)
    assert {item["key"] for item in payload["compared_metrics"]} == {
        key for key, _, _ in COMPARED_METRICS
    }


def test_the_definition_explains_that_no_change_is_a_result(client: TestClient) -> None:
    """The most important thing about these reports is what they refuse to say, so the
    refusal is documented in the payload rather than left to look like a missing
    feature."""
    notes = " ".join(client.get(f"{PREFIX}/definition").json()["notes"])

    assert "accurate result rather than a missing feature" in notes
    assert "bills the same money three times" in notes


def test_the_definition_states_the_sample_needed_per_report_type(client: TestClient) -> None:
    thresholds = client.get(f"{PREFIX}/definition").json()[
        "sessions_needed_before_concluding"
    ]

    assert thresholds["daily"] < thresholds["monthly"] < thresholds["annual"]


def test_a_non_calendar_report_type_is_rejected(client: TestClient) -> None:
    """`compliance` is a report *shape*, not a period — it has no calendar boundary."""
    response = client.post(
        f"{PREFIX}/generate",
        json={"report_type": "compliance", "session": "2026-04-15"},
    )

    assert response.status_code == 400
    assert "not a calendar period" in response.json()["error"]["message"]


def test_an_unknown_report_type_is_rejected_by_validation(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/generate",
        json={"report_type": "fortnightly", "session": "2026-04-15"},
    )

    assert response.status_code == 422


def test_a_schedule_run_caps_its_batch_size(client: TestClient) -> None:
    """A trader importing three years at once has hundreds of due periods; building
    them all in one request would time out with no record of how far it got."""
    response = client.post(f"{PREFIX}/run-schedule", json={"limit": 500})

    assert response.status_code == 422


def test_a_known_report_type_reaches_the_database_layer(client: TestClient) -> None:
    """Validation runs before any query, so a legitimate type must get past it.

    With the session overridden to ``None`` the request fails inside the repository,
    which is the evidence it was not rejected earlier.
    """
    response = client.post(
        f"{PREFIX}/generate",
        json={"report_type": "monthly", "session": "2026-01-15"},
    )

    assert response.status_code != 400
    assert response.status_code != 422
