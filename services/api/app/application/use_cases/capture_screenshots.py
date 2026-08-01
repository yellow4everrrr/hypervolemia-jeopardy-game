"""Capturing chart images for a trade, automatically, at the six moments that matter.

The schema has described this since milestone 1 — "capture is automatic at six moments per
trade" is in the ``screenshots`` docstring — and nothing implemented it. The table, the
enum and the object store all existed; no code ever wrote a row.

**Why the images are rendered rather than photographed.** The obvious reading of
"automatic screenshot" is a headless browser pointed at the replay page. That captures the
trader's own indicators, and costs a Chromium in the API image, seconds of latency per
frame, and a picture whose contents no test can assert on. Rendering the same bars the
replay endpoint already serves is deterministic, runs in milliseconds, and can be checked
pixel by pixel. What it cannot reproduce is a drawing the trader made by hand — which is
why manual upload stays supported and `Screenshot.source` distinguishes the two.

**Idempotent by content.** The storage key is a hash of the image bytes, and the render is
a pure function of the bars, so re-capturing an unchanged trade writes the same key and
the same row. That matters because this runs from an at-least-once job queue: a worker
that dies after storing the object but before committing the row will redo both, and the
second attempt must converge rather than accumulate.

**A trade with no bars produces no screenshots, and says so.** Not six blank images —
those would sit in a gallery looking like a chart that happened to be empty. The outcome
carries `skipped` with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.core.logging import get_logger
from app.domain.common.enums import ScreenshotKind, Timeframe
from app.domain.marketdata.bars import Bar, BarSeries, resample
from app.domain.marketdata.replay import ReplayWindow
from app.rendering.chart import render_chart

logger = get_logger(__name__)

#: What each frame is *of*, and how much of the window it shows. The fractions are of the
#: trade's own duration, so a two-minute scalp and a six-hour swing both get a
#: proportionate before-and-after rather than a fixed clock window that would show one
#: nothing and the other everything.
BEFORE_ENTRY_LEAD = Decimal("1.0")
AFTER_EXIT_TRAIL = Decimal("1.0")

#: Frames rendered for every trade. Ordered so a gallery reads chronologically.
CAPTURE_KINDS: tuple[ScreenshotKind, ...] = (
    ScreenshotKind.BEFORE_ENTRY,
    ScreenshotKind.ENTRY,
    ScreenshotKind.EXIT,
    ScreenshotKind.AFTER_EXIT,
    ScreenshotKind.EXECUTION_TIMEFRAME,
    ScreenshotKind.HIGHER_TIMEFRAME,
)

CONTENT_TYPE = "image/png"


@dataclass(frozen=True, slots=True)
class Frame:
    """One rendered image, before it is stored."""

    kind: ScreenshotKind
    timeframe: Timeframe
    png: bytes
    width: int
    height: int
    bars: int
    captured_at: datetime


@dataclass
class CaptureOutcome:
    trade_id: UUID
    captured: list[Frame] = field(default_factory=list)
    skipped: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "trade_id": str(self.trade_id),
            "captured": [
                {
                    "kind": frame.kind.value,
                    "timeframe": frame.timeframe.value,
                    "bytes": len(frame.png),
                    "bars": frame.bars,
                }
                for frame in self.captured
            ],
            "skipped": self.skipped,
        }


class ScreenshotRepository(Protocol):
    async def load_window(self, user_id: UUID, trade_id: UUID) -> ReplayWindow | None: ...

    async def load_bars(
        self, instrument_id: UUID, timeframe: Timeframe, *, start: datetime, end: datetime
    ) -> BarSeries: ...

    async def instrument_for(self, user_id: UUID, trade_id: UUID) -> UUID | None: ...

    async def record(
        self, user_id: UUID, trade_id: UUID, frame: Frame, *, storage_key: str
    ) -> None: ...


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> Any: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


def _slice(bars: list[Bar], *, start: datetime, end: datetime) -> list[Bar]:
    return [bar for bar in bars if start <= bar.ts <= end]


def frames_for(
    window: ReplayWindow,
    primary: list[Bar],
    higher: list[Bar],
    *,
    captured_at: datetime,
) -> list[Frame]:
    """Render the six frames from bars already loaded.

    Pure: separated from the use case so the framing decisions — which slice each kind
    gets, what it highlights — are testable without a database, an object store or a
    clock. The frames are where this feature is either useful or six copies of one chart.
    """
    markers = list(window.markers)
    duration = window.trade_end - window.trade_start
    lead = timedelta(seconds=int(Decimal(duration.total_seconds()) * BEFORE_ENTRY_LEAD))
    trail = timedelta(seconds=int(Decimal(duration.total_seconds()) * AFTER_EXIT_TRAIL))

    def frame(
        kind: ScreenshotKind,
        bars: list[Bar],
        timeframe: Timeframe,
        highlight: datetime | None,
    ) -> Frame:
        # Only the markers inside the slice are drawn. An exit marker on a before-entry
        # frame would show the outcome on the image whose entire purpose is the moment
        # before it was known — the same leakage the entry-snapshot barrier prevents in
        # the ML layer, in picture form.
        visible = [
            marker
            for marker in markers
            if marker.spans_trade
            or marker.at is None
            or (bars and bars[0].ts <= marker.at <= bars[-1].ts)
        ]
        render = render_chart(bars, markers=visible, highlight_at=highlight)
        return Frame(
            kind=kind,
            timeframe=timeframe,
            png=render.png,
            width=render.width,
            height=render.height,
            bars=render.bars,
            captured_at=captured_at,
        )

    tf = window.primary_timeframe
    return [
        frame(
            ScreenshotKind.BEFORE_ENTRY,
            _slice(primary, start=window.trade_start - lead, end=window.trade_start),
            tf,
            window.trade_start,
        ),
        frame(
            ScreenshotKind.ENTRY,
            _slice(primary, start=window.trade_start - lead, end=window.trade_end),
            tf,
            window.trade_start,
        ),
        frame(
            ScreenshotKind.EXIT,
            _slice(primary, start=window.trade_start, end=window.trade_end + trail),
            tf,
            window.trade_end,
        ),
        frame(
            ScreenshotKind.AFTER_EXIT,
            _slice(primary, start=window.trade_end, end=window.window_end),
            tf,
            window.trade_end,
        ),
        frame(ScreenshotKind.EXECUTION_TIMEFRAME, primary, tf, window.trade_start),
        frame(
            ScreenshotKind.HIGHER_TIMEFRAME,
            higher,
            window.higher_timeframe,
            window.trade_start,
        ),
    ]


class CaptureScreenshots:
    """Render and store the chart images for one trade."""

    def __init__(
        self,
        *,
        repository: ScreenshotRepository,
        storage: ObjectStorage,
        uow: UnitOfWork,
        key_builder: Any,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._uow = uow
        self._key_builder = key_builder

    async def execute(
        self, *, user_id: UUID, trade_id: UUID, now: datetime
    ) -> CaptureOutcome:
        outcome = CaptureOutcome(trade_id=trade_id)

        window = await self._repository.load_window(user_id, trade_id)
        instrument_id = await self._repository.instrument_for(user_id, trade_id)
        if window is None or instrument_id is None:
            outcome.skipped = "trade not found"
            return outcome

        series = await self._repository.load_bars(
            instrument_id,
            window.primary_timeframe,
            start=window.window_start,
            end=window.window_end,
        )
        if not series.bars:
            # No blank frames. Six empty images in a gallery look like six charts of a
            # quiet market rather than an absence of data.
            outcome.skipped = "no bars are stored for this window"
            logger.info(
                "screenshots.skipped", trade_id=str(trade_id), reason=outcome.skipped
            )
            return outcome

        primary = list(series.bars)
        higher = list(resample(series, window.higher_timeframe).bars)

        for frame in frames_for(window, primary, higher, captured_at=now):
            key = self._key_builder(
                user_id=user_id,
                trade_id=trade_id,
                kind=frame.kind,
                timeframe=frame.timeframe,
                checksum=_checksum(frame.png),
            )
            await self._storage.put(key, frame.png, content_type=CONTENT_TYPE)
            await self._repository.record(user_id, trade_id, frame, storage_key=key)
            outcome.captured.append(frame)

        await self._uow.commit()
        logger.info(
            "screenshots.captured", trade_id=str(trade_id), frames=len(outcome.captured)
        )
        return outcome


def _checksum(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
