"""Briefs built from a scored book: the weekly brief and per-account explanations.

CONTRACT: the models and signatures below are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from cs_brief.core.models import Health, Motion, Reason, ScoredAccount


class WeeklyBrief(BaseModel):
    as_of: date
    total_accounts: int
    total_arr: float
    arr_at_risk: float  # sum of ARR for accounts with Health.AT_RISK
    counts_by_health: dict[Health, int]  # every Health key present, zero if none
    counts_by_motion: dict[Motion, int]  # every Motion key present, zero if none
    top: list[ScoredAccount]  # first `top_n` of the book, in book order
    notable: list[str]  # short plain-English call-outs, e.g. "3 renewals in the next 30 days"


class Explanation(BaseModel):
    account_id: str
    name: str
    motion: Motion
    health: Health
    risk: float
    priority: float
    reasons: list[Reason]
    suggested_action: str
    summary: str  # one or two sentences combining health, motion and the top reasons


def suggest_action(motion: Motion, reasons: list[Reason]) -> str:
    """Next best action from a rule table keyed by (motion, top reason kind), with fallbacks."""
    raise NotImplementedError


def weekly_brief(book: list[ScoredAccount], as_of: date, top_n: int = 10) -> WeeklyBrief:
    raise NotImplementedError


def explain(scored: ScoredAccount) -> Explanation:
    raise NotImplementedError
