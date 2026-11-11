"""Honest forecast: your call on each renewal, the ARR behind each call, and how calls held up.

You tag each upcoming renewal Commit, Likely or At risk (journal.Journal.make_call). Wardmind
totals the ARR per call, points out where its score disagrees with you, and once outcomes are
recorded it checks you: "When you said Commit, 9 of 10 renewed." It checks itself the same way:
"When Wardmind said At risk, 3 of 4 churned." Counts only, never percentages: samples are small.

Pure and deterministic: no I/O and no clock reads (callers pass `as_of`).

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before
changing them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from wardmind.core.journal import AccountJournal, CallKind, ForecastCall, Outcome, OutcomeKind
from wardmind.core.models import Health, Motion, ScoredAccount


class Disagreement(StrEnum):
    """Where your call and Wardmind's score point different ways."""

    CALL_HIGHER = "call_higher"  # you said Commit or Likely; Wardmind says At risk
    CALL_LOWER = "call_lower"  # you said At risk; Wardmind says Healthy

    @property
    def text(self) -> str:
        return {
            "call_higher": "Wardmind says At risk. Check what it sees before you commit.",
            "call_lower": "Wardmind sees no risk signals. Note what you know that it doesn't.",
        }[self.value]


class ForecastRow(BaseModel):
    account_id: str
    name: str
    owner: str
    arr: float
    renewal_date: date
    days_to_renewal: int  # renewal_date - as_of, 0 or more
    health: Health
    motion: Motion
    call: CallKind | None  # the call whose renewal_date equals the account's; None if not called
    made_on: date | None  # that call's made_on
    disagreement: Disagreement | None


class ForecastTotal(BaseModel):
    call: CallKind | None  # None: not called yet
    count: int
    arr: float


class ForecastView(BaseModel):
    # Accounts whose renewal_date is within [as_of, as_of + window_days], soonest first, then
    # name (case-insensitive), then id.
    rows: list[ForecastRow]
    # Always four, in this order: Commit, Likely, At risk, not called (None). Zero counts kept.
    totals: list[ForecastTotal]
    arr_total: float  # every row's ARR
    disagreements: int  # rows with a disagreement


class CallRecord(BaseModel):
    """How one kind of call (yours) or one health (Wardmind's) turned out."""

    label: str  # CallKind.label, or Health.label for Wardmind's records
    decided: int = Field(ge=0)  # calls (or scores) whose renewal has an outcome
    renewed: int = Field(ge=0)  # outcomes RENEWED, EXPANDED or CONTRACTED
    churned: int = Field(ge=0)  # outcomes CHURNED
    # f"When you said {label}, {renewed} of {decided} renewed." for Commit and Likely;
    # f"When you said At risk, {churned} of {decided} churned." for At risk.
    # Wardmind's: f"When Wardmind said {label}, {renewed} of {decided} renewed." for Healthy and
    # Watch; f"When Wardmind said At risk, {churned} of {decided} churned." for At risk.
    sentence: str


class Calibration(BaseModel):
    yours: list[CallRecord]  # in CallKind order; only kinds with decided > 0
    wardmind: list[CallRecord]  # Healthy, Watch, At risk order; only those with decided > 0
    decided: int  # your calls that have an outcome (the sum of yours' decided)


RENEWED_KINDS = frozenset({OutcomeKind.RENEWED, OutcomeKind.EXPANDED, OutcomeKind.CONTRACTED})


def disagreement(call: CallKind | None, health: Health) -> Disagreement | None:
    """CALL_HIGHER for Commit or Likely with At risk; CALL_LOWER for At risk with Healthy; else
    None (including no call)."""
    if call in (CallKind.COMMIT, CallKind.LIKELY) and health is Health.AT_RISK:
        return Disagreement.CALL_HIGHER
    if call is CallKind.AT_RISK and health is Health.HEALTHY:
        return Disagreement.CALL_LOWER
    return None


def current_call(journal: AccountJournal, renewal_date: date) -> ForecastCall | None:
    """The call in journal.calls whose renewal_date equals renewal_date (the last one, should
    there be several), else None."""
    found = [entry for entry in journal.calls if entry.renewal_date == renewal_date]
    return found[-1] if found else None


