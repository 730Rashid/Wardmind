"""Automatic follow-ups (core/rules.py): the weekend rule, each rule's trigger and non-trigger,
dedupe (open and done), the open cap, the run cap, the at-risk start day from the score history,
the writer (demo data, a rule that is off and a locked vault add nothing), the hook after the
day's snapshot, and the "Added automatically" tag. Also the journal's new `rule` field and the
settings-aware parameters of awaiting_replies and today_view.

Dates are fixed (TODAY is a Wednesday) except where the page itself uses the clock.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, history, journal, rules, service, vault
from wardmind.core.connectors.combined import CombinedConnector
from wardmind.core.journal import (
    MAX_OPEN_FOLLOW_UPS,
    AccountJournal,
    Direction,
    FollowUp,
    FollowUpStatus,
    Journal,
    JournalData,
    Note,
    NoteSource,
    TouchKind,
)
from wardmind.core.models import (
    Account,
    Health,
    Milestones,
    Motion,
    ScoredAccount,
    Signal,
    SignalKind,
)
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.core.store import JOURNAL_FILE, LocalStore
from wardmind.web import app as web

TODAY = date(2026, 11, 11)  # a Wednesday
SATURDAY = date(2026, 11, 14)
SUNDAY = date(2026, 11, 15)
NOW = datetime(2026, 11, 11, 9, 0)
ACC = "acc-1"
TAG = "Added automatically"


def mine(**changes: object) -> Settings:
    return Settings(source=DataSource.MINE, **changes)


def scored(
    account_id: str = ACC,
    *,
    health: Health = Health.HEALTHY,
    renewal: date = date(2027, 3, 1),
    name: str | None = None,
) -> ScoredAccount:
    return ScoredAccount(
        account=Account(
            id=account_id,
            name=name or f"Account {account_id}",
            arr=100_000.0,
            tier="Growth",
            owner="Sam Rivera",
            renewal_date=renewal,
            milestones=Milestones(done=3, total=3, overdue=0),
        ),
        motion=Motion.RENEWAL,
        health=health,
        risk=50.0 if health is Health.AT_RISK else 20.0,
        priority=50.0 if health is Health.AT_RISK else 20.0,
        reasons=[],
    )


def follow_up(
    *,
    item_id: str = "f-00000001",
    rule: str | None = None,
    status: FollowUpStatus = FollowUpStatus.OPEN,
    due: date = TODAY,
) -> FollowUp:
    return FollowUp(
        id=item_id,
        text="Something",
        due=due,
        status=status,
        created_at=NOW,
        done_at=NOW if status is FollowUpStatus.DONE else None,
        rule=rule,
    )


def journal_with(*items: FollowUp) -> AccountJournal:
    return AccountJournal(follow_ups=list(items))


def champion(value: float, when: datetime = datetime(2026, 10, 30, 9, 0)) -> Signal:
    return Signal(
        account_id=ACC, kind=SignalKind.CHAMPION_LEFT, value=value, observed_at=when, source="x"
    )


def plan(
    book: list[ScoredAccount],
    settings: Settings,
    *,
    journals: dict[str, AccountJournal] | None = None,
    at_risk: dict[str, date] | None = None,
    signals: list[Signal] | None = None,
    today: date = TODAY,
) -> list[rules.PlannedFollowUp]:
    return rules.planned_follow_ups(
        book, journals or {}, settings, today, at_risk or {}, signals=signals or []
    )


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


@pytest.fixture
def journal_file(tmp_path: Path) -> Journal:
    return Journal(LocalStore(tmp_path / "data"))


@pytest.fixture
def history_store(tmp_path: Path) -> history.HistoryStore:
    return history.HistoryStore(LocalStore(tmp_path / "data"))


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _use_your_data(settings: Settings) -> None:
    save_settings(settings)
    service.reset_connectors()


# --- The weekend rule -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (date(2026, 11, 13), date(2026, 11, 13)),  # Friday stays
        (SATURDAY, date(2026, 11, 16)),  # Saturday moves to Monday
        (SUNDAY, date(2026, 11, 16)),  # Sunday moves to Monday
        (date(2026, 11, 16), date(2026, 11, 16)),  # Monday stays
    ],
)
def test_a_weekend_day_moves_to_monday_when_skipping_weekends(start, expected) -> None:
    assert rules.weekend_shift(start, skip_weekends=True) == expected


@pytest.mark.parametrize("start", [SATURDAY, SUNDAY])
def test_a_weekend_day_stays_put_when_weekends_are_not_skipped(start) -> None:
    assert rules.weekend_shift(start, skip_weekends=False) == start


def test_due_day_adds_the_days_and_then_applies_the_weekend_rule() -> None:
    # TODAY is a Wednesday: two days out is Friday (kept), three is Saturday (moved to Monday).
    assert rules.due_day(TODAY, 2, skip_weekends=True) == date(2026, 11, 13)
    assert rules.due_day(TODAY, 3, skip_weekends=True) == date(2026, 11, 16)
    assert rules.due_day(TODAY, 3, skip_weekends=False) == SATURDAY
    assert rules.due_day(TODAY, 0, skip_weekends=True) == TODAY


def test_a_renewal_follow_up_planned_on_a_saturday_is_due_monday_when_skipping_weekends() -> None:
    settings = mine(rule_renewal=True, rule_renewal_days=120, skip_weekends=True)
    book = [scored(renewal=SATURDAY + timedelta(days=30))]
    [item] = plan(book, settings, today=SATURDAY)
    assert item.due == date(2026, 11, 16)


def test_a_renewal_follow_up_planned_on_a_saturday_stays_saturday_when_not_skipping() -> None:
    settings = mine(rule_renewal=True, rule_renewal_days=120, skip_weekends=False)
    book = [scored(renewal=SATURDAY + timedelta(days=30))]
    [item] = plan(book, settings, today=SATURDAY)
    assert item.due == SATURDAY


# --- Nothing on, nothing planned --------------------------------------------------------------


def test_with_no_rule_on_nothing_is_planned_even_for_a_book_that_would_match() -> None:
    book = [scored(health=Health.AT_RISK, renewal=TODAY + timedelta(days=10))]
    assert plan(book, mine(), at_risk={ACC: TODAY}) == []


# --- At risk ----------------------------------------------------------------------------------


def test_an_at_risk_account_gets_a_check_in_due_in_the_chosen_days() -> None:
    settings = mine(rule_at_risk=True, rule_at_risk_days=5)
    book = [scored(health=Health.AT_RISK)]
    [item] = plan(book, settings, at_risk={ACC: date(2026, 11, 2)})
    assert item.text == "Check in: health dropped to At risk"
    assert item.due == TODAY + timedelta(days=5)
    assert item.rule == "at_risk:2026-11-02"


def test_zero_days_makes_the_check_in_due_today() -> None:
    settings = mine(rule_at_risk=True, rule_at_risk_days=0)
    [item] = plan([scored(health=Health.AT_RISK)], settings, at_risk={ACC: TODAY})
    assert item.due == TODAY


@pytest.mark.parametrize("health", [Health.WATCH, Health.HEALTHY])
def test_an_account_that_is_not_at_risk_gets_no_check_in(health: Health) -> None:
    settings = mine(rule_at_risk=True)
    assert plan([scored(health=health)], settings, at_risk={ACC: TODAY}) == []


def test_an_at_risk_account_with_no_known_run_start_counts_from_today() -> None:
    settings = mine(rule_at_risk=True)
    [item] = plan([scored(health=Health.AT_RISK)], settings, at_risk={})
    assert item.rule == f"at_risk:{TODAY.isoformat()}"


# --- Renewal ----------------------------------------------------------------------------------


def test_a_renewal_inside_the_window_gets_prepare_the_renewal_due_today() -> None:
    settings = mine(rule_renewal=True, rule_renewal_days=60)
    renewal = date(2027, 1, 14)
    [item] = plan([scored(renewal=renewal)], settings, today=renewal - timedelta(days=60))
    assert item.text == "Prepare the renewal (renews 14 Jan 2027)"
    assert item.due == renewal - timedelta(days=60)
    assert item.rule == "renewal:2027-01-14"


def test_a_renewal_today_is_inside_the_window() -> None:
    settings = mine(rule_renewal=True, rule_renewal_days=30)
    [item] = plan([scored(renewal=TODAY)], settings, today=TODAY)
    assert item.due == TODAY


@pytest.mark.parametrize(("days_out", "expected_count"), [(61, 0), (60, 1), (-1, 0)])
def test_a_renewal_outside_the_window_or_already_past_gets_nothing(
    days_out: int, expected_count: int
) -> None:
    settings = mine(rule_renewal=True, rule_renewal_days=60)
    book = [scored(renewal=TODAY + timedelta(days=days_out))]
    assert len(plan(book, settings)) == expected_count


# --- Champion left ----------------------------------------------------------------------------


def test_a_champion_who_left_gets_find_a_new_champion_due_in_three_days() -> None:
    settings = mine(rule_champion_left=True)
    [item] = plan([scored()], settings, signals=[champion(1.0)])
    assert item.text == "Find a new champion"
    assert item.due == TODAY + timedelta(days=3)
    assert item.rule == "champion_left:2026-10-30"


def test_a_champion_back_in_post_after_leaving_is_not_planned() -> None:
    settings = mine(rule_champion_left=True)
    earlier = champion(1.0, datetime(2026, 10, 1, 9, 0))
    later = champion(0.0, datetime(2026, 10, 20, 9, 0))
    assert plan([scored()], settings, signals=[earlier, later]) == []


def test_no_champion_reading_means_no_follow_up() -> None:
    assert plan([scored()], mine(rule_champion_left=True), signals=[]) == []


def test_a_new_departure_after_an_earlier_one_is_a_new_occasion() -> None:
    settings = mine(rule_champion_left=True)
    first = champion(1.0, datetime(2026, 10, 1, 9, 0))
    gone = champion(0.0, datetime(2026, 10, 5, 9, 0))
    again = champion(1.0, datetime(2026, 11, 3, 9, 0))
    [item] = plan([scored()], settings, signals=[first, gone, again])
    assert item.rule == "champion_left:2026-11-03"


# --- Dedupe -----------------------------------------------------------------------------------


def test_an_occasion_already_on_an_open_follow_up_is_not_added_again() -> None:
    settings = mine(rule_at_risk=True)
    journals = {ACC: journal_with(follow_up(rule="at_risk:2026-11-02"))}
    book = [scored(health=Health.AT_RISK)]
    assert plan(book, settings, journals=journals, at_risk={ACC: date(2026, 11, 2)}) == []


def test_an_occasion_already_on_a_done_follow_up_is_not_added_again() -> None:
    settings = mine(rule_at_risk=True)
    done = follow_up(rule="at_risk:2026-11-02", status=FollowUpStatus.DONE)
    book = [scored(health=Health.AT_RISK)]
    journals = {ACC: journal_with(done)}
    assert plan(book, settings, journals=journals, at_risk={ACC: date(2026, 11, 2)}) == []


def test_a_new_occasion_is_added_even_when_an_old_one_is_on_the_account() -> None:
    settings = mine(rule_at_risk=True)
    old = follow_up(rule="at_risk:2026-09-01", status=FollowUpStatus.DONE)
    book = [scored(health=Health.AT_RISK)]
    [item] = plan(
        book, settings, journals={ACC: journal_with(old)}, at_risk={ACC: date(2026, 11, 2)}
    )
    assert item.rule == "at_risk:2026-11-02"


def test_a_follow_up_the_csm_wrote_is_not_an_occasion() -> None:
    settings = mine(rule_at_risk=True)
    journals = {ACC: journal_with(follow_up(rule=None))}
    book = [scored(health=Health.AT_RISK)]
    assert len(plan(book, settings, journals=journals, at_risk={ACC: TODAY})) == 1


# --- Caps -------------------------------------------------------------------------------------


def test_an_account_at_the_open_cap_gets_nothing() -> None:
    settings = mine(rule_at_risk=True)
    full = [follow_up(item_id=f"f-{n:08x}") for n in range(MAX_OPEN_FOLLOW_UPS)]
    book = [scored(health=Health.AT_RISK)]
    assert plan(book, settings, journals={ACC: journal_with(*full)}, at_risk={ACC: TODAY}) == []


def test_the_room_left_is_shared_between_rules_in_rule_order() -> None:
    settings = mine(rule_at_risk=True, rule_champion_left=True)
    room_for_one = [follow_up(item_id=f"f-{n:08x}") for n in range(MAX_OPEN_FOLLOW_UPS - 1)]
    book = [scored(health=Health.AT_RISK)]
    planned = plan(
        book,
        settings,
        journals={ACC: journal_with(*room_for_one)},
        at_risk={ACC: TODAY},
        signals=[champion(1.0)],
    )
    assert [item.rule for item in planned] == [f"at_risk:{TODAY.isoformat()}"]


def test_one_run_plans_at_most_fifty_follow_ups() -> None:
    settings = mine(rule_at_risk=True)
    book = [scored(f"acc-{n:03d}", health=Health.AT_RISK) for n in range(60)]
    assert len(plan(book, settings)) == rules.MAX_PLANNED == 50


def test_the_planner_does_not_change_the_journals_it_is_given() -> None:
    settings = mine(rule_at_risk=True)
    journals = {ACC: journal_with(follow_up())}
    before = journals[ACC].model_dump_json()
    plan([scored(health=Health.AT_RISK)], settings, journals=journals, at_risk={ACC: TODAY})
    assert journals[ACC].model_dump_json() == before


# --- The at-risk start day, from the score history ---------------------------------------------


def _snapshot(day: date, health: Health | None) -> history.BookSnapshot:
    accounts: dict[str, history.SnapshotEntry] = {}
    if health is not None:
        accounts[ACC] = history.SnapshotEntry(
            name="Acme",
            rank=1,
            risk=50.0,
            priority=50.0,
            health=health,
            motion=Motion.RENEWAL,
            arr=100_000.0,
        )
    taken = datetime(day.year, day.month, day.day, 9, 0)
    return history.BookSnapshot(day=day, taken_at=taken, accounts=accounts)


def _at_risk_now() -> list[ScoredAccount]:
    return [scored(health=Health.AT_RISK)]


def test_the_at_risk_run_starts_at_the_first_snapshot_of_the_run(history_store) -> None:
    for days_back, health in [(3, Health.WATCH), (2, Health.AT_RISK), (1, Health.AT_RISK)]:
        history_store.save(_snapshot(TODAY - timedelta(days=days_back), health))
    found = rules.at_risk_since(_at_risk_now(), history_store, TODAY)
    assert found == {ACC: TODAY - timedelta(days=2)}


def test_a_snapshot_that_was_not_at_risk_ends_the_run(history_store) -> None:
    history_store.save(_snapshot(TODAY - timedelta(days=4), Health.AT_RISK))
    history_store.save(_snapshot(TODAY - timedelta(days=2), Health.WATCH))
    history_store.save(_snapshot(TODAY - timedelta(days=1), Health.AT_RISK))
    found = rules.at_risk_since(_at_risk_now(), history_store, TODAY)
    assert found == {ACC: TODAY - timedelta(days=1)}


def test_a_day_with_no_snapshot_does_not_end_the_run(history_store) -> None:
    history_store.save(_snapshot(TODAY - timedelta(days=9), Health.AT_RISK))
    history_store.save(_snapshot(TODAY - timedelta(days=1), Health.AT_RISK))
    found = rules.at_risk_since(_at_risk_now(), history_store, TODAY)
    assert found == {ACC: TODAY - timedelta(days=9)}


def test_an_account_missing_from_a_snapshot_ends_the_run(history_store) -> None:
    history_store.save(_snapshot(TODAY - timedelta(days=2), Health.AT_RISK))
    history_store.save(_snapshot(TODAY - timedelta(days=1), None))
    assert rules.at_risk_since(_at_risk_now(), history_store, TODAY) == {ACC: TODAY}


def test_an_unreadable_snapshot_counts_as_absent(history_store) -> None:
    history_store.save(_snapshot(TODAY - timedelta(days=2), Health.AT_RISK))
    bad = history_store._file(TODAY - timedelta(days=1))
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not json", encoding="utf-8")
    found = rules.at_risk_since(_at_risk_now(), history_store, TODAY)
    assert found == {ACC: TODAY - timedelta(days=2)}


def test_no_history_means_the_run_starts_today(history_store) -> None:
    assert rules.at_risk_since(_at_risk_now(), history_store, TODAY) == {ACC: TODAY}


def test_today_and_later_snapshots_are_not_looked_at(history_store) -> None:
    history_store.save(_snapshot(TODAY, Health.WATCH))
    assert rules.at_risk_since(_at_risk_now(), history_store, TODAY) == {ACC: TODAY}


def test_accounts_not_at_risk_now_are_not_in_the_result(history_store) -> None:
    history_store.save(_snapshot(TODAY - timedelta(days=1), Health.AT_RISK))
    assert rules.at_risk_since([scored(health=Health.WATCH)], history_store, TODAY) == {}


# --- The journal's rule field ------------------------------------------------------------------


def test_a_follow_up_without_a_rule_field_still_loads(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    store.root.mkdir(parents=True, exist_ok=True)
    old = {
        "by_account": {
            ACC: {
                "follow_ups": [
                    {
                        "id": "f-00000001",
                        "text": "Send the deck",
                        "due": "2026-11-12",
                        "status": "open",
                        "created_at": "2026-11-01T09:00:00",
                    }
                ]
            }
        }
    }
    (store.root / JOURNAL_FILE).write_text(json.dumps(old), encoding="utf-8")
    [item] = Journal(store).for_account(ACC).follow_ups
    assert item.text == "Send the deck"
    assert item.rule is None


def test_the_rule_round_trips_through_the_journal_file(journal_file: Journal) -> None:
    item = journal_file.add_follow_up(
        ACC, "Check in", TODAY, now=NOW, today=TODAY, rule="at_risk:2026-11-02"
    )
    assert item.rule == "at_risk:2026-11-02"
    assert [entry.rule for entry in journal_file.for_account(ACC).follow_ups] == [
        "at_risk:2026-11-02"
    ]
    saved = JournalData.model_validate_json(
        (journal_file._store.root / JOURNAL_FILE).read_text(encoding="utf-8")
    )
    assert saved.by_account[ACC].follow_ups[0].rule == "at_risk:2026-11-02"


def test_a_follow_up_the_csm_adds_has_no_rule(journal_file: Journal) -> None:
    item = journal_file.add_follow_up(ACC, "Call back", TODAY, now=NOW, today=TODAY)
    assert item.rule is None


def test_the_journal_refuses_a_second_follow_up_with_the_same_rule(journal_file: Journal) -> None:
    journal_file.add_follow_up(ACC, "Check in", TODAY, now=NOW, today=TODAY, rule="renewal:x")
    with pytest.raises(ValueError, match="already added"):
        journal_file.add_follow_up(ACC, "Check in", TODAY, now=NOW, today=TODAY, rule="renewal:x")


def test_the_journal_refuses_a_repeat_even_after_the_first_is_done(journal_file: Journal) -> None:
    first = journal_file.add_follow_up(
        ACC, "Check in", TODAY, now=NOW, today=TODAY, rule="renewal:x"
    )
    journal_file.complete_follow_up(ACC, first.id, now=NOW)
    with pytest.raises(ValueError, match="already added"):
        journal_file.add_follow_up(ACC, "Check in", TODAY, now=NOW, today=TODAY, rule="renewal:x")


def test_a_removed_automatic_follow_up_is_never_added_back(journal_file: Journal) -> None:
    settings = mine(rule_at_risk=True, rule_at_risk_days=2)
    book = [scored(health=Health.AT_RISK)]
    assert rules.apply_rules(journal_file, book, settings, TODAY, {ACC: TODAY}, now=NOW) == 1
    [item] = journal_file.for_account(ACC).follow_ups
    assert journal_file.remove_follow_up(ACC, item.id)
    assert journal_file.for_account(ACC).dismissed_rules == [item.rule]
    assert rules.apply_rules(journal_file, book, settings, TODAY, {ACC: TODAY}, now=NOW) == 0
    assert journal_file.for_account(ACC).follow_ups == []


def test_removing_your_own_follow_up_remembers_nothing(journal_file: Journal) -> None:
    item = journal_file.add_follow_up(ACC, "Call back", TODAY, now=NOW, today=TODAY)
    journal_file.remove_follow_up(ACC, item.id)
    assert journal_file.for_account(ACC).dismissed_rules == []


# --- The writer -------------------------------------------------------------------------------


def test_apply_rules_writes_the_planned_follow_ups_and_counts_them(journal_file: Journal) -> None:
    settings = mine(rule_at_risk=True, rule_at_risk_days=2)
    book = [scored(health=Health.AT_RISK)]
    added = rules.apply_rules(journal_file, book, settings, TODAY, {ACC: TODAY}, now=NOW)
    assert added == 1
    [item] = journal_file.for_account(ACC).follow_ups
    assert (item.text, item.due, item.rule) == (
        "Check in: health dropped to At risk",
        TODAY + timedelta(days=2),
        f"at_risk:{TODAY.isoformat()}",
    )


def test_a_second_run_adds_nothing(journal_file: Journal) -> None:
    settings = mine(rule_at_risk=True)
    book = [scored(health=Health.AT_RISK)]
    rules.apply_rules(journal_file, book, settings, TODAY, {ACC: TODAY}, now=NOW)
    assert rules.apply_rules(journal_file, book, settings, TODAY, {ACC: TODAY}, now=NOW) == 0
    assert len(journal_file.for_account(ACC).follow_ups) == 1


def test_demo_data_adds_nothing_and_writes_nothing(journal_file: Journal) -> None:
    settings = Settings(source=DataSource.DEMO, rule_at_risk=True, rule_champion_left=True)
    book = [scored(health=Health.AT_RISK)]
    assert rules.apply_rules(journal_file, book, settings, TODAY, {ACC: TODAY}, now=NOW) == 0
    assert not (journal_file._store.root / JOURNAL_FILE).exists()


def test_with_no_rule_on_nothing_is_written(journal_file: Journal) -> None:
    book = [scored(health=Health.AT_RISK)]
    assert rules.apply_rules(journal_file, book, mine(), TODAY, {ACC: TODAY}, now=NOW) == 0
    assert not (journal_file._store.root / JOURNAL_FILE).exists()


def test_a_locked_vault_adds_nothing_and_raises_nothing(
    journal_file: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(*args: object, **kwargs: object) -> str:
        raise vault.Locked("Wardmind is locked.")

    monkeypatch.setattr(vault, "read_text", locked)
    book = [scored(health=Health.AT_RISK)]
    settings = mine(rule_at_risk=True)
    assert rules.apply_rules(journal_file, book, settings, TODAY, {ACC: TODAY}, now=NOW) == 0


def test_a_vault_that_locks_during_the_writes_keeps_what_was_added(
    journal_file: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_add = Journal.add_follow_up
    written: list[str] = []

    def add_then_lock(self: Journal, account_id: str, text: str, due: date, **kwargs: object):
        if written:
            raise vault.Locked("Wardmind is locked.")
        written.append(account_id)
        return real_add(self, account_id, text, due, **kwargs)

    monkeypatch.setattr(Journal, "add_follow_up", add_then_lock)
    book = [scored("acc-1", health=Health.AT_RISK), scored("acc-2", health=Health.AT_RISK)]
    added = rules.apply_rules(journal_file, book, mine(rule_at_risk=True), TODAY, {}, now=NOW)
    assert added == 1
    assert len(journal_file.for_account("acc-1").follow_ups) == 1
    assert journal_file.for_account("acc-2").follow_ups == []


# --- The hook: the day's snapshot runs the rules ----------------------------------------------


def _add_account(name: str) -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": name,
            "arr": "$100,000",
            "renewal_date": (TODAY + timedelta(days=300)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _make_at_risk(account_id: str) -> None:
    """Readings that together put the account over the At risk line: the champion left (25),
    three Sev 1 tickets (20) and 60 days of overdue billing (20), 65 in all."""
    store = service.get_store()
    connector = service.get_connector()
    assert isinstance(connector, CombinedConnector)
    editing.add_reading(store, connector, account_id, SignalKind.CHAMPION_LEFT, "yes", now=NOW)
    editing.add_reading(store, connector, account_id, SignalKind.OPEN_TICKETS_SEV1, "3", now=NOW)
    editing.add_reading(
        store, connector, account_id, SignalKind.BILLING_OVERDUE_DAYS, "60", now=NOW
    )
    service.reset_connectors()


def test_remember_book_adds_the_check_in_once_the_day_is_recorded(store: LocalStore) -> None:
    _use_your_data(mine(rule_at_risk=True, rule_at_risk_days=2))
    account_id = _add_account("Acme Robotics")
    _make_at_risk(account_id)
    assert service.remember_book(now=NOW, today=TODAY) is True
    [item] = service.account_journal(account_id).follow_ups
    assert item.rule == f"at_risk:{TODAY.isoformat()}"
    assert item.due == TODAY + timedelta(days=2)
    # The same hour: no new snapshot, so the rules don't run again.
    assert service.remember_book(now=NOW, today=TODAY) is False
    assert len(service.account_journal(account_id).follow_ups) == 1


def test_remember_book_adds_nothing_while_the_rules_are_off(store: LocalStore) -> None:
    _use_your_data(mine())
    account_id = _add_account("Acme Robotics")
    _make_at_risk(account_id)
    assert service.remember_book(now=NOW, today=TODAY) is True
    assert service.account_journal(account_id).follow_ups == []


def test_remember_book_still_records_the_snapshot_when_the_rules_fail(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_your_data(mine(rule_at_risk=True))
    _add_account("Acme Robotics")

    def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(rules, "apply_rules", broken)
    assert service.remember_book(now=NOW, today=TODAY) is True
    assert service.history_store().load(TODAY) is not None


# --- Settings-aware parameters keep their defaults --------------------------------------------


def _email(entry_day: date, direction: Direction) -> Note:
    return Note(
        id="n-00000001",
        kind=TouchKind.EMAIL,
        text="Hello",
        happened_on=entry_day,
        created_at=NOW,
        direction=direction,
        source=NoteSource.MAILBOX,
        message_id="m-1",
        thread="renewal plan",
        subject="Renewal plan",
    )


def test_awaiting_replies_uses_the_settings_waits() -> None:
    entry = AccountJournal(notes=[_email(TODAY - timedelta(days=3), Direction.SENT)])
    assert journal.awaiting_replies(entry, TODAY) == []  # the default reply wait is 5 days
    [status] = journal.awaiting_replies(entry, TODAY, reply_wait_days=3)
    assert status.waiting_on_them is True


def test_today_view_uses_the_quiet_days_setting() -> None:
    book = [scored(health=Health.AT_RISK, name="Acme")]
    contacted = AccountJournal(
        notes=[
            Note(
                id="n-00000002",
                kind=TouchKind.CALL,
                text="Spoke",
                happened_on=TODAY - timedelta(days=5),
                created_at=NOW,
            )
        ]
    )
    journals = {ACC: contacted}
    assert journal.today_view(book, journals, TODAY).attention == []  # 5 days is within 14
    assert len(journal.today_view(book, journals, TODAY, quiet_days=3).attention) == 1


def test_the_settings_defaults_are_the_journal_constants() -> None:
    fresh = load_settings()
    assert fresh.follow_up_days == journal.FOLLOW_UP_DEFAULT_DAYS
    assert fresh.quiet_days == journal.QUIET_DAYS
    assert fresh.reply_wait_days == journal.REPLY_WAIT_DAYS
    assert fresh.answer_wait_days == journal.ANSWER_WAIT_DAYS


# --- The "Added automatically" tag on the pages ------------------------------------------------


def test_the_account_page_and_today_tag_only_the_rule_made_follow_up(
    client: TestClient,
) -> None:
    today = date.today()
    _use_your_data(mine())
    account_id = _add_account("Acme Robotics")
    journal_file = service.get_journal()
    journal_file.add_follow_up(
        account_id,
        "Check in: health dropped to At risk",
        today,
        now=NOW,
        today=today,
        rule="at_risk:2026-11-02",
    )
    journal_file.add_follow_up(account_id, "Call the sponsor", today, now=NOW, today=today)
    assert client.get(f"/accounts/{account_id}").text.count(TAG) == 1
    today_text = client.get("/today").text
    assert today_text.count(TAG) == 1
    assert "Check in: health dropped to At risk" in today_text
