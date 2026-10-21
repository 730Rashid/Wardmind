from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from cs_brief.core import service
from cs_brief.core.briefs import AccountDetail, BreakdownRow, account_detail, explain
from cs_brief.core.config import ScoringConfig, SignalRule
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.models import Account, Milestones, Signal, SignalKind
from cs_brief.core.scoring import score_book

AS_OF = date(2026, 10, 19)
READ_AT = datetime(2026, 10, 18, 9, 0)
EARLIER = datetime(2026, 10, 1, 9, 0)
LATER = datetime(2026, 10, 15, 9, 0)

USAGE = SignalKind.USAGE_TREND_30D
DAYS = SignalKind.DAYS_SINCE_LAST_LOGIN
OPEN = SignalKind.OPEN_TICKETS_SEV1
NPS = SignalKind.NPS_LATEST
CHAMPION = SignalKind.CHAMPION_LEFT
SEATS = SignalKind.SEAT_UTILISATION
BILLING = SignalKind.BILLING_OVERDUE_DAYS
ONBOARDING = SignalKind.ONBOARDING_OVERDUE_MILESTONES


def _account(
    id: str,
    *,
    arr: float = 50_000,
    start_date: date = date(2025, 1, 1),
    renewal_in_days: int = 200,
) -> Account:
    return Account(
        id=id,
        name=f"Account {id}",
        arr=arr,
        tier="Growth",
        owner="Owner",
        start_date=start_date,
        renewal_date=AS_OF + timedelta(days=renewal_in_days),
        milestones=Milestones(done=4, total=4),
    )


def _reading(
    account_id: str, kind: SignalKind, value: float, observed_at: datetime = READ_AT
) -> Signal:
    return Signal(
        account_id=account_id, kind=kind, value=value, observed_at=observed_at, source="test"
    )


def _detail(
    accounts: list[Account],
    signals: list[Signal],
    account_id: str,
    config: ScoringConfig | None = None,
) -> AccountDetail:
    config = ScoringConfig() if config is None else config
    book = score_book(accounts, signals, config, AS_OF)
    detail = account_detail(book, account_id, signals, config, AS_OF)
    assert detail is not None
    return detail


def _row(detail: AccountDetail, kind: SignalKind) -> BreakdownRow:
    return next(row for row in detail.breakdown if row.kind is kind)


def _assert_rows_match_reasons(detail: AccountDetail) -> None:
    rows = [(row.kind, row.contribution) for row in detail.breakdown if row.contribution > 0]
    assert rows == [(reason.kind, reason.contribution) for reason in detail.scored.reasons]


# breakdown


def test_breakdown_has_one_row_for_every_kind():
    detail = _detail([_account("a")], [_reading("a", NPS, 3)], "a")
    assert len(detail.breakdown) == len(SignalKind)
    assert {row.kind for row in detail.breakdown} == set(SignalKind)


