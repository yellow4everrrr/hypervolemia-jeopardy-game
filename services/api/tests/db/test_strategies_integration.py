"""Listing strategies loaded from a cold session.

``GET /strategies`` returned a 500 for every user who had a strategy. ``StrategySummary``
declares a ``rules`` field and sets ``from_attributes=True``, so
``StrategySummary.model_validate(strategy)`` read ``strategy.rules`` — a lazy
relationship. Under asyncio a lazy load raises ``MissingGreenlet`` instead of emitting a
query, and the endpoint died before it could assign the rules it had already fetched.

The reason no test caught it is the reason this file exists. Every existing test created a
strategy through the repository and listed it **in the same session**, where the
relationship is already populated in the identity map and no lazy load occurs. Production
never does that: a request loads a strategy it did not create, in a session that has never
seen it. The fixture below reproduces that by committing, expunging, and reading back.

The endpoint is exercised rather than ``_to_summary`` directly, because the defect was in
how the ORM object reached Pydantic, not in the projection's arithmetic.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Environment, Settings
from app.core.ids import uuid7
from app.domain.common.enums import RuleSeverity, RuleType
from app.domain.rules.context import STARTER_RULES
from app.infrastructure.db.models.catalog import Strategy, StrategyRule
from app.infrastructure.db.models.identity import User
from app.interfaces.http.app import create_app
from app.interfaces.http.deps import get_current_user, get_session

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    created = create_async_engine(os.environ["LEDGERLINE_TEST_DATABASE_URL"])
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def seeded(engine: Any) -> AsyncIterator[tuple[User, UUID]]:
    """A user with one strategy and the full starter rule set, committed and forgotten.

    Committing matters: an uncommitted strategy lives in the creating session's identity
    map, and reading it back from there is exactly the shortcut that hid the bug.
    """
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    user = User(
        id=uuid7(), clerk_user_id=f"user_{uuid7().hex[:12]}", email="s@example.com"
    )
    strategy = Strategy(
        id=uuid7(), user_id=user.id, name=f"Strategy {uuid7().hex[:6]}", version=1
    )

    async with maker() as session:
        # Flushed separately: `strategy.user_id` is a plain column rather than a mapped
        # relationship, so the unit of work has no dependency to order the inserts by and
        # will happily write the strategy first.
        session.add(user)
        await session.flush()
        session.add(strategy)
        await session.flush()
        for rule in STARTER_RULES:
            session.add(
                StrategyRule(
                    id=uuid7(),
                    user_id=user.id,
                    strategy_id=strategy.id,
                    rule_type=RuleType(rule["rule_type"]),
                    code=rule["code"],
                    label=rule["label"],
                    severity=RuleSeverity(rule["severity"]),
                    expression=rule["expression"],
                )
            )
        await session.commit()

    yield user, strategy.id

    async with maker() as session:
        await session.delete(await session.get(User, user.id))  # type: ignore[arg-type]
        await session.commit()


@pytest_asyncio.fixture
async def client(engine: Any, seeded: tuple[User, UUID]) -> AsyncIterator[AsyncClient]:
    """A client whose session is **new**, as a real request's would be.

    `AsyncClient` over an ASGI transport rather than `TestClient`. The distinction is not
    stylistic here: `TestClient` drives the app through its own blocking portal, on a
    different event loop from the one the engine was created in, and asyncpg connections
    are bound to their loop — the request fails with "attached to a different loop" before
    it can reach the code under test. Everything below shares one loop.
    """
    user, _ = seeded
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(Settings(environment=Environment.TEST, log_json=False))
    app.dependency_overrides[get_current_user] = lambda: user

    async def fresh_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = fresh_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


async def test_listing_strategies_does_not_lazy_load(client: AsyncClient) -> None:
    """The regression. Against the previous code this is a 500, not a 200."""
    response = await client.get("/api/v1/strategies")

    assert response.status_code == 200, response.text
    assert response.json()["items"], "the seeded strategy was not returned"


async def test_the_rules_are_the_ones_that_were_stored(client: AsyncClient) -> None:
    """Guards the fix rather than only the status code.

    The projection now names every field explicitly, so a mistyped field would return 200
    with the wrong contents — a quieter failure than the one being fixed.
    """
    payload = (await client.get("/api/v1/strategies")).json()["items"][0]

    assert {rule["code"] for rule in payload["rules"]} == {
        rule["code"] for rule in STARTER_RULES
    }
    assert payload["version"] == 1
    assert payload["is_active"] is True
    assert payload["name"]


async def test_fetching_one_strategy_also_avoids_the_relationship(
    client: AsyncClient, seeded: tuple[User, UUID]
) -> None:
    """The detail endpoint shares `_to_summary` and would share the defect."""
    _, strategy_id = seeded

    response = await client.get(f"/api/v1/strategies/{strategy_id}")

    assert response.status_code == 200, response.text
    assert len(response.json()["rules"]) == len(STARTER_RULES)
