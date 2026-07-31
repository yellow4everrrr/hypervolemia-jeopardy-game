"""HTTP surface tests that do not require a database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.interfaces.http.app import create_app
from app.interfaces.http.middleware import REQUEST_ID_HEADER


@pytest.fixture
def client() -> TestClient:
    settings = Settings(environment=Environment.TEST, log_json=False, auth_dev_bypass=True)
    return TestClient(create_app(settings), raise_server_exceptions=False)


def test_liveness_does_not_touch_dependencies(client: TestClient) -> None:
    """A database outage must not cause orchestrators to restart healthy containers."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_a_missing_database_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness must fail when the database is unreachable.

    The unreachable database is configured explicitly rather than assumed absent: a
    developer with Postgres running locally would otherwise see this test pass for the
    wrong reason, or fail for one.
    """
    from app.core.config import get_settings
    from app.infrastructure.db.session import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    monkeypatch.setenv(
        "LEDGERLINE_DATABASE_URL",
        "postgresql+asyncpg://nobody@127.0.0.1:59999/nothing",
    )
    monkeypatch.setenv("LEDGERLINE_ENVIRONMENT", "test")

    try:
        probe = TestClient(create_app(), raise_server_exceptions=False)
        response = probe.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "unavailable"
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


def test_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-me"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-me"


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers[REQUEST_ID_HEADER]


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/trades")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_error_envelope_shape(client: TestClient) -> None:
    """One envelope for every error means the frontend writes one handler."""
    payload = client.get("/api/v1/trades").json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "details"}


def test_openapi_is_served_outside_production(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200


def test_openapi_is_hidden_in_production() -> None:
    settings = Settings(environment=Environment.PRODUCTION, log_json=True)
    production = TestClient(create_app(settings), raise_server_exceptions=False)
    assert production.get("/openapi.json").status_code == 404
    assert production.get("/docs").status_code == 404


def test_dev_bypass_is_refused_in_production() -> None:
    """A misconfigured deployment must fail at boot, not authenticate anyone it likes."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LEDGERLINE_ENVIRONMENT", "production")
        patch.setenv("LEDGERLINE_AUTH_DEV_BYPASS", "true")
        with pytest.raises(ValueError, match="never be enabled in production"):
            get_settings()
    get_settings.cache_clear()
