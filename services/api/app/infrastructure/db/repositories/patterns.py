"""Storing what a pattern scan found — and what it looked at and could not establish.

``replace_patterns`` deletes the user's previous scan before inserting the new one,
rather than upserting. A scan is a *snapshot* of a body of evidence: as trades
accumulate, a pattern that cleared its threshold last month may not this month, and a
row left behind by upsert would keep asserting a finding the current evidence no longer
supports. Delete-then-insert makes the stored set always equal the last scan's verdict.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.discovery import DISCOVERY_VERSION
from app.analytics.types import TradeRecord
from app.application.use_cases.detect_patterns import PatternRow
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.common.enums import TradeStatus
from app.infrastructure.db.models.ai import DetectedPattern
from app.infrastructure.db.models.catalog import Setup, Strategy
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade

logger = get_logger(__name__)


class SqlAlchemyPatternRepository:
    """Reads trades for a scan and stores the scan's verdicts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
    ) -> list[TradeRecord]:
        """Closed trades, flattened for the analytics engine.

        Deliberately the same :class:`TradeRecord` shape the performance engine uses, so
        a pattern scan and a metric run always see identical inputs. Two loaders would
        eventually disagree about which trades count, and the AI layer would be citing
        statistics computed over different samples in the same sentence.
        """
        statement: Select[Any] = (
            select(
                Trade,
                Instrument.root_symbol,
                Instrument.symbol,
                Strategy.name,
                Setup.name,
            )
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .outerjoin(Strategy, Strategy.id == Trade.strategy_id)
            .outerjoin(Setup, Setup.id == Trade.setup_id)
            .where(Trade.user_id == user_id, Trade.status == TradeStatus.CLOSED)
            .order_by(Trade.opened_at)
        )
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)
        if session_from is not None:
            statement = statement.where(Trade.session_date >= session_from)
        if session_to is not None:
            statement = statement.where(Trade.session_date <= session_to)

        rows = (await self._session.execute(statement)).all()
        return [
            TradeRecord(
                trade_id=trade.id,
                account_id=trade.account_id,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                direction=trade.direction,
                net_pnl=trade.net_pnl,
                gross_pnl=trade.gross_pnl,
                commission=trade.commission,
                fees=trade.fees,
                quantity=trade.max_position_size,
                r_multiple=trade.realized_r,
                duration_seconds=trade.duration_seconds,
                session_date=trade.session_date,
                session_segment=trade.session_segment,
                entry_hour=trade.entry_hour,
                entry_weekday=trade.entry_weekday,
                instrument_symbol=symbol,
                instrument_root=root,
                strategy=strategy_name,
                setup=setup_name,
                mae_r=trade.mae_r,
                mfe_r=trade.mfe_r,
            )
            for trade, root, symbol, strategy_name, setup_name in rows
        ]

    async def replace_patterns(self, user_id: UUID, rows: list[PatternRow]) -> int:
        await self._session.execute(
            delete(DetectedPattern).where(DetectedPattern.user_id == user_id)
        )
        if not rows:
            return 0

        await self._session.execute(
            insert(DetectedPattern).values(
                [
                    {
                        "id": uuid7(),
                        "user_id": user_id,
                        "pattern_kind": row.pattern_kind,
                        "label": row.label,
                        "description": row.description,
                        "polarity": row.polarity,
                        "sample_size": row.sample_size,
                        "effect_size": row.effect_size,
                        "p_value": row.p_value,
                        "confidence_low": row.confidence_low,
                        "confidence_high": row.confidence_high,
                        "is_significant": row.is_significant,
                        "estimated_annual_impact": row.estimated_annual_impact,
                        "detail": row.detail,
                        "first_observed_at": row.first_observed_at,
                        "last_observed_at": row.last_observed_at,
                        "engine_version": DISCOVERY_VERSION,
                    }
                    for row in rows
                ]
            )
        )
        logger.info("patterns.stored", user_id=str(user_id), rows=len(rows))
        return len(rows)

    async def list_patterns(
        self, user_id: UUID, *, significant_only: bool = False
    ) -> list[DetectedPattern]:
        """The last scan's verdicts.

        Ordered by impact rather than by p-value: the most certain finding is rarely the
        most expensive one, and the trader is choosing what to work on.
        """
        statement = (
            select(DetectedPattern)
            .where(DetectedPattern.user_id == user_id)
            .order_by(
                DetectedPattern.is_significant.desc(),
                DetectedPattern.estimated_annual_impact.asc().nullslast(),
            )
        )
        if significant_only:
            statement = statement.where(DetectedPattern.is_significant.is_(True))
        return list((await self._session.execute(statement)).scalars().all())

    async def apply_setup(
        self,
        user_id: UUID,
        *,
        setup_name: str,
        assignments: list[dict[str, Any]],
    ) -> int:
        """Create (or reuse) a setup and label the proposed trades with it.

        Only ever called from an explicit accept action. An automatically applied label
        is a claim about what the trader was thinking, and ``setup_source = 'auto'``
        records that the machine proposed it so a later model evaluation can tell
        proposed labels from corrected ones.
        """
        setup = (
            await self._session.execute(
                select(Setup).where(Setup.user_id == user_id, Setup.name == setup_name)
            )
        ).scalar_one_or_none()

        if setup is None:
            setup = Setup(id=uuid7(), user_id=user_id, name=setup_name, is_system=False)
            self._session.add(setup)
            await self._session.flush()

        updated = 0
        for assignment in assignments:
            result = await self._session.execute(
                select(Trade).where(
                    Trade.id == UUID(assignment["trade_id"]), Trade.user_id == user_id
                )
            )
            trade = result.scalar_one_or_none()
            if trade is None:
                continue
            trade.setup_id = setup.id
            trade.setup_source = "auto"
            trade.setup_confidence = assignment["confidence"]
            updated += 1

        logger.info(
            "patterns.setup_applied",
            user_id=str(user_id),
            setup=setup_name,
            trades=updated,
        )
        return updated