def test_breakdown_sorts_by_contribution_then_kind_name():
    readings = [
        _reading("a", USAGE, -50),  # 25.0
        _reading("a", DAYS, 30),  # 15.0
        _reading("a", NPS, 3),  # 15 * 4/7 = 8.6
        _reading("a", OPEN, 1),  # 20 * 1/3 = 6.7
        _reading("a", CHAMPION, 0),  # 0, but still a reading
    ]
    detail = _detail([_account("a")], readings, "a")
    assert [row.kind for row in detail.breakdown] == [
        USAGE,
        DAYS,
        NPS,
        OPEN,
        # Zero rows go by kind name: billing, champion, onboarding, seat.
        BILLING,
        CHAMPION,
        ONBOARDING,
        SEATS,
    ]
    assert [row.contribution for row in detail.breakdown] == [
        25.0,
        15.0,
        8.6,
        6.7,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    _assert_rows_match_reasons(detail)


def test_equal_contributions_sort_by_kind_name():
    readings = [
        _reading("a", USAGE, -50),  # 25.0
        _reading("a", CHAMPION, 1),  # 25.0
    ]
    detail = _detail([_account("a")], readings, "a")
    # champion_left sorts before usage_trend_30d, even though USAGE is declared first.
    assert [row.kind for row in detail.breakdown[:2]] == [CHAMPION, USAGE]
    _assert_rows_match_reasons(detail)


def test_a_kind_without_a_reading_says_no_reading():
    detail = _detail([_account("a")], [_reading("a", USAGE, -10)], "a")
    row = _row(detail, DAYS)
    assert row.label == "Last login"
    assert row.value_text == "No reading"
    assert row.contribution == 0
    assert row.observed_at is None
    assert row.max_points == 15


def test_row_shows_the_latest_reading_and_when_it_was_taken():
    older = _reading("a", USAGE, -10, EARLIER)
    newer = _reading("a", USAGE, -50, READ_AT)
    row = _row(_detail([_account("a")], [older, newer], "a"), USAGE)
    assert row.value_text == "Active users down 50% in 30 days"
    assert row.contribution == 25.0
    assert row.observed_at == READ_AT


def test_same_timestamp_resolves_to_the_reading_the_score_uses():
    # latest_signals breaks an observed_at tie by the larger value, so -10 wins over -50.
    weaker = _reading("a", USAGE, -50, READ_AT)
    stronger = _reading("a", USAGE, -10, READ_AT)
    detail = _detail([_account("a")], [weaker, stronger], "a")
    row = _row(detail, USAGE)
    assert row.value_text == "Active users down 10% in 30 days"
    assert row.contribution == 5.0
    assert detail.scored.risk == 5.0
    assert [entry.value for entry in detail.timeline if entry.is_latest] == [-10]


def test_max_points_come_from_the_config():
    config = ScoringConfig(signals={NPS: SignalRule(weight=40, start=7, full=0)})
    detail = _detail([_account("a")], [_reading("a", NPS, 3)], "a", config)
    row = _row(detail, NPS)
    assert row.max_points == 40
    assert row.contribution == round(40 * 4 / 7, 1)
    assert _row(detail, DAYS).max_points == 15


def test_breakdown_can_sum_past_100_while_the_score_caps_at_100():
    readings = [
        _reading("a", USAGE, -50),  # 25
        _reading("a", DAYS, 30),  # 15
        _reading("a", OPEN, 3),  # 20
        _reading("a", NPS, 0),  # 15
        _reading("a", CHAMPION, 1),  # 25
        _reading("a", SEATS, 30),  # 10
        _reading("a", BILLING, 60),  # 20
        _reading("a", ONBOARDING, 3),  # 20
    ]
    detail = _detail([_account("a")], readings, "a")
    assert sum(row.contribution for row in detail.breakdown) == 150
    assert detail.scored.risk == 100


# timeline


def test_timeline_is_newest_first_and_ties_follow_signal_order():
    readings = [
        _reading("a", USAGE, -10, EARLIER),
        _reading("a", NPS, 3, LATER),
        _reading("a", DAYS, 30, LATER),
        _reading("a", CHAMPION, 1, LATER),
    ]
    detail = _detail([_account("a")], readings, "a")
    assert [entry.kind for entry in detail.timeline] == [DAYS, NPS, CHAMPION, USAGE]


def test_timeline_entry_describes_the_reading():
    detail = _detail([_account("a")], [_reading("a", NPS, 3, LATER)], "a")
    entry = detail.timeline[0]
    assert entry.observed_at == LATER
    assert entry.kind is NPS
    assert entry.label == "NPS"
    assert entry.value == 3
    assert entry.plain_english == "Latest NPS response was 3 out of 10"
    assert entry.contribution == 8.6
    assert entry.is_latest is True


def test_an_older_reading_has_its_own_contribution_and_is_not_latest():
    older = _reading("a", USAGE, -10, EARLIER)
    newer = _reading("a", USAGE, -50, READ_AT)
    detail = _detail([_account("a")], [older, newer], "a")
    assert [(entry.value, entry.contribution, entry.is_latest) for entry in detail.timeline] == [
        (-50, 25.0, True),
        (-10, 5.0, False),
    ]


def test_is_latest_marks_one_newest_reading_per_kind():
    readings = [
        _reading("a", USAGE, -10, EARLIER),
        _reading("a", USAGE, -50, READ_AT),
        _reading("a", USAGE, -20, datetime(2026, 10, 5, 9, 0)),
        _reading("a", NPS, 3, EARLIER),
        _reading("a", NPS, 5, datetime(2026, 10, 12, 9, 0)),
        _reading("a", DAYS, 30, READ_AT),
    ]
    detail = _detail([_account("a")], readings, "a")
    assert sum(entry.is_latest for entry in detail.timeline) == 3
    latest = {entry.kind: entry.value for entry in detail.timeline if entry.is_latest}
    assert latest == {USAGE: -50, NPS: 5, DAYS: 30}


def test_signals_from_other_accounts_are_ignored():
    accounts = [_account("a"), _account("b")]
    own = [_reading("a", USAGE, -10, EARLIER)]
    others = [_reading("b", USAGE, -50), _reading("b", NPS, 0)]
    alone = _detail(accounts, own, "a")
    mixed = _detail(accounts, own + others, "a")
    assert mixed.breakdown == alone.breakdown
    assert mixed.timeline == alone.timeline
    assert [entry.kind for entry in mixed.timeline] == [USAGE]


# account facts


def test_unknown_account_id_returns_none():
    signals = [_reading("a", USAGE, -10)]
    book = score_book([_account("a")], signals, ScoringConfig(), AS_OF)
    assert account_detail(book, "acc-999", signals, ScoringConfig(), AS_OF) is None


def test_rank_is_the_position_in_the_book_and_book_size_is_its_length():
    accounts = [_account("a"), _account("b"), _account("c")]
    signals = [_reading("a", USAGE, -50), _reading("c", DAYS, 30)]
    book = score_book(accounts, signals, ScoringConfig(), AS_OF)
    assert [item.account.id for item in book] == ["a", "c", "b"]
    detail = account_detail(book, "c", signals, ScoringConfig(), AS_OF)
    assert detail is not None
    assert detail.rank == 2
    assert detail.book_size == 3


def test_explanation_is_explain_of_the_scored_account():
    detail = _detail([_account("a")], [_reading("a", USAGE, -50)], "a")
    assert detail.scored.account.id == "a"
    assert detail.explanation == explain(detail.scored)


def test_day_counts_run_from_as_of():
    started = _account("a", start_date=AS_OF - timedelta(days=100), renewal_in_days=30)
    detail = _detail([started], [], "a")
    assert detail.days_since_start == 100
    assert detail.days_to_renewal == 30


def test_days_to_renewal_is_negative_once_lapsed():
    detail = _detail([_account("a", renewal_in_days=-5)], [], "a")
    assert detail.days_to_renewal == -5


# get_account_detail with an injected connector


class _StubConnector:
    name = "stub"

    def __init__(self, accounts: list[Account], signals: list[Signal]) -> None:
        self._accounts = accounts
        self._signals = signals

    def list_accounts(self) -> list[Account]:
        return list(self._accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        if account_id is None:
            return list(self._signals)
        return [signal for signal in self._signals if signal.account_id == account_id]


def test_get_account_detail_reads_book_and_history_from_the_given_connector():
    stub = _StubConnector(
        [_account("a"), _account("b")],
        [_reading("a", USAGE, -50), _reading("b", NPS, 0)],
    )
    detail = service.get_account_detail("a", connector=stub, config=ScoringConfig(), as_of=AS_OF)
    assert detail is not None
    assert detail.book_size == 2
    assert _row(detail, USAGE).contribution == 25.0
    assert [entry.kind for entry in detail.timeline] == [USAGE]
    assert (
        service.get_account_detail("acc-999", connector=stub, config=ScoringConfig(), as_of=AS_OF)
        is None
    )


# demo checks


@pytest.fixture(scope="module")
def demo():
    connector = DemoConnector(today=AS_OF)
    config = ScoringConfig()
    book = service.get_book(connector=connector, config=config, as_of=AS_OF)
    return connector, config, book


def _demo_detail(demo, account_id: str) -> AccountDetail:
    connector, config, _ = demo
    detail = service.get_account_detail(account_id, connector=connector, config=config, as_of=AS_OF)
    assert detail is not None, account_id
    return detail


def test_demo_account_view_matches_the_book_entry(demo):
    _, _, book = demo
    for item in book:
        assert _demo_detail(demo, item.account.id).scored == item, item.account.id


def test_demo_breakdown_sums_to_the_score(demo):
    _, _, book = demo
    for item in book:
        detail = _demo_detail(demo, item.account.id)
        total = sum(row.contribution for row in detail.breakdown)
        assert round(min(100.0, total), 1) == detail.scored.risk, item.account.id


def test_demo_non_zero_breakdown_rows_match_the_reasons(demo):
    _, _, book = demo
    for item in book:
        _assert_rows_match_reasons(_demo_detail(demo, item.account.id))


def test_demo_one_latest_entry_per_kind_with_a_reading(demo):
    connector, _, book = demo
    for item in book:
        detail = _demo_detail(demo, item.account.id)
        kinds = {signal.kind for signal in connector.list_signals(item.account.id)}
        latest = [entry for entry in detail.timeline if entry.is_latest]
        assert len(latest) == len(kinds), item.account.id
        assert {entry.kind for entry in latest} == kinds, item.account.id


def test_demo_rank_matches_the_book_position(demo):
    _, _, book = demo
    for index, item in enumerate(book):
        detail = _demo_detail(demo, item.account.id)
        assert detail.rank == index + 1, item.account.id
        assert detail.book_size == len(book), item.account.id


def test_demo_unknown_id_returns_none(demo):
    connector, config, _ = demo
    assert (
        service.get_account_detail("acc-999", connector=connector, config=config, as_of=AS_OF)
        is None
    )
