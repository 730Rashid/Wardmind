"""core/proposals.py, the workstream half: plan (the five detectors), refresh and expiry, accept and
dismiss, and history.health_since. The store's models and caps are in test_proposals_store.py.

Every day is fixed (TODAY, NOW), and the files live in the temp folders from conftest, so nothing
reads the real book or journal. The demo book check at the end goes through service.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from wardmind.core import history, proposals, service
from wardmind.core.config import ScoringConfig
from wardmind.core.drafts import DraftKind
from wardmind.core.journal import (
    AccountJournal,
    CallKind,
    Direction,
    FollowUp,
    FollowUpStatus,
    ForecastCall,
    Journal,
    Note,
    NoteSource,
    Snooze,
    TouchKind,
)
from wardmind.core.models import (
    Account,
    Health,
    Milestones,
    Motion,
    Reason,
    ScoredAccount,
    Signal,
    SignalKind,
)
from wardmind.core.proposals import (
    Detector,
    Edits,
    Evidence,
    Proposal,
    ProposalKind,
    Proposals,
    ProposalSource,
    ProposalStatus,
)
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import LocalStore

TODAY = date(2026, 11, 13)
NOW = datetime(2026, 11, 13, 9, 0)
CONFIG = ScoringConfig()


def _account(account_id: str = "a-1", name: str = "Acme", renewal: date | None = None) -> Account:
    return Account(
        id=account_id,
        name=name,
        arr=120_000,
        tier="Enterprise",
        owner="Sam",
        renewal_date=renewal or TODAY + timedelta(days=200),
        milestones=Milestones(done=0, total=0),
    )


def _scored(
    account: Account | None = None,
    health: Health = Health.HEALTHY,
    risk: float = 20.0,
    reasons: list[Reason] | None = None,
) -> ScoredAccount:
    return ScoredAccount(
        account=account or _account(),
        motion=Motion.RENEWAL,
        health=health,
        risk=risk,
        priority=risk,
        reasons=reasons or [],
    )


def _reading(account_id: str, kind: SignalKind, observed: date, value: float = 0.0) -> Signal:
    return Signal(
        account_id=account_id,
        kind=kind,
        value=value,
        observed_at=datetime(observed.year, observed.month, observed.day, 8, 0),
    )


def _all_readings(account_id: str, observed: date) -> dict[SignalKind, Signal]:
    return {kind: _reading(account_id, kind, observed) for kind in SignalKind}


def _email(
    happened_on: date,
    direction: Direction,
    thread: str = "renewal plan",
    subject: str = "Renewal plan",
    note_id: str = "n-1",
) -> Note:
    return Note(
        id=note_id,
        kind=TouchKind.EMAIL,
        text="Snippet",
        happened_on=happened_on,
        created_at=datetime(happened_on.year, happened_on.month, happened_on.day, 9, 0),
        subject=subject,
        direction=direction,
        source=NoteSource.MAILBOX,
        message_id=f"<{note_id}@example.com>",
        thread=thread,
    )


def _call_note(happened_on: date, note_id: str = "n-call") -> Note:
    return Note(
        id=note_id,
        kind=TouchKind.CALL,
        text="Spoke",
        happened_on=happened_on,
        created_at=datetime(happened_on.year, happened_on.month, happened_on.day, 10, 0),
    )


def _worse(account_id: str = "a-1", name: str = "Acme") -> history.WeekChanges:
    change = history.Change(
        account_id=account_id,
        name=name,
        kind=history.ChangeKind.WORSE,
        sentence="Moved from Watch to At risk (risk 38 to 52).",
        health_before=Health.WATCH,
        health_after=Health.AT_RISK,
        risk_before=38,
        risk_after=52,
        priority_after=52,
    )
    return history.WeekChanges(since=TODAY - timedelta(days=7), changes=[change])


def _plan(
    book: list[ScoredAccount],
    journals: dict[str, AccountJournal] | None = None,
    *,
    latest: dict[str, dict[SignalKind, Signal]] | None = None,
    changes: history.WeekChanges | None = None,
    since: dict[str, date] | None = None,
    settings: Settings | None = None,
) -> list[Proposal]:
    return proposals.plan(
        book,
        journals or {},
        latest or {},
        changes,
        since or {},
        settings or Settings(),
        TODAY,
        NOW,
        config=CONFIG,
    )


def _only(planned: list[Proposal], detector: Detector) -> Proposal | None:
    found = [item for item in planned if item.detector is detector]
    assert len(found) <= 1, "a detector suggests at most once per account"
    return found[0] if found else None


@pytest.fixture
def store(tmp_path) -> Proposals:
    return Proposals(LocalStore(tmp_path))


@pytest.fixture
def journal_file(tmp_path) -> Journal:
    return Journal(LocalStore(tmp_path))


# --- plan: the five detectors -----------------------------------------------------------------


def test_plan_is_pure_and_the_same_twice() -> None:
    book = [_scored(_account("a-1"), Health.AT_RISK, 52), _scored(_account("a-2", "Beta"))]
    changes = _worse("a-1")
    first = _plan(book, changes=changes)
    second = _plan(book, changes=changes)
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]
    assert all(item.id == "" for item in first)
    assert all(item.basis_day == TODAY and item.created_at == NOW for item in first)


def test_health_dropped_checks_in_with_the_change_as_evidence() -> None:
    book = [_scored(_account(), Health.AT_RISK, 52)]
    found = _only(
        _plan(book, changes=_worse(), since={"a-1": date(2026, 11, 10)}), Detector.HEALTH_DROPPED
    )
    assert found is not None
    assert found.kind is ProposalKind.FOLLOW_UP and found.source is ProposalSource.DETECTOR
    assert found.title == "Check in with Acme: health dropped to At risk"
    assert found.text == found.title
    assert found.key == "health_dropped:at_risk:2026-11-10"
    assert found.draft_kind is DraftKind.CHECK_IN
    assert found.due == TODAY
    assert found.why[0] == Evidence(
        text="Moved from Watch to At risk (risk 38 to 52).", observed=TODAY
    )


def test_health_dropped_keys_on_today_without_a_health_run_day() -> None:
    book = [_scored(_account(), Health.AT_RISK, 52)]
    found = _only(_plan(book, changes=_worse()), Detector.HEALTH_DROPPED)
    assert found is not None
    assert found.key == "health_dropped:at_risk:2026-11-13"


def test_an_improvement_or_no_week_to_compare_suggests_no_check_in() -> None:
    better = history.Change(
        account_id="a-1",
        name="Acme",
        kind=history.ChangeKind.BETTER,
        sentence="Moved from At risk to Watch (risk 60 to 40).",
        health_before=Health.AT_RISK,
        health_after=Health.WATCH,
    )
    book = [_scored(_account(), Health.WATCH, 40)]
    assert (
        _only(
            _plan(book, changes=history.WeekChanges(since=TODAY, changes=[better])),
            Detector.HEALTH_DROPPED,
        )
        is None
    )
    assert _only(_plan(book, changes=None), Detector.HEALTH_DROPPED) is None


@pytest.mark.parametrize(
    ("days_ahead", "call_on", "expected"),
    [
        (0, None, True),  # the renewal is today
        (60, None, True),  # the last day of the default window
        (61, None, False),  # just outside it
        (-1, None, False),  # already passed
        (60, TODAY + timedelta(days=60), False),  # the forecast call is made
        (60, TODAY + timedelta(days=90), True),  # a call on another renewal doesn't count
    ],
)
def test_renewal_no_call_window_and_call(
    days_ahead: int, call_on: date | None, expected: bool
) -> None:
    renewal = TODAY + timedelta(days=days_ahead)
    entry = AccountJournal()
    if call_on is not None:
        entry = AccountJournal(
            calls=[
                ForecastCall(
                    id="c-1",
                    kind=CallKind.LIKELY,
                    renewal_date=call_on,
                    made_on=TODAY,
                    health=Health.HEALTHY,
                    risk=20,
                    arr=120_000,
                    recorded_at=NOW,
                )
            ]
        )
    book = [_scored(_account(renewal=renewal))]
    found = _only(_plan(book, {"a-1": entry}), Detector.RENEWAL_NO_CALL)
    assert (found is not None) is expected
    if found is not None:
        assert found.title == "Make your renewal call for Acme"
        assert found.key == f"renewal_no_call:{renewal.isoformat()}"
        assert found.draft_kind is DraftKind.RENEWAL
        assert found.why[0].text.startswith("Renews ")


def test_renewal_window_follows_the_setting() -> None:
    book = [_scored(_account(renewal=TODAY + timedelta(days=31)))]
    assert (
        _only(_plan(book, settings=Settings(rule_renewal_days=30)), Detector.RENEWAL_NO_CALL)
        is None
    )
    assert _only(_plan(book, settings=Settings(rule_renewal_days=60)), Detector.RENEWAL_NO_CALL)


@pytest.mark.parametrize(
    ("days_ago", "waits_on_them", "expected"),
    [
        (4, True, False),  # one day short of reply_wait_days (5)
        (5, True, True),  # exactly the wait: the reply is overdue
        (2, False, False),  # they wrote, and our answer is not theirs to chase
    ],
)
def test_reply_overdue_boundary(days_ago: int, waits_on_them: bool, expected: bool) -> None:
    sent_on = TODAY - timedelta(days=days_ago)
    direction = Direction.SENT if waits_on_them else Direction.RECEIVED
    entry = AccountJournal(notes=[_email(sent_on, direction)])
    found = _only(_plan([_scored()], {"a-1": entry}), Detector.REPLY_OVERDUE)
    assert (found is not None) is expected
    if found is not None:
        assert found.title == "Chase Acme for a reply"
        assert found.key == "reply_overdue:renewal plan:2026-11-08"
        assert found.draft_kind is DraftKind.CHASE_REPLY
        assert found.why[0] == Evidence(
            text="Your email on 8 Nov 2026 has had no reply for 5 days", observed=sent_on
        )
        assert found.why[1].text == "Subject: Renewal plan"


def test_reply_overdue_uses_the_settings_wait() -> None:
    entry = AccountJournal(notes=[_email(TODAY - timedelta(days=5), Direction.SENT)])
    assert (
        _only(
            _plan([_scored()], {"a-1": entry}, settings=Settings(reply_wait_days=7)),
            Detector.REPLY_OVERDUE,
        )
        is None
    )
    assert _only(
        _plan([_scored()], {"a-1": entry}, settings=Settings(reply_wait_days=3)),
        Detector.REPLY_OVERDUE,
    )


def test_reply_overdue_hashes_a_long_thread_into_its_key() -> None:
    long_subject = "Quarterly business review and the plan for next year " * 2
    entry = AccountJournal(
        notes=[
            _email(
                TODAY - timedelta(days=6),
                Direction.SENT,
                thread=long_subject.casefold(),
                subject=long_subject,
            )
        ]
    )
    found = _only(_plan([_scored()], {"a-1": entry}), Detector.REPLY_OVERDUE)
    assert found is not None
    assert len(found.key) <= proposals.MAX_KEY
    assert found.key.startswith("reply_overdue:") and found.key.endswith(":2026-11-07")


@pytest.mark.parametrize(
    ("health", "days_since_contact", "expected"),
    [
        (Health.AT_RISK, 14, False),  # contacted exactly quiet_days ago: not older than that
        (Health.AT_RISK, 15, True),
        (Health.WATCH, None, True),  # never contacted
        (Health.HEALTHY, 30, False),  # healthy accounts don't go quiet on the list
    ],
)
def test_gone_quiet_boundary(
    health: Health, days_since_contact: int | None, expected: bool
) -> None:
    entry = AccountJournal()
    if days_since_contact is not None:
        entry = AccountJournal(notes=[_call_note(TODAY - timedelta(days=days_since_contact))])
    found = _only(_plan([_scored(health=health, risk=60)], {"a-1": entry}), Detector.GONE_QUIET)
    assert (found is not None) is expected
    if found is not None:
        assert found.title == "Get back in touch with Acme"
        assert found.draft_kind is DraftKind.RE_ENGAGE


def test_gone_quiet_key_and_evidence_name_the_last_contact() -> None:
    last = TODAY - timedelta(days=20)
    found = _only(
        _plan(
            [_scored(health=Health.WATCH, risk=40)],
            {"a-1": AccountJournal(notes=[_call_note(last)])},
        ),
        Detector.GONE_QUIET,
    )
    assert found is not None
    assert found.key == "gone_quiet:2026-10-24"
    assert found.why[0] == Evidence(text="Last contact 24 Oct 2026", observed=last)
    never = _only(_plan([_scored(health=Health.WATCH, risk=40)]), Detector.GONE_QUIET)
    assert never is not None and never.key == "gone_quiet:never"
    assert never.why[0].text == "No contact logged yet"


def test_blind_spot_fires_for_a_healthy_account_on_thin_data() -> None:
    found = _only(_plan([_scored()]), Detector.BLIND_SPOT)
    assert found is not None
    assert found.title == "Get fresh data for Acme"
    assert found.key.startswith("blind_spot:") and len(found.key) == len("blind_spot:") + 10
    assert found.draft_kind is None
    assert found.why[0].text.startswith("Based on 0 of ")


def test_blind_spot_is_quiet_on_full_fresh_data_and_for_an_at_risk_account() -> None:
    full = {"a-1": _all_readings("a-1", TODAY)}
    assert _only(_plan([_scored()], latest=full), Detector.BLIND_SPOT) is None
    thin_at_risk = _plan([_scored(health=Health.AT_RISK, risk=60)])
    assert _only(thin_at_risk, Detector.BLIND_SPOT) is None


def test_blind_spot_key_depends_on_which_labels_are_missing_or_stale() -> None:
    none_read = _only(_plan([_scored()]), Detector.BLIND_SPOT)
    assert none_read is not None
    again = _only(_plan([_scored()]), Detector.BLIND_SPOT)
    assert again is not None and again.key == none_read.key  # the same inputs, the same key
    one_fresh = {"a-1": {SignalKind.NPS_LATEST: _reading("a-1", SignalKind.NPS_LATEST, TODAY)}}
    other = _only(_plan([_scored()], latest=one_fresh), Detector.BLIND_SPOT)
    assert other is not None and other.key != none_read.key
    stale_nps = {
        "a-1": {
            SignalKind.NPS_LATEST: _reading(
                "a-1", SignalKind.NPS_LATEST, TODAY - timedelta(days=400)
            )
        }
    }
    stale = _only(_plan([_scored()], latest=stale_nps), Detector.BLIND_SPOT)
    assert stale is not None
    assert "Out of date" in stale.why[0].text


def test_a_snoozed_account_gets_nothing_while_the_snooze_holds() -> None:
    snoozed = AccountJournal(
        snooze=Snooze(until=TODAY + timedelta(days=3), health=Health.AT_RISK, created_at=NOW)
    )
    book = [_scored(_account(renewal=TODAY + timedelta(days=10)), Health.AT_RISK, 52)]
    assert _plan(book, {"a-1": snoozed}, changes=_worse()) == []


def test_a_worse_health_ends_the_snooze_for_planning() -> None:
    snoozed = AccountJournal(
        snooze=Snooze(until=TODAY + timedelta(days=3), health=Health.HEALTHY, created_at=NOW)
    )
    book = [_scored(health=Health.AT_RISK, risk=52)]
    found = _plan(book, {"a-1": snoozed}, changes=_worse())
    assert _only(found, Detector.HEALTH_DROPPED) is not None


def test_the_journal_rule_key_collision_is_never_suggested_again() -> None:
    open_rule = FollowUp(
        id="f-aaaaaaaa",
        text="Check in: health dropped",
        due=TODAY,
        status=FollowUpStatus.OPEN,
        created_at=NOW,
        rule="health_dropped:at_risk:2026-11-13",
    )
    book = [_scored(health=Health.AT_RISK, risk=52)]
    assert (
        _only(
            _plan(book, {"a-1": AccountJournal(follow_ups=[open_rule])}, changes=_worse()),
            Detector.HEALTH_DROPPED,
        )
        is None
    )
    removed = AccountJournal(dismissed_rules=["health_dropped:at_risk:2026-11-13"])
    assert _only(_plan(book, {"a-1": removed}, changes=_worse()), Detector.HEALTH_DROPPED) is None


def test_evidence_adds_the_top_reasons_with_their_signal_and_day() -> None:
    reasons = [
        Reason(
            kind=SignalKind.USAGE_TREND_30D,
            contribution=30,
            plain_english="Active users down 38% in 30 days",
        ),
        Reason(kind=SignalKind.NPS_LATEST, contribution=10, plain_english="Latest NPS 4"),
    ]
    latest = {
        "a-1": {
            SignalKind.USAGE_TREND_30D: _reading(
                "a-1", SignalKind.USAGE_TREND_30D, TODAY - timedelta(days=2)
            )
        }
    }
    book = [_scored(health=Health.AT_RISK, risk=52, reasons=reasons)]
    found = _only(_plan(book, latest=latest, changes=_worse()), Detector.HEALTH_DROPPED)
    assert found is not None
    assert found.why[1] == Evidence(
        text="Active users down 38% in 30 days",
        signal=SignalKind.USAGE_TREND_30D.value,
        observed=TODAY - timedelta(days=2),
    )
    assert found.why[2].signal == SignalKind.NPS_LATEST.value
    assert found.why[2].observed is None
    assert len(found.why) <= proposals.MAX_WHY


def test_one_suggestion_per_detector_per_account_in_book_order() -> None:
    entry = AccountJournal(notes=[_email(TODAY - timedelta(days=6), Direction.SENT)])
    book = [
        _scored(_account("a-1", "Acme", TODAY + timedelta(days=10)), Health.AT_RISK, 52),
        _scored(_account("a-2", "Beta", TODAY + timedelta(days=10)), Health.WATCH, 40),
    ]
    planned = _plan(book, {"a-1": entry}, changes=_worse())
    assert [item.account_id for item in planned][:1] == ["a-1"]
    names = [item.detector for item in planned if item.account_id == "a-1"]
    assert names == sorted(set(names), key=list(Detector).index)


# --- refresh, expiry and the caps --------------------------------------------------------------


def test_refresh_adds_once_and_a_second_run_adds_nothing(store: Proposals) -> None:
    book = [_scored(_account(renewal=TODAY + timedelta(days=10)), Health.AT_RISK, 52)]
    first = proposals.refresh(
        store, book, {}, {}, _worse(), {}, Settings(), TODAY, NOW, config=CONFIG
    )
    assert first.added >= 2 and first.waiting == 0
    assert store.load().last_run == TODAY
    second = proposals.refresh(
        store, book, {}, {}, _worse(), {}, Settings(), TODAY, NOW, config=CONFIG
    )
    assert second.added == 0 and second.expired == 0 and second.waiting == 0
    assert store.open_count() == first.added


def test_refresh_does_nothing_when_suggestions_are_off(store: Proposals) -> None:
    book = [_scored(_account(renewal=TODAY + timedelta(days=10)), Health.AT_RISK, 52)]
    result = proposals.refresh(
        store, book, {}, {}, _worse(), {}, Settings(suggestions=False), TODAY, NOW, config=CONFIG
    )
    assert result == proposals.RefreshResult()
    assert store.all() == []
    assert store.load().last_run is None


def test_a_dismissed_key_never_returns(store: Proposals) -> None:
    book = [_scored(_account(renewal=TODAY + timedelta(days=10)))]
    proposals.refresh(store, book, {}, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG)
    renewal = store.open(account_id="a-1")
    target = next(item for item in renewal if item.detector is Detector.RENEWAL_NO_CALL)
    proposals.dismiss(store, target.id, now=NOW)
    again = proposals.refresh(
        store, book, {}, {}, None, {}, Settings(), TODAY + timedelta(days=1), NOW, config=CONFIG
    )
    assert again.added == 0
    assert all(item.key != target.key for item in store.open())
    assert (
        store.get(target.id) is not None and store.get(target.id).status is ProposalStatus.DISMISSED
    )


def test_caps_report_what_they_held_back(store: Proposals, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proposals, "MAX_PER_RUN", 2)
    book = [
        _scored(_account(f"a-{index}", f"Account {index}", TODAY + timedelta(days=5)))
        for index in range(1, 5)
    ]
    # Fresh readings and a recent contact leave only the renewal call to suggest, for four accounts.
    latest = {item.account.id: _all_readings(item.account.id, TODAY) for item in book}
    contacts = {item.account.id: AccountJournal(notes=[_call_note(TODAY)]) for item in book}
    result = proposals.refresh(
        store, book, contacts, latest, None, {}, Settings(), TODAY, NOW, config=CONFIG
    )
    assert result.added == 2
    assert result.waiting == 2  # four were planned, two fit in the run


def test_expiry_says_why_the_reason_went(store: Proposals) -> None:
    account = _account(renewal=TODAY + timedelta(days=10))
    proposals.refresh(
        store,
        [_scored(account, Health.AT_RISK, 52)],
        {},
        {},
        _worse(),
        {},
        Settings(),
        TODAY,
        NOW,
        config=CONFIG,
    )
    worse = next(item for item in store.open() if item.detector is Detector.HEALTH_DROPPED)
    result = proposals.refresh(
        store,
        [_scored(account, Health.HEALTHY, 20)],
        {},
        {},
        None,
        {},
        Settings(),
        TODAY + timedelta(days=1),
        NOW,
        config=CONFIG,
    )
    assert result.expired >= 1
    expired = store.get(worse.id)
    assert expired is not None and expired.status is ProposalStatus.EXPIRED
    assert expired.expired_reason == "Health is no longer worse."
    assert expired.decided_at == NOW


def test_a_contact_logged_after_the_suggestion_expires_a_gone_quiet_one(store: Proposals) -> None:
    book = [_scored(health=Health.AT_RISK, risk=60)]
    proposals.refresh(store, book, {}, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG)
    quiet = next(item for item in store.open() if item.detector is Detector.GONE_QUIET)
    contacted = {"a-1": AccountJournal(notes=[_call_note(TODAY)])}
    proposals.expire_cleared(
        store, book, contacted, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG
    )
    gone = store.get(quiet.id)
    assert gone is not None and gone.status is ProposalStatus.EXPIRED
    assert gone.expired_reason == "You logged a contact on 13 Nov 2026."


def test_an_account_that_left_the_book_expires_its_suggestions(store: Proposals) -> None:
    book = [_scored(health=Health.AT_RISK, risk=60)]
    proposals.refresh(store, book, {}, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG)
    count = store.open_count()
    expired = proposals.expire_cleared(
        store, [], {}, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG
    )
    assert expired == count
    assert store.open_count() == 0
    assert {item.expired_reason for item in store.all()} == {"No longer in your book."}


def test_refresh_returns_the_count_of_what_it_expired(store: Proposals) -> None:
    book = [_scored(health=Health.AT_RISK, risk=60)]
    proposals.refresh(store, book, {}, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG)
    result = proposals.refresh(store, [], {}, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG)
    assert result.expired >= 1 and result.added == 0


# --- accept and dismiss -----------------------------------------------------------------------


def _store_one(store: Proposals, *, renewal_days: int = 10) -> Proposal:
    book = [_scored(_account(renewal=TODAY + timedelta(days=renewal_days)))]
    proposals.refresh(store, book, {}, {}, None, {}, Settings(), TODAY, NOW, config=CONFIG)
    return next(item for item in store.open() if item.detector is Detector.RENEWAL_NO_CALL)


def test_accept_writes_exactly_one_follow_up_and_records_its_id(
    store: Proposals, journal_file: Journal
) -> None:
    item = _store_one(store)
    accepted = proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY)
    entries = journal_file.for_account("a-1").follow_ups
    assert len(entries) == 1
    assert entries[0].text == item.title and entries[0].due == TODAY
    assert entries[0].rule == item.key
    assert accepted.status is ProposalStatus.ACCEPTED
    assert accepted.result_id == entries[0].id
    assert accepted.decided_at == NOW


def test_a_second_accept_changes_nothing(store: Proposals, journal_file: Journal) -> None:
    item = _store_one(store)
    first = proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY)
    second = proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY)
    assert second == first
    assert len(journal_file.for_account("a-1").follow_ups) == 1


def test_a_dismissed_suggestion_cannot_be_accepted(store: Proposals, journal_file: Journal) -> None:
    item = _store_one(store)
    proposals.dismiss(store, item.id, now=NOW)
    with pytest.raises(ValueError, match="dismissed"):
        proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY)
    assert journal_file.for_account("a-1").follow_ups == []


def test_an_accepted_suggestion_cannot_be_dismissed(
    store: Proposals, journal_file: Journal
) -> None:
    item = _store_one(store)
    proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY)
    with pytest.raises(ValueError, match="accepted"):
        proposals.dismiss(store, item.id, now=NOW)
    assert store.get(item.id).status is ProposalStatus.ACCEPTED


def test_dismiss_twice_is_a_no_op(store: Proposals) -> None:
    item = _store_one(store)
    first = proposals.dismiss(store, item.id, now=NOW)
    second = proposals.dismiss(store, item.id, now=NOW)
    assert first.status is second.status is ProposalStatus.DISMISSED


def test_unknown_id_is_a_lookup_error(store: Proposals, journal_file: Journal) -> None:
    with pytest.raises(LookupError, match="isn't there any more"):
        proposals.accept(store, journal_file, "p-missing1", now=NOW, today=TODAY)
    with pytest.raises(LookupError):
        proposals.dismiss(store, "p-missing1", now=NOW)


def test_edits_change_the_text_and_the_due_day(store: Proposals, journal_file: Journal) -> None:
    item = _store_one(store)
    edits = Edits(text="Call Sam about the renewal", due=TODAY + timedelta(days=3))
    proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY, edits=edits)
    follow_up = journal_file.for_account("a-1").follow_ups[0]
    assert follow_up.text == "Call Sam about the renewal"
    assert follow_up.due == TODAY + timedelta(days=3)


def test_an_edited_due_day_in_the_past_is_today(store: Proposals, journal_file: Journal) -> None:
    item = _store_one(store)
    proposals.accept(
        store,
        journal_file,
        item.id,
        now=NOW,
        today=TODAY,
        edits=Edits(due=TODAY - timedelta(days=4)),
    )
    assert journal_file.for_account("a-1").follow_ups[0].due == TODAY


def test_a_blank_edited_text_is_refused_by_the_journal(
    store: Proposals, journal_file: Journal
) -> None:
    item = _store_one(store)
    with pytest.raises(ValueError, match="Say what to follow up on"):
        proposals.accept(
            store, journal_file, item.id, now=NOW, today=TODAY, edits=Edits(text="   ")
        )
    assert store.get(item.id).status is ProposalStatus.OPEN


def test_a_follow_up_the_journal_already_has_is_not_written_again(
    store: Proposals, journal_file: Journal
) -> None:
    item = _store_one(store)
    existing = journal_file.add_follow_up(
        "a-1", "Added by a rule", TODAY, now=NOW, today=TODAY, rule=item.key
    )
    accepted = proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY)
    assert accepted.status is ProposalStatus.ACCEPTED
    assert accepted.result_id == existing.id
    assert len(journal_file.for_account("a-1").follow_ups) == 1


def test_a_key_the_journal_removed_is_refused(store: Proposals, journal_file: Journal) -> None:
    item = _store_one(store)
    journal_file.add_follow_up("a-1", "Removed", TODAY, now=NOW, today=TODAY, rule=item.key)
    journal_file.remove_follow_up("a-1", journal_file.for_account("a-1").follow_ups[0].id)
    with pytest.raises(ValueError, match="removed this follow-up"):
        proposals.accept(store, journal_file, item.id, now=NOW, today=TODAY)


def test_an_expired_suggestion_cannot_be_accepted_and_says_why(
    store: Proposals, journal_file: Journal
) -> None:
    book = [_scored(_account(renewal=TODAY + timedelta(days=10)), Health.AT_RISK, 52)]
    proposals.refresh(store, book, {}, {}, _worse(), {}, Settings(), TODAY, NOW, config=CONFIG)
    worse = next(item for item in store.open() if item.detector is Detector.HEALTH_DROPPED)
    proposals.refresh(
        store,
        [_scored(_account(renewal=TODAY + timedelta(days=10)))],
        {},
        {},
        None,
        {},
        Settings(),
        TODAY,
        NOW,
        config=CONFIG,
    )
    with pytest.raises(ValueError, match="Health is no longer worse"):
        proposals.accept(store, journal_file, worse.id, now=NOW, today=TODAY)
    with pytest.raises(ValueError, match="expired"):
        proposals.dismiss(store, worse.id, now=NOW)


def test_a_note_suggestion_writes_a_suggestion_note(
    store: Proposals, journal_file: Journal
) -> None:
    item = Proposal(
        id="",
        account_id="a-1",
        kind=ProposalKind.NOTE,
        source=ProposalSource.MCP,
        key="mcp:req-1",
        title="Record the call",
        why=[Evidence(text="Claude read the call summary")],
        text="Spoke with Sam about seats.",
        created_at=NOW,
        basis_day=TODAY,
    )
    stored = store.add(item)
    assert stored is not None
    accepted = proposals.accept(store, journal_file, stored.id, now=NOW, today=TODAY)
    note = journal_file.for_account("a-1").notes[0]
    assert note.text == "Spoke with Sam about seats."
    assert note.source is NoteSource.SUGGESTION and note.kind is TouchKind.NOTE
    assert accepted.result_id == note.id


def test_a_snooze_needs_the_account_health_and_writes_the_snooze(
    store: Proposals, journal_file: Journal
) -> None:
    until = TODAY + timedelta(days=7)
    stored = store.add(
        Proposal(
            id="",
            account_id="a-1",
            kind=ProposalKind.SNOOZE,
            source=ProposalSource.MCP,
            key="mcp:req-2",
            title="Snooze Acme for a week",
            why=[Evidence(text="Claude: the renewal is handled")],
            text="The renewal is handled.",
            until=until,
            created_at=NOW,
            basis_day=TODAY,
        )
    )
    assert stored is not None
    with pytest.raises(ValueError, match="snoozed here"):
        proposals.accept(store, journal_file, stored.id, now=NOW, today=TODAY)
    accepted = proposals.accept(
        store, journal_file, stored.id, now=NOW, today=TODAY, health=Health.WATCH
    )
    snooze = journal_file.for_account("a-1").snooze
    assert snooze is not None and snooze.until == until and snooze.health is Health.WATCH
    assert accepted.result_id == "snooze"


# --- history.health_since ------------------------------------------------------------------


def _snapshot(day: date, health: Health, account_id: str = "a-1") -> history.BookSnapshot:
    entry = history.SnapshotEntry(
        name="Acme", rank=1, risk=40, priority=40, health=health, motion=Motion.RENEWAL, arr=1.0
    )
    return history.BookSnapshot(day=day, taken_at=NOW, accounts={account_id: entry})


def test_health_since_runs_back_through_the_same_health_and_skips_missing_days(tmp_path) -> None:
    files = history.HistoryStore(LocalStore(tmp_path))
    files.save(_snapshot(TODAY - timedelta(days=1), Health.AT_RISK))
    # today - 2 has no file: Wardmind wasn't open, and that doesn't end the run
    files.save(_snapshot(TODAY - timedelta(days=3), Health.AT_RISK))
    files.save(_snapshot(TODAY - timedelta(days=4), Health.WATCH))
    book = [_scored(health=Health.AT_RISK)]
    since = history.health_since(book, files, TODAY)
    assert since == {"a-1": TODAY - timedelta(days=3)}


def test_health_since_is_today_without_earlier_snapshots(tmp_path) -> None:
    files = history.HistoryStore(LocalStore(tmp_path))
    assert history.health_since([_scored(health=Health.WATCH)], files, TODAY) == {"a-1": TODAY}


def test_health_since_follows_each_health_separately(tmp_path) -> None:
    files = history.HistoryStore(LocalStore(tmp_path))
    files.save(_snapshot(TODAY - timedelta(days=1), Health.WATCH))
    assert history.health_since([_scored(health=Health.AT_RISK)], files, TODAY) == {"a-1": TODAY}
    assert history.health_since([_scored(health=Health.WATCH)], files, TODAY) == {
        "a-1": TODAY - timedelta(days=1)
    }


# --- the demo book, through service ------------------------------------------------------------


def test_the_demo_book_gives_a_stable_non_empty_set() -> None:
    save_settings(Settings(source=DataSource.DEMO))
    service.reset_connectors()
    result = service.refresh_suggestions()
    assert result is not None and result.added > 0
    first = {(item.account_id, item.key) for item in service.get_proposals().all()}
    assert service.refresh_suggestions() is None  # the day's check already ran
    again = service.refresh_suggestions(force=True)
    assert again is not None and again.expired == 0
    second = {(item.account_id, item.key) for item in service.get_proposals().all()}
    # A run adds only what the caps held back: nothing repeats, nothing disappears.
    assert first <= second
    assert len(second) == len(first) + again.added
