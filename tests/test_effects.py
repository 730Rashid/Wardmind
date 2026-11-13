"""Did it work? (core/effects.py): which suggestions are steps, the two checks on each (14 and 30
days after the step, against the score history), the readings, what Today asks about, the summary
table, and review(). Most tests compare in-memory snapshots (FakeSnapshots); the end-to-end tests
use a real history folder, a suggestions file and a journal, all in tmp_path. Nothing reads the
real book, journal or history."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from wardmind.core import proposals
from wardmind.core.config import CustomSignal, ScoringConfig, SignalRule
from wardmind.core.effects import (
    AFTER_WINDOW,
    BEFORE_WINDOW,
    CHECK_DAYS,
    CLAUDE_LABEL,
    MAX_READINGS,
    REVIEW_FOR_DAYS,
    RISK_STEP,
    Check,
    CheckStatus,
    DetectorSummary,
    Direction,
    Effect,
    Reading,
    Snapshots,
    effect_of,
    effects_for,
    review,
    steps_done,
    summary,
    to_review,
)
from wardmind.core.history import BookSnapshot, HistoryStore, SnapshotEntry
from wardmind.core.journal import AccountJournal, FollowUp, FollowUpStatus, Journal
from wardmind.core.models import CustomKind, Health, Motion, SignalKey, SignalKind
from wardmind.core.proposals import (
    NOT_FOUND,
    Detector,
    Evidence,
    Proposal,
    ProposalKind,
    Proposals,
    ProposalSource,
    ProposalStatus,
    Review,
    Verdict,
)
from wardmind.core.store import LocalStore

DONE_ON = date(2026, 10, 1)  # the day the step's follow-up was marked done
DONE_AT = datetime(2026, 10, 1, 9, 0)
DUE_14 = DONE_ON + timedelta(days=14)  # 15 Oct 2026
DUE_30 = DONE_ON + timedelta(days=30)  # 31 Oct 2026
TODAY = date(2026, 11, 13)
NOW = datetime(2026, 11, 13, 9, 0)
ACCOUNT = "a-1"
OTHER = "a-2"
CONFIG = ScoringConfig()

USAGE = SignalKind.USAGE_TREND_30D
TICKETS = SignalKind.OPEN_TICKETS_SEV1
NPS = SignalKind.NPS_LATEST
CHAMPION = SignalKind.CHAMPION_LEFT
SEATS = SignalKind.SEAT_UTILISATION
BILLING = SignalKind.BILLING_OVERDUE_DAYS
API = CustomKind.of("api_calls")

FORBIDDEN_WORDS = re.compile(r"\b(because|caused|due to)\b", re.IGNORECASE)


# --- Helpers


class FakeSnapshots:
    """effects.Snapshots over a list of snapshots, with HistoryStore's window rules: the latest (or
    earliest) snapshot whose day is in [since, until]. Counts its reads."""

    def __init__(self, *snapshots: BookSnapshot) -> None:
        self._days = sorted(snapshots, key=lambda snapshot: snapshot.day)
        self.latest_reads = 0
        self.earliest_reads = 0

    def _within(self, since: date, until: date) -> list[BookSnapshot]:
        return [snapshot for snapshot in self._days if since <= snapshot.day <= until]

    def latest_between(self, since: date, until: date) -> BookSnapshot | None:
        self.latest_reads += 1
        found = self._within(since, until)
        return found[-1] if found else None

    def earliest_between(self, since: date, until: date) -> BookSnapshot | None:
        self.earliest_reads += 1
        found = self._within(since, until)
        return found[0] if found else None


def entry(
    health: Health = Health.HEALTHY,
    risk: float = 20.0,
    signals: dict[SignalKey, float] | None = None,
) -> SnapshotEntry:
    return SnapshotEntry(
        name="Acme",
        rank=1,
        risk=risk,
        priority=risk,
        health=health,
        motion=Motion.RENEWAL,
        arr=120_000.0,
        signals=dict(signals or {}),
    )


def snap(day: date, accounts: dict[str, SnapshotEntry]) -> BookSnapshot:
    taken = datetime(day.year, day.month, day.day, 8, 0)
    return BookSnapshot(day=day, taken_at=taken, accounts=accounts)


def timeline(before: dict[str, SnapshotEntry], after: dict[str, SnapshotEntry]) -> FakeSnapshots:
    """The step's day has `before`; the 14 and the 30 day checks each have `after` on their day."""
    return FakeSnapshots(snap(DONE_ON, before), snap(DUE_14, after), snap(DUE_30, after))


def evidence(*signals: str | None) -> list[Evidence]:
    return [
        Evidence(text=f"Reason {number}", signal=name)
        for number, name in enumerate(signals, start=1)
    ]


def step(
    proposal_id: str = "p-00000001",
    account_id: str = ACCOUNT,
    *,
    kind: ProposalKind = ProposalKind.FOLLOW_UP,
    status: ProposalStatus = ProposalStatus.ACCEPTED,
    detector: Detector | None = Detector.HEALTH_DROPPED,
    why: list[Evidence] | None = None,
    result_id: str | None = "f-00000001",
    review_answer: Review | None = None,
) -> Proposal:
    """An accepted follow-up suggestion (a step once its journal follow-up is done) by default."""
    return Proposal(
        id=proposal_id,
        account_id=account_id,
        kind=kind,
        source=ProposalSource.MCP if detector is None else ProposalSource.DETECTOR,
        detector=detector,
        key=f"health_dropped:{proposal_id}",
        title="Check in with Acme: health dropped to At risk",
        why=why if why is not None else evidence("usage_trend_30d"),
        text="Check in with Acme",
        due=TODAY if kind is ProposalKind.FOLLOW_UP else None,
        until=TODAY + timedelta(days=7) if kind is ProposalKind.SNOOZE else None,
        status=status,
        created_at=NOW,
        decided_at=None if status is ProposalStatus.OPEN else NOW,
        result_id=result_id,
        basis_day=TODAY,
        review=review_answer,
    )


