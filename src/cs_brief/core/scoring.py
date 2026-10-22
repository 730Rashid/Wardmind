"""Scoring engine: turns accounts + signals into a ranked, explained book.

Pure and deterministic: no I/O, no clock reads (callers pass `as_of`).
CONTRACT: function signatures below are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime

from cs_brief.core.config import ArrScaling, MotionWindows, ScoringConfig, SignalRule, Thresholds
from cs_brief.core.models import (
    Account,
    Health,
    Motion,
    Reason,
    ScoredAccount,
    Signal,
    SignalKind,
)


def latest_signals(signals: Iterable[Signal]) -> dict[str, dict[SignalKind, Signal]]:
    """Group by account id, keeping only the most recent signal of each kind."""
    # Ties on observed_at go to the larger value, so the result does not depend on input order.
    latest: dict[str, dict[SignalKind, Signal]] = {}
    for signal in signals:
        per_kind = latest.setdefault(signal.account_id, {})
        current = per_kind.get(signal.kind)
        if current is None or _recency(signal) > _recency(current):
            per_kind[signal.kind] = signal
    return latest


def _recency(signal: Signal) -> tuple[datetime, float]:
    return (signal.observed_at, signal.value)


def normalise(value: float, rule: SignalRule) -> float:
    """Linear 0..1 risk between rule.start (0) and rule.full (1), clamped."""
    return _clamp((value - rule.start) / (rule.full - rule.start))


def classify_motion(account: Account, as_of: date, windows: MotionWindows) -> Motion:
    """Onboarding, then Renewal, then Churn; first match wins (see config.MotionWindows).

    Onboarding needs a known start date: an account without one is never in onboarding,
    because there is no way to tell it is in its first `onboarding_days` days."""
    if (
        account.start_date is not None
        and (as_of - account.start_date).days < windows.onboarding_days
        and not account.milestones.complete
    ):
        return Motion.ONBOARDING
    days_to_renewal = (account.renewal_date - as_of).days
    if 0 <= days_to_renewal <= windows.renewal_window_days:
        return Motion.RENEWAL
    return Motion.CHURN


def classify_health(risk: float, thresholds: Thresholds) -> Health:
    # Both boundaries are inclusive: risk equal to a threshold falls in the higher band.
    if risk >= thresholds.at_risk:
        return Health.AT_RISK
    if risk >= thresholds.watch:
        return Health.WATCH
    return Health.HEALTHY


def arr_factor(arr: float, scaling: ArrScaling) -> float:
    """(1 - weight) + weight * arr_norm, where arr_norm is log-scaled floor..ceiling into 0..1."""
    if arr <= 0:
        arr_norm = 0.0
    else:
        log_floor = math.log(scaling.floor)
        log_ceiling = math.log(scaling.ceiling)
        arr_norm = _clamp((math.log(arr) - log_floor) / (log_ceiling - log_floor))
    return (1 - scaling.weight) + scaling.weight * arr_norm


def describe(kind: SignalKind, value: float) -> str:
    """Plain-English sentence for a signal value, e.g. 'Active users down 38% in 30 days'."""
    # Wording is decided on the rounded number, so the sentence always matches what it prints.
    n = _whole(value)
    match kind:
        case SignalKind.USAGE_TREND_30D:
            if n == 0:
                return "Active users flat over 30 days"
            if n < 0:
                return f"Active users down {-n}% in 30 days"
            return f"Active users up {n}% in 30 days"
        case SignalKind.DAYS_SINCE_LAST_LOGIN:
            if n == 0:
                return "Last login today"
            return f"No one has logged in for {n} {_unit(n, 'day', 'days')}"
        case SignalKind.OPEN_TICKETS_SEV1:
            if n == 0:
                return "No open severity-1 tickets"
            return f"{n} open severity-1 {_unit(n, 'ticket', 'tickets')}"
        case SignalKind.NPS_LATEST:
            return f"Latest NPS response was {n} out of 10"
        case SignalKind.CHAMPION_LEFT:
            return "Champion has left the company" if value >= 0.5 else "Champion still in place"
        case SignalKind.SEAT_UTILISATION:
            if n < 70:
                return f"Only {n}% of seats in use"
            return f"{n}% of seats in use"
        case SignalKind.BILLING_OVERDUE_DAYS:
            if n == 0:
                return "Billing up to date"
            return f"Invoice {n} {_unit(n, 'day', 'days')} overdue"
        case SignalKind.ONBOARDING_OVERDUE_MILESTONES:
            if n == 0:
                return "Onboarding milestones on track"
            return f"{n} onboarding {_unit(n, 'milestone', 'milestones')} overdue"
    raise ValueError(f"no wording for signal kind {kind!r}")


def score_account(
    account: Account,
    signals: dict[SignalKind, Signal],
    config: ScoringConfig,
    as_of: date,
) -> ScoredAccount:
    reasons: list[Reason] = []
    total = 0.0
    for kind in SignalKind:  # fixed order, so the float sum is identical on every run
        signal = signals.get(kind)
        rule = config.signals.get(kind)
        if signal is None or rule is None:
            continue
        contribution = round(rule.weight * normalise(signal.value, rule), 1)
        total += contribution
        if contribution > 0:
            reasons.append(
                Reason(
                    kind=kind,
                    contribution=contribution,
                    plain_english=describe(kind, signal.value),
                )
            )
    reasons.sort(key=lambda reason: (-reason.contribution, reason.kind.value))

    risk = round(min(100.0, total), 1)
    priority = round(min(100.0, risk * arr_factor(account.arr, config.arr)), 1)
    return ScoredAccount(
        account=account,
        motion=classify_motion(account, as_of, config.motion),
        health=classify_health(risk, config.thresholds),
        risk=risk,
        priority=priority,
        reasons=reasons,
        suggested_action="",
    )


def score_book(
    accounts: Iterable[Account],
    signals: Iterable[Signal],
    config: ScoringConfig,
    as_of: date | None = None,
) -> list[ScoredAccount]:
    """Score every account and sort by priority desc, then ARR desc, then name asc."""
    if as_of is None:
        as_of = date.today()
    by_account = latest_signals(signals)
    scored = [
        score_account(account, by_account.get(account.id, {}), config, as_of)
        for account in accounts
    ]
    # The account id is a last resort, so equal names still sort the same for any input order.
    scored.sort(key=_book_order)
    return scored


def _book_order(item: ScoredAccount) -> tuple[float, float, str, str]:
    """Sort key: priority desc, then ARR desc, then name and id asc."""
    return (-item.priority, -item.account.arr, item.account.name, item.account.id)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _whole(value: float) -> int:
    """Round half away from zero, so 12.5 reads as 13 (Python's round() would give 12)."""
    rounded = math.floor(abs(value) + 0.5)
    return -rounded if value < 0 else rounded


def _unit(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural
