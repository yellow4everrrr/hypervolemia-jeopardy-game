"""Rule-builder endpoints, which need no database.

These three endpoints are the contract between the rule builder UI and the engine: the
field vocabulary, the starter set, and the validator that reads an expression back in
English before it is saved.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.domain.rules.context import AVAILABLE_FIELDS, STARTER_RULES
from app.interfaces.http.app import create_app

PREFIX = "/api/v1/strategies"


@pytest.fixture
def client() -> TestClient:
    settings = Settings(environment=Environment.TEST, log_json=False, auth_dev_bypass=True)
    return TestClient(create_app(settings), raise_server_exceptions=False)


def test_the_field_vocabulary_is_published(client: TestClient) -> None:
    """The builder renders this list, so it must match the engine's own contract."""
    payload = client.get(f"{PREFIX}/fields").json()

    assert {field["name"] for field in payload["fields"]} == set(AVAILABLE_FIELDS)
    assert all(field["description"] for field in payload["fields"])
    assert payload["severity_weights"]["critical"] == "8"


def test_every_starter_rule_parses_and_reads_as_english(client: TestClient) -> None:
    payload = client.get(f"{PREFIX}/starter-rules").json()

    assert len(payload["rules"]) == len(STARTER_RULES)
    for rule in payload["rules"]:
        assert rule["reads_as"]
        assert rule["severity"] in {"advisory", "minor", "major", "critical"}


def test_validation_reads_an_expression_back(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/validate",
        json={
            "expression": {
                "all": [
                    {"field": "has_stop", "op": "eq", "value": True},
                    {"field": "risk_pct_of_equity", "op": "lte", "value": "0.01"},
                ]
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_valid"] is True
    assert payload["fields"] == ["has_stop", "risk_pct_of_equity"]
    assert "and" in payload["reads_as"]


def test_a_typo_is_rejected_with_the_vocabulary(client: TestClient) -> None:
    """A rule naming a field we do not record would read as unevaluable forever."""
    response = client.post(
        f"{PREFIX}/validate",
        json={"expression": {"field": "has_stopp", "op": "eq", "value": True}},
    )

    assert response.status_code == 400
    details = response.json()["error"]["details"]
    assert details["unknown_fields"] == ["has_stopp"]
    assert "has_stop" in details["available"]


def test_an_unknown_operator_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/validate",
        json={"expression": {"field": "net_pnl", "op": "approximately", "value": 1}},
    )

    assert response.status_code == 400
    assert "lte" in response.json()["error"]["details"]["allowed"]


def test_a_deeply_nested_expression_is_refused(client: TestClient) -> None:
    """Guards the recursive evaluator against a generated or hostile tree."""
    expression: dict = {"field": "net_pnl", "op": "gte", "value": 0}
    for _ in range(20):
        expression = {"all": [expression]}

    response = client.post(f"{PREFIX}/validate", json={"expression": expression})

    assert response.status_code == 400
    assert "nests deeper" in response.json()["error"]["message"]
