"""Score history (core/history.py): the daily snapshot of the book, the demo's rescored history,
"since last week" (changes_since), the risk trend, and the history folder: one file per day,
written at most hourly, pruned after KEEP_DAYS, and forgotten account by account.

Snapshots are written in temp folders (see LocalStore's root). No network.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

import wardmind.core.history as history_module
from wardmind.core.config import ScoringConfig, write_atomic
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.history import (
    KEEP_DAYS,
    MAX_CHANGES,
    REWRITE_AFTER,
    BookSnapshot,
    Change,
    ChangeKind,
    HistoryStore,
    SnapshotEntry,
    TrendPoint,
    WeekChanges,
    changes_since,
    rescored_snapshot,
    snapshot_of,
    trend_points,
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
from wardmind.core.scoring import score_book
from wardmind.core.store import HISTORY_DIR, LocalStore

CONFIG = ScoringConfig()
TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 9, 0)
LAST_WEEK = TODAY - timedelta(days=7)


def account(
    account_id: str,
    name: str | None = None,
    *,
    arr: float = 50_000.0,
    renewal: date | None = None,
) -> Account:
    return Account(
        id=account_id,
        name=name or f"Account {account_id}",
        arr=arr,
        tier="Growth",
        owner="Owner",
        renewal_date=renewal or TODAY + timedelta(days=200),
        milestones=Milestones(done=0, total=0),
    )


def reading(account_id: str, kind: SignalKind, value: float, observed_at: datetime = NOW) -> Signal:
    return Signal(account_id=account_id, kind=kind, value=value, observed_at=observed_at)


def scored(
    account_id: str,
    risk: float,
    health: Health,
    *,
    name: str | None = None,
    priority: float | None = None,
    motion: Motion = Motion.CHURN,
) -> ScoredAccount:
    return ScoredAccount(
        account=account(account_id, name),
        motion=motion,
        health=health,
        risk=risk,
        priority=risk if priority is None else priority,
        reasons=[],
    )


def entry(
    name: str,
    rank: int,
    risk: float,
    health: Health,
    *,
    priority: float | None = None,
    signals: dict[SignalKind, float] | None = None,
) -> SnapshotEntry:
    return SnapshotEntry(
        name=name,
        rank=rank,
        risk=risk,
        priority=risk if priority is None else priority,
        health=health,
        motion=Motion.CHURN,
        arr=50_000.0,
        signals=signals or {},
    )


def earlier(*pairs: tuple[str, SnapshotEntry], day: date = LAST_WEEK) -> BookSnapshot:
    return BookSnapshot(day=day, taken_at=datetime.combine(day, time(9)), accounts=dict(pairs))


def steady(count: int) -> tuple[list[ScoredAccount], list[tuple[str, SnapshotEntry]]]:
    """`count` accounts that did not move (same rank, risk and health in both snapshots). They
    put the account under test at the rank a test wants, without making a change of their own."""
    book: list[ScoredAccount] = []
    before: list[tuple[str, SnapshotEntry]] = []
    for rank in range(1, count + 1):
        account_id = f"steady-{rank}"
        book.append(scored(account_id, 5.0, Health.HEALTHY, name=f"Steady {rank}"))
        before.append((account_id, entry(f"Steady {rank}", rank, 5.0, Health.HEALTHY)))
    return book, before


def only(changes: WeekChanges) -> Change:
    assert len(changes.changes) == 1, [c.sentence for c in changes.changes]
    return changes.changes[0]


def dated(day: date, account_id: str, risk: float, health: Health, rank: int) -> BookSnapshot:
    return BookSnapshot(
        day=day,
        taken_at=datetime.combine(day, time(9)),
        accounts={account_id: entry("Acme", rank, risk, health)},
    )


def snap(
    day: date,
    *,
    risk: float = 30.0,
    taken_at: datetime | None = None,
    accounts: tuple[str, ...] = ("a",),
) -> BookSnapshot:
    return BookSnapshot(
        day=day,
        taken_at=taken_at or datetime.combine(day, time(9)),
        accounts={
            account_id: entry(f"Name {account_id}", rank, risk, Health.WATCH)
            for rank, account_id in enumerate(accounts, start=1)
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


@pytest.fixture
def history_store(store: LocalStore) -> HistoryStore:
    return HistoryStore(store)


def path_of(store: LocalStore, day: date) -> Path:
    return store.root / HISTORY_DIR / f"{day.isoformat()}.json"


def put(store: LocalStore, snapshot: BookSnapshot) -> None:
    """Write a snapshot's file directly, so a test isn't about save's rules for skipping writes."""
    write_atomic(path_of(store, snapshot.day), snapshot.model_dump_json() + "\n")


def write_text(store: LocalStore, day: date, text: str) -> None:
    write_atomic(path_of(store, day), text)


# snapshot_of -----------------------------------------------------------------------------------


def test_snapshot_ranks_accounts_from_one_in_book_order():
    accounts = [account("a", "Acme"), account("b", "Bolt"), account("c", "Crest")]
    readings = [reading("c", SignalKind.CHAMPION_LEFT, 1.0)]
    book = score_book(accounts, readings, CONFIG, as_of=TODAY)

    result = snapshot_of(book, readings, TODAY, NOW)

    assert book[0].account.id == "c"
    assert list(result.accounts) == [item.account.id for item in book]
    assert [result.accounts[item.account.id].rank for item in book] == [1, 2, 3]


def test_each_entry_copies_the_scored_account():
    book = [scored("a", 38.0, Health.WATCH, name="Acme", priority=30.5, motion=Motion.RENEWAL)]

    copied = snapshot_of(book, [], TODAY, NOW).accounts["a"]

    assert copied.name == "Acme"
    assert (copied.rank, copied.risk, copied.priority) == (1, 38.0, 30.5)
    assert (copied.health, copied.motion, copied.arr) == (
        Health.WATCH,
        Motion.RENEWAL,
        50_000.0,
    )
    assert copied.signals == {}


def test_the_snapshot_keeps_the_latest_value_of_each_kind_and_a_tie_takes_the_larger():
    usage = SignalKind.USAGE_TREND_30D
    tickets = SignalKind.OPEN_TICKETS_SEV1
    readings = [
        reading("a", usage, -10.0, NOW - timedelta(days=20)),
        reading("a", usage, -40.0, NOW - timedelta(days=2)),
        reading("a", SignalKind.NPS_LATEST, 9.0, NOW - timedelta(days=30)),
        reading("a", tickets, 2.0, NOW),
        reading("a", tickets, 5.0, NOW),  # the same moment: the larger value is the latest
    ]
    book = score_book([account("a")], readings, CONFIG, as_of=TODAY)

    signals = snapshot_of(book, readings, TODAY, NOW).accounts["a"].signals

    assert signals == {usage: -40.0, SignalKind.NPS_LATEST: 9.0, tickets: 5.0}


def test_readings_for_accounts_outside_the_book_are_left_out():
    readings = [reading("ghost", SignalKind.CHAMPION_LEFT, 1.0)]

    result = snapshot_of([scored("a", 0.0, Health.HEALTHY)], readings, TODAY, NOW)

    assert list(result.accounts) == ["a"]
    assert result.accounts["a"].signals == {}


def test_snapshot_records_the_day_and_when_it_was_taken():
    result = snapshot_of([], [], date(2026, 11, 6), datetime(2026, 11, 6, 17, 30))

    assert result.day == date(2026, 11, 6)
    assert result.taken_at == datetime(2026, 11, 6, 17, 30)
    assert result.accounts == {}


# rescored_snapshot -----------------------------------------------------------------------------


def test_readings_after_the_day_are_left_out():
    day = TODAY - timedelta(days=10)
    later = datetime.combine(day + timedelta(days=3), time(9))
    readings = [reading("a", SignalKind.CHAMPION_LEFT, 1.0, later)]

    before = rescored_snapshot([account("a")], readings, CONFIG, day, NOW)
    after = rescored_snapshot([account("a")], readings, CONFIG, day + timedelta(days=3), NOW)

    assert (before.accounts["a"].risk, before.accounts["a"].signals) == (0.0, {})
    assert after.accounts["a"].risk == 25.0


def test_a_reading_late_on_the_day_counts_but_the_next_midnight_does_not():
    day = TODAY - timedelta(days=10)
    accounts = [account("a")]
    late = [reading("a", SignalKind.CHAMPION_LEFT, 1.0, datetime.combine(day, time(23, 59, 59)))]
    midnight = [
        reading(
            "a", SignalKind.CHAMPION_LEFT, 1.0, datetime.combine(day + timedelta(days=1), time(0))
        )
    ]

    assert rescored_snapshot(accounts, late, CONFIG, day, NOW).accounts["a"].risk == 25.0
    assert rescored_snapshot(accounts, midnight, CONFIG, day, NOW).accounts["a"].risk == 0.0


def test_the_same_book_scores_differently_on_an_earlier_day():
    accounts = [account("a")]
    readings = [
        reading(
            "a",
            SignalKind.CHAMPION_LEFT,
            0.0,
            datetime.combine(TODAY - timedelta(days=10), time(9)),
        ),
        reading(
            "a", SignalKind.CHAMPION_LEFT, 1.0, datetime.combine(TODAY - timedelta(days=2), time(9))
        ),
    ]

    then = rescored_snapshot(accounts, readings, CONFIG, TODAY - timedelta(days=5), NOW)
    now = rescored_snapshot(accounts, readings, CONFIG, TODAY, NOW)

    assert then.day == TODAY - timedelta(days=5)
    assert (then.accounts["a"].risk, now.accounts["a"].risk) == (0.0, 25.0)


def test_motion_is_worked_out_as_of_the_day():
    renewal = TODAY + timedelta(days=100)
    accounts = [account("a", renewal=renewal)]

    # 130 days before renewal is outside the 120-day window; 100 days before is inside it.
    then = rescored_snapshot(accounts, [], CONFIG, TODAY - timedelta(days=30), NOW)
    now = rescored_snapshot(accounts, [], CONFIG, TODAY, NOW)

    assert then.accounts["a"].motion is Motion.CHURN
    assert now.accounts["a"].motion is Motion.RENEWAL


def test_every_account_is_in_the_snapshot_even_without_readings_by_the_day():
    accounts = [account("a"), account("b", arr=10_000.0)]
    later = [reading("a", SignalKind.CHAMPION_LEFT, 1.0, datetime.combine(TODAY, time(9)))]

    rescored = rescored_snapshot(accounts, later, CONFIG, TODAY - timedelta(days=5), NOW)

    assert set(rescored.accounts) == {"a", "b"}
    assert {item.risk for item in rescored.accounts.values()} == {0.0}
    assert sorted(item.rank for item in rescored.accounts.values()) == [1, 2]


def test_with_every_reading_by_the_day_the_rescore_is_the_live_snapshot():
    accounts = [account("a"), account("b")]
    readings = [
        reading("a", SignalKind.CHAMPION_LEFT, 1.0, datetime.combine(TODAY, time(8))),
        reading(
            "b", SignalKind.NPS_LATEST, 2.0, datetime.combine(TODAY - timedelta(days=1), time(8))
        ),
    ]
    live = snapshot_of(score_book(accounts, readings, CONFIG, as_of=TODAY), readings, TODAY, NOW)

    assert rescored_snapshot(accounts, readings, CONFIG, TODAY, NOW) == live


def test_the_demo_book_scored_21_days_ago_differs_from_today_and_keeps_every_account():
    connector = DemoConnector(today=TODAY)
    accounts, readings = connector.list_accounts(), connector.list_signals()
    today = snapshot_of(score_book(accounts, readings, CONFIG, as_of=TODAY), readings, TODAY, NOW)

    past = rescored_snapshot(accounts, readings, CONFIG, TODAY - timedelta(days=21), NOW)

    ids = {item.id for item in accounts}
    assert set(past.accounts) == ids == set(today.accounts)
    assert any(past.accounts[i].risk != today.accounts[i].risk for i in ids)


# changes_since: health moves, new and gone -----------------------------------------------------


@pytest.mark.parametrize(
    ("before_health", "before_risk", "after_health", "after_risk", "sentence", "kind"),
    [
        (
            Health.WATCH,
            38.0,
            Health.AT_RISK,
            52.0,
            "Moved from Watch to At risk (risk 38 to 52).",
            ChangeKind.WORSE,
        ),
        (
            Health.HEALTHY,
            12.0,
            Health.WATCH,
            25.0,
            "Moved from Healthy to Watch (risk 12 to 25).",
            ChangeKind.WORSE,
        ),
        (
            Health.HEALTHY,
            8.0,
            Health.AT_RISK,
            60.0,
            "Moved from Healthy to At risk (risk 8 to 60).",
            ChangeKind.WORSE,
        ),
        (
            Health.AT_RISK,
            47.0,
            Health.WATCH,
            30.0,
            "Moved from At risk to Watch (risk 47 to 30).",
            ChangeKind.BETTER,
        ),
        (
            Health.WATCH,
            22.0,
            Health.HEALTHY,
            8.0,
            "Moved from Watch to Healthy (risk 22 to 8).",
            ChangeKind.BETTER,
        ),
    ],
)
def test_a_health_move_gets_its_sentence_and_kind(
    before_health, before_risk, after_health, after_risk, sentence, kind
):
    prior = earlier(("a", entry("Acme", 3, before_risk, before_health)))

    change = only(changes_since([scored("a", after_risk, after_health, name="Acme")], prior))

    assert change.kind is kind
    assert change.sentence == sentence


def test_a_health_move_carries_the_values_on_both_sides():
    prior = earlier(("a", entry("Acme", 3, 38.0, Health.WATCH)))

    change = only(
        changes_since([scored("a", 52.0, Health.AT_RISK, name="Acme", priority=44.0)], prior)
    )

    assert (change.health_before, change.health_after) == (Health.WATCH, Health.AT_RISK)
    assert (change.risk_before, change.risk_after) == (38.0, 52.0)
    assert (change.rank_before, change.rank_after) == (3, 1)
    assert change.priority_after == 44.0


def test_an_account_new_to_the_book_is_new_with_its_rank():
    book, before = steady(6)
    book.append(scored("fresh", 10.0, Health.HEALTHY, name="Fresh"))

    change = only(changes_since(book, earlier(*before)))

    assert change.kind is ChangeKind.NEW
    assert change.sentence == "New in your book, ranked #7."
    assert (change.rank_after, change.health_after) == (7, Health.HEALTHY)


def test_an_account_that_left_is_gone_with_its_earlier_name():
    prior = earlier(("a", entry("Left Co", 2, 30.0, Health.WATCH)))

    change = only(changes_since([], prior))

    assert change.kind is ChangeKind.GONE
    assert change.name == "Left Co"
    assert change.sentence == "No longer in your book."
    assert (change.rank_before, change.risk_before, change.health_before) == (2, 30.0, Health.WATCH)
    assert change.priority_after == 0.0


# changes_since: rising risk and rank -----------------------------------------------------------


def test_risk_up_by_ten_points_in_the_same_health_is_rising():
    prior = earlier(("a", entry("Acme", 4, 30.0, Health.WATCH)))

    change = only(changes_since([scored("a", 44.0, Health.WATCH, name="Acme")], prior))

    assert change.kind is ChangeKind.RISING
    assert change.sentence == "Risk up from 30 to 44."
    assert (change.risk_before, change.risk_after) == (30.0, 44.0)
    assert (change.rank_before, change.rank_after) == (4, 1)


@pytest.mark.parametrize(
    ("risk_now", "listed"),
    [(39.9, False), (40.0, True)],
)
def test_a_risk_rise_counts_from_ten_points_inclusive(risk_now, listed):
    prior = earlier(("a", entry("Acme", 1, 30.0, Health.WATCH)))

    changes = changes_since([scored("a", risk_now, Health.WATCH, name="Acme")], prior)

    assert bool(changes.changes) is listed


def test_a_rank_move_of_five_places_alone_is_listed_as_up():
    fillers, before = steady(5)
    prior = earlier(*before, ("a", entry("Acme", 14, 30.0, Health.WATCH)))

    change = only(changes_since([*fillers, scored("a", 30.0, Health.WATCH, name="Acme")], prior))

    assert change.kind is ChangeKind.RISING
    assert change.sentence == "Up from #14 to #6."
    assert (change.rank_before, change.rank_after) == (14, 6)


@pytest.mark.parametrize(("rank_before", "listed"), [(11, True), (10, False)])
def test_a_rank_rise_counts_from_five_places_inclusive(rank_before, listed):
    fillers, before = steady(5)
    prior = earlier(*before, ("a", entry("Acme", rank_before, 30.0, Health.WATCH)))

    changes = changes_since([*fillers, scored("a", 30.0, Health.WATCH, name="Acme")], prior)

    assert bool(changes.changes) is listed


def test_when_the_risk_and_the_rank_both_moved_the_risk_sentence_wins():
    fillers, before = steady(5)
    prior = earlier(*before, ("a", entry("Acme", 14, 30.0, Health.WATCH)))

    change = only(changes_since([*fillers, scored("a", 44.0, Health.WATCH, name="Acme")], prior))

    assert change.sentence == "Risk up from 30 to 44."
    assert (change.rank_before, change.rank_after) == (14, 6)


