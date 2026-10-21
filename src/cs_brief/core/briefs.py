"""Briefs built from a scored book: the weekly brief and per-account explanations.

CONTRACT: the models and signatures below are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from cs_brief.core.config import ScoringConfig
from cs_brief.core.models import Health, Motion, Reason, ScoredAccount, Signal, SignalKind
from cs_brief.core.scoring import describe, latest_signals, normalise

# Short names for each signal, used as row labels in the account view.
SIGNAL_LABELS: dict[SignalKind, str] = {
    SignalKind.USAGE_TREND_30D: "Usage trend",
    SignalKind.DAYS_SINCE_LAST_LOGIN: "Last login",
    SignalKind.OPEN_TICKETS_SEV1: "Severity-1 tickets",
    SignalKind.NPS_LATEST: "NPS",
    SignalKind.CHAMPION_LEFT: "Champion",
    SignalKind.SEAT_UTILISATION: "Seats in use",
    SignalKind.BILLING_OVERDUE_DAYS: "Billing",
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: "Onboarding milestones",
}


class WeeklyBrief(BaseModel):
    as_of: date
    total_accounts: int
    total_arr: float
    arr_at_risk: float  # sum of ARR for accounts with Health.AT_RISK
    arr_by_health: dict[Health, float]  # every Health key present, zero if none
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


class BreakdownRow(BaseModel):
    """One signal's part in the score. The account view shows a row for every signal kind."""

    kind: SignalKind
    label: str  # SIGNAL_LABELS[kind]
    value_text: str  # describe(kind, latest value), or "No reading" when there is none
    contribution: float  # risk points from the latest reading (0 when none or not triggered)
    max_points: float  # the rule's weight: what this signal adds when fully triggered
    observed_at: datetime | None  # when the latest reading was taken


class TimelineEntry(BaseModel):
    """One reading in the account's signal history."""

    observed_at: datetime
    kind: SignalKind
    label: str  # SIGNAL_LABELS[kind]
    value: float
    plain_english: str  # describe(kind, value)
    contribution: float  # risk points this reading adds under the current config
    is_latest: bool  # True for the reading the score uses (one per kind)


class AccountDetail(BaseModel):
    scored: ScoredAccount
    explanation: Explanation
    rank: int  # 1-based position in the full book
    book_size: int
    days_since_start: int
    days_to_renewal: int  # negative once the renewal date has passed
    # Every SignalKind: contribution desc, then kind value asc (the same tie-break as
    # ScoredAccount.reasons, so the non-zero rows match the reasons exactly).
    breakdown: list[BreakdownRow]
    timeline: list[TimelineEntry]  # every reading for the account, newest first


class RankChange(BaseModel):
    """How one account's rank moves when the scoring config changes (for the Settings preview)."""

    account_id: str
    name: str
    health: Health  # under the new config
    priority: float  # under the new config
    new_rank: int  # 1-based, in the new book
    old_rank: int  # 1-based, in the old book
    movement: int  # old_rank - new_rank: positive means it moved up the list
    entered_top: bool  # True when old_rank was outside the top_n being compared


def compare_rankings(
    before: list[ScoredAccount], after: list[ScoredAccount], top_n: int = 10
) -> list[RankChange]:
    """The first `top_n` of `after`, in order, each with its rank in `before`.

    Both are full books of the same accounts (the same data scored under two configs).
    Raises ValueError if an account in `after` is missing from `before`.
    """
    raise NotImplementedError


def account_detail(
    book: list[ScoredAccount],
    account_id: str,
    signals: list[Signal],
    config: ScoringConfig,
    as_of: date,
) -> AccountDetail | None:
    """Everything the account view needs. `signals` is that account's full history.

    Returns None when the account isn't in the book. Ties in the timeline (same observed_at)
    sort by SignalKind order so the output is deterministic.
    """
    position = next(
        (index for index, item in enumerate(book) if item.account.id == account_id), None
    )
    if position is None:
        return None
    scored = book[position]
    account = scored.account
    readings = [signal for signal in signals if signal.account_id == account_id]
    latest = latest_signals(readings).get(account_id, {})
    return AccountDetail(
        scored=scored,
        explanation=explain(scored),
        rank=position + 1,
        book_size=len(book),
        days_since_start=(as_of - account.start_date).days,
        days_to_renewal=(account.renewal_date - as_of).days,
        breakdown=_breakdown(latest, config),
        timeline=_timeline(readings, latest, config),
    )


_NO_READING = "No reading"
# Declaration position of each kind, the timeline's tie-break on equal observed_at.
_KIND_ORDER: dict[SignalKind, int] = {kind: index for index, kind in enumerate(SignalKind)}


def _breakdown(latest: dict[SignalKind, Signal], config: ScoringConfig) -> list[BreakdownRow]:
    rows: list[BreakdownRow] = []
    for kind in SignalKind:
        rule = config.signals[kind]
        reading = latest.get(kind)
        if reading is None:
            rows.append(
                BreakdownRow(
                    kind=kind,
                    label=SIGNAL_LABELS[kind],
                    value_text=_NO_READING,
                    contribution=0.0,
                    max_points=rule.weight,
                    observed_at=None,
                )
            )
        else:
            rows.append(
                BreakdownRow(
                    kind=kind,
                    label=SIGNAL_LABELS[kind],
                    value_text=describe(kind, reading.value),
                    contribution=_contribution(reading, config),
                    max_points=rule.weight,
                    observed_at=reading.observed_at,
                )
            )
    # Same tie-break as scoring.score_account's reasons, so the non-zero rows match them exactly.
    return sorted(rows, key=lambda row: (-row.contribution, row.kind.value))


def _timeline(
    readings: list[Signal], latest: dict[SignalKind, Signal], config: ScoringConfig
) -> list[TimelineEntry]:
    # Newest first. Equal timestamps go by SignalKind order, then by the larger value.
    ordered = sorted(
        readings,
        key=lambda signal: (signal.observed_at, -_KIND_ORDER[signal.kind], signal.value),
        reverse=True,
    )
    entries: list[TimelineEntry] = []
    flagged: set[SignalKind] = set()
    for signal in ordered:
        # Only the one reading latest_signals picked for each kind is flagged.
        is_latest = signal is latest.get(signal.kind) and signal.kind not in flagged
        if is_latest:
            flagged.add(signal.kind)
        entries.append(
            TimelineEntry(
                observed_at=signal.observed_at,
                kind=signal.kind,
                label=SIGNAL_LABELS[signal.kind],
                value=signal.value,
                plain_english=describe(signal.kind, signal.value),
                contribution=_contribution(signal, config),
                is_latest=is_latest,
            )
        )
    return entries


def _contribution(signal: Signal, config: ScoringConfig) -> float:
    """Risk points one reading adds. Same formula and rounding as scoring.score_account."""
    rule = config.signals[signal.kind]
    return round(rule.weight * normalise(signal.value, rule), 1)


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
    (Motion.ONBOARDING, SignalKind.OPEN_TICKETS_SEV1): (
        "Get a fix date for the severity-1 tickets and share it with the onboarding lead."
    ),
    (Motion.ONBOARDING, SignalKind.NPS_LATEST): (
        "Ask the onboarding lead what has frustrated them so far."
    ),
    (Motion.ONBOARDING, SignalKind.SEAT_UTILISATION): (
        "Plan the next rollout wave with the onboarding lead to get more seats used."
    ),
    (Motion.ONBOARDING, SignalKind.BILLING_OVERDUE_DAYS): (
        "Ask finance to confirm the first invoice reached the right billing contact."
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
    arr_by_health = dict.fromkeys(Health, 0.0)
    for item in book:
        counts_by_health[item.health] += 1
        counts_by_motion[item.motion] += 1
        arr_by_health[item.health] += item.account.arr
    return WeeklyBrief(
        as_of=as_of,
        total_accounts=len(book),
        total_arr=sum((item.account.arr for item in book), 0.0),
        arr_at_risk=arr_by_health[Health.AT_RISK],
        arr_by_health=arr_by_health,
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
