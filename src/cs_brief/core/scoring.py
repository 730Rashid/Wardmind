"""Scoring engine: turns accounts + signals into a ranked, explained book.

Pure and deterministic: no I/O, no clock reads (callers pass `as_of`).
CONTRACT: function signatures below are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from cs_brief.core.config import ArrScaling, MotionWindows, ScoringConfig, SignalRule, Thresholds
from cs_brief.core.models import (
    Account,
    Health,
    Motion,
    ScoredAccount,
    Signal,
    SignalKind,
)


def latest_signals(signals: Iterable[Signal]) -> dict[str, dict[SignalKind, Signal]]:
    """Group by account id, keeping only the most recent signal of each kind."""
    raise NotImplementedError


def normalise(value: float, rule: SignalRule) -> float:
    """Linear 0..1 risk between rule.start (0) and rule.full (1), clamped."""
    raise NotImplementedError


def classify_motion(account: Account, as_of: date, windows: MotionWindows) -> Motion:
    """Onboarding, then Renewal, then Churn; first match wins (see config.MotionWindows)."""
    raise NotImplementedError


def classify_health(risk: float, thresholds: Thresholds) -> Health:
    raise NotImplementedError


def arr_factor(arr: float, scaling: ArrScaling) -> float:
    """(1 - weight) + weight * arr_norm, where arr_norm is log-scaled floor..ceiling into 0..1."""
    raise NotImplementedError


def describe(kind: SignalKind, value: float) -> str:
    """Plain-English sentence for a signal value, e.g. 'Active users down 38% in 30 days'."""
    raise NotImplementedError


def score_account(
    account: Account,
    signals: dict[SignalKind, Signal],
    config: ScoringConfig,
    as_of: date,
) -> ScoredAccount:
    raise NotImplementedError


def score_book(
    accounts: Iterable[Account],
    signals: Iterable[Signal],
    config: ScoringConfig,
    as_of: date | None = None,
) -> list[ScoredAccount]:
    """Score every account and sort by priority desc, then ARR desc, then name asc."""
    raise NotImplementedError