def test_a_risk_drop_within_the_same_health_is_not_listed():
    prior = earlier(("a", entry("Acme", 1, 40.0, Health.WATCH)))

    changes = changes_since([scored("a", 25.0, Health.WATCH, name="Acme")], prior)

    assert changes.changes == []
    assert changes.unchanged is True


def test_falling_down_the_book_within_the_same_health_is_not_listed():
    fillers, before = steady(8)
    prior = earlier(*before, ("a", entry("Acme", 3, 30.0, Health.WATCH)))

    changes = changes_since([*fillers, scored("a", 30.0, Health.WATCH, name="Acme")], prior)

    assert changes.changes == []


def test_a_health_move_takes_precedence_over_a_rise_in_risk():
    prior = earlier(("a", entry("Acme", 14, 30.0, Health.WATCH)))

    change = only(changes_since([scored("a", 50.0, Health.AT_RISK, name="Acme")], prior))

    assert change.kind is ChangeKind.WORSE
    assert change.sentence == "Moved from Watch to At risk (risk 30 to 50)."


@pytest.mark.parametrize(("raw", "shown"), [(36.5, 37), (37.5, 38), (38.4, 38)])
def test_risk_is_shown_as_a_whole_number_with_halves_rounded_away_from_zero(raw, shown):
    prior = earlier(("a", entry("Acme", 1, raw, Health.WATCH)))

    change = only(changes_since([scored("a", 52.0, Health.AT_RISK, name="Acme")], prior))

    assert change.sentence == f"Moved from Watch to At risk (risk {shown} to 52)."


# changes_since: order, cap, empty and unchanged -------------------------------------------------


def test_changes_are_ordered_by_kind_then_priority_then_name():
    prior = earlier(
        ("worse-big", entry("Big Worse", 2, 10.0, Health.HEALTHY)),
        ("worse-a", entry("Amy Worse", 3, 10.0, Health.HEALTHY)),
        ("worse-z", entry("Zed Worse", 4, 10.0, Health.HEALTHY)),
        ("rising", entry("Rising", 9, 30.0, Health.WATCH)),
        ("better", entry("Better", 5, 50.0, Health.AT_RISK)),
        ("gone", entry("Gone Co", 1, 90.0, Health.AT_RISK, priority=99.0)),
    )
    book = [
        scored("new-low", 10.0, Health.HEALTHY, name="New Low", priority=10.0),
        scored("worse-z", 25.0, Health.WATCH, name="Zed Worse", priority=40.0),
        scored("better", 30.0, Health.WATCH, name="Better", priority=80.0),
        scored("worse-big", 25.0, Health.WATCH, name="Big Worse", priority=60.0),
        scored("rising", 42.0, Health.WATCH, name="Rising", priority=70.0),
        scored("worse-a", 25.0, Health.WATCH, name="Amy Worse", priority=40.0),
        scored("new-high", 10.0, Health.HEALTHY, name="New High", priority=90.0),
    ]

    changes = changes_since(book, prior).changes

    assert [c.name for c in changes] == [
        "Big Worse",
        "Amy Worse",
        "Zed Worse",
        "New High",
        "New Low",
        "Rising",
        "Better",
        "Gone Co",
    ]
    assert [c.kind for c in changes] == [
        ChangeKind.WORSE,
        ChangeKind.WORSE,
        ChangeKind.WORSE,
        ChangeKind.NEW,
        ChangeKind.NEW,
        ChangeKind.RISING,
        ChangeKind.BETTER,
        ChangeKind.GONE,
    ]


def test_at_most_max_changes_are_listed_and_the_rest_are_counted():
    book = [
        scored(f"n{p}", 10.0, Health.HEALTHY, name=f"P{p}", priority=float(p)) for p in range(1, 16)
    ]

    changes = changes_since(book, earlier())

    assert len(changes.changes) == MAX_CHANGES
    assert changes.more == 15 - MAX_CHANGES
    assert [c.priority_after for c in changes.changes] == [
        float(p) for p in range(15, 15 - MAX_CHANGES, -1)
    ]
    assert changes.unchanged is False


def test_with_no_earlier_entries_everything_is_new_and_the_day_is_last_weeks():
    book = [
        scored("a", 10.0, Health.HEALTHY, name="Acme", priority=20.0),
        scored("b", 5.0, Health.HEALTHY, name="Bolt", priority=10.0),
    ]

    changes = changes_since(book, earlier())

    assert all(c.kind is ChangeKind.NEW for c in changes.changes)
    assert [c.sentence for c in changes.changes] == [
        "New in your book, ranked #1.",
        "New in your book, ranked #2.",
    ]
    assert changes.since == LAST_WEEK


def test_unchanged_is_true_only_when_nothing_changed():
    prior = earlier(("a", entry("Acme", 1, 10.0, Health.HEALTHY)))

    same = changes_since([scored("a", 10.0, Health.HEALTHY, name="Acme")], prior)
    moved = changes_since([scored("a", 25.0, Health.WATCH, name="Acme")], prior)

    assert (same.changes, same.more, same.unchanged) == ([], 0, True)
    assert same.since == LAST_WEEK
    assert moved.unchanged is False
    assert changes_since([], earlier()).unchanged is True


# trend_points ----------------------------------------------------------------------------------


def test_trend_points_come_oldest_first_with_each_days_risk_health_and_rank():
    newest = dated(TODAY, "a", 52.0, Health.AT_RISK, 1)
    oldest = dated(TODAY - timedelta(days=14), "a", 18.0, Health.HEALTHY, 9)
    middle = dated(TODAY - timedelta(days=7), "a", 38.0, Health.WATCH, 3)

    assert trend_points([newest, oldest, middle], "a") == [
        TrendPoint(day=TODAY - timedelta(days=14), risk=18.0, health=Health.HEALTHY, rank=9),
        TrendPoint(day=TODAY - timedelta(days=7), risk=38.0, health=Health.WATCH, rank=3),
        TrendPoint(day=TODAY, risk=52.0, health=Health.AT_RISK, rank=1),
    ]


def test_a_snapshot_without_the_account_gives_no_point():
    with_it = dated(TODAY - timedelta(days=7), "a", 10.0, Health.HEALTHY, 4)
    without = dated(TODAY - timedelta(days=3), "b", 60.0, Health.AT_RISK, 1)

    assert [p.day for p in trend_points([with_it, without], "a")] == [TODAY - timedelta(days=7)]
    assert trend_points([with_it, without], "nobody") == []


# HistoryStore: reading the folder -------------------------------------------------------------


def test_days_lists_only_files_named_by_a_day_oldest_first(store, history_store):
    put(store, snap(TODAY))
    put(store, snap(TODAY - timedelta(days=2)))
    folder_path = store.root / HISTORY_DIR
    for junk in (
        "notes.txt",
        "readme.json",
        "2026-13-01.json",
        "20261106.json",  # a basic ISO date parses, but it is not the name a save writes
        "2026-11-04.json.tmp",
        ".2026-11-03.json-abc.tmp",
        ".2026-11-02.json",  # hidden, even with a date name
    ):
        (folder_path / junk).write_text("{}", encoding="utf-8")
    (folder_path / "2026-11-01.json").mkdir()  # a folder with a date name

    assert history_store.days() == [TODAY - timedelta(days=2), TODAY]


def test_a_missing_store_has_no_days_no_snapshots_and_no_first_day(tmp_path):
    empty = HistoryStore(LocalStore(tmp_path / "nowhere"))

    assert empty.days() == []
    assert empty.first_day() is None
    assert empty.load(TODAY) is None
    assert empty.on_or_before(TODAY) is None
    assert empty.between(LAST_WEEK, TODAY) == []


def test_first_day_is_the_oldest_file(store, history_store):
    put(store, snap(TODAY))
    put(store, snap(LAST_WEEK))

    assert history_store.first_day() == LAST_WEEK


def test_a_saved_snapshot_loads_back_equal_with_its_signal_kinds_as_keys(history_store):
    readings = [
        reading("a", SignalKind.CHAMPION_LEFT, 1.0),
        reading("a", SignalKind.NPS_LATEST, 9.0),
    ]
    book = score_book([account("a", "Acme")], readings, CONFIG, as_of=TODAY)
    full = snapshot_of(book, readings, TODAY, NOW)

    assert history_store.save(full) is True
    loaded = history_store.load(TODAY)

    assert loaded == full
    assert loaded.accounts["a"].signals == {
        SignalKind.CHAMPION_LEFT: 1.0,
        SignalKind.NPS_LATEST: 9.0,
    }
    assert all(isinstance(kind, SignalKind) for kind in loaded.accounts["a"].signals)


def test_load_of_a_missing_day_is_none(history_store):
    assert history_store.load(TODAY) is None


@pytest.mark.parametrize("text", ["{ not json", ""])
def test_load_of_a_corrupt_or_empty_file_is_none(store, history_store, text):
    write_text(store, TODAY, text)

    assert history_store.load(TODAY) is None