def new_suggestion(key: str = "health_dropped:2026-10-01") -> Proposal:
    """An open follow-up suggestion, not yet stored (Proposals.add gives it an id)."""
    return Proposal(
        id="",
        account_id=ACCOUNT,
        kind=ProposalKind.FOLLOW_UP,
        source=ProposalSource.DETECTOR,
        detector=Detector.HEALTH_DROPPED,
        key=key,
        title="Check in with Acme: health dropped to At risk",
        why=evidence("usage_trend_30d"),
        text="Check in with Acme",
        due=TODAY,
        created_at=NOW,
        basis_day=TODAY,
    )


def new_note(key: str = "health_dropped:note") -> Proposal:
    return Proposal(
        id="",
        account_id=ACCOUNT,
        kind=ProposalKind.NOTE,
        source=ProposalSource.DETECTOR,
        detector=Detector.HEALTH_DROPPED,
        key=key,
        title="Note the health drop",
        why=evidence("usage_trend_30d"),
        text="Health dropped this month.",
        created_at=NOW,
        basis_day=TODAY,
    )


def follow_up(
    follow_up_id: str,
    *,
    done_at: datetime | None = None,
) -> FollowUp:
    """An open follow-up, or a done one when done_at is given."""
    return FollowUp(
        id=follow_up_id,
        text="Check in with Acme",
        due=DONE_ON,
        status=FollowUpStatus.OPEN if done_at is None else FollowUpStatus.DONE,
        created_at=NOW,
        done_at=done_at,
    )


def journals(*follow_ups: FollowUp, account_id: str = ACCOUNT) -> dict[str, AccountJournal]:
    return {account_id: AccountJournal(follow_ups=list(follow_ups))}


def checks_of(
    snapshots: Snapshots,
    *,
    today: date = TODAY,
    item: Proposal | None = None,
    config: ScoringConfig = CONFIG,
) -> list[Check]:
    """The checks of a step done on DONE_ON (the default step unless `item` is given)."""
    chosen = item if item is not None else step()
    return effect_of(chosen, DONE_ON, snapshots, today, config).checks


def made(
    proposal_id: str,
    *,
    detector: Detector | None = Detector.HEALTH_DROPPED,
    directions: tuple[Direction | None, ...] = (Direction.SAME, None),
    review_answer: Review | None = None,
    done_on: date = DONE_ON,
) -> Effect:
    """An effect whose 14 and 30 day checks are READY with these directions (None: WAITING)."""
    checks: list[Check] = []
    for days, direction in zip(CHECK_DAYS, directions, strict=True):
        due_on = done_on + timedelta(days=days)
        if direction is None:
            checks.append(
                Check(
                    days=days,
                    due_on=due_on,
                    status=CheckStatus.WAITING,
                    sentence=f"The {days}-day check is on later.",
                )
            )
        else:
            checks.append(
                Check(
                    days=days,
                    due_on=due_on,
                    status=CheckStatus.READY,
                    direction=direction,
                    sentence=f"The {days}-day check was read.",
                )
            )
    return Effect(
        proposal_id=proposal_id,
        account_id=ACCOUNT,
        title="Check in with Acme",
        detector=detector,
        source="Wardmind" if detector is not None else "Claude",
        done_on=done_on,
        checks=checks,
        review=review_answer,
    )


@pytest.fixture
def files(tmp_path) -> tuple[Proposals, Journal]:
    """The suggestions file and the journal of one book, in tmp_path."""
    return Proposals(LocalStore(tmp_path)), Journal(LocalStore(tmp_path))


def accepted_step(store: Proposals, journal: Journal, key: str = "health_dropped:2026-10-01"):
    suggestion = store.add(new_suggestion(key))
    assert suggestion is not None
    return proposals.accept(store, journal, suggestion.id, now=NOW, today=TODAY)


# --- steps_done: which suggestions are steps


def test_only_an_accepted_follow_up_whose_journal_follow_up_is_done_is_a_step():
    items = [
        step("p-1", status=ProposalStatus.OPEN, result_id=None),  # still waiting on the CSM
        step("p-2", status=ProposalStatus.DISMISSED, result_id="f-00000001"),  # dismissed
        step("p-3", kind=ProposalKind.NOTE, result_id="f-00000001"),  # a note, not a follow-up
        step("p-4", kind=ProposalKind.SNOOZE, result_id="snooze"),  # a snooze
        step("p-5", result_id="f-00000002"),  # the journal follow-up is still open
        step("p-6", result_id="f-00000009"),  # that follow-up was removed from the journal
        step("p-7", account_id=OTHER, result_id="f-00000001"),  # no journal for this account
        step("p-8", result_id="f-00000001"),  # accepted, and the follow-up is done: a step
    ]
    book = journals(follow_up("f-00000001", done_at=DONE_AT), follow_up("f-00000002"))
    found = steps_done(items, book)
    assert [(item.id, done_on) for item, done_on in found] == [("p-8", DONE_ON)]


