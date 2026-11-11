"""Automatic follow-ups: three rules that add a follow-up when something happens to an account.

Each rule is off until the CSM turns it on in Settings > Follow-ups:
- **At risk.** The account's health is At risk. "Check in: health dropped to At risk", due in
  rule_at_risk_days days (0 is today). The occasion is the day its current At-risk run began.
- **Renewal.** The renewal date is today or within rule_renewal_days days. "Prepare the renewal
  (renews 14 Jan 2027)", due today. The occasion is the renewal date.
- **Champion left.** The latest CHAMPION_LEFT reading says yes. "Find a new champion", due in
  CHAMPION_DAYS days. The occasion is the day of that reading.

The rules only add follow-ups; they never change or delete one. Each adds at most one follow-up per
account per occasion, open or done (FollowUp.rule holds the occasion key). An account with
MAX_OPEN_FOLLOW_UPS open follow-ups gets no more, and one run adds at most MAX_PLANNED. Demo data
never gets them. A due date Wardmind picks lands on Monday when it falls on a weekend and
skip_weekends is on.

planned_follow_ups is pure (no I/O). apply_rules writes its result through Journal.add_follow_up,
and service.remember_book calls it right after the day's score snapshot is saved, so the rules run
at most as often as the snapshot is taken.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from wardmind.core import history, vault
from wardmind.core.journal import MAX_OPEN_FOLLOW_UPS, AccountJournal, Journal, open_follow_ups
from wardmind.core.models import Health, ScoredAccount, Signal, SignalKind
from wardmind.core.scoring import latest_signals
from wardmind.core.settings import DataSource, Settings

MAX_PLANNED = 50  # the most follow-ups one run adds
CHAMPION_DAYS = 3  # "Find a new champion" is due this many days out
AT_RISK_TEXT = "Check in: health dropped to At risk"
RENEWAL_TEXT = "Prepare the renewal"
CHAMPION_TEXT = "Find a new champion"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FIRST_WEEKEND_DAY = 5  # weekday(): Monday is 0, so 5 (Saturday) and 6 (Sunday) are the weekend


class PlannedFollowUp(BaseModel):
    """A follow-up the rules say should exist: the account, its text, its due day and the
    occasion key that makes it unique (FollowUp.rule)."""

    account_id: str
    text: str
    due: date
    rule: str


@dataclass(frozen=True)
class _Occasion:
    rule: str
    text: str
    due: date


def any_rule_on(settings: Settings) -> bool:
    return settings.rule_at_risk or settings.rule_renewal or settings.rule_champion_left


def weekend_shift(day: date, skip_weekends: bool) -> date:
    """`day`, or, when skip_weekends is on and it is a Saturday or Sunday, the Monday after it."""
    if skip_weekends and day.weekday() >= _FIRST_WEEKEND_DAY:
        return day + timedelta(days=7 - day.weekday())
    return day


def due_day(today: date, days: int, skip_weekends: bool) -> date:
    """`days` from today, moved to Monday by weekend_shift. For a due date Wardmind picks itself
    (the default in the follow-up form, and rule-made follow-ups); a date the CSM picks is never
    passed through here."""
    return weekend_shift(today + timedelta(days=days), skip_weekends)


def at_risk_since(
    book: Sequence[ScoredAccount], store: history.HistoryStore, today: date
) -> dict[str, date]:
    """For each account that is At risk in `book` now, the first day of its current At-risk run.

    The run goes back through the recorded snapshots before today for as long as the account is
    At risk in each one. A day with no snapshot file (Wardmind wasn't open) is skipped and does not
    end the run. The first snapshot where the account is not At risk, or is not in it, ends the run.
    An account with no earlier snapshot to look at starts its run today."""
    start: dict[str, date] = {}
    running: set[str] = set()
    for scored in book:
        if scored.health is Health.AT_RISK:
            start[scored.account.id] = today
            running.add(scored.account.id)
    for day in reversed([known for known in store.days() if known < today]):
        if not running:
            break
        snapshot = store.load(day)
        if snapshot is None:  # an unreadable file counts as absent, so it doesn't end a run
            continue
        for account_id in list(running):
            entry = snapshot.accounts.get(account_id)
            if entry is not None and entry.health is Health.AT_RISK:
                start[account_id] = day
            else:
                running.discard(account_id)
    return start


def _champion_left_on(signals: Iterable[Signal]) -> dict[str, date]:
    """Each account whose latest CHAMPION_LEFT reading says the champion left, mapped to the day
    of that reading. A reading of 1.0 means yes (contacts.py and editing.py write 1.0 or 0.0)."""
    found: dict[str, date] = {}
    for account_id, readings in latest_signals(signals).items():
        reading = readings.get(SignalKind.CHAMPION_LEFT)
        if reading is not None and reading.value >= 1.0:
            found[account_id] = reading.observed_at.date()
    return found


def _renews_on(day: date) -> str:
    """'14 Jan 2027'. Built here, so core doesn't depend on the web's date wording."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def _occasions(
    scored: ScoredAccount,
    settings: Settings,
    today: date,
    at_risk_started: Mapping[str, date],
    champion_left: Mapping[str, date],
) -> list[_Occasion]:
    """The occasions this account is in right now, in the order the rules are listed."""
    account = scored.account
    found: list[_Occasion] = []
    if settings.rule_at_risk and scored.health is Health.AT_RISK:
        since = at_risk_started.get(account.id, today)
        found.append(
            _Occasion(
                rule=f"at_risk:{since.isoformat()}",
                text=AT_RISK_TEXT,
                due=due_day(today, settings.rule_at_risk_days, settings.skip_weekends),
            )
        )
    until_renewal = (account.renewal_date - today).days
    if settings.rule_renewal and 0 <= until_renewal <= settings.rule_renewal_days:
        renewal = account.renewal_date
        found.append(
            _Occasion(
                rule=f"renewal:{renewal.isoformat()}",
                text=f"{RENEWAL_TEXT} (renews {_renews_on(renewal)})",
                due=due_day(today, 0, settings.skip_weekends),
            )
        )
    if settings.rule_champion_left and account.id in champion_left:
        observed = champion_left[account.id]
        found.append(
            _Occasion(
                rule=f"champion_left:{observed.isoformat()}",
                text=CHAMPION_TEXT,
                due=due_day(today, CHAMPION_DAYS, settings.skip_weekends),
            )
        )
    return found


