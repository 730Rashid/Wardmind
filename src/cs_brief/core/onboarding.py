"""Onboarding prep: a call brief for an account in (or near the end of) onboarding.

Everything is derived from the account, its scored signals and the standard onboarding plan, with
no generated text from a model, so the same inputs always give the same brief.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from cs_brief.core.briefs import SIGNAL_LABELS, AccountDetail, BreakdownRow
from cs_brief.core.config import MotionWindows
from cs_brief.core.models import Account, Health, Motion, Reason, SignalKind

# The standard onboarding plan, in order. Accounts only record how many milestones they have
# (milestones.total), so an account with N milestones follows the first N-1 steps of this plan
# plus its final step, the onboarding review. The UI labels these as the standard plan.
STANDARD_PLAN: tuple[str, ...] = (
    "Kickoff call",
    "Admins set up",
    "Data imported",
    "Integrations connected",
    "Pilot team trained",
    "First workflow live",
    "Rolled out to all teams",
    "Onboarding review",
)

# Signals that matter during onboarding, in the order the brief lists them.
ONBOARDING_SIGNALS: tuple[SignalKind, ...] = (
    SignalKind.ONBOARDING_OVERDUE_MILESTONES,
    SignalKind.DAYS_SINCE_LAST_LOGIN,
    SignalKind.USAGE_TREND_30D,
    SignalKind.SEAT_UTILISATION,
    SignalKind.OPEN_TICKETS_SEV1,
    SignalKind.CHAMPION_LEFT,
)


class MilestoneStatus(StrEnum):
    DONE = "done"
    OVERDUE = "overdue"
    NEXT = "next"  # the first not-done, not-overdue milestone
    UPCOMING = "upcoming"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class PlanMilestone(BaseModel):
    position: int  # 1-based step number in this account's plan
    name: str
    status: MilestoneStatus


class AgendaItem(BaseModel):
    topic: str  # short, sentence case, e.g. "Progress on the plan"
    detail: str  # one or two sentences saying what to cover


class OnboardingBrief(BaseModel):
    account: Account
    health: Health
    motion: Motion
    in_onboarding: bool  # motion is Motion.ONBOARDING
    day: int | None  # days_since_start + 1, so the start date is day 1; None if it isn't known
    window_days: int  # MotionWindows.onboarding_days
    milestones: list[PlanMilestone]  # this account's plan, in order
    signals: list[BreakdownRow]  # the account's ONBOARDING_SIGNALS breakdown rows, in that order
    risks: list[Reason]  # the account's reasons, at most 3, largest first
    summary: str  # one or two sentences: where onboarding stands and the biggest risk
    agenda: list[AgendaItem]  # 3 to 6 items, in call order
    questions: list[str]  # 2 to 5 questions, each ending in "?"
    suggested_action: str


def plan_for(account: Account) -> list[PlanMilestone]:
    """This account's milestones with statuses.

    With total N (N >= 1): steps are STANDARD_PLAN[: N - 1] plus STANDARD_PLAN[-1]; N > 8 is
    capped at the 8 standard steps. The first `done` are DONE, the next `overdue` are OVERDUE,
    the first remaining one is NEXT and the rest are UPCOMING. N == 0 gives [].
    """
    total = min(account.milestones.total, len(STANDARD_PLAN))
    if total == 0:
        return []
    names = (*STANDARD_PLAN[: total - 1], STANDARD_PLAN[-1])
    # Clamp so the statuses still add up when the capped plan is shorter than the counts.
    done = min(account.milestones.done, total)
    overdue = min(account.milestones.overdue, total - done)
    return [
        PlanMilestone(position=index + 1, name=name, status=_status(index, done, overdue))
        for index, name in enumerate(names)
    ]


def _status(index: int, done: int, overdue: int) -> MilestoneStatus:
    """Status of the 0-based step `index` when `done` steps are done and `overdue` are late."""
    if index < done:
        return MilestoneStatus.DONE
    if index < done + overdue:
        return MilestoneStatus.OVERDUE
    if index == done + overdue:
        return MilestoneStatus.NEXT
    return MilestoneStatus.UPCOMING


def onboarding_brief(detail: AccountDetail, windows: MotionWindows) -> OnboardingBrief:
    """Build the call brief from an account's detail (see briefs.account_detail)."""
    scored = detail.scored
    window_days = windows.onboarding_days
    day = detail.days_since_start + 1
    days_left = window_days - day + 1
    milestones = plan_for(scored.account)
    risks = scored.reasons[:_MAX_RISKS]
    rows = {row.kind: row for row in detail.breakdown}
    in_onboarding = scored.motion is Motion.ONBOARDING
    return OnboardingBrief(
        account=scored.account,
        health=scored.health,
        motion=scored.motion,
        in_onboarding=in_onboarding,
        day=day,
        window_days=window_days,
        milestones=milestones,
        signals=[rows[kind] for kind in ONBOARDING_SIGNALS if kind in rows],
        risks=risks,
        summary=_summary(in_onboarding, days_left, window_days, milestones, risks),
        agenda=_agenda(in_onboarding, window_days, milestones, risks),
        questions=_questions(in_onboarding, milestones, risks),
        suggested_action=scored.suggested_action,
    )


