"""Formatting for the account view: dates, ages, the facts-row phrase, the risk sentence and bars.

Pure functions only. Nothing here reads the clock or the config; callers pass `today` and
thresholds in, which keeps every branch testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from itertools import groupby

from cs_brief.core.briefs import AccountDetail, BreakdownRow, TimelineEntry
from cs_brief.core.config import Thresholds
from cs_brief.core.models import Health, Motion

BAR_TRACK_PX = 160  # the track of the signal with the largest max_points
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def day_label(day: date | datetime, today: date | datetime) -> str:
    """'14 October', with the year added ('14 October 2025') only when it differs from today's."""
    chosen = _as_date(day)
    label = f"{chosen.day} {_MONTHS[chosen.month - 1]}"
    if chosen.year != _as_date(today).year:
        label = f"{label} {chosen.year}"
    return label


def ago(days: int) -> str:
    """'today', 'N days ago' under 60 days, 'N months ago' under 730, then 'N years ago'."""
    if days <= 0:
        return "today"
    if days < 60:
        return _ago_phrase(days, "day")
    if days < 730:
        return _ago_phrase(days // 30, "month")
    return _ago_phrase(days // 365, "year")


def _ago_phrase(count: int, unit: str) -> str:
    return f"{count} {unit if count == 1 else unit + 's'} ago"


def motion_fact(detail: AccountDetail) -> str:
    """The facts-row phrase: the onboarding day, or where the renewal stands."""
    if detail.scored.motion is Motion.ONBOARDING:
        return f"Day {detail.days_since_start + 1} of onboarding"
    days = detail.days_to_renewal
    if days > 1:
        return f"Renewal in {days} days"
    if days == 1:
        return "Renewal tomorrow"
    if days == 0:
        return "Renews today"
    if days == -1:
        return "Renewal was yesterday"
    return f"Renewal was {-days} days ago"


def risk_sentence(risk: float, priority: float, health: Health, thresholds: Thresholds) -> str:
    """'Risk 82 of 100, which is At risk (45 or more). Priority 79 after weighting by ARR.'"""
    at_risk = number(thresholds.at_risk)
    watch = number(thresholds.watch)
    if health is Health.AT_RISK:
        band = f"{at_risk} or more"
    elif health is Health.WATCH:
        band = f"{watch} or more, under {at_risk}"
    else:
        band = f"under {watch}"
    return (
        f"Risk {number(risk)} of 100, which is {health.label} ({band}). "
        f"Priority {number(priority)} after weighting by ARR."
    )


def number(value: float) -> str:
    """Whole numbers with no decimal ('82'), otherwise one decimal place ('8.6')."""
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def bar_geometry(row: BreakdownRow, longest_max: float) -> tuple[int, int]:
    """(track, fill) in pixels for one breakdown row.

    The track is proportional to the signal's weight against the largest weight in the
    breakdown. The fill is the contribution's share of that track.
    """
    track = round(BAR_TRACK_PX * row.max_points / longest_max) if longest_max > 0 else 0
    if row.max_points <= 0:
        return track, 0
    return track, round(track * row.contribution / row.max_points)


def group_by_day(entries: Sequence[TimelineEntry]) -> list[tuple[date, list[TimelineEntry]]]:
    """Consecutive entries that share a calendar day, in the order given (newest first)."""
    return [
        (day, list(group))
        for day, group in groupby(entries, key=lambda entry: entry.observed_at.date())
    ]


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value
