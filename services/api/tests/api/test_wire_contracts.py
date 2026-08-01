"""The seam between Python and TypeScript, checked in the only place that can see both.

Every field name the frontend reads is transcribed by hand from a Python shape into
``apps/web/src/types/evidence.ts``. Nothing enforces the transcription: FastAPI does not
know the frontend exists, and TypeScript happily type-checks an interface that describes
an endpoint nobody has ever called. So a rename on the Python side leaves the TypeScript
compiling, the tests green, and the page rendering — with a blank where the number was,
because reading an absent property off a JSON object is ``undefined`` in JavaScript
rather than an error.

That is not hypothetical. Four fields shipped wrong at once — ``quantity`` for
``quantity_opened``, ``r_multiple`` for ``realized_r``, an ``instrument_symbol`` that no
endpoint returns, and a ``behaviours`` key on a payload whose key is ``items``. Three of
them rendered as silent blanks in the blotter. The fourth took the patterns page down,
and only because ``.filter`` on ``undefined`` happens to throw — had that call site used
optional chaining, as others do, it would instead have been an empty page reading
"nothing was found" over a scan that found two things.

The check runs in one direction on purpose: **every field TypeScript declares must exist
in the Python shape.** The converse is not required — an endpoint may return more than a
screen consumes, and demanding otherwise would turn every backend addition into a
frontend chore. What is forbidden is the frontend believing in a field the API does not
send.

Skipped rather than failed when the web app is absent, so the API suite still runs in a
backend-only checkout.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.domain.common.enums import JobKind, JobState
from app.infrastructure.db.models.ai import DetectedPattern
from app.infrastructure.db.models.jobs import Job
from app.interfaces.http.routers.jobs import _serialise as serialise_job
from app.interfaces.http.routers.patterns import serialise_pattern
from app.interfaces.http.schemas.trades import TradeListResponse, TradeSummary

TYPES_FILE = (
    Path(__file__).resolve().parents[4] / "apps" / "web" / "src" / "types" / "evidence.ts"
)

#: ``name?: type`` and ``name: type`` at the top level of an interface body — exactly two
#: spaces of indent. Fields of nested object literals are indented further and are
#: deliberately not matched: they are not separate wire shapes.
FIELD = re.compile(r"^  (\w+)\??:", re.MULTILINE)


def typescript_fields(interface: str) -> set[str]:
    """Field names declared on one exported interface.

    A deliberately small parser. Pulling a TypeScript AST into the Python test suite
    would make the check heavier than the thing it checks, and these shapes are plain
    data — no generics, no intersections, no computed keys. The assertion that it parsed
    *something* is what keeps the regex honest: a syntax change that silently matched
    nothing would otherwise turn every test below into a tautology.
    """
    if not TYPES_FILE.exists():  # pragma: no cover - backend-only checkout
        pytest.skip(f"{TYPES_FILE} not present")

    source = TYPES_FILE.read_text()
    match = re.search(
        rf"^export interface {interface} \{{$(.*?)^\}}$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"no exported interface {interface} in {TYPES_FILE}"
    fields = set(FIELD.findall(match.group(1)))
    assert fields, f"parsed no fields from {interface} — the regex has drifted"
    return fields


def assert_declared_fields_exist(interface: str, sent: set[str]) -> None:
    missing = typescript_fields(interface) - sent
    assert not missing, (
        f"{interface} in evidence.ts declares {sorted(missing)}, which the API does not "
        f"send. In JavaScript these read as undefined rather than raising, so they render "
        f"as blanks rather than failing. The API sends: {sorted(sent)}"
    )


def pydantic_fields(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


def pattern_projection_keys() -> set[str]:
    """Keys ``serialise_pattern`` emits, from a row with every column populated.

    Built by calling the projection rather than restating its keys, because a restated
    contract is not a checked one.
    """
    row = DetectedPattern(
        id=uuid4(),
        user_id=uuid4(),
        pattern_kind="overtrading",
        label="Overtrading",
        description="Trades taken beyond the fourth in a session",
        polarity="leak",
        sample_size=100,
        effect_size=Decimal("-0.41"),
        p_value=Decimal("0.004"),
        confidence_low=Decimal("-343.67"),
        confidence_high=Decimal("-78.20"),
        is_significant=True,
        estimated_annual_impact=Decimal("-21113.24"),
        detail={},
        first_observed_at=datetime.now(UTC),
        last_observed_at=datetime.now(UTC),
        engine_version=1,
    )
    return set(serialise_pattern(row))


def job_projection_keys() -> set[str]:
    now = datetime.now(UTC)
    job = Job(
        id=uuid4(),
        user_id=uuid4(),
        kind=JobKind.DETECT_PATTERNS,
        state=JobState.SUCCEEDED,
        attempts=1,
        max_attempts=5,
        run_after=now,
        created_at=now,
        started_at=now,
        finished_at=now,
        duration_ms=12,
        result={},
        last_error=None,
        payload={},
    )
    return set(serialise_job(job))


def test_trade_summary_matches_the_blotter_row() -> None:
    assert_declared_fields_exist("TradeSummary", pydantic_fields(TradeSummary))


def test_trade_list_response_matches_the_paginated_envelope() -> None:
    assert_declared_fields_exist("TradeListResponse", pydantic_fields(TradeListResponse))


def test_stored_pattern_matches_the_patterns_projection() -> None:
    assert_declared_fields_exist("StoredPattern", pattern_projection_keys())


def test_job_matches_the_queue_projection() -> None:
    assert_declared_fields_exist("Job", job_projection_keys())


def test_enqueued_job_matches_what_post_jobs_returns() -> None:
    """`POST /jobs` returns an acknowledgement, not a Job.

    Added after making exactly the mistake this file exists to prevent: the simulator was
    wired to read `.id` off the enqueue response, which has `job_id`. `undefined` then
    became the URL of the polling request. Nothing raised — it 404ed forever behind a
    spinner.
    """
    assert_declared_fields_exist("EnqueuedJob", enqueue_response_keys())


def enqueue_response_keys() -> set[str]:
    """Keys `POST /jobs` returns, read from `EnqueuedJob.to_payload` in the queue.

    Built by calling the same method the endpoint returns, so a rename there fails here.
    """
    from app.jobs.queue import Enqueued

    return set(
        Enqueued(job_id=uuid4(), kind=JobKind.RUN_SIMULATION, created=True).to_payload()
    )


def test_the_check_would_catch_the_bug_that_motivated_it() -> None:
    """The guard's own regression test.

    Without this, a parser that quietly matched nothing would leave every assertion above
    passing over an empty set. Here the failing case is constructed explicitly: the
    original wrong field names, against the shape the API actually sends.
    """
    sent = pydantic_fields(TradeSummary)
    for wrong in ("quantity", "r_multiple", "instrument_symbol"):
        assert wrong not in sent

    with pytest.raises(AssertionError, match="does not send"):
        assert_declared_fields_exist_for({"quantity", "r_multiple"}, sent)


def assert_declared_fields_exist_for(declared: set[str], sent: set[str]) -> None:
    """The body of :func:`assert_declared_fields_exist` with the parse step injected."""
    missing = declared - sent
    assert not missing, f"declares {sorted(missing)}, which the API does not send"
