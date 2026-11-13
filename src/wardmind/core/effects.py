"""Did it work? What moved after a step the CSM took on a suggestion.

CONTRACT FILE. The constants, enums, models and function signatures below are the contract; the
lead wrote them. The function bodies marked "Built against the contract" are workstream A's.

A "step" is an ACCEPTED follow-up suggestion (core/proposals.py, kind FOLLOW_UP) whose journal
follow-up (Proposal.result_id) has been marked done. The day it was done is the step's day.
Notes, snoozes, open follow-ups and follow-ups since removed from the journal are not steps.

For each step, Wardmind compares the account's score snapshot from just before the step with the
ones CHECK_DAYS later (core/history.py; demo data scores the demo book as of those days). Each
check says how the account's health, risk and the suggestion's own evidence signals moved. The
CSM can then say whether it helped (Proposal.review), which later rounds learn from.

Honesty rules (these are the guardrail, and tests hold them):
- The wording is "since", never "because" or "caused": a check reports what moved after a step.
- No comparison without both snapshots: a check with no "before" or no "after" reading says so
  (MISSED or WAITING) and has no direction.
- Nothing here changes the journal or the score. review() only records the CSM's answer.

Pure apart from review() (which saves through the Proposals store): the same inputs give the same
effects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from wardmind.core import signal_info
from wardmind.core.config import ScoringConfig
from wardmind.core.history import BookSnapshot, SnapshotEntry
from wardmind.core.journal import HEALTH_ORDER, AccountJournal, FollowUpStatus
from wardmind.core.models import Health, SignalKey, SignalKind, signal_key
from wardmind.core.proposals import (
    NOT_FOUND,
    Detector,
    Proposal,
    ProposalKind,
    Proposals,
    ProposalStatus,
    Review,
    Verdict,
    _day,
    naive_utc,
)
from wardmind.core.scoring import _whole, describe, format_number
from wardmind.core.store import WRITE_LOCK

CHECK_DAYS: tuple[int, ...] = (14, 30)  # days after the step each check compares with
BEFORE_WINDOW = 7  # the "before" snapshot is the latest one in [step day - 7, step day]
AFTER_WINDOW = 7  # a check's "after" snapshot is the earliest in [step + N, step + N + 7]
REVIEW_FOR_DAYS = 60  # after this many days from the step, it no longer asks for a review
MAX_REVIEW_CARDS = 3  # steps Today asks about at once; the rest are counted
MAX_READINGS = 3  # evidence signals compared on one check
RISK_STEP = 10.0  # same health: a risk move of at least this many points counts as a move


class CheckStatus(StrEnum):
    WAITING = "waiting"  # its day hasn't come, or it has and the window is still open
    READY = "ready"  # both snapshots found: the comparison is filled in
    MISSED = "missed"  # no "before" snapshot, or the window passed with no "after" snapshot


class Direction(StrEnum):
    """How the account moved between the two snapshots of a READY check."""

    BETTER = "better"  # health better; or same health and risk down by RISK_STEP or more
    SAME = "same"  # same health and risk within RISK_STEP
    WORSE = "worse"  # health worse; or same health and risk up by RISK_STEP or more
    GONE = "gone"  # the account is in the "before" snapshot but not the "after" one

    @property
    def label(self) -> str:
        return {
            "better": "Better since",
            "same": "About the same since",
            "worse": "Worse since",
            "gone": "Left the book since",
        }[self.value]


class Reading(BaseModel):
    """One evidence signal, before and after. A side with no reading is None, and its text is
    "No reading"."""

    signal: str  # the SignalKey value, e.g. "usage_trend_30d" or "custom:api_calls"
    label: str  # signal_info.label: "Usage trend"
    before: float | None = None
    after: float | None = None
    before_text: str  # scoring.describe of the value ("Active users down 38% in 30 days")
    after_text: str


class Check(BaseModel):
    days: int  # one of CHECK_DAYS
    due_on: date  # step day + days
    status: CheckStatus
    before_day: date | None = None  # the "before" snapshot's day
    after_day: date | None = None  # the "after" snapshot's day (READY, GONE included)
    health_before: Health | None = None
    health_after: Health | None = None  # None when GONE
    risk_before: float | None = None
    risk_after: float | None = None
    direction: Direction | None = None  # READY only
    readings: list[Reading] = Field(default_factory=list)  # READY (not GONE) only
    # One plain sentence, always set, e.g.
    #   READY:   "Better since: health At risk to Watch, risk 62 to 48."
    #            "About the same since: health Watch, risk 41 to 44."
    #            "Left the book since."
    #   WAITING: "The 14-day check is on 24 Nov 2026."
    #            "Waiting for the 14-day reading." (its day has come, the window is open)
    #   MISSED:  "No score history from before this step."
    #            "Wardmind wasn't open around 24 Nov 2026, so there is nothing to compare."
    sentence: str


class Effect(BaseModel):
    proposal_id: str
    account_id: str
    title: str  # the suggestion's title
    detector: Detector | None = None  # None for one Claude suggested
    source: str  # ProposalSource.label: "Wardmind" or "Claude"
    done_on: date  # the day the follow-up was marked done
    checks: list[Check]  # one per CHECK_DAYS, in that order
    review: Review | None = None  # the CSM's answer, from the Proposal

    @property
    def latest_ready(self) -> Check | None:
        """The READY check with the most days, or None."""
        ready = [check for check in self.checks if check.status is CheckStatus.READY]
        return ready[-1] if ready else None

    def needs_review(self, today: date) -> bool:
        """Today asks about it: a check is READY, the step is at most REVIEW_FOR_DAYS old, and
        the CSM hasn't answered, or answered TOO_EARLY on an earlier check than the latest ready
        one."""
        latest = self.latest_ready
        if latest is None or (today - self.done_on).days > REVIEW_FOR_DAYS:
            return False
        if self.review is None:
            return True
        return self.review.verdict is Verdict.TOO_EARLY and self.review.check_days < latest.days


class DetectorSummary(BaseModel):
    """One row of Settings > Assistant's "What followed your steps" table."""

    detector: Detector | None  # None: the steps Claude suggested
    label: str  # "Health dropped", ..., or "Suggested by Claude"
    steps: int  # steps done
    checked: int  # steps with at least one READY check
    better: int  # of `checked`, by the direction of their latest READY check
    same: int
    worse: int  # GONE counts here too
    helped: int  # the CSM's answers (TOO_EARLY not counted)
    didnt_help: int


