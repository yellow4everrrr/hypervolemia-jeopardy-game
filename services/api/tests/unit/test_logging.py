"""Tests for logging configuration.

Logging is easy to get subtly wrong in ways nothing else catches: a doubly-rendered
line still "works", it just becomes unparseable the moment anything tries to query it.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.config import Environment, Settings
from app.core.logging import bind_contextvars, clear_contextvars, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    clear_contextvars()
    yield
    clear_contextvars()
    logging.getLogger().handlers = []


def test_events_are_rendered_exactly_once(capsys: pytest.CaptureFixture[str]) -> None:
    """One log call must produce one JSON object, not one nested inside another."""
    configure_logging(Settings(environment=Environment.TEST, log_json=True))
    get_logger("test").info("trade.reconstructed", trade_count=3)

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)  # Fails outright if the line was rendered twice.

    assert payload["event"] == "trade.reconstructed"
    assert payload["trade_count"] == 3
    assert payload["level"] == "info"
    assert payload["logger"] == "test"
    assert payload["timestamp"].endswith("Z")


def test_context_variables_are_merged_into_every_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Request id and user id must reach logs written deep in the call stack."""
    configure_logging(Settings(environment=Environment.TEST, log_json=True))
    bind_contextvars(request_id="abc123", user_id="user-1")
    get_logger("test").warning("ingest.slow")

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["request_id"] == "abc123"
    assert payload["user_id"] == "user-1"


def test_third_party_records_are_formatted_too(capsys: pytest.CaptureFixture[str]) -> None:
    """A uvicorn or SQLAlchemy record must not bypass the JSON formatter."""
    configure_logging(Settings(environment=Environment.TEST, log_json=True))
    logging.getLogger("uvicorn.error").error("socket closed")

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "socket closed"
    assert payload["level"] == "error"


def test_console_renderer_used_when_json_disabled(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(environment=Environment.LOCAL, log_json=False))
    get_logger("test").info("app.starting")

    out = capsys.readouterr().out
    assert "app.starting" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(environment=Environment.TEST, log_json=True, log_level="WARNING"))
    logger = get_logger("test")
    logger.info("should.not.appear")
    logger.warning("should.appear")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "should.appear"
