"""Trading-session resolution.

"What day was this trade?" is not a question about UTC dates. CME futures open at
17:00 America/Chicago and the fills that follow belong to the *next* calendar
session — so a trade at 23:30 UTC on Sunday is Monday's business. Getting this wrong
misattributes P&L to the wrong day, breaks weekday statistics, and makes daily
journals line up with the wrong trades.

Segments are derived in the exchange's timezone so that daylight-saving transitions
are handled by the tz database rather than by arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.errors import DomainError
from app.domain.common.enums import SessionSegment

#: CME equity-index convention: the session opens the previous calendar day at 17:00
#: Chicago time and runs to 16:00 the next day.
DEFAULT_SESSION_ROLLOVER = time(17, 0)

#: Segment boundaries as (start_time, segment), evaluated in exchange-local time and
#: applied to the *clock time of day*, independent of which session date it belongs to.
_DEFAULT_SEGMENTS: tuple[tuple[time, SessionSegment], ...] = (
    (time(0, 0), SessionSegment.ASIA),
    (time(2, 0), SessionSegment.LONDON),
    (time(7, 0), SessionSegment.PRE_MARKET),
    (time(8, 30), SessionSegment.RTH_OPEN),
    (time(9, 30), SessionSegment.RTH_MORNING),
    (time(11, 0), SessionSegment.RTH_MIDDAY),
    (time(13, 0), SessionSegment.RTH_AFTERNOON),
    (time(14, 45), SessionSegment.RTH_CLOSE),
    (time(15, 15), SessionSegment.POST_MARKET),
    (time(17, 0), SessionSegment.OVERNIGHT),
)


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """Exchange session rules for one instrument family.

    Attributes:
        timezone: IANA zone the exchange quotes its session in.
        rollover: Local time at which the session date advances.
        segments: Ordered ``(local_start_time, segment)`` boundaries.
    """

    timezone: str = "America/Chicago"
    rollover: time = DEFAULT_SESSION_ROLLOVER
    segments: tuple[tuple[time, SessionSegment], ...] = _DEFAULT_SEGMENTS

    def __post_init__(self) -> None:
        if not self.segments:
            raise DomainError("a session calendar needs at least one segment")
        times = [entry[0] for entry in self.segments]
        if times != sorted(times):
            raise DomainError("session segments must be ordered by start time")
        if times[0] != time(0, 0):
            raise DomainError("session segments must cover from midnight")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class SessionPoint:
    """Where a timestamp falls in the trading week."""

    session_date: date
    segment: SessionSegment
    local_time: datetime

    @property
    def weekday(self) -> int:
        """ISO weekday of the *session*, 1 = Monday."""
        return self.session_date.isoweekday()

    @property
    def hour_of_day(self) -> int:
        """Exchange-local hour, the unit hourly performance is bucketed by."""
        return self.local_time.hour


def resolve_session(moment: datetime, calendar: SessionCalendar | None = None) -> SessionPoint:
    """Map a UTC instant to its session date and intraday segment.

    Args:
        moment: Timezone-aware timestamp.
        calendar: Exchange rules; defaults to the CME equity-index convention.

    Raises:
        DomainError: If ``moment`` is naive. A naive timestamp in a trading system is
            never safe to interpret — the "obvious" timezone is wrong for someone.
    """
    if moment.tzinfo is None:
        raise DomainError("cannot resolve a session for a naive datetime")

    calendar = calendar or SessionCalendar()
    local = moment.astimezone(calendar.tz)

    session_date = local.date()
    if local.time() >= calendar.rollover:
        session_date = session_date + timedelta(days=1)
    # Saturday fills belong to the week that just ended, not to a Sunday session; and a
    # Sunday-evening open rolls to Monday, which the rollover above already handles.
    if session_date.isoweekday() == 6:
        session_date = session_date + timedelta(days=2)

    return SessionPoint(
        session_date=session_date,
        segment=_segment_for(local.time(), calendar),
        local_time=local,
    )


def _segment_for(local_time: time, calendar: SessionCalendar) -> SessionSegment:
    segment = calendar.segments[0][1]
    for start, candidate in calendar.segments:
        if local_time >= start:
            segment = candidate
        else:
            break
    return segment