def forecast_view(
    book: Sequence[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    as_of: date,
    window_days: int,
) -> ForecastView:
    """The renewals in the window with your call on each (current_call against the account's
    renewal_date; a call on an older renewal date does not count), the totals per call and the
    disagreements (from the account's health now)."""
    end = as_of + timedelta(days=window_days)
    rows = [
        _forecast_row(scored, journals.get(scored.account.id, AccountJournal()), as_of)
        for scored in book
        if as_of <= scored.account.renewal_date <= end
    ]
    rows.sort(key=lambda row: (row.renewal_date, row.name.casefold(), row.account_id))
    return ForecastView(
        rows=rows,
        totals=[
            _forecast_total(kind, [row for row in rows if row.call is kind])
            for kind in _TOTAL_ORDER
        ],
        arr_total=sum((row.arr for row in rows), 0.0),
        disagreements=sum(1 for row in rows if row.disagreement is not None),
    )


def calibration(journals: Mapping[str, AccountJournal]) -> Calibration:
    """Match each call to the outcome with the same renewal_date in the same account's journal;
    a call with no such outcome is not counted. Wardmind's records use each outcome's own
    `health` (what the score said when the outcome was recorded), so they count every outcome,
    called or not."""
    yours = {kind: _Tally() for kind in CallKind}
    wardmind = {health: _Tally() for health in _WARDMIND_ORDER}
    for journal in journals.values():
        # One call per renewal: a call superseded by a later one for the same renewal is not
        # counted, the same rule forecast_view uses.
        for renewal_date in dict.fromkeys(entry.renewal_date for entry in journal.calls):
            made = current_call(journal, renewal_date)
            answer = _outcome_on(journal, renewal_date)
            if made is not None and answer is not None:
                yours[made.kind].add(answer.kind)
        for outcome in journal.outcomes:
            wardmind[outcome.health].add(outcome.kind)
    return Calibration(
        yours=[
            _calibration_record(kind.label, tally, _your_sentence(kind, tally))
            for kind, tally in yours.items()
            if tally.decided > 0
        ],
        wardmind=[
            _calibration_record(health.label, tally, _wardmind_sentence(health, tally))
            for health, tally in wardmind.items()
            if tally.decided > 0
        ],
        decided=sum(tally.decided for tally in yours.values()),
    )


# --- Helpers (private) --------------------------------------------------------------------------

_TOTAL_ORDER: tuple[CallKind | None, ...] = (
    CallKind.COMMIT,
    CallKind.LIKELY,
    CallKind.AT_RISK,
    None,
)
_WARDMIND_ORDER: tuple[Health, ...] = (Health.HEALTHY, Health.WATCH, Health.AT_RISK)


def _forecast_row(scored: ScoredAccount, journal: AccountJournal, as_of: date) -> ForecastRow:
    """One account's row: its current call on its renewal date, and where that disagrees."""
    account = scored.account
    made = current_call(journal, account.renewal_date)
    kind = made.kind if made is not None else None
    return ForecastRow(
        account_id=account.id,
        name=account.name,
        owner=account.owner,
        arr=account.arr,
        renewal_date=account.renewal_date,
        days_to_renewal=(account.renewal_date - as_of).days,
        health=scored.health,
        motion=scored.motion,
        call=kind,
        made_on=made.made_on if made is not None else None,
        disagreement=disagreement(kind, scored.health),
    )


def _forecast_total(kind: CallKind | None, rows: list[ForecastRow]) -> ForecastTotal:
    return ForecastTotal(call=kind, count=len(rows), arr=sum((row.arr for row in rows), 0.0))


def _outcome_on(journal: AccountJournal, renewal_date: date) -> Outcome | None:
    """The outcome for this renewal date (the last one, should there be several), else None."""
    found = [entry for entry in journal.outcomes if entry.renewal_date == renewal_date]
    return found[-1] if found else None


@dataclass
class _Tally:
    """Outcomes counted for one call kind or one health."""

    decided: int = 0
    renewed: int = 0
    churned: int = 0

    def add(self, outcome: OutcomeKind) -> None:
        self.decided += 1
        if outcome in RENEWED_KINDS:
            self.renewed += 1
        elif outcome is OutcomeKind.CHURNED:
            self.churned += 1


def _calibration_record(label: str, tally: _Tally, sentence: str) -> CallRecord:
    return CallRecord(
        label=label,
        decided=tally.decided,
        renewed=tally.renewed,
        churned=tally.churned,
        sentence=sentence,
    )


def _your_sentence(kind: CallKind, tally: _Tally) -> str:
    if kind is CallKind.AT_RISK:
        return f"When you said At risk, {tally.churned} of {tally.decided} churned."
    return f"When you said {kind.label}, {tally.renewed} of {tally.decided} renewed."


def _wardmind_sentence(health: Health, tally: _Tally) -> str:
    if health is Health.AT_RISK:
        return f"When Wardmind said At risk, {tally.churned} of {tally.decided} churned."
    return f"When Wardmind said {health.label}, {tally.renewed} of {tally.decided} renewed."