DETECTOR_LABELS: dict[Detector, str] = {
    Detector.HEALTH_DROPPED: "Health dropped",
    Detector.RENEWAL_NO_CALL: "Renewal with no call",
    Detector.REPLY_OVERDUE: "Reply overdue",
    Detector.GONE_QUIET: "Gone quiet",
    Detector.BLIND_SPOT: "Thin data",
}
CLAUDE_LABEL = "Suggested by Claude"


class Snapshots(Protocol):
    """Where the score snapshots come from: history.HistoryStore for your data, the rescored
    demo book for demo data (service._DemoSnapshots)."""

    def latest_between(self, since: date, until: date) -> BookSnapshot | None: ...

    def earliest_between(self, since: date, until: date) -> BookSnapshot | None: ...


# --- Built against the contract (workstream A) ---------------------------------------------


def steps_done(
    items: Sequence[Proposal], journals: Mapping[str, AccountJournal]
) -> list[tuple[Proposal, date]]:
    """The steps: each ACCEPTED FOLLOW_UP suggestion whose journal follow-up (found by
    result_id in its account's journal) is DONE, with the day it was done (done_at's date).
    Newest step first; ties by proposal id."""
    found: list[tuple[Proposal, date]] = []
    for item in items:
        if item.status is not ProposalStatus.ACCEPTED or item.kind is not ProposalKind.FOLLOW_UP:
            continue
        account = journals.get(item.account_id)
        if account is None or item.result_id is None:
            continue
        follow_up = next(
            (entry for entry in account.follow_ups if entry.id == item.result_id), None
        )
        if follow_up is None or follow_up.status is not FollowUpStatus.DONE:
            continue
        if follow_up.done_at is None:
            continue
        found.append((item, follow_up.done_at.date()))
    # Ties on the day go by proposal id: sort by id first, then stably by day, newest first.
    found.sort(key=lambda pair: pair[0].id)
    found.sort(key=lambda pair: pair[1], reverse=True)
    return found


