"""Briefs built from a scored book: the weekly brief and per-account explanations.

CONTRACT: the models and signatures below are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from cs_brief.core.models import Health, Motion, Reason, ScoredAccount, SignalKind


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


_NO_ACTION = "No action needed this week."
_RENEWAL_CALLOUT_DAYS = 30
_MAX_SUMMARY_REASONS = 2
_MOTION_PHRASES: dict[Motion, str] = {
    Motion.ONBOARDING: "in onboarding",
    Motion.RENEWAL: "up for renewal",
    Motion.CHURN: "on churn watch",
}

# Next action per (motion, top reason). Each one is a single imperative sentence.
_ACTIONS: dict[tuple[Motion, SignalKind], str] = {
    (Motion.CHURN, SignalKind.USAGE_TREND_30D): (
        "Book a usage review with the admin to find what stopped working."
    ),
    (Motion.CHURN, SignalKind.DAYS_SINCE_LAST_LOGIN): (
        "Call the main admin to find out why nobody has logged in."
    ),
    (Motion.CHURN, SignalKind.OPEN_TICKETS_SEV1): (
        "Ask support for a fix date on each open severity-1 ticket."
    ),
    (Motion.CHURN, SignalKind.NPS_LATEST): "Book a call to ask what drove the low NPS score.",
    (Motion.CHURN, SignalKind.CHAMPION_LEFT): (
        "Find a new champion on the account and book an introductory call."
    ),
    (Motion.CHURN, SignalKind.SEAT_UTILISATION): (
        "Review unused seats with the admin and agree a plan to get them used."
    ),
    (Motion.CHURN, SignalKind.BILLING_OVERDUE_DAYS): (
        "Ask finance to confirm the invoice reached the right billing contact."
    ),
    (Motion.CHURN, SignalKind.ONBOARDING_OVERDUE_MILESTONES): (
        "Agree a date to clear the overdue milestones with the customer's project lead."
    ),
    (Motion.RENEWAL, SignalKind.USAGE_TREND_30D): (
        "Walk the admin through the usage drop and agree a fix before renewal."
    ),
    (Motion.RENEWAL, SignalKind.DAYS_SINCE_LAST_LOGIN): (
        "Check that the renewal contact is still active and book a check-in this week."
    ),
    (Motion.RENEWAL, SignalKind.OPEN_TICKETS_SEV1): (
        "Get a fix date for the severity-1 tickets before the renewal conversation."
    ),
    (Motion.RENEWAL, SignalKind.NPS_LATEST): (
        "Ask the sponsor what would make them renew with confidence."
    ),
    (Motion.RENEWAL, SignalKind.CHAMPION_LEFT): (
        "Find the new decision-maker and meet them before the renewal conversation."
    ),
    (Motion.RENEWAL, SignalKind.SEAT_UTILISATION): (
        "Offer a renewal quote sized to the seats actually in use."
    ),
    (Motion.RENEWAL, SignalKind.BILLING_OVERDUE_DAYS): (
        "Ask finance to chase the overdue invoice before the renewal quote goes out."
    ),
    (Motion.RENEWAL, SignalKind.ONBOARDING_OVERDUE_MILESTONES): (
        "Agree a date to close the overdue onboarding milestones before the renewal."
    ),
    (Motion.ONBOARDING, SignalKind.ONBOARDING_OVERDUE_MILESTONES): (
        "Reset the onboarding plan with the customer and agree new milestone dates."
    ),
    (Motion.ONBOARDING, SignalKind.USAGE_TREND_30D): (
        "Run a training session with the users who have stopped using the product."
    ),
    (Motion.ONBOARDING, SignalKind.DAYS_SINCE_LAST_LOGIN): (
        "Call the onboarding lead to find out why no one has logged in."
    ),
    (Motion.ONBOARDING, SignalKind.CHAMPION_LEFT): (
        "Find the new champion and re-confirm the onboarding goals with them."
    ),
}

# Used when the top reason has no entry in _ACTIONS.
_FALLBACK_ACTIONS: dict[Motion, str] = {
    Motion.ONBOARDING: "Book a check-in with the onboarding lead to review progress this week.",
    Motion.RENEWAL: "Book a renewal check-in with the sponsor to review value and open risks.",
    Motion.CHURN: "Book a check-in with the main contact to review how the account is doing.",
}


def suggest_action(motion: Motion, reasons: list[Reason]) -> str:
    """Next best action from a rule table keyed by (motion, top reason kind), with fallbacks."""
    if not reasons:
        return _NO_ACTION
    return _ACTIONS.get((motion, reasons[0].kind), _FALLBACK_ACTIONS[motion])


def weekly_brief(book: list[ScoredAccount], as_of: date, top_n: int = 10) -> WeeklyBrief:
    """Totals, counts by health and motion, the top of the book, and this week's call-outs."""
    counts_by_health = dict.fromkeys(Health, 0)
    counts_by_motion = dict.fromkeys(Motion, 0)
    for item in book:
        counts_by_health[item.health] += 1
        counts_by_motion[item.motion] += 1
    return WeeklyBrief(
        as_of=as_of,
        total_accounts=len(book),
        total_arr=sum((item.account.arr for item in book), 0.0),
        arr_at_risk=sum((item.account.arr for item in book if item.health is Health.AT_RISK), 0.0),
        counts_by_health=counts_by_health,
        counts_by_motion=counts_by_motion,
        top=book[:top_n],
        notable=_notable(book, as_of),
    )


def explain(scored: ScoredAccount) -> Explanation:
    """The account's fields plus a one- or two-sentence summary of why it is on the list."""
    return Explanation(
        account_id=scored.account.id,
        name=scored.account.name,
        motion=scored.motion,
        health=scored.health,
        risk=scored.risk,
        priority=scored.priority,
        reasons=list(scored.reasons),
        suggested_action=scored.suggested_action,
        summary=_summary(scored),
    )


def _notable(book: list[ScoredAccount], as_of: date) -> list[str]:
    """Call-outs in a fixed order. Each one appears only when its count is above zero."""
    renewals_due = sum(
        1
        for item in book
        if item.motion is Motion.RENEWAL
        and 0 <= (item.account.renewal_date - as_of).days <= _RENEWAL_CALLOUT_DAYS
    )
    champions_left = sum(1 for item in book if _has_reason(item, SignalKind.CHAMPION_LEFT))
    sev1_accounts = sum(1 for item in book if _has_reason(item, SignalKind.OPEN_TICKETS_SEV1))
    behind_onboarding = sum(
        1
        for item in book
        if item.motion is Motion.ONBOARDING
        and _has_reason(item, SignalKind.ONBOARDING_OVERDUE_MILESTONES)
    )

    notable: list[str] = []
    if renewals_due:
        renewal_word = _plural(renewals_due, "renewal", "renewals")
        notable.append(
            f"{renewals_due} {renewal_word} due in the next {_RENEWAL_CALLOUT_DAYS} days"
        )
    if champions_left:
        champion_word = _plural(champions_left, "champion", "champions")
        verb = _plural(champions_left, "has", "have")
        notable.append(f"{champions_left} {champion_word} {verb} left")
    if sev1_accounts:
        account_word = _plural(sev1_accounts, "account", "accounts")
        notable.append(f"{sev1_accounts} {account_word} with open severity-1 tickets")
    if behind_onboarding:
        account_word = _plural(behind_onboarding, "account", "accounts")
        notable.append(f"{behind_onboarding} onboarding {account_word} behind on milestones")
    return notable


def _summary(scored: ScoredAccount) -> str:
    """'{Health} and {motion}. {reason}, and {reason}.' with at most two reasons."""
    opening = f"{scored.health.label} and {_MOTION_PHRASES[scored.motion]}."
    if not scored.reasons:
        return f"{opening} No risk signals."
    first, *rest = [_reason_text(r.plain_english) for r in scored.reasons[:_MAX_SUMMARY_REASONS]]
    if not rest:
        return f"{opening} {first}."
    return f"{opening} {first}, and {_lower_first(rest[0])}."


def _reason_text(text: str) -> str:
    """Strip a trailing full stop so the summary never ends in two."""
    return text.strip().rstrip(".")


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _has_reason(item: ScoredAccount, kind: SignalKind) -> bool:
    return any(reason.kind is kind for reason in item.reasons)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural
