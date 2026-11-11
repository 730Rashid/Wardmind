"""The honest forecast's stored calls (core/journal.py): make_call and clear_call, how calls move
with an account when accounts are linked, that forgetting an account forgets its calls, that a
journal holding only calls is kept, and that a journal file written before calls existed loads.

The store is in a temp folder (see the store fixture). No network.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from wardmind.core import journal as journal_module
from wardmind.core.journal import AccountJournal, CallKind, ForecastCall, Journal, JournalData
from wardmind.core.models import Account, Health, Milestones, Motion, ScoredAccount
from wardmind.core.store import DEMO_JOURNAL_FILE, JOURNAL_FILE, LocalStore

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 9, 0)
LATER = datetime(2026, 11, 9, 10, 0)
ACC = "acc-1"
RENEWAL = date(2027, 3, 1)  # the default renewal of scored()
NEXT_RENEWAL = date(2027, 6, 1)


def refused(message: str):
    """A ValueError whose text is exactly this plain message."""
    return pytest.raises(ValueError, match=re.escape(message))


# Fixtures and builders -----------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


@pytest.fixture
def journal(store: LocalStore) -> Journal:
    return Journal(store)


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every path the journal writes to, in order (the write still happens)."""
    seen: list[Path] = []
    real = journal_module.vault.write_text

    def spy(path: Path, text: str) -> None:
        seen.append(path)
        real(path, text)

    monkeypatch.setattr(journal_module.vault, "write_text", spy)
    return seen


def scored(
    account_id: str = ACC,
    *,
    renewal: date = RENEWAL,
    health: Health = Health.HEALTHY,
    arr: float = 100_000.0,
    risk: float = 20.0,
) -> ScoredAccount:
    return ScoredAccount(
        account=Account(
            id=account_id,
            name=f"Account {account_id}",
            arr=arr,
            tier="Growth",
            owner="Sam Rivera",
            renewal_date=renewal,
            milestones=Milestones(done=3, total=3, overdue=0),
        ),
        motion=Motion.RENEWAL,
        health=health,
        risk=risk,
        priority=risk,
        reasons=[],
    )


def call_at(
    renewal: date, kind: CallKind = CallKind.COMMIT, *, call_id: str = "c-00000001"
) -> ForecastCall:
    return ForecastCall(
        id=call_id,
        kind=kind,
        renewal_date=renewal,
        made_on=TODAY,
        health=Health.HEALTHY,
        risk=20.0,
        arr=100_000.0,
        recorded_at=NOW,
    )


def save_raw(store: LocalStore, data: JournalData) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_text(data.model_dump_json(), encoding="utf-8")


def saved_file(store: LocalStore) -> dict:
    return json.loads((store.root / JOURNAL_FILE).read_text(encoding="utf-8"))


def pairs(calls: list[ForecastCall]) -> list[tuple[date, CallKind]]:
    return [(entry.renewal_date, entry.kind) for entry in calls]


# make_call -----------------------------------------------------------------------------------


def test_a_call_keeps_its_kind_renewal_day_and_what_wardmind_thought(journal):
    made = journal.make_call(
        ACC,
        CallKind.LIKELY,
        scored(health=Health.WATCH, arr=80_000.0, risk=35.0),
        now=NOW,
        today=TODAY,
    )

    assert re.fullmatch(r"c-[0-9a-f]{8}", made.id)
    assert made.kind is CallKind.LIKELY
    assert made.renewal_date == RENEWAL
    assert made.made_on == TODAY
    assert (made.health, made.risk, made.arr) == (Health.WATCH, 35.0, 80_000.0)
    assert made.recorded_at == NOW
    assert journal.for_account(ACC).calls == [made]


def test_the_recorded_time_is_stored_as_naive_utc(journal):
    local = datetime(2026, 11, 9, 10, 0, tzinfo=timezone(timedelta(hours=1)))

    made = journal.make_call(ACC, CallKind.COMMIT, scored(), now=local, today=TODAY)

    assert made.recorded_at == NOW
    assert made.recorded_at.tzinfo is None


def test_a_call_for_another_account_is_refused_and_nothing_is_written(journal, writes):
    with refused("That call is for a different account."):
        journal.make_call("acc-2", CallKind.COMMIT, scored(ACC), now=NOW, today=TODAY)

    assert writes == []
    assert journal.all() == {}


def test_a_new_call_on_the_same_renewal_replaces_the_old_one(journal):
    journal.make_call(ACC, CallKind.COMMIT, scored(), now=NOW, today=TODAY)

    replaced = journal.make_call(ACC, CallKind.AT_RISK, scored(), now=LATER, today=TODAY)

    assert journal.for_account(ACC).calls == [replaced]
    assert replaced.kind is CallKind.AT_RISK


def test_calls_on_other_renewal_dates_are_kept_in_the_order_made(journal):
    journal.make_call(ACC, CallKind.COMMIT, scored(renewal=RENEWAL), now=NOW, today=TODAY)
    journal.make_call(ACC, CallKind.LIKELY, scored(renewal=NEXT_RENEWAL), now=NOW, today=TODAY)
    assert pairs(journal.for_account(ACC).calls) == [
        (RENEWAL, CallKind.COMMIT),
        (NEXT_RENEWAL, CallKind.LIKELY),
    ]

    # A new call for the first renewal replaces it, and it is the latest one made.
    journal.make_call(ACC, CallKind.AT_RISK, scored(renewal=RENEWAL), now=LATER, today=TODAY)

    assert pairs(journal.for_account(ACC).calls) == [
        (NEXT_RENEWAL, CallKind.LIKELY),
        (RENEWAL, CallKind.AT_RISK),
    ]


def test_a_call_is_written_only_to_the_journal_file_and_survives_a_reload(store, journal, writes):
    made = journal.make_call(ACC, CallKind.COMMIT, scored(), now=NOW, today=TODAY)

    assert writes == [store.root / JOURNAL_FILE]
    assert Journal(store).for_account(ACC).calls == [made]


def test_demo_and_your_data_keep_separate_calls(store):
    Journal(store, demo=True).make_call(ACC, CallKind.COMMIT, scored(), now=NOW, today=TODAY)

    assert Journal(store).all() == {}
    assert (store.root / DEMO_JOURNAL_FILE).exists()
    assert not (store.root / JOURNAL_FILE).exists()


# clear_call ----------------------------------------------------------------------------------


def test_clear_call_removes_that_renewals_call_and_says_true_once_then_false(journal, writes):
    journal.make_call(ACC, CallKind.COMMIT, scored(), now=NOW, today=TODAY)
    writes.clear()

    assert journal.clear_call(ACC, RENEWAL) is True
    assert journal.for_account(ACC).calls == []
    assert journal.clear_call(ACC, RENEWAL) is False
    assert len(writes) == 1


def test_clear_call_for_another_renewal_or_account_says_false_and_writes_nothing(journal, writes):
    journal.make_call(ACC, CallKind.COMMIT, scored(renewal=RENEWAL), now=NOW, today=TODAY)
    writes.clear()

    assert journal.clear_call(ACC, NEXT_RENEWAL) is False
    assert journal.clear_call("acc-none", RENEWAL) is False
    assert writes == []
    assert pairs(journal.for_account(ACC).calls) == [(RENEWAL, CallKind.COMMIT)]


def test_clear_call_keeps_the_calls_on_other_renewal_dates(journal):
    journal.make_call(ACC, CallKind.COMMIT, scored(renewal=RENEWAL), now=NOW, today=TODAY)
    journal.make_call(ACC, CallKind.LIKELY, scored(renewal=NEXT_RENEWAL), now=NOW, today=TODAY)

    assert journal.clear_call(ACC, RENEWAL) is True

    assert pairs(journal.for_account(ACC).calls) == [(NEXT_RENEWAL, CallKind.LIKELY)]


def test_clearing_the_only_call_leaves_no_journal_behind(store, journal):
    journal.make_call(ACC, CallKind.COMMIT, scored(), now=NOW, today=TODAY)

    assert journal.clear_call(ACC, RENEWAL) is True

    assert saved_file(store) == {"by_account": {}}


# Moving accounts with calls -------------------------------------------------------------------


def test_moving_an_account_moves_its_calls_to_the_target_with_their_ids(journal):
    made = journal.make_call("acc-a", CallKind.COMMIT, scored("acc-a"), now=NOW, today=TODAY)

    journal.move_account("acc-a", "acc-b")

    assert journal.for_account("acc-b").calls == [made]
    assert "acc-a" not in journal.all()


def test_a_moved_call_whose_renewal_the_target_has_is_dropped_and_the_targets_call_wins(journal):
    target_call = journal.make_call(
        "acc-b", CallKind.AT_RISK, scored("acc-b"), now=NOW, today=TODAY
    )
    journal.make_call("acc-a", CallKind.COMMIT, scored("acc-a"), now=NOW, today=TODAY)

    journal.move_account("acc-a", "acc-b")

    assert journal.for_account("acc-b").calls == [target_call]


def test_moved_calls_on_renewals_the_target_lacks_are_kept_beside_the_targets_own(journal):
    target_call = journal.make_call(
        "acc-b", CallKind.AT_RISK, scored("acc-b", renewal=RENEWAL), now=NOW, today=TODAY
    )
    moved = journal.make_call(
        "acc-a", CallKind.COMMIT, scored("acc-a", renewal=NEXT_RENEWAL), now=NOW, today=TODAY
    )
    journal.make_call(
        "acc-a", CallKind.LIKELY, scored("acc-a", renewal=RENEWAL), now=NOW, today=TODAY
    )

    journal.move_account("acc-a", "acc-b")

    assert journal.for_account("acc-b").calls == [target_call, moved]


def test_moved_calls_whose_ids_clash_with_the_targets_get_new_call_ids(store, journal):
    save_raw(
        store,
        JournalData(
            by_account={
                "acc-a": AccountJournal(calls=[call_at(date(2026, 12, 1), call_id="c-00000001")]),
                "acc-b": AccountJournal(calls=[call_at(date(2027, 1, 5), call_id="c-00000001")]),
            }
        ),
    )

    journal.move_account("acc-a", "acc-b")

    target = journal.for_account("acc-b")
    assert [entry.renewal_date for entry in target.calls] == [date(2027, 1, 5), date(2026, 12, 1)]
    assert target.calls[0].id == "c-00000001"
    assert re.fullmatch(r"c-[0-9a-f]{8}", target.calls[1].id)
    assert target.calls[1].id != "c-00000001"


def test_moving_an_account_onto_a_target_with_no_calls_keeps_the_ids(journal):
    made = journal.make_call("acc-a", CallKind.LIKELY, scored("acc-a"), now=NOW, today=TODAY)

    journal.move_account("acc-a", "acc-b")

    assert journal.for_account("acc-b").calls[0].id == made.id


# Forgetting accounts --------------------------------------------------------------------------


def test_forget_removes_an_accounts_calls_with_the_rest_of_its_journal(store, journal):
    journal.make_call(ACC, CallKind.COMMIT, scored(ACC), now=NOW, today=TODAY)
    journal.make_call("acc-2", CallKind.AT_RISK, scored("acc-2"), now=NOW, today=TODAY)

    assert journal.forget(lambda account_id: account_id == ACC) == 1

    assert journal.for_account(ACC).calls == []
    assert set(journal.all()) == {"acc-2"}
    assert list(saved_file(store)["by_account"]) == ["acc-2"]


# The journal with calls ----------------------------------------------------------------------


def test_a_journal_holding_only_calls_is_not_empty_and_is_kept_in_the_file(store, journal):
    assert AccountJournal().empty
    assert not AccountJournal(calls=[call_at(RENEWAL)]).empty

    journal.make_call(ACC, CallKind.COMMIT, scored(), now=NOW, today=TODAY)

    assert list(saved_file(store)["by_account"]) == [ACC]


def test_a_journal_file_from_before_calls_existed_loads_with_no_calls(store, journal, writes):
    store.root.mkdir(parents=True, exist_ok=True)
    old_file = {
        "by_account": {
            ACC: {
                "notes": [
                    {
                        "id": "n-00000001",
                        "kind": "call",
                        "text": "Old call",
                        "happened_on": "2026-11-08",
                        "created_at": "2026-11-08T09:00:00",
                        "edited_at": None,
                    }
                ],
                "follow_ups": [],
                "snooze": None,
                "outcomes": [
                    {
                        "id": "o-00000001",
                        "kind": "renewed",
                        "renewal_date": "2026-11-01",
                        "decided_on": "2026-11-01",
                        "arr_before": 100000.0,
                        "risk": 20.0,
                        "priority": 20.0,
                        "health": "healthy",
                        "motion": "renewal",
                        "recorded_at": "2026-11-01T09:00:00",
                    }
                ],
                "team": [],
            }
        }
    }
    (store.root / JOURNAL_FILE).write_text(json.dumps(old_file), encoding="utf-8")

    loaded = journal.for_account(ACC)

    assert loaded.calls == []
    assert [note.text for note in loaded.notes] == ["Old call"]
    assert len(loaded.outcomes) == 1
    assert journal.clear_call(ACC, RENEWAL) is False
    assert writes == []

    made = journal.make_call(ACC, CallKind.COMMIT, scored(), now=NOW, today=TODAY)

    reloaded = Journal(store).for_account(ACC)
    assert reloaded.calls == [made]
    assert [note.text for note in reloaded.notes] == ["Old call"]