def test_a_step_is_dated_by_the_day_its_follow_up_was_done_not_by_its_due_day():
    done_late = datetime(2026, 10, 5, 23, 30)
    found = steps_done([step("p-1")], journals(follow_up("f-00000001", done_at=done_late)))
    assert [done_on for _, done_on in found] == [date(2026, 10, 5)]


def test_steps_are_newest_first_and_ties_on_a_day_go_by_proposal_id():
    items = [
        step("p-old", result_id="f-1"),
        step("p-b", result_id="f-2"),
        step("p-a", result_id="f-3"),
    ]
    book = journals(
        follow_up("f-1", done_at=DONE_AT),
        follow_up("f-2", done_at=datetime(2026, 10, 5, 23, 0)),
        follow_up("f-3", done_at=datetime(2026, 10, 5, 0, 10)),
    )
    found = steps_done(items, book)
    assert [(item.id, done_on) for item, done_on in found] == [
        ("p-a", date(2026, 10, 5)),
        ("p-b", date(2026, 10, 5)),
        ("p-old", DONE_ON),
    ]


# --- effect_of: the checks and the windows


def test_checks_wait_until_their_day_and_read_only_the_history_before_the_step():
    snapshots = timeline({ACCOUNT: entry()}, {ACCOUNT: entry()})
    checks = checks_of(snapshots, today=DONE_ON + timedelta(days=3))
    assert [check.status for check in checks] == [CheckStatus.WAITING, CheckStatus.WAITING]
    assert [check.sentence for check in checks] == [
        "The 14-day check is on 15 Oct 2026.",
        "The 30-day check is on 31 Oct 2026.",
    ]
    assert [check.due_on for check in checks] == [DUE_14, DUE_30]
    assert all(check.before_day is None and check.direction is None for check in checks)
    assert snapshots.latest_reads == 1  # the "before" snapshot is read once
    assert snapshots.earliest_reads == 0  # and no "after" snapshot is read for a waiting check


def test_the_day_before_a_check_is_due_is_still_waiting():
    checks = checks_of(
        timeline({ACCOUNT: entry()}, {ACCOUNT: entry()}), today=DUE_14 - timedelta(1)
    )
    assert checks[0].status is CheckStatus.WAITING
    assert checks[0].sentence == "The 14-day check is on 15 Oct 2026."


def test_a_check_due_today_is_compared_and_the_one_after_is_still_waiting():
    snapshots = timeline({ACCOUNT: entry(Health.WATCH, 41.0)}, {ACCOUNT: entry(Health.WATCH, 44.0)})
    checks = checks_of(snapshots, today=DUE_14)
    assert checks[0].status is CheckStatus.READY
    assert checks[0].after_day == DUE_14
    assert checks[1].status is CheckStatus.WAITING
    assert checks[1].sentence == "The 30-day check is on 31 Oct 2026."


def test_the_after_window_is_open_through_its_last_day_and_missed_after_it():
    history_only = FakeSnapshots(snap(DONE_ON, {ACCOUNT: entry()}))
    last_day = DUE_14 + timedelta(days=AFTER_WINDOW)
    waiting = checks_of(history_only, today=last_day)[0]
    assert waiting.status is CheckStatus.WAITING
    assert waiting.sentence == "Waiting for the 14-day reading."
    missed = checks_of(history_only, today=last_day + timedelta(days=1))[0]
    assert missed.status is CheckStatus.MISSED
    assert missed.sentence == (
        "Wardmind wasn't open around 15 Oct 2026, so there is nothing to compare."
    )
    assert missed.direction is None


def test_an_after_snapshot_later_than_today_is_not_used_yet():
    later = DUE_14 + timedelta(days=5)
    snapshots = FakeSnapshots(snap(DONE_ON, {ACCOUNT: entry()}), snap(later, {ACCOUNT: entry()}))
    assert checks_of(snapshots, today=DUE_14 + timedelta(days=3))[0].status is CheckStatus.WAITING
    ready = checks_of(snapshots, today=later)[0]
    assert ready.status is CheckStatus.READY
    assert ready.after_day == later


def test_the_earliest_after_snapshot_in_the_window_is_the_one_compared():
    snapshots = FakeSnapshots(
        snap(DONE_ON, {ACCOUNT: entry(Health.WATCH, 41.0)}),
        snap(DUE_14 + timedelta(days=2), {ACCOUNT: entry(Health.WATCH, 44.0)}),
        snap(DUE_14 + timedelta(days=6), {ACCOUNT: entry(Health.WATCH, 30.0)}),
    )
    check = checks_of(snapshots, today=DUE_14 + timedelta(days=7))[0]
    assert check.after_day == DUE_14 + timedelta(days=2)
    assert check.risk_after == 44.0


def test_the_before_snapshot_is_the_latest_one_within_seven_days_before_the_step():
    snapshots = FakeSnapshots(
        snap(DONE_ON - timedelta(days=BEFORE_WINDOW), {ACCOUNT: entry(risk=50.0)}),
        snap(DONE_ON - timedelta(days=3), {ACCOUNT: entry(risk=30.0)}),
        snap(DUE_14, {ACCOUNT: entry(risk=31.0)}),
    )
    check = checks_of(snapshots)[0]
    assert check.before_day == DONE_ON - timedelta(days=3)
    assert check.risk_before == 30.0


