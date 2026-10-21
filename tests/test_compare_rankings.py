"""compare_rankings: how each account's rank moves between two scorings of the same book."""

from __future__ import annotations

from datetime import date

import pytest

from cs_brief.core.briefs import RankChange, compare_rankings
from cs_brief.core.models import Account, Health, Milestones, Motion, ScoredAccount


def _scored(id: str, *, health: Health = Health.HEALTHY, priority: float = 0.0) -> ScoredAccount:
    account = Account(
        id=id,
        name=f"Account {id}",
        arr=50_000,
        tier="Growth",
        owner="Owner",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=4, total=4),
    )
    return ScoredAccount(
        account=account,
        motion=Motion.CHURN,
        health=health,
        risk=priority,
        priority=priority,
        reasons=[],
    )


def _book(*ids: str) -> list[ScoredAccount]:
    """A book in the order given, with priority falling down the list."""
    return [_scored(id, priority=float(100 - index)) for index, id in enumerate(ids)]


def _ids(changes: list[RankChange]) -> list[str]:
    return [change.account_id for change in changes]


def test_no_change_keeps_every_rank() -> None:
    book = _book("a", "b", "c", "d", "e")
    changes = compare_rankings(book, book, top_n=3)
    assert _ids(changes) == ["a", "b", "c"]
    assert [change.new_rank for change in changes] == [1, 2, 3]
    assert [change.old_rank for change in changes] == [1, 2, 3]
    assert all(change.movement == 0 for change in changes)
    assert not any(change.entered_top for change in changes)


def test_an_account_that_moves_up_has_a_positive_movement() -> None:
    before = _book("a", "b", "c", "d")
    after = _book("c", "a", "b", "d")
    by_id = {change.account_id: change for change in compare_rankings(before, after)}
    assert (by_id["c"].old_rank, by_id["c"].new_rank, by_id["c"].movement) == (3, 1, 2)
    assert by_id["c"].entered_top is False


def test_an_account_that_moves_down_has_a_negative_movement() -> None:
    before = _book("a", "b", "c")
    after = _book("b", "c", "a")
    by_id = {change.account_id: change for change in compare_rankings(before, after)}
    assert (by_id["a"].old_rank, by_id["a"].new_rank, by_id["a"].movement) == (1, 3, -2)
    assert (by_id["b"].movement, by_id["c"].movement) == (1, 1)


def test_an_account_entering_the_top_is_flagged_with_its_old_rank() -> None:
    ids = [f"acc-{n:02d}" for n in range(1, 15)]
    before = _book(*ids)
    after = _book(ids[13], *ids[:13])  # the fourteenth account jumps to the top
    changes = compare_rankings(before, after, top_n=10)
    assert len(changes) == 10
    jumper = changes[0]
    assert jumper.account_id == "acc-14"
    assert (jumper.old_rank, jumper.new_rank, jumper.movement) == (14, 1, 13)
    assert jumper.entered_top is True
    assert not any(change.entered_top for change in changes[1:])


def test_an_account_already_in_the_top_is_not_flagged_as_entering() -> None:
    before = _book("a", "b", "c", "d")
    after = _book("b", "a", "c", "d")
    changes = compare_rankings(before, after, top_n=2)
    assert [change.entered_top for change in changes] == [False, False]


def test_entered_top_follows_the_top_n_being_compared() -> None:
    before = _book("a", "b", "c", "d", "e")
    after = _book("e", "a", "b", "c", "d")
    changes = compare_rankings(before, after, top_n=3)
    assert changes[0].account_id == "e"
    assert changes[0].entered_top is True  # old rank 5 is outside the top 3


def test_health_and_priority_come_from_the_new_book() -> None:
    before = [_scored("a", health=Health.HEALTHY, priority=10.0)]
    after = [_scored("a", health=Health.AT_RISK, priority=82.5)]
    (change,) = compare_rankings(before, after)
    assert change.health is Health.AT_RISK
    assert change.priority == 82.5
    assert change.name == "Account a"


def test_an_account_missing_from_the_old_book_raises() -> None:
    before = _book("a", "b")
    after = _book("a", "zz")
    with pytest.raises(ValueError, match="zz"):
        compare_rankings(before, after)


def test_a_missing_account_raises_even_outside_the_top_n() -> None:
    before = _book("a", "b", "c")
    after = _book("a", "b", "zz")
    with pytest.raises(ValueError, match="zz"):
        compare_rankings(before, after, top_n=2)


@pytest.mark.parametrize(
    ("top_n", "expected"),
    [(50, 4), (4, 4), (1, 1), (0, 0), (-3, 0)],
)
def test_top_n_edges(top_n: int, expected: int) -> None:
    book = _book("a", "b", "c", "d")
    assert len(compare_rankings(book, book, top_n=top_n)) == expected


def test_top_n_defaults_to_ten() -> None:
    ids = [f"acc-{n:02d}" for n in range(1, 13)]
    book = _book(*ids)
    assert len(compare_rankings(book, book)) == 10


def test_empty_books_give_no_changes() -> None:
    assert compare_rankings([], []) == []
