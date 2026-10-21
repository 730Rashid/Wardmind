"""Onboarding prep: a call brief for an account in (or near the end of) onboarding.

Everything is derived from the account, its scored signals and the standard onboarding plan, with
no generated text from a model, so the same inputs always give the same brief.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from cs_brief.core.briefs import AccountDetail, BreakdownRow
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
    day: int  # days_since_start + 1, so the start date is day 1
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
    raise NotImplementedError


def onboarding_brief(detail: AccountDetail, windows: MotionWindows) -> OnboardingBrief:
    """Build the call brief from an account's detail (see briefs.account_detail)."""
    raise NotImplementedError