_MAX_RISKS = 3  # the reasons a brief lists, largest first
_MIN_AGENDA = 3
_MAX_AGENDA = 6
_MIN_QUESTIONS = 2
_MAX_QUESTIONS = 5

_NO_OTHER_RISKS = "No other risk signals so far."
_WINS_TALKING_POINT = "Ask what has gone well so the team can share it more widely."
_WINS_QUESTION = "What has gone well so far?"
_CLOSING_QUESTION_ONBOARDING = "What would make the onboarding review a success for you?"
_CLOSING_QUESTION_NEXT = "What do you want to achieve next quarter?"

# One imperative sentence per signal: what to do with that risk on the call.
_TALKING_POINTS: dict[SignalKind, str] = {
    SignalKind.USAGE_TREND_30D: "Ask which teams have slowed down and what changed for them.",
    SignalKind.DAYS_SINCE_LAST_LOGIN: "Find out who should be using it this week.",
    SignalKind.OPEN_TICKETS_SEV1: "Share the fix dates for each ticket.",
    SignalKind.NPS_LATEST: "Ask what has frustrated them so far.",
    SignalKind.CHAMPION_LEFT: "Confirm who now owns the rollout.",
    SignalKind.SEAT_UTILISATION: "Plan who gets invited next.",
    SignalKind.BILLING_OVERDUE_DAYS: "Check the invoice reached the right contact.",
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: "Agree new dates for the late steps.",
}

# One question per signal. The milestones entry is unused: late steps get their own question.
_QUESTIONS: dict[SignalKind, str] = {
    SignalKind.USAGE_TREND_30D: "Which teams have stopped using it, and what changed for them?",
    SignalKind.DAYS_SINCE_LAST_LOGIN: "Who on your team should be logging in this week?",
    SignalKind.OPEN_TICKETS_SEV1: "Are the open severity-1 tickets blocking the rollout?",
    SignalKind.NPS_LATEST: "What would make the next month better for you?",
    SignalKind.CHAMPION_LEFT: "Who is our main contact now?",
    SignalKind.SEAT_UTILISATION: "Which teams haven't been invited yet?",
    SignalKind.BILLING_OVERDUE_DAYS: "Is the invoice with the right person in your team?",
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: "Which of the late milestones matters most to you?",
}


def _summary(
    in_onboarding: bool,
    days_left: int,
    window_days: int,
    milestones: list[PlanMilestone],
    risks: list[Reason],
) -> str:
    done = len(_with_status(milestones, MilestoneStatus.DONE))
    total = len(milestones)
    steps = _plural(total, "step", "steps")
    if not in_onboarding:
        opening = f"{done} of {total} onboarding {steps} done."
    elif _with_status(milestones, MilestoneStatus.OVERDUE):
        if days_left > 0:
            opening = f"Behind on the plan with {_days_text(days_left)} left."
        else:
            opening = f"Behind on the plan and past the {window_days}-day window."
    elif days_left > 0:
        opening = f"On track: {done} of {total} {steps} done with {_days_text(days_left)} left."
    else:
        opening = f"On track: {done} of {total} {steps} done, past the {window_days}-day window."
    return f"{opening} {_top_risk_sentence(risks)}"