def planned_follow_ups(
    book: Sequence[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    settings: Settings,
    today: date,
    at_risk_since: Mapping[str, date],
    *,
    signals: Iterable[Signal] = (),
) -> list[PlannedFollowUp]:
    """The follow-ups the enabled rules say should exist, in book order then rule order. Pure.

    An occasion is skipped when its key is already on any of the account's follow-ups (open or
    done). An account already at MAX_OPEN_FOLLOW_UPS open follow-ups gets none, and the room left
    is counted as items are planned, so one run never takes an account past the cap. Champion
    readings come from `signals` (only read when that rule is on). at_risk_since maps an account
    to the day its current At-risk run began; an At-risk account missing from it counts from today.
    At most MAX_PLANNED items."""
    if not any_rule_on(settings):
        return []
    champion_left = _champion_left_on(signals) if settings.rule_champion_left else {}
    planned: list[PlannedFollowUp] = []
    for scored in book:
        account_id = scored.account.id
        entry = journals.get(account_id, AccountJournal())
        room = MAX_OPEN_FOLLOW_UPS - len(open_follow_ups(entry))
        taken = {item.rule for item in entry.follow_ups if item.rule is not None}
        taken.update(entry.dismissed_rules)  # removed by the CSM: never added again
        for occasion in _occasions(scored, settings, today, at_risk_since, champion_left):
            if room <= 0:
                break
            if occasion.rule in taken:
                continue
            planned.append(
                PlannedFollowUp(
                    account_id=account_id,
                    text=occasion.text,
                    due=occasion.due,
                    rule=occasion.rule,
                )
            )
            if len(planned) == MAX_PLANNED:
                return planned
            room -= 1
    return planned


def apply_rules(
    journal_file: Journal,
    book: Sequence[ScoredAccount],
    settings: Settings,
    today: date,
    at_risk_started: Mapping[str, date],
    *,
    signals: Sequence[Signal] = (),
    now: datetime,
) -> int:
    """Add the planned follow-ups to `journal_file` (Your data) and return how many were added.

    Nothing is written when no rule is on, for demo data, or while the book is locked: a locked
    vault (vault.Locked) ends the run, and what was already added stays. A follow-up the journal
    refuses (the account is full, or its occasion was just added) is skipped."""
    if settings.source is DataSource.DEMO or not any_rule_on(settings):
        return 0
    added = 0
    try:
        planned = planned_follow_ups(
            book, journal_file.all(), settings, today, at_risk_started, signals=signals
        )
        for item in planned:
            try:
                journal_file.add_follow_up(
                    item.account_id, item.text, item.due, now=now, today=today, rule=item.rule
                )
            except ValueError:
                continue
            added += 1
    except vault.Locked:
        pass  # the book locked part way through: what was added stays, nothing more is written
    return added