def test_the_step_day_itself_and_the_first_day_of_the_before_window_count():
    on_the_day = FakeSnapshots(snap(DONE_ON, {ACCOUNT: entry()}), snap(DUE_14, {ACCOUNT: entry()}))
    assert checks_of(on_the_day)[0].before_day == DONE_ON
    first_day = FakeSnapshots(
        snap(DONE_ON - timedelta(days=BEFORE_WINDOW), {ACCOUNT: entry()}),
        snap(DUE_14, {ACCOUNT: entry()}),
    )
    assert checks_of(first_day)[0].before_day == DONE_ON - timedelta(days=BEFORE_WINDOW)


def test_a_snapshot_one_day_before_the_before_window_is_no_history():
    too_early = FakeSnapshots(
        snap(DONE_ON - timedelta(days=BEFORE_WINDOW + 1), {ACCOUNT: entry()}),
        snap(DUE_14, {ACCOUNT: entry()}),
        snap(DUE_30, {ACCOUNT: entry()}),
    )
    checks = checks_of(too_early)
    assert [check.status for check in checks] == [CheckStatus.MISSED, CheckStatus.MISSED]
    assert all(check.sentence == "No score history from before this step." for check in checks)


def test_no_history_before_the_step_is_missed_even_with_an_after_snapshot():
    no_before = FakeSnapshots(snap(DUE_14, {ACCOUNT: entry()}), snap(DUE_30, {ACCOUNT: entry()}))
    checks = checks_of(no_before)
    assert [check.status for check in checks] == [CheckStatus.MISSED, CheckStatus.MISSED]
    assert checks[0].sentence == "No score history from before this step."


def test_an_account_missing_from_the_before_snapshot_is_missed():
    other_only = timeline({OTHER: entry()}, {ACCOUNT: entry()})
    checks = checks_of(other_only)
    assert [check.status for check in checks] == [CheckStatus.MISSED, CheckStatus.MISSED]
    assert checks[0].sentence == "No score history from before this step."


def test_an_account_that_left_the_book_is_gone_with_no_readings_or_health_after():
    snapshots = timeline(
        {ACCOUNT: entry(Health.WATCH, 41.0, {USAGE: -38.0})},
        {OTHER: entry()},
    )
    checks = checks_of(snapshots, item=step(why=evidence("usage_trend_30d")))
    assert [check.status for check in checks] == [CheckStatus.READY, CheckStatus.READY]
    for check in checks:
        assert check.direction is Direction.GONE
        assert check.sentence == "Left the book since."
        assert check.health_before is Health.WATCH
        assert check.risk_before == 41.0
        assert check.health_after is None
        assert check.risk_after is None
        assert check.after_day == check.due_on
        assert check.readings == []
    assert Direction.GONE.label == "Left the book since"


# --- effect_of: direction and sentence

DIRECTION_CASES = [
    pytest.param(
        Health.AT_RISK,
        62.0,
        Health.WATCH,
        48.0,
        Direction.BETTER,
        "Better since: health At risk to Watch, risk 62 to 48.",
        id="health-better",
    ),
    pytest.param(
        Health.WATCH,
        38.0,
        Health.AT_RISK,
        52.0,
        Direction.WORSE,
        "Worse since: health Watch to At risk, risk 38 to 52.",
        id="health-worse",
    ),
    pytest.param(
        Health.WATCH,
        30.0,
        Health.HEALTHY,
        35.0,
        Direction.BETTER,
        "Better since: health Watch to Healthy, risk 30 to 35.",
        id="health-decides-over-risk",
    ),
    pytest.param(
        Health.WATCH,
        41.0,
        Health.WATCH,
        44.0,
        Direction.SAME,
        "About the same since: health Watch, risk 41 to 44.",
        id="same-small-move",
    ),
    pytest.param(
        Health.WATCH,
        50.0,
        Health.WATCH,
        40.0,
        Direction.BETTER,
        "Better since: health Watch, risk 50 to 40.",
        id="risk-down-by-exactly-the-step",
    ),
    pytest.param(
        Health.WATCH,
        50.0,
        Health.WATCH,
        60.0,
        Direction.WORSE,
        "Worse since: health Watch, risk 50 to 60.",
        id="risk-up-by-exactly-the-step",
    ),
    pytest.param(
        Health.WATCH,
        50.0,
        Health.WATCH,
        59.9,
        Direction.SAME,
        "About the same since: health Watch, risk 50 to 60.",
        id="just-short-of-the-step",
    ),
    pytest.param(
        Health.WATCH,
        41.3,
        Health.WATCH,
        31.3,
        Direction.BETTER,
        "Better since: health Watch, risk 41 to 31.",
        id="one-decimal-float-edge",
    ),
    pytest.param(
        Health.WATCH,
        41.5,
        Health.WATCH,
        44.4,
        Direction.SAME,
        "About the same since: health Watch, risk 42 to 44.",
        id="whole-number-rounding",
    ),
]


@pytest.mark.parametrize(
    ("before_health", "risk_before", "after_health", "risk_after", "direction", "sentence"),
    DIRECTION_CASES,
)
def test_the_direction_and_sentence_of_a_ready_check(
    before_health, risk_before, after_health, risk_after, direction, sentence
):
    snapshots = timeline(
        {ACCOUNT: entry(before_health, risk_before)},
        {ACCOUNT: entry(after_health, risk_after)},
    )
    check = checks_of(snapshots)[0]
    assert check.status is CheckStatus.READY
    assert check.direction is direction
    assert check.sentence == sentence
    assert (check.health_before, check.health_after) == (before_health, after_health)
    assert (check.risk_before, check.risk_after) == (risk_before, risk_after)
    assert (check.before_day, check.after_day) == (DONE_ON, DUE_14)
    assert check.due_on == DUE_14


def test_the_direction_labels_and_the_risk_step_the_cases_are_written_for():
    assert [direction.label for direction in Direction] == [
        "Better since",
        "About the same since",
        "Worse since",
        "Left the book since",
    ]
    assert RISK_STEP == 10.0  # the DIRECTION_CASES above are written for this step


# --- Readings


def test_readings_follow_the_evidence_in_order_without_repeats_or_unknown_kinds():
    item = step(
        why=evidence(
            "usage_trend_30d",
            None,  # an evidence line with no signal
            "usage_trend_30d",  # a repeat
            "open_tickets_sev1",
            "not_a_signal",  # not a signal key
            "nps_latest",
        )
    )
    snapshots = timeline(
        {ACCOUNT: entry(signals={USAGE: -38.0, TICKETS: 2.0, NPS: 6.0})},
        {ACCOUNT: entry(signals={USAGE: -10.0, TICKETS: 0.0, NPS: 7.0})},
    )
    readings = checks_of(snapshots, item=item)[0].readings
    assert [reading.signal for reading in readings] == [
        "usage_trend_30d",
        "open_tickets_sev1",
        "nps_latest",
    ]
    assert readings[0] == Reading(
        signal="usage_trend_30d",
        label="Usage trend",
        before=-38.0,
        after=-10.0,
        before_text="Active users down 38% in 30 days",
        after_text="Active users down 10% in 30 days",
    )
    assert readings[1].label == "Severity-1 tickets"
    assert readings[1].before_text == "2 open severity-1 tickets"
    assert readings[1].after_text == "No open severity-1 tickets"
    assert readings[2].before_text == "Latest NPS response was 6 out of 10"


def test_at_most_max_readings_are_compared_on_one_check():
    assert MAX_READINGS == 3
    item = step(why=evidence("usage_trend_30d", "open_tickets_sev1", "nps_latest", "champion_left"))
    both_sides = {USAGE: -38.0, TICKETS: 2.0, NPS: 6.0, CHAMPION: 0.0}
    snapshots = timeline({ACCOUNT: entry(signals=both_sides)}, {ACCOUNT: entry(signals=both_sides)})
    readings = checks_of(snapshots, item=item)[0].readings
    assert len(readings) == MAX_READINGS
    assert [reading.signal for reading in readings] == [
        "usage_trend_30d",
        "open_tickets_sev1",
        "nps_latest",
    ]


def test_a_signal_with_no_value_on_either_side_is_left_out_and_a_missing_side_says_no_reading():
    item = step(
        why=evidence("champion_left", "seat_utilisation", "billing_overdue_days", "usage_trend_30d")
    )
    snapshots = timeline(
        {ACCOUNT: entry(signals={SEATS: 85.0, USAGE: -38.0})},
        {ACCOUNT: entry(signals={BILLING: 12.0, USAGE: -10.0})},
    )
    readings = checks_of(snapshots, item=item)[0].readings
    assert [reading.signal for reading in readings] == [
        "seat_utilisation",
        "billing_overdue_days",
        "usage_trend_30d",
    ]
    seats, billing, _ = readings
    assert seats.before == 85.0
    assert seats.after is None
    assert seats.before_text == "85% of seats in use"
    assert seats.after_text == "No reading"
    assert billing.before is None
    assert billing.before_text == "No reading"
    assert billing.after_text == "Invoice 12 days overdue"


def test_a_custom_signal_reads_with_its_name_and_its_unit():
    config = ScoringConfig(
        custom=[
            CustomSignal(
                id="api_calls",
                name="API calls per week",
                rule=SignalRule(weight=10, start=0, full=500),
            )
        ]
    )
    snapshots = timeline(
        {ACCOUNT: entry(signals={API: 120.0})},
        {ACCOUNT: entry(signals={API: 480.0})},
    )
    check = checks_of(snapshots, item=step(why=evidence("custom:api_calls")), config=config)[0]
    (only,) = check.readings
    assert only == Reading(
        signal="custom:api_calls",
        label="API calls per week",
        before=120.0,
        after=480.0,
        before_text="API calls per week: 120",
        after_text="API calls per week: 480",
    )


def test_a_custom_signal_the_book_has_since_removed_reads_by_its_label_and_value():
    snapshots = timeline(
        {ACCOUNT: entry(signals={API: 120.0})},
        {ACCOUNT: entry(signals={API: 480.0})},
    )
    checks = checks_of(snapshots, item=step(why=evidence("custom:api_calls")), config=CONFIG)
    (only,) = checks[0].readings
    assert only.label == "Api calls"
    assert only.before_text == "Api calls: 120"
    assert only.after_text == "Api calls: 480"


# --- effects_for and the whole path (in memory)


def test_effects_for_lists_every_step_newest_first_or_only_one_accounts():
    items = [
        step("p-old", result_id="f-1"),
        step("p-new", result_id="f-2"),
        step("p-other", account_id=OTHER, result_id="f-3"),
    ]
    book = {
        ACCOUNT: AccountJournal(
            follow_ups=[
                follow_up("f-1", done_at=DONE_AT),
                follow_up("f-2", done_at=DONE_AT + timedelta(days=5)),
            ]
        ),
        OTHER: AccountJournal(follow_ups=[follow_up("f-3", done_at=DONE_AT + timedelta(days=2))]),
    }
    everyone = effects_for(items, book, FakeSnapshots(), TODAY, CONFIG)
    assert [effect.proposal_id for effect in everyone] == ["p-new", "p-other", "p-old"]
    only_one = effects_for(items, book, FakeSnapshots(), TODAY, CONFIG, account_id=ACCOUNT)
    assert [effect.proposal_id for effect in only_one] == ["p-new", "p-old"]
    # No score history at all: both checks of each step are missed, and say so.
    for effect in everyone:
        assert [check.status for check in effect.checks] == [CheckStatus.MISSED] * 2


def test_effect_of_is_pure_the_same_inputs_give_the_same_effect():
    def build() -> Effect:
        snapshots = timeline(
            {ACCOUNT: entry(Health.WATCH, 41.0)}, {ACCOUNT: entry(Health.WATCH, 44.0)}
        )
        return effect_of(step(why=evidence("usage_trend_30d")), DONE_ON, snapshots, TODAY, CONFIG)

    assert build().model_dump() == build().model_dump()


def test_no_sentence_says_because_caused_or_due_to_on_any_path():
    """Every check's sentence and every reading's text, across each status and direction."""
    scenarios = [
        effect_of(
            step(why=evidence("usage_trend_30d")),
            DONE_ON,
            timeline(
                {ACCOUNT: entry(Health.WATCH, 41.0, {USAGE: -38.0})},
                {ACCOUNT: entry(Health.AT_RISK, 52.0, {USAGE: -49.0})},
            ),
            TODAY,
            CONFIG,
        ),
        effect_of(
            step(),
            DONE_ON,
            timeline({ACCOUNT: entry(Health.WATCH, 41.0)}, {OTHER: entry()}),
            TODAY,
            CONFIG,
        ),
        effect_of(
            step(),
            DONE_ON,
            timeline({ACCOUNT: entry(Health.AT_RISK, 62.0)}, {ACCOUNT: entry(Health.WATCH, 48.0)}),
            TODAY,
            CONFIG,
        ),
        effect_of(
            step(),
            DONE_ON,
            timeline({ACCOUNT: entry(Health.WATCH, 41.0)}, {ACCOUNT: entry(Health.WATCH, 44.0)}),
            TODAY,
            CONFIG,
        ),
        effect_of(step(), DONE_ON, FakeSnapshots(), DONE_ON + timedelta(days=3), CONFIG),
        effect_of(
            step(),
            DONE_ON,
            FakeSnapshots(snap(DUE_14, {ACCOUNT: entry()}), snap(DUE_30, {ACCOUNT: entry()})),
            TODAY,
            CONFIG,
        ),
        effect_of(
            step(),
            DONE_ON,
            FakeSnapshots(snap(DONE_ON, {ACCOUNT: entry()})),
            DUE_14 + timedelta(days=AFTER_WINDOW + 1),
            CONFIG,
        ),
    ]
    statuses = {check.status for effect in scenarios for check in effect.checks}
    directions = {check.direction for effect in scenarios for check in effect.checks}
    assert statuses == set(CheckStatus)
    assert directions - {None} == set(Direction)
    texts = [check.sentence for effect in scenarios for check in effect.checks]
    texts += [
        text
        for effect in scenarios
        for check in effect.checks
        for reading in check.readings
        for text in (reading.before_text, reading.after_text)
    ]
    assert texts
    for text in texts:
        assert not FORBIDDEN_WORDS.search(text), text


# --- needs_review and to_review


def test_an_unanswered_ready_step_is_asked_about():
    assert made("p-1", directions=(Direction.SAME, None)).needs_review(TODAY)


def test_a_step_with_no_ready_check_is_not_asked_about_yet():
    assert not made("p-1", directions=(None, None)).needs_review(TODAY)


@pytest.mark.parametrize("verdict", [Verdict.HELPED, Verdict.DIDNT_HELP])
def test_an_answer_stops_the_question(verdict):
    answered = made("p-1", review_answer=Review(verdict=verdict, check_days=14, at=NOW))
    assert not answered.needs_review(TODAY)


def test_too_early_on_the_latest_ready_check_is_not_asked_again_but_a_later_one_is():
    early_on_14 = Review(verdict=Verdict.TOO_EARLY, check_days=14, at=NOW)
    only_14_ready = made("p-1", directions=(Direction.SAME, None), review_answer=early_on_14)
    assert not only_14_ready.needs_review(TODAY)
    both_ready = made(
        "p-1", directions=(Direction.SAME, Direction.BETTER), review_answer=early_on_14
    )
    assert both_ready.needs_review(TODAY)
    early_on_30 = Review(verdict=Verdict.TOO_EARLY, check_days=30, at=NOW)
    answered_at_30 = made(
        "p-1", directions=(Direction.SAME, Direction.BETTER), review_answer=early_on_30
    )
    assert not answered_at_30.needs_review(TODAY)


def test_too_early_on_14_is_asked_again_at_the_30_day_check():
    """The CSM said "too early" on the 14-day check; once the 30-day check reads, Today asks."""
    item = step(review_answer=Review(verdict=Verdict.TOO_EARLY, check_days=14, at=NOW))
    snapshots = timeline({ACCOUNT: entry(Health.WATCH, 41.0)}, {ACCOUNT: entry(Health.WATCH, 44.0)})
    at_14 = effect_of(item, DONE_ON, snapshots, DUE_14 + timedelta(days=1), CONFIG)
    assert at_14.latest_ready is not None and at_14.latest_ready.days == 14
    assert not at_14.needs_review(DUE_14 + timedelta(days=1))
    at_30 = effect_of(item, DONE_ON, snapshots, DUE_30 + timedelta(days=1), CONFIG)
    assert at_30.latest_ready is not None and at_30.latest_ready.days == 30
    assert at_30.needs_review(DUE_30 + timedelta(days=1))


def test_a_step_older_than_review_for_days_is_no_longer_asked_about():
    assert REVIEW_FOR_DAYS == 60
    old = made("p-1", directions=(Direction.SAME, None))
    assert old.needs_review(DONE_ON + timedelta(days=REVIEW_FOR_DAYS))
    assert not old.needs_review(DONE_ON + timedelta(days=REVIEW_FOR_DAYS + 1))


def test_to_review_asks_about_the_unanswered_steps_oldest_first():
    earlier = DONE_ON - timedelta(days=11)  # 20 Sep 2026
    effects_now = [
        made("p-b", done_on=DONE_ON),
        made(
            "p-c",
            done_on=DONE_ON,
            review_answer=Review(verdict=Verdict.HELPED, check_days=14, at=NOW),
        ),
        made("p-a", done_on=earlier),
        made("p-d", done_on=earlier),  # the same day as p-a: by id
        made("p-e", directions=(None, None)),  # still waiting: not asked
        made("p-f", done_on=TODAY - timedelta(days=REVIEW_FOR_DAYS + 1)),  # too old to ask
    ]
    asked = to_review(effects_now, TODAY)
    assert [effect.proposal_id for effect in asked] == ["p-a", "p-d", "p-b"]


# --- summary


def test_summary_counts_each_detector_in_order_then_claude_and_leaves_out_empty_rows():
    effects_now = [
        made(
            "p-claude",
            detector=None,
            directions=(Direction.BETTER, Direction.SAME),
        ),
        made("p-1", directions=(Direction.BETTER, None), review_answer=None),
        made(
            "p-2",
            directions=(Direction.WORSE, Direction.BETTER),
            review_answer=Review(verdict=Verdict.DIDNT_HELP, check_days=30, at=NOW),
        ),
        made(
            "p-3",
            directions=(Direction.SAME, None),
            review_answer=Review(verdict=Verdict.TOO_EARLY, check_days=14, at=NOW),
        ),
        made("p-4", directions=(None, None)),  # no check ready yet: a step, not checked
        made(
            "p-5",
            detector=Detector.GONE_QUIET,
            directions=(Direction.GONE, None),
        ),
    ]
    rows = summary(effects_now)
    assert rows == [
        DetectorSummary(
            detector=Detector.HEALTH_DROPPED,
            label="Health dropped",
            steps=4,
            checked=3,
            better=2,  # p-2 counts by its latest READY check (30 days): better
            same=1,
            worse=0,
            helped=0,
            didnt_help=1,
        ),
        DetectorSummary(
            detector=Detector.GONE_QUIET,
            label="Gone quiet",
            steps=1,
            checked=1,
            better=0,
            same=0,
            worse=1,  # left the book: counted as worse
            helped=0,
            didnt_help=0,
        ),
        DetectorSummary(
            detector=None,
            label=CLAUDE_LABEL,
            steps=1,
            checked=1,
            better=0,
            same=1,
            worse=0,
            helped=0,
            didnt_help=0,
        ),
    ]


def test_summary_counts_the_answers_and_leaves_too_early_out_of_them():
    effects_now = [
        made("p-1", review_answer=Review(verdict=Verdict.HELPED, check_days=14, at=NOW)),
        made("p-2", review_answer=Review(verdict=Verdict.HELPED, check_days=30, at=NOW)),
        made("p-3", review_answer=Review(verdict=Verdict.TOO_EARLY, check_days=14, at=NOW)),
    ]
    (row,) = summary(effects_now)
    assert (row.steps, row.helped, row.didnt_help) == (3, 2, 0)


def test_summary_of_no_steps_is_empty():
    assert summary([]) == []


# --- review()


def test_review_saves_the_answer_as_naive_utc_and_changes_nothing_else(files):
    store, journal = files
    suggestion = accepted_step(store, journal)
    journal_before = journal.all()
    later = datetime(2026, 11, 13, 10, 0, tzinfo=timezone(timedelta(hours=1)))  # 09:00 UTC
    answered = review(store, suggestion.id, Verdict.HELPED, 14, now=later)
    assert answered.review == Review(
        verdict=Verdict.HELPED, check_days=14, at=datetime(2026, 11, 13, 9, 0)
    )
    stored = store.get(suggestion.id)
    assert stored == answered
    assert stored is not None
    assert stored.status is ProposalStatus.ACCEPTED
    assert stored.result_id == suggestion.result_id
    assert stored.decided_at == suggestion.decided_at
    assert journal.all() == journal_before


def test_a_later_answer_replaces_the_earlier_one(files):
    store, journal = files
    suggestion = accepted_step(store, journal)
    review(store, suggestion.id, Verdict.HELPED, 14, now=NOW)
    review(store, suggestion.id, Verdict.DIDNT_HELP, 30, now=NOW + timedelta(days=16))
    stored = store.get(suggestion.id)
    assert stored is not None
    assert stored.review == Review(
        verdict=Verdict.DIDNT_HELP, check_days=30, at=NOW + timedelta(days=16)
    )
    assert len(store.all()) == 1


def test_an_unknown_suggestion_is_not_found(files):
    store, _ = files
    with pytest.raises(LookupError, match=re.escape(NOT_FOUND)):
        review(store, "p-missing", Verdict.HELPED, 14, now=NOW)


@pytest.mark.parametrize("state", ["open", "dismissed", "expired", "note"])
def test_only_an_accepted_step_can_be_reviewed(files, state):
    store, journal = files
    if state == "note":
        note = store.add(new_note())
        assert note is not None
        suggestion = proposals.accept(store, journal, note.id, now=NOW, today=TODAY)
    else:
        added = store.add(new_suggestion(key=f"health_dropped:{state}"))
        assert added is not None
        suggestion = added
        if state == "dismissed":
            suggestion = proposals.dismiss(store, added.id, now=NOW)
        if state == "expired":
            suggestion = store.replace(
                added.model_copy(
                    update={
                        "status": ProposalStatus.EXPIRED,
                        "decided_at": NOW,
                        "expired_reason": "Health is no longer worse.",
                    }
                )
            )
    with pytest.raises(ValueError, match=re.escape("Only a step you took can be reviewed.")):
        review(store, suggestion.id, Verdict.HELPED, 14, now=NOW)
    stored = store.get(suggestion.id)
    assert stored is not None
    assert stored.review is None


@pytest.mark.parametrize("check_days", [0, 7, 15, 60])
def test_the_check_must_be_14_or_30_days(files, check_days):
    store, journal = files
    suggestion = accepted_step(store, journal)
    with pytest.raises(ValueError, match=re.escape("Pick a check of 14 or 30 days.")):
        review(store, suggestion.id, Verdict.HELPED, check_days, now=NOW)
    stored = store.get(suggestion.id)
    assert stored is not None
    assert stored.review is None


@pytest.mark.parametrize("check_days", CHECK_DAYS)
def test_either_check_can_be_answered(files, check_days):
    store, journal = files
    suggestion = accepted_step(store, journal)
    answered = review(store, suggestion.id, Verdict.TOO_EARLY, check_days, now=NOW)
    assert answered.review is not None
    assert answered.review.check_days == check_days


# --- The whole path on real files


def test_the_whole_path_on_a_real_history_folder_and_journal(tmp_path):
    store = Proposals(LocalStore(tmp_path))
    journal = Journal(LocalStore(tmp_path))
    history = HistoryStore(LocalStore(tmp_path))
    suggestion = accepted_step(store, journal)
    assert suggestion.result_id is not None
    journal.complete_follow_up(ACCOUNT, suggestion.result_id, now=DONE_AT)
    history.save(snap(DONE_ON - timedelta(days=2), {ACCOUNT: entry(Health.WATCH, 41.0)}))
    history.save(snap(DUE_14 + timedelta(days=2), {ACCOUNT: entry(Health.WATCH, 44.0)}))

    today = DUE_14 + timedelta(days=3)
    found = effects_for(store.all(), journal.all(), history, today, CONFIG)
    assert [effect.proposal_id for effect in found] == [suggestion.id]
    effect = found[0]
    assert effect.done_on == DONE_ON
    ready, waiting = effect.checks
    assert ready.status is CheckStatus.READY
    assert ready.direction is Direction.SAME
    assert ready.before_day == DONE_ON - timedelta(days=2)
    assert ready.after_day == DUE_14 + timedelta(days=2)
    assert ready.sentence == "About the same since: health Watch, risk 41 to 44."
    assert waiting.status is CheckStatus.WAITING
    assert waiting.sentence == "The 30-day check is on 31 Oct 2026."
    assert to_review(found, today) == found

    review(store, suggestion.id, Verdict.HELPED, 14, now=NOW)
    answered = effects_for(store.all(), journal.all(), history, today, CONFIG)
    assert answered[0].review == Review(verdict=Verdict.HELPED, check_days=14, at=NOW)
    assert to_review(answered, today) == []


def test_a_day_with_no_snapshot_on_the_real_folder_is_missed_as_not_open(tmp_path):
    store = Proposals(LocalStore(tmp_path))
    journal = Journal(LocalStore(tmp_path))
    history = HistoryStore(LocalStore(tmp_path))
    suggestion = accepted_step(store, journal)
    assert suggestion.result_id is not None
    journal.complete_follow_up(ACCOUNT, suggestion.result_id, now=DONE_AT)
    history.save(snap(DONE_ON, {ACCOUNT: entry(Health.WATCH, 41.0)}))  # nothing after it
    found = effects_for(store.all(), journal.all(), history, DUE_14 + timedelta(days=8), CONFIG)
    fourteen = found[0].checks[0]
    assert fourteen.status is CheckStatus.MISSED
    assert fourteen.sentence == (
        "Wardmind wasn't open around 15 Oct 2026, so there is nothing to compare."
    )
    assert found[0].checks[1].status is CheckStatus.WAITING