def _top_risk_sentence(risks: list[Reason]) -> str:
    """The first risk that isn't the milestone one, which the opening sentence already covers."""
    for risk in risks:
        if risk.kind is not SignalKind.ONBOARDING_OVERDUE_MILESTONES:
            return f"{risk.plain_english}."
    return _NO_OTHER_RISKS


def _agenda(
    in_onboarding: bool,
    window_days: int,
    milestones: list[PlanMilestone],
    risks: list[Reason],
) -> list[AgendaItem]:
    progress = AgendaItem(topic="Progress on the plan", detail=_progress_detail(milestones))
    risk_items = [
        AgendaItem(
            topic=SIGNAL_LABELS[risk.kind],
            detail=f"{risk.plain_english}. {_TALKING_POINTS[risk.kind]}",
        )
        for risk in risks
        if risk.kind is not SignalKind.ONBOARDING_OVERDUE_MILESTONES
    ]
    closing: list[AgendaItem] = []
    if 1 + len(risk_items) < _MIN_AGENDA:
        closing.append(AgendaItem(topic="Wins so far", detail=_WINS_TALKING_POINT))
    if in_onboarding:
        closing.append(
            AgendaItem(
                topic="The onboarding review",
                detail=f"Confirm what success looks like by day {window_days}.",
            )
        )
    elif 1 + len(risk_items) + len(closing) < _MIN_AGENDA:
        closing.append(
            AgendaItem(topic="What comes next", detail="Agree the goals for the next quarter.")
        )
    # Over the cap, risk items give way first, from the end. The progress item and the closing
    # items always stay.
    room = max(0, _MAX_AGENDA - 1 - len(closing))
    return [progress, *risk_items[:room], *closing]


def _progress_detail(milestones: list[PlanMilestone]) -> str:
    total = len(milestones)
    done = len(_with_status(milestones, MilestoneStatus.DONE))
    overdue = [step.name for step in _with_status(milestones, MilestoneStatus.OVERDUE)]
    next_steps = _with_status(milestones, MilestoneStatus.NEXT)
    if done == total:
        return "Every step is done."
    verb = "is" if total == 1 else "are"
    head = f"{done} of {total} {_plural(total, 'step', 'steps')} {verb} done."
    if len(overdue) == 1:
        return f"{head} {overdue[0]} is overdue. Agree a new date."
    if overdue:
        return f"{head} {_join(overdue)} are overdue. Agree new dates."
    # Not all done and nothing overdue, so there is always a NEXT step here.
    return f"{head} Next up is {next_steps[0].name}. Agree who owns it and when."


def _questions(
    in_onboarding: bool,
    milestones: list[PlanMilestone],
    risks: list[Reason],
) -> list[str]:
    body: list[str] = []
    overdue = _with_status(milestones, MilestoneStatus.OVERDUE)
    next_steps = _with_status(milestones, MilestoneStatus.NEXT)
    if overdue:
        body.append(f"What is blocking {overdue[0].name}?")
    elif next_steps:
        body.append(f"Who owns {next_steps[0].name}, and when will it be done?")
    body.extend(
        _QUESTIONS[risk.kind]
        for risk in risks
        if risk.kind is not SignalKind.ONBOARDING_OVERDUE_MILESTONES
    )
    # One place is kept back for the closing question, which always comes last.
    body = list(dict.fromkeys(body))[: _MAX_QUESTIONS - 1]
    if len(body) + 1 < _MIN_QUESTIONS:
        body.append(_WINS_QUESTION)
    closing = _CLOSING_QUESTION_ONBOARDING if in_onboarding else _CLOSING_QUESTION_NEXT
    return [*body, closing]


def _with_status(milestones: list[PlanMilestone], status: MilestoneStatus) -> list[PlanMilestone]:
    return [step for step in milestones if step.status is status]


def _days_text(days: int) -> str:
    return f"{days} {_plural(days, 'day', 'days')}"


def _join(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural
