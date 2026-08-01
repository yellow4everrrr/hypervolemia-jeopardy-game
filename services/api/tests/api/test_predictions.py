"""Prediction endpoints that need no database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.core.ids import uuid7
from app.infrastructure.db.models.identity import User
from app.interfaces.http.app import create_app
from app.interfaces.http.deps import get_current_user, get_session
from app.ml.features import FEATURES
from app.ml.snapshot import OUTCOME_FIELDS

PREFIX = "/api/v1/predictions"


@pytest.fixture
def client() -> TestClient:
    settings = Settings(environment=Environment.TEST, log_json=False, auth_dev_bypass=True)
    app = create_app(settings)

    user = User(id=uuid7(), clerk_user_id="user_prediction_tests", email="p@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: None

    return TestClient(app, raise_server_exceptions=False)


def test_the_feature_space_is_published(client: TestClient) -> None:
    payload = client.get(f"{PREFIX}/features").json()

    assert len(payload["features"]) == len(FEATURES)
    assert all(item["description"] for item in payload["features"])


def test_the_excluded_outcome_fields_are_published_too(client: TestClient) -> None:
    """"What does the model know about my trade" deserves a complete answer.

    Listing what the model *cannot* see is the clearest way to state that it is not
    reading the outcome, and it is checked against the tuple the barrier itself uses
    rather than a copy.
    """
    payload = client.get(f"{PREFIX}/features").json()

    assert payload["excluded_outcome_fields"] == list(OUTCOME_FIELDS)
    assert "net_pnl" in payload["excluded_outcome_fields"]


def test_no_published_feature_is_an_outcome_field(client: TestClient) -> None:
    """The contract the endpoint advertises must match the one the code enforces."""
    payload = client.get(f"{PREFIX}/features").json()
    names = {item["name"] for item in payload["features"]}

    assert names.isdisjoint(set(OUTCOME_FIELDS))


def test_the_note_explains_the_absent_categoricals(client: TestClient) -> None:
    """Strategy and setup are conspicuously missing, so the omission is explained."""
    note = client.get(f"{PREFIX}/features").json()["note"]

    assert "overfits or leaks" in note


def test_an_unknown_head_is_rejected(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/models/accuracy")

    assert response.status_code == 404
    assert "accuracy" in response.json()["error"]["message"]


def test_a_known_head_reaches_the_database_layer(client: TestClient) -> None:
    """The head name is validated before any query is attempted.

    With the session overridden to ``None``, a request that gets past validation fails
    inside the repository — which is the evidence that validation let it through. A 404
    here would mean the head check had rejected a legitimate head.
    """
    response = client.get(f"{PREFIX}/models/win_probability")

    assert response.status_code != 404