GOOD_ENTRY = {
    "name": "Acme",
    "rank": 1,
    "risk": 30.0,
    "priority": 30.0,
    "health": "watch",
    "motion": "churn",
    "arr": 50000.0,
}
GOOD_FILE = {
    "day": "2026-11-09",
    "taken_at": "2026-11-09T09:00:00",
    "accounts": {"a": GOOD_ENTRY},
}
WRONG_SHAPES = [
    [],
    {"day": "2026-11-09"},
    {"day": "nope", "taken_at": "2026-11-09T09:00:00", "accounts": {}},
    {**GOOD_FILE, "accounts": {"a": {**GOOD_ENTRY, "rank": 0}}},
    {**GOOD_FILE, "accounts": {"a": {**GOOD_ENTRY, "health": "purple"}}},
]


def test_a_well_formed_hand_written_file_loads(store, history_store):
    write_text(store, TODAY, json.dumps(GOOD_FILE))

    assert history_store.load(TODAY).accounts["a"].name == "Acme"


@pytest.mark.parametrize("payload", WRONG_SHAPES)
def test_load_of_a_wrong_shaped_file_is_none(store, history_store, payload):
    write_text(store, TODAY, json.dumps(payload))

    assert history_store.load(TODAY) is None


def test_load_of_a_file_that_says_another_day_is_none(store, history_store):
    other_day = snap(TODAY - timedelta(days=1))
    write_text(store, TODAY, other_day.model_dump_json())

    assert history_store.load(TODAY) is None


def test_on_or_before_is_the_latest_day_not_after_the_given_one(store, history_store):
    for days_back in (6, 4, 0):  # files on Nov 3, Nov 5 and Nov 9
        put(store, snap(TODAY - timedelta(days=days_back)))

    assert history_store.on_or_before(TODAY - timedelta(days=5)).day == TODAY - timedelta(days=6)
    assert history_store.on_or_before(TODAY - timedelta(days=3)).day == TODAY - timedelta(days=4)
    assert history_store.on_or_before(TODAY).day == TODAY
    assert history_store.on_or_before(TODAY - timedelta(days=7)) is None


def test_on_or_before_uses_the_earlier_day_when_the_latest_file_is_unreadable(store, history_store):
    put(store, snap(TODAY - timedelta(days=4)))
    write_text(store, TODAY, "{ broken")

    assert history_store.on_or_before(TODAY).day == TODAY - timedelta(days=4)


def test_between_is_inclusive_oldest_first_and_skips_an_unreadable_day(store, history_store):
    for days_back in range(6, -1, -1):
        put(store, snap(TODAY - timedelta(days=days_back)))
    write_text(store, TODAY - timedelta(days=3), "{ broken")

    found = history_store.between(TODAY - timedelta(days=5), TODAY - timedelta(days=2))

    assert [s.day for s in found] == [
        TODAY - timedelta(days=5),
        TODAY - timedelta(days=4),
        TODAY - timedelta(days=2),
    ]


# HistoryStore: writing -------------------------------------------------------------------------


def test_the_first_save_writes_the_day_file_and_reports_it(store, history_store):
    assert history_store.save(snap(TODAY)) is True

    assert (store.root / HISTORY_DIR / "2026-11-09.json").is_file()
    assert history_store.days() == [TODAY]


def test_save_skips_a_second_write_within_an_hour(history_store):
    assert history_store.save(snap(TODAY, risk=30.0, taken_at=NOW)) is True

    later = NOW + REWRITE_AFTER - timedelta(seconds=1)
    assert history_store.save(snap(TODAY, risk=50.0, taken_at=later)) is False
    assert history_store.load(TODAY).accounts["a"].risk == 30.0


def test_save_rewrites_once_an_hour_has_passed(history_store):
    history_store.save(snap(TODAY, risk=30.0, taken_at=NOW))

    assert history_store.save(snap(TODAY, risk=50.0, taken_at=NOW + REWRITE_AFTER)) is True
    assert history_store.load(TODAY).accounts["a"].risk == 50.0


def test_an_older_snapshot_never_replaces_a_newer_one(history_store):
    history_store.save(snap(TODAY, risk=30.0, taken_at=NOW + timedelta(hours=2)))

    assert history_store.save(snap(TODAY, risk=50.0, taken_at=NOW)) is False
    assert history_store.load(TODAY).accounts["a"].risk == 30.0