def effect_of(
    item: Proposal,
    done_on: date,
    snapshots: Snapshots,
    today: date,
    config: ScoringConfig,
) -> Effect:
    """The step's checks, one per CHECK_DAYS (see Check and the module's honesty rules). The
    "before" snapshot is read once: latest_between(done_on - BEFORE_WINDOW, done_on). A check
    whose due day is after `today` is WAITING without reading anything; otherwise its "after"
    snapshot is earliest_between(due_on, min(due_on + AFTER_WINDOW, today)). Readings: the
    suggestion's evidence signals (Evidence.signal, first seen first, unknown kinds skipped),
    at most MAX_READINGS, each only when the before or the after snapshot has a value for it."""
    before = snapshots.latest_between(done_on - timedelta(days=BEFORE_WINDOW), done_on)
    checks = [_check(item, days, done_on, before, snapshots, today, config) for days in CHECK_DAYS]
    return Effect(
        proposal_id=item.id,
        account_id=item.account_id,
        title=item.title,
        detector=item.detector,
        source=item.source.label,
        done_on=done_on,
        checks=checks,
        review=item.review,
    )


def effects_for(
    items: Sequence[Proposal],
    journals: Mapping[str, AccountJournal],
    snapshots: Snapshots,
    today: date,
    config: ScoringConfig,
    account_id: str | None = None,
) -> list[Effect]:
    """effect_of for every step (only that account's when account_id is given), newest step
    first."""
    chosen = [item for item in items if account_id is None or item.account_id == account_id]
    return [
        effect_of(item, done_on, snapshots, today, config)
        for item, done_on in steps_done(chosen, journals)
    ]


def to_review(effects: Sequence[Effect], today: date) -> list[Effect]:
    """The effects Today asks about (Effect.needs_review), oldest step first."""
    asked = [effect for effect in effects if effect.needs_review(today)]
    return sorted(asked, key=lambda effect: (effect.done_on, effect.proposal_id))


def summary(effects: Sequence[Effect]) -> list[DetectorSummary]:
    """One row per detector in Detector order, then the CLAUDE_LABEL row; rows with no steps are
    left out."""
    detectors: list[Detector | None] = [*Detector, None]
    rows: list[DetectorSummary] = []
    for detector in detectors:
        group = [effect for effect in effects if effect.detector is detector]
        if group:
            rows.append(_summary_row(detector, group))
    return rows


def review(
    store: Proposals, proposal_id: str, verdict: Verdict, check_days: int, *, now: datetime
) -> Proposal:
    """Record the CSM's answer on the suggestion (Proposal.review, replacing an earlier one) and
    save it with store.replace. LookupError(proposals.NOT_FOUND) when it is gone; ValueError
    ("Only a step you took can be reviewed.") when it isn't ACCEPTED; ValueError ("Pick a check
    of 14 or 30 days.") when check_days isn't in CHECK_DAYS. `now` is stored as naive UTC."""
    with WRITE_LOCK:
        current = store.get(proposal_id)
        if current is None:
            raise LookupError(NOT_FOUND)
        is_step = current.kind is ProposalKind.FOLLOW_UP
        if current.status is not ProposalStatus.ACCEPTED or not is_step:
            raise ValueError(_NOT_A_STEP)
        if check_days not in CHECK_DAYS:
            raise ValueError(_BAD_CHECK)
        answer = Review(verdict=verdict, check_days=check_days, at=naive_utc(now))
        return store.replace(current.model_copy(update={"review": answer}))


# --- Helpers (private) ---------------------------------------------------------------------------

NO_READING = "No reading"
_NOT_A_STEP = "Only a step you took can be reviewed."
_BAD_CHECK = "Pick a check of 14 or 30 days."
_NO_HISTORY_BEFORE = "No score history from before this step."


def _check(
    item: Proposal,
    days: int,
    done_on: date,
    before: BookSnapshot | None,
    snapshots: Snapshots,
    today: date,
    config: ScoringConfig,
) -> Check:
    """One check of a step: WAITING until its day, then READY, MISSED or WAITING (the window)."""
    due_on = done_on + timedelta(days=days)
    if due_on > today:
        return Check(
            days=days,
            due_on=due_on,
            status=CheckStatus.WAITING,
            sentence=f"The {days}-day check is on {_day(due_on)}.",
        )
    if before is None or item.account_id not in before.accounts:
        return Check(
            days=days,
            due_on=due_on,
            status=CheckStatus.MISSED,
            sentence=_NO_HISTORY_BEFORE,
        )
    window_end = min(due_on + timedelta(days=AFTER_WINDOW), today)
    after = snapshots.earliest_between(due_on, window_end)
    if after is None:
        if today > due_on + timedelta(days=AFTER_WINDOW):
            return Check(
                days=days,
                due_on=due_on,
                status=CheckStatus.MISSED,
                sentence=(
                    f"Wardmind wasn't open around {_day(due_on)}, so there is nothing to compare."
                ),
            )
        return Check(
            days=days,
            due_on=due_on,
            status=CheckStatus.WAITING,
            sentence=f"Waiting for the {days}-day reading.",
        )
    before_entry = before.accounts[item.account_id]
    after_entry = after.accounts.get(item.account_id)
    if after_entry is None:
        return Check(
            days=days,
            due_on=due_on,
            status=CheckStatus.READY,
            before_day=before.day,
            after_day=after.day,
            health_before=before_entry.health,
            risk_before=before_entry.risk,
            direction=Direction.GONE,
            sentence="Left the book since.",
        )
    direction = _direction(before_entry, after_entry)
    return Check(
        days=days,
        due_on=due_on,
        status=CheckStatus.READY,
        before_day=before.day,
        after_day=after.day,
        health_before=before_entry.health,
        health_after=after_entry.health,
        risk_before=before_entry.risk,
        risk_after=after_entry.risk,
        direction=direction,
        readings=_readings(item, before_entry, after_entry, config),
        sentence=_sentence(direction, before_entry, after_entry),
    )


