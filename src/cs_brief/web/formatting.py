"""Formatting for the account view, the weekly brief and onboarding prep: dates, ages, sentences
and bars.

Pure functions only. Nothing here reads the clock or the config; callers pass `today` and
thresholds in, which keeps every branch testable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from itertools import groupby

from cs_brief.core.briefs import AccountDetail, BreakdownRow, TimelineEntry
from cs_brief.core.config import Thresholds
from cs_brief.core.models import Health, Motion
from cs_brief.core.onboarding import MilestoneStatus, OnboardingBrief, PlanMilestone

BAR_TRACK_PX = 160  # the track of the signal with the largest max_points
# The book bar and its legend read left to right in this order.
BOOK_BAR_ORDER: tuple[Health, ...] = (Health.AT_RISK, Health.WATCH, Health.HEALTHY)
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


def long_date(day: date | datetime) -> str:
    """'21 October 2026'."""
    chosen = _as_date(day)
    return f"{chosen.day} {_MONTHS[chosen.month - 1]} {chosen.year}"


def week_of(day: date | datetime) -> str:
    """'Week of 19 October 2026', where 19 October is the Monday of the week that holds `day`."""
    chosen = _as_date(day)
    return f"Week of {long_date(chosen - timedelta(days=chosen.weekday()))}"


def book_bar_segments(
    arr_by_health: Mapping[Health, float], total_arr: float
) -> list[tuple[Health, float, float]]:
    """(health, x, width) in percent of `total_arr`, in BOOK_BAR_ORDER. Empty when there is no ARR.

    Bands with no ARR are left out. Edges are rounded to 2 decimals, and the last band is
    100 - x wide, so the bar has no gap at its end.
    """
    if total_arr <= 0:
        return []
    bands = [
        (health, arr_by_health.get(health, 0.0))
        for health in BOOK_BAR_ORDER
        if arr_by_health.get(health, 0.0) > 0
    ]
    starts: list[float] = []
    running = 0.0
    for _, arr in bands:
        starts.append(round(running / total_arr * 100, 2))
        running += arr
    segments: list[tuple[Health, float, float]] = []
    for index, (health, _) in enumerate(bands):
        start = starts[index]
        if index + 1 < len(bands):
            width = round(starts[index + 1] - start, 2)
        else:
            width = round(100 - start, 2)
        segments.append((health, start, width))
    return segments


def count_bar_width(count: int, largest: int) -> float:
    """A bar's fill as a percent of its track: 0 to 100, one decimal. 0 when `largest` is 0."""
    if largest <= 0:
        return 0.0
    return round(min(max(count, 0), largest) / largest * 100, 1)


def onboarding_summary(in_onboarding: int, behind: int) -> str:
    """The picker's sentence, e.g. '8 accounts are in onboarding. 3 are behind on their plan.'

    With no accounts in onboarding it is one sentence. `behind` counts accounts with an overdue
    milestone.
    """
    if in_onboarding == 0:
        return "No accounts are in onboarding right now."
    if in_onboarding == 1:
        first = "1 account is in onboarding."
    else:
        first = f"{in_onboarding} accounts are in onboarding."
    if behind == 0:
        second = "None are behind on their plan."
    elif behind == 1:
        second = "1 is behind on its plan."
    else:
        second = f"{behind} are behind on their plan."
    return f"{first} {second}"


def behind_on_plan(milestones: Sequence[PlanMilestone]) -> bool:
    """True when a milestone is overdue, which is what 'behind on the plan' means."""
    return _count_status(milestones, MilestoneStatus.OVERDUE) > 0


def plan_summary(milestones: Sequence[PlanMilestone]) -> str:
    """'2 of 6 done, 1 overdue'. The overdue part is left out when nothing is overdue."""
    done = _count_status(milestones, MilestoneStatus.DONE)
    overdue = _count_status(milestones, MilestoneStatus.OVERDUE)
    text = f"{done} of {len(milestones)} done"
    return f"{text}, {overdue} overdue" if overdue else text


_DOT_STATES: dict[MilestoneStatus, str] = {
    MilestoneStatus.DONE: "done",
    MilestoneStatus.OVERDUE: "overdue",
}


def plan_dots(milestones: Sequence[PlanMilestone]) -> list[str]:
    """One dot state per milestone, in plan order: done, overdue, or open for every other step."""
    return [_DOT_STATES.get(step.status, "open") for step in milestones]


def onboarding_plain_text(brief: OnboardingBrief) -> str:
    """The brief as plain text, for Copy as text. Sections are separated by a blank line."""
    account = brief.account
    facts = (
        f"Day {brief.day} of {brief.window_days}. {brief.health.label}. "
        f"${account.arr:,.0f} ARR. {account.owner}."
    )
    plan = [f"{step.position}. {step.name}: {step.status.label}" for step in brief.milestones]
    agenda = [
        f"{number}. {item.topic}: {item.detail}"
        for number, item in enumerate(brief.agenda, start=1)
    ]
    questions = [f"- {question}" for question in brief.questions]
    sections = [
        _section(f"{account.name} onboarding brief", [facts]),
        brief.summary,
        _section("The plan", plan),
        _section("Agenda for the call", agenda),
        _section("Questions to ask", questions),
        f"Next step: {brief.suggested_action}",
    ]
    return "\n\n".join(sections)


def _section(heading: str, lines: list[str]) -> str:
    return "\n".join([heading, *lines])


def _count_status(milestones: Sequence[PlanMilestone], status: MilestoneStatus) -> int:
    return sum(1 for step in milestones if step.status is status)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value