def test_save_replaces_a_corrupt_file(store, history_store):
    write_text(store, TODAY, "{ broken")

    assert history_store.save(snap(TODAY, risk=40.0)) is True
    assert history_store.load(TODAY).accounts["a"].risk == 40.0


def test_save_writes_compact_json_with_one_trailing_newline(store, history_store):
    history_store.save(snap(TODAY))

    text = path_of(store, TODAY).read_text(encoding="utf-8")

    assert text.endswith("\n")
    assert "\n" not in text[:-1]
    assert '": ' not in text


def test_save_creates_the_history_folder_in_a_new_store(tmp_path):
    fresh = HistoryStore(LocalStore(tmp_path / "not-yet"))

    assert fresh.save(snap(TODAY)) is True
    assert fresh.days() == [TODAY]


def test_saving_prunes_days_older_than_keep_days_and_keeps_the_boundary(store, history_store):
    keep = TODAY - timedelta(days=KEEP_DAYS)
    for day in (
        keep - timedelta(days=1),
        keep - timedelta(days=200),
        keep,
        TODAY - timedelta(days=1),
    ):
        put(store, snap(day))

    history_store.save(snap(TODAY))

    assert history_store.days() == [keep, TODAY - timedelta(days=1), TODAY]


def test_pruning_leaves_unrelated_files_alone(store, history_store):
    folder_path = store.root / HISTORY_DIR
    put(store, snap(TODAY - timedelta(days=KEEP_DAYS + 5)))
    (folder_path / "notes.txt").write_text("keep me", encoding="utf-8")

    history_store.save(snap(TODAY))

    assert (folder_path / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert history_store.days() == [TODAY]


# HistoryStore: forgetting and clearing ---------------------------------------------------------


def test_forget_removes_the_account_from_every_file_and_counts_the_entries(store, history_store):
    days = [TODAY - timedelta(days=2), TODAY - timedelta(days=1), TODAY]
    for day in days:
        put(store, snap(day, accounts=("a", "b")))

    assert history_store.forget(lambda account_id: account_id == "b") == 3

    for day in days:
        loaded = history_store.load(day)
        assert set(loaded.accounts) == {"a"}
        assert loaded.accounts["a"] == snap(day, accounts=("a", "b")).accounts["a"]
        assert loaded.taken_at == datetime.combine(day, time(9))


def test_forget_rewrites_only_the_files_that_changed(store, history_store, monkeypatch):
    put(store, snap(TODAY - timedelta(days=2), accounts=("a",)))
    put(store, snap(TODAY - timedelta(days=1), accounts=("a", "b")))
    put(store, snap(TODAY, accounts=("a",)))
    written: list[Path] = []
    real_write = history_module.vault.write_text

    def recording(path: Path, text: str) -> None:
        written.append(path)
        real_write(path, text)

    monkeypatch.setattr(history_module.vault, "write_text", recording)

    assert history_store.forget(lambda account_id: account_id == "b") == 1
    assert written == [path_of(store, TODAY - timedelta(days=1))]


def test_forget_that_removes_nothing_writes_nothing_and_an_empty_store_gives_zero(
    store, history_store, monkeypatch
):
    assert history_store.forget(lambda account_id: True) == 0  # no folder yet

    put(store, snap(TODAY))
    written: list[Path] = []
    real_write = history_module.vault.write_text

    def recording(path: Path, text: str) -> None:
        written.append(path)
        real_write(path, text)

    monkeypatch.setattr(history_module.vault, "write_text", recording)

    assert history_store.forget(lambda account_id: False) == 0
    assert written == []


def test_clear_deletes_the_folder_and_every_snapshot(store, history_store):
    history_store.save(snap(TODAY))
    put(store, snap(TODAY - timedelta(days=1)))

    history_store.clear()

    assert not (store.root / HISTORY_DIR).exists()
    assert history_store.days() == []


def test_clear_is_fine_when_there_is_no_folder(history_store):
    history_store.clear()
    history_store.clear()


def test_forget_deletes_an_unreadable_file_that_might_name_a_removed_account(store, history_store):
    put(store, snap(TODAY, accounts=("a",)))
    broken = path_of(store, TODAY - timedelta(days=1))
    broken.write_bytes(b'{"day": "not a date", "accounts": {"a": {"name": "Acme"')
    history_store.forget(lambda account_id: account_id == "a")
    assert not broken.exists()
    assert history_store.days() == [TODAY]