def _direction(before: SnapshotEntry, after: SnapshotEntry) -> Direction:
    """A health change decides (HEALTH_ORDER: higher is better); otherwise the risk move, to one
    decimal, is compared with RISK_STEP."""
    if after.health is not before.health:
        if HEALTH_ORDER[after.health] > HEALTH_ORDER[before.health]:
            return Direction.BETTER
        return Direction.WORSE
    moved = round(after.risk - before.risk, 1)
    if moved <= -RISK_STEP:
        return Direction.BETTER
    if moved >= RISK_STEP:
        return Direction.WORSE
    return Direction.SAME


def _sentence(direction: Direction, before: SnapshotEntry, after: SnapshotEntry) -> str:
    """One sentence such as "Better since: health At risk to Watch, risk 62 to 48."; the health is
    named once when it didn't change."""
    if after.health is before.health:
        health = f"health {before.health.label}"
    else:
        health = f"health {before.health.label} to {after.health.label}"
    return f"{direction.label}: {health}, risk {_whole(before.risk)} to {_whole(after.risk)}."


def _readings(
    item: Proposal,
    before: SnapshotEntry,
    after: SnapshotEntry,
    config: ScoringConfig,
) -> list[Reading]:
    """The suggestion's evidence signals, first seen first, skipping repeats, missing and unknown
    kinds, and any with no value on either side; at most MAX_READINGS."""
    readings: list[Reading] = []
    seen: set[str] = set()
    for evidence in item.why:
        name = evidence.signal
        if name is None or name in seen:
            continue
        seen.add(name)
        try:
            kind = signal_key(name)
        except ValueError:
            continue
        before_value = before.signals.get(kind)
        after_value = after.signals.get(kind)
        if before_value is None and after_value is None:
            continue
        readings.append(
            Reading(
                signal=kind.value,
                label=signal_info.label(kind, config),
                before=before_value,
                after=after_value,
                before_text=_reading_text(kind, before_value, config),
                after_text=_reading_text(kind, after_value, config),
            )
        )
        if len(readings) == MAX_READINGS:
            break
    return readings


def _reading_text(kind: SignalKey, value: float | None, config: ScoringConfig) -> str:
    """scoring.describe of the value; a custom signal the book has since removed reads as its
    readable label and value, not its raw id."""
    if value is None:
        return NO_READING
    custom = config.custom_signal(kind)
    if not isinstance(kind, SignalKind) and custom is None:
        return f"{signal_info.label(kind, config)}: {format_number(value)}"
    return describe(kind, value, custom)


def _summary_row(detector: Detector | None, group: list[Effect]) -> DetectorSummary:
    """One row of the table: the steps of one detector (None: Claude's)."""
    latest = [effect.latest_ready for effect in group]
    checked = [check for check in latest if check is not None]
    directions = [check.direction for check in checked]
    verdicts = [effect.review.verdict for effect in group if effect.review is not None]
    return DetectorSummary(
        detector=detector,
        label=CLAUDE_LABEL if detector is None else DETECTOR_LABELS[detector],
        steps=len(group),
        checked=len(checked),
        better=directions.count(Direction.BETTER),
        same=directions.count(Direction.SAME),
        worse=directions.count(Direction.WORSE) + directions.count(Direction.GONE),
        helped=verdicts.count(Verdict.HELPED),
        didnt_help=verdicts.count(Verdict.DIDNT_HELP),
    )
