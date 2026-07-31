"""Tests for session resolution.

Every case here is a real misattribution bug in trading journals: Sunday-evening fills
filed under Sunday, a DST week where a fixed UTC offset shifts every trade into the
wrong hour bucket, Friday-evening fills creating a phantom Saturday session.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from app.core.errors import DomainError
from app.domain.common.enums import SessionSegment
from app.domain.trading.session import SessionCalendar, resolve_session


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_regular_session_date_matches_the_calendar_day() -> None:
    """09:30 Chicago on a Tuesday is Tuesday's session."""
    point = resolve_session(utc(2026, 3, 10, 14, 30))  # 09:30 CDT
    assert point.session_date == date(2026, 3, 10)
    assert point.hour_of_day == 9


def test_evening_open_rolls_to_the_next_session_date() -> None:
    """18:00 Chicago on Sunday belongs to Monday's session, not Sunday's."""
    point = resolve_session(utc(2026, 3, 8, 23, 0))  # 18:00 CDT Sunday
    assert point.session_date == date(2026, 3, 9)
    assert point.session_date.isoweekday() == 1
    assert point.segment is SessionSegment.OVERNIGHT


def test_friday_evening_does_not_create_a_saturday_session() -> None:
    """Fills after Friday's close belong to the following Monday, not to a Saturday."""
    point = resolve_session(utc(2026, 3, 13, 23, 0))  # 18:00 CDT Friday
    assert point.session_date.isoweekday() == 1
    assert point.session_date == date(2026, 3, 16)


def test_just_before_rollover_stays_on_the_same_session() -> None:
    point = resolve_session(utc(2026, 3, 10, 21, 59))  # 16:59 CDT
    assert point.session_date == date(2026, 3, 10)


def test_daylight_saving_transition_is_handled_by_the_tz_database() -> None:
    """The same exchange-local hour maps to different UTC hours across a DST change.

    A fixed −6 offset would put the March trade in the 08:00 bucket and quietly split
    one hour-of-day statistic across two buckets.
    """
    winter = resolve_session(utc(2026, 1, 13, 15, 30))  # 09:30 CST
    summer = resolve_session(utc(2026, 7, 14, 14, 30))  # 09:30 CDT

    assert winter.hour_of_day == summer.hour_of_day == 9
    assert winter.segment is summer.segment is SessionSegment.RTH_MORNING


@pytest.mark.parametrize(
    ("local_hour", "local_minute", "expected"),
    [
        (1, 0, SessionSegment.ASIA),
        (3, 0, SessionSegment.LONDON),
        (7, 30, SessionSegment.PRE_MARKET),
        (8, 30, SessionSegment.RTH_OPEN),
        (9, 30, SessionSegment.RTH_MORNING),
        (11, 30, SessionSegment.RTH_MIDDAY),
        (13, 30, SessionSegment.RTH_AFTERNOON),
        (14, 50, SessionSegment.RTH_CLOSE),
        (16, 0, SessionSegment.POST_MARKET),
        (18, 0, SessionSegment.OVERNIGHT),
    ],
)
def test_segment_boundaries(local_hour: int, local_minute: int, expected: SessionSegment) -> None:
    from zoneinfo import ZoneInfo

    moment = datetime(
        2026, 3, 10, local_hour, local_minute, tzinfo=ZoneInfo("America/Chicago")
    )
    assert resolve_session(moment).segment is expected


def test_weekday_is_the_session_weekday_not_the_utc_weekday() -> None:
    """23:00 UTC Sunday is Monday's session — weekday statistics must agree."""
    point = resolve_session(utc(2026, 3, 8, 23, 30))
    assert point.weekday == 1


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(DomainError, match="naive datetime"):
        resolve_session(datetime(2026, 3, 10, 9, 30))  # noqa: DTZ001 — the point of the test


def test_custom_calendar_for_a_different_exchange() -> None:
    """NYMEX/New York products roll at their own local 17:00, not Chicago's."""
    calendar = SessionCalendar(timezone="America/New_York", rollover=time(18, 0))
    point = resolve_session(utc(2026, 3, 10, 23, 0), calendar)  # 19:00 New York
    assert point.session_date == date(2026, 3, 11)


def test_calendar_rejects_unordered_segments() -> None:
    with pytest.raises(DomainError, match="ordered by start time"):
        SessionCalendar(
            segments=(
                (time(0, 0), SessionSegment.ASIA),
                (time(2, 0), SessionSegment.LONDON),
                (time(1, 0), SessionSegment.RTH_OPEN),
            )
        )


def test_calendar_must_cover_midnight() -> None:
    with pytest.raises(DomainError, match="cover from midnight"):
        SessionCalendar(segments=((time(9, 0), SessionSegment.RTH_MORNING),))
