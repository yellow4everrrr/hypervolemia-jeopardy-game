"""Pattern detection endpoints.

A scan is an explicit POST rather than something a dashboard triggers on render. It runs
a permutation test per detector plus a null-reference battery for the clustering, which
puts it in the seconds — acceptable when a trader asks "what am I doing wrong?", not
acceptable on every page load. Milestone 13 moves it to a background job; until then the
stored result is read back from ``GET /patterns``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.analytics.discovery import DiscoveryConfig
from app.application.use_cases.detect_patterns import DetectPatterns
from app.core.errors import ValidationError
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep

router = APIRouter(prefix="/patterns", tags=["patterns"])

#: A faster, more permissive scan for interactive use. Fewer permutations coarsens the
#: p-value, which makes the test *more* conservative, never less — a finding that
#: survives a quick scan would also survive a thorough one.
QUICK = DiscoveryConfig(permutations=2_000, references=20)


class ApplySetupRequest(BaseModel):
    """Accept a proposed cluster as a named setup."""

    setup_name: Annotated[str, Field(min_length=1, max_length=120)]
    trade_ids: Annotated[list[UUID], Field(min_length=1, max_length=5_000)]
    confidences: dict[UUID, Decimal] = Field(default_factory=dict)


@router.post("/scan", summary="Scan for behavioural leaks and hidden groupings")
async def scan(
    user: CurrentUserDep,
    session: SessionDep,
    account_id: Annotated[UUID | None, Query()] = None,
    session_from: Annotated[date | None, Query()] = None,
    session_to: Annotated[date | None, Query()] = None,
    quick: Annotated[bool, Query(description="Fewer permutations, faster response")] = True,
    persist: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Run the whole scan and store every tested pattern.

    The response distinguishes three things that are easy to conflate: what was tested
    (`sample.tests_performed`), what was found (`sample.findings`), and everything the
    scan looked at with its individual verdict (`behaviours`, `clusters`). Only the
    middle one is a claim.
    """
    if session_from and session_to and session_from > session_to:
        raise ValidationError("session_from must not be after session_to")

    use_case = DetectPatterns(
        repository=SqlAlchemyPatternRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    outcome = await use_case.execute(
        user_id=user.id,
        account_id=account_id,
        session_from=session_from,
        session_to=session_to,
        config=QUICK if quick else None,
        persist=persist,
    )
    return outcome.to_payload()


@router.get("", summary="Patterns from the last scan")
async def list_patterns(
    user: CurrentUserDep,
    session: SessionDep,
    significant_only: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Read back the stored scan.

    Non-significant patterns are returned by default and flagged rather than hidden. A
    trader who can see that revenge trading *was* tested and did not clear the bar knows
    something real about their own trading; hiding it leaves them assuming it was never
    examined.
    """
    repository = SqlAlchemyPatternRepository(session)
    rows = await repository.list_patterns(user.id, significant_only=significant_only)

    return {
        "items": [
            {
                "id": str(row.id),
                "kind": row.pattern_kind,
                "label": row.label,
                "description": row.description,
                "polarity": row.polarity,
                "sample_size": row.sample_size,
                "effect_size": _s(row.effect_size),
                "p_value": _s(row.p_value),
                "confidence_low": _s(row.confidence_low),
                "confidence_high": _s(row.confidence_high),
                "is_significant": row.is_significant,
                "estimated_annual_impact": _s(row.estimated_annual_impact),
                "detail": row.detail,
                "engine_version": row.engine_version,
            }
            for row in rows
        ],
        "note": (
            "Patterns that did not clear the significance threshold are included and "
            "marked is_significant = false. They were tested and not established, which "
            "is different from not having been looked at."
        ),
    }


@router.post("/setups", summary="Accept a proposed cluster as a named setup")
async def apply_setup(
    payload: ApplySetupRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Label the trades of an accepted cluster with a setup the trader names.

    Explicit by design. The scan proposes; it does not apply. An automatic label is a
    claim about what the trader was thinking when they took the trade, and the journal
    does not get to make that claim on their behalf.
    """
    repository = SqlAlchemyPatternRepository(session)
    updated = await repository.apply_setup(
        user.id,
        setup_name=payload.setup_name,
        assignments=[
            {
                "trade_id": str(trade_id),
                "confidence": payload.confidences.get(trade_id),
            }
            for trade_id in payload.trade_ids
        ],
    )
    await SqlAlchemyUnitOfWork(session).commit()
    return {"setup_name": payload.setup_name, "trades_labelled": updated}


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
