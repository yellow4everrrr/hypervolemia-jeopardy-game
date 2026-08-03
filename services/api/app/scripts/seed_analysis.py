"""Queue the analyses the demo screens read from, so a fresh stack fills itself in.

`seed_demo` writes trades, bars and two monthly reports. It does not scan for patterns,
sweep counterfactual rules, or capture chart images, because those are *queued* work: in a
real deployment the worker picks them up after a broker sync. A freshly started demo stack
has never synced, so nothing is queued, so three screens open on their empty states —
Patterns saying "no scan has been run yet", Simulator saying "no sweep has been run yet",
and the replay gallery with no images.

Every one of those empty states is correct, and together they make a working application
look half-finished. The gap was never in the product: `make demo` promised a journal
"ready to open" and delivered a stack whose analytical half had not been asked to do
anything.

**Jobs are enqueued rather than executed here, and that is deliberate.** The simulator
screen reads its report from ``jobs.result`` — that is where the sweep lives, since it
computes a payload rather than writing to a table — so running the use case directly would
compute the right answer and put it nowhere the UI looks. Enqueuing also gets the work in
front of the worker that `docker-compose` runs, which is the path a real deployment uses,
and leaves the Jobs screen showing real rows instead of an empty queue.

The trade-off is that this returns before the work finishes. The sweep alone is about
ninety seconds. That is the honest shape of it: the stack is usable immediately, and the
two analytical screens fill in shortly after, with the Jobs screen showing exactly where
they have got to.

Safe to re-run: each job carries an idempotency key, so a second run adopts the existing
job rather than queueing a duplicate.

Usage::

    LEDGERLINE_DATABASE_URL=... python -m app.scripts.seed_analysis
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.domain.common.enums import JobKind
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.session import dispose_engine, session_scope
from app.jobs.queue import JobQueue
from app.scripts.seed_demo import DEMO_CLERK_USER_ID

logger = get_logger(__name__)

#: How many trades to capture charts for. The blotter opens newest-first, so the trades a
#: reviewer is most likely to click get images without spending several minutes rendering
#: six frames apiece for a year of history.
CAPTURE_TRADES = 12

#: What a freshly seeded demo needs before every screen has something real on it. Ordered
#: cheapest first, so the queue delivers the quick wins while the sweep is still running.
WORK: tuple[tuple[JobKind, dict[str, Any]], ...] = (
    (JobKind.DETECT_PATTERNS, {}),
    (JobKind.CAPTURE_SCREENSHOTS, {"limit": CAPTURE_TRADES}),
    (JobKind.RUN_SIMULATION, {}),
)


async def main() -> None:
    configure_logging(get_settings())

    async with session_scope() as session:
        user = (
            await session.execute(
                select(User).where(User.clerk_user_id == DEMO_CLERK_USER_ID)
            )
        ).scalar_one_or_none()
        if user is None:
            logger.error(
                "seed_analysis.no_demo_user",
                detail="run `python -m app.scripts.seed_demo` first",
            )
            return

        queue = JobQueue(session)
        for kind, payload in WORK:
            enqueued = await queue.enqueue(
                user_id=user.id,
                kind=kind,
                payload=payload,
                # Stable per kind, so re-running this script adopts the job already queued
                # instead of stacking a second copy behind it.
                idempotency_key=f"seed-analysis:{kind.value}",
            )
            logger.info(
                "seed_analysis.enqueued",
                kind=kind.value,
                job_id=str(enqueued.job_id),
                # False on a re-run: the idempotency key found the job already queued.
                created=enqueued.created,
            )

    logger.info("seed_analysis.queued", jobs=len(WORK))
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
