"""The honest forecast (core/forecast.py): the disagreement rules, the current call on a renewal,
forecast_view (the window, the order of rows, the totals and the disagreements) and calibration
(your calls and Wardmind's scores checked against recorded outcomes).

Pure: no store, no clock, no network. Every date is worked out from AS_OF.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

import pytest

from wardmind.core.forecast import (
    RENEWED_KINDS,
    Calibration,
    CallRecord,
    Disagreement,
    ForecastRow,
    ForecastTotal,
    calibration,
    current_call,
    disagreement,
    forecast_view,
)
from wardmind.core.journal import AccountJournal, CallKind, ForecastCall, Outcome, OutcomeKind
from wardmind.core.models import Account, Health, Milestones, Motion, ScoredAccount

AS_OF = date(2026, 11, 9)
WINDOW = 30  # days
NOW = datetime(2026, 11, 9, 9, 0)
RENEWAL = AS_OF + timedelta(days=20)  # the renewal the calibration tests answer


def ahead(days: int) -> date:
    return AS_OF + timedelta(days=days)


# Builders -------------------------------------------------------------------------------------


def scored_account(
    account_id: str = "acc-1",
    *,
    name: str | None = None,
    renewal: date = AS_OF + timedelta(days=10),
    arr: float = 100_000.0,
    health: Health = Health.HEALTHY,
    owner: str = "Sam Rivera",
    motion: Motion = Motion.RENEWAL,
) -> ScoredAccount:
    return ScoredAccount(
        account=Account(
            id=account_id,
            name=name if name is not None else f"Account {account_id}",
            arr=arr,
            tier="Growth",
            owner=owner,
            renewal_date=renewal,
            milestones=Milestones(done=3, total=3, overdue=0),
        ),
        motion=motion,
        health=health,
        risk=20.0,
        priority=20.0,
        reasons=[],
    )


def call_on(
    renewal: date,
    kind: CallKind = CallKind.COMMIT,
    *,
    call_id: str = "c-00000001",
    made_on: date = AS_OF,
) -> ForecastCall:
    return ForecastCall(
        id=call_id,
        kind=kind,
        renewal_date=renewal,
        made_on=made_on,
        health=Health.HEALTHY,
        risk=20.0,
        arr=100_000.0,
        recorded_at=NOW,
    )


def outcome_on(
    renewal: date,
    kind: OutcomeKind = OutcomeKind.RENEWED,
    *,
    health: Health = Health.HEALTHY,
    outcome_id: str = "o-00000001",
) -> Outcome:
    return Outcome(
        id=outcome_id,
        kind=kind,
        renewal_date=renewal,
        decided_on=renewal,
        arr_before=100_000.0,
        risk=20.0,
        priority=20.0,
        health=health,
        motion=Motion.RENEWAL,
        recorded_at=NOW,
    )


def journal_of(
    *, calls: Sequence[ForecastCall] = (), outcomes: Sequence[Outcome] = ()
) -> AccountJournal:
    return AccountJournal(calls=list(calls), outcomes=list(outcomes))


# disagreement ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "health", "expected"),
    [
        (CallKind.COMMIT, Health.AT_RISK, Disagreement.CALL_HIGHER),
        (CallKind.LIKELY, Health.AT_RISK, Disagreement.CALL_HIGHER),
        (CallKind.AT_RISK, Health.HEALTHY, Disagreement.CALL_LOWER),
        (CallKind.COMMIT, Health.HEALTHY, None),
        (CallKind.LIKELY, Health.HEALTHY, None),
        (CallKind.AT_RISK, Health.AT_RISK, None),
        (None, Health.AT_RISK, None),
        (None, Health.HEALTHY, None),
    ],
)
def test_only_a_call_above_or_below_the_score_disagrees(call, health, expected):
    assert disagreement(call, health) is expected


@pytest.mark.parametrize("call", [*CallKind, None])
def test_watch_never_disagrees_whatever_the_call(call):
    assert disagreement(call, Health.WATCH) is None


def test_each_disagreement_says_what_to_check():
    assert Disagreement.CALL_HIGHER.text == (
        "Wardmind says At risk. Check what it sees before you commit."
    )
    assert Disagreement.CALL_LOWER.text == (
        "Wardmind sees no risk signals. Note what you know that it doesn't."
    )


# current_call ---------------------------------------------------------------------------------


def test_there_is_no_current_call_without_one_on_that_renewal_date():
    assert current_call(AccountJournal(), RENEWAL) is None
    assert current_call(journal_of(calls=[call_on(ahead(40))]), RENEWAL) is None


def test_the_current_call_is_the_one_on_that_renewal_date():
    mine = call_on(RENEWAL, CallKind.LIKELY, call_id="c-00000002")
    journal = journal_of(calls=[call_on(ahead(40)), mine])

    assert current_call(journal, RENEWAL) is mine


def test_the_last_call_wins_when_a_renewal_has_several():
    first = call_on(RENEWAL, CallKind.COMMIT, call_id="c-00000001")
    second = call_on(RENEWAL, CallKind.AT_RISK, call_id="c-00000002")

    assert current_call(journal_of(calls=[first, second]), RENEWAL) is second


# forecast_view: the window --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "listed"),
    [(-1, False), (0, True), (WINDOW, True), (WINDOW + 1, False)],
)
def test_a_renewal_is_listed_from_today_to_the_last_day_of_the_window(offset, listed):
    view = forecast_view([scored_account(renewal=ahead(offset))], {}, AS_OF, WINDOW)

    assert [row.account_id for row in view.rows] == (["acc-1"] if listed else [])


def test_a_window_of_no_days_lists_only_the_renewals_due_today():
    book = [scored_account("today", renewal=AS_OF), scored_account("tomorrow", renewal=ahead(1))]

    view = forecast_view(book, {}, AS_OF, 0)

    assert [row.account_id for row in view.rows] == ["today"]


def test_rows_run_soonest_first_then_by_name_ignoring_case_then_by_id():
    book = [
        scored_account("x1", name="Beta", renewal=ahead(10)),
        scored_account("x3", name="ACME", renewal=ahead(10)),
        scored_account("x2", name="acme", renewal=ahead(10)),
        scored_account("x4", name="Zeta", renewal=ahead(2)),
    ]

    expected = ["x4", "x2", "x3", "x1"]
    assert [row.account_id for row in forecast_view(book, {}, AS_OF, WINDOW).rows] == expected
    assert [row.account_id for row in forecast_view(book[::-1], {}, AS_OF, WINDOW).rows] == expected


# forecast_view: rows, calls and disagreements -------------------------------------------------


def test_a_row_carries_the_account_its_days_to_renewal_its_call_and_its_disagreement():
    scored = scored_account(
        "acc-1",
        name="Acme",
        renewal=ahead(12),
        arr=50_000.0,
        health=Health.AT_RISK,
        owner="Ann Lee",
    )
    journals = {
        "acc-1": journal_of(calls=[call_on(ahead(12), CallKind.LIKELY, made_on=ahead(-3))]),
    }

    (row,) = forecast_view([scored], journals, AS_OF, WINDOW).rows

    assert row == ForecastRow(
        account_id="acc-1",
        name="Acme",
        owner="Ann Lee",
        arr=50_000.0,
        renewal_date=ahead(12),
        days_to_renewal=12,
        health=Health.AT_RISK,
        motion=Motion.RENEWAL,
        call=CallKind.LIKELY,
        made_on=ahead(-3),
        disagreement=Disagreement.CALL_HIGHER,
    )


def test_a_call_on_an_older_renewal_date_does_not_count_for_the_current_one():
    # The renewal moved from ahead(3) to ahead(10); the call was made for the old date.
    book = [scored_account("acc-1", renewal=ahead(10))]
    journals = {"acc-1": journal_of(calls=[call_on(ahead(3), CallKind.COMMIT)])}

    view = forecast_view(book, journals, AS_OF, WINDOW)

    (row,) = view.rows
    assert (row.call, row.made_on, row.disagreement) == (None, None, None)
    assert view.totals[0] == ForecastTotal(call=CallKind.COMMIT, count=0, arr=0.0)
    assert view.totals[3] == ForecastTotal(call=None, count=1, arr=100_000.0)


def test_journal_entries_for_accounts_not_in_the_book_are_ignored():
    book = [scored_account("acc-1", renewal=ahead(3))]
    journals = {"gone": journal_of(calls=[call_on(ahead(3), CallKind.COMMIT)])}

    view = forecast_view(book, journals, AS_OF, WINDOW)

    assert [row.call for row in view.rows] == [None]
    assert view.totals[0].count == 0
    assert view.totals[3] == ForecastTotal(call=None, count=1, arr=100_000.0)


def test_disagreements_are_marked_on_each_row_and_counted():
    book = [
        scored_account("a", renewal=ahead(3), health=Health.AT_RISK),
        scored_account("b", renewal=ahead(4), health=Health.HEALTHY),
        scored_account("c", renewal=ahead(5), health=Health.AT_RISK),
        scored_account("d", renewal=ahead(6), health=Health.WATCH),
        scored_account("e", renewal=ahead(7), health=Health.AT_RISK),
        scored_account("f", renewal=ahead(8), health=Health.HEALTHY),
    ]
    journals = {
        "a": journal_of(calls=[call_on(ahead(3), CallKind.COMMIT)]),
        "b": journal_of(calls=[call_on(ahead(4), CallKind.AT_RISK)]),
        "c": journal_of(calls=[call_on(ahead(5), CallKind.LIKELY)]),
        "d": journal_of(calls=[call_on(ahead(6), CallKind.COMMIT)]),
        "e": journal_of(calls=[call_on(ahead(7), CallKind.AT_RISK)]),
    }

    view = forecast_view(book, journals, AS_OF, WINDOW)

    assert [row.disagreement for row in view.rows] == [
        Disagreement.CALL_HIGHER,
        Disagreement.CALL_LOWER,
        Disagreement.CALL_HIGHER,
        None,
        None,
        None,
    ]
    assert view.disagreements == 3


# forecast_view: totals ------------------------------------------------------------------------


def test_totals_count_and_sum_the_arr_of_each_call_and_the_uncalled_inside_the_window():
    book = [
        scored_account("a", renewal=ahead(3), arr=100_000.0),
        scored_account("b", renewal=ahead(5), arr=40_000.0),
        scored_account("c", renewal=ahead(7), arr=25_000.0),
        scored_account("d", renewal=ahead(9), arr=10_000.0),
        scored_account("e", renewal=ahead(11), arr=5_000.0),
        scored_account("late", renewal=ahead(WINDOW + 1), arr=999_000.0),
    ]
    journals = {
        "a": journal_of(calls=[call_on(ahead(3), CallKind.COMMIT)]),
        "b": journal_of(calls=[call_on(ahead(5), CallKind.COMMIT, call_id="c-00000002")]),
        "c": journal_of(calls=[call_on(ahead(7), CallKind.LIKELY)]),
        "d": journal_of(calls=[call_on(ahead(9), CallKind.AT_RISK)]),
        "late": journal_of(calls=[call_on(ahead(WINDOW + 1), CallKind.COMMIT)]),
    }

    view = forecast_view(book, journals, AS_OF, WINDOW)

    assert view.totals == [
        ForecastTotal(call=CallKind.COMMIT, count=2, arr=140_000.0),
        ForecastTotal(call=CallKind.LIKELY, count=1, arr=25_000.0),
        ForecastTotal(call=CallKind.AT_RISK, count=1, arr=10_000.0),
        ForecastTotal(call=None, count=1, arr=5_000.0),
    ]
    assert view.arr_total == 180_000.0
    assert len(view.rows) == 5


def test_an_empty_book_gives_four_zero_totals_and_no_rows():
    view = forecast_view([], {}, AS_OF, WINDOW)

    assert view.rows == []
    assert view.totals == [
        ForecastTotal(call=CallKind.COMMIT, count=0, arr=0.0),
        ForecastTotal(call=CallKind.LIKELY, count=0, arr=0.0),
        ForecastTotal(call=CallKind.AT_RISK, count=0, arr=0.0),
        ForecastTotal(call=None, count=0, arr=0.0),
    ]
    assert view.arr_total == 0.0
    assert view.disagreements == 0


# calibration ---------------------------------------------------------------------------------


def test_renewed_means_renewed_expanded_or_contracted():
    assert RENEWED_KINDS == frozenset(
        {OutcomeKind.RENEWED, OutcomeKind.EXPANDED, OutcomeKind.CONTRACTED}
    )


def test_with_nothing_recorded_there_is_nothing_to_report():
    assert calibration({}) == Calibration(yours=[], wardmind=[], decided=0)


def test_a_call_with_no_outcome_is_not_counted_and_an_outcome_for_it_is_not_matched():
    journals = {
        "acc-1": journal_of(
            calls=[call_on(RENEWAL)],
            outcomes=[outcome_on(RENEWAL + timedelta(days=1))],
        ),
    }

    result = calibration(journals)

    assert result.yours == []
    assert result.decided == 0


def test_an_outcome_without_a_call_counts_only_for_wardmind():
    journals = {"acc-1": journal_of(outcomes=[outcome_on(RENEWAL, OutcomeKind.RENEWED)])}

    result = calibration(journals)

    assert result.yours == []
    assert result.decided == 0
    assert result.wardmind == [
        CallRecord(
            label="Healthy",
            decided=1,
            renewed=1,
            churned=0,
            sentence="When Wardmind said Healthy, 1 of 1 renewed.",
        ),
    ]


def test_a_call_is_matched_only_with_an_outcome_in_its_own_account():
    journals = {
        "acc-1": journal_of(calls=[call_on(RENEWAL)]),
        "acc-2": journal_of(
            outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED, health=Health.AT_RISK)]
        ),
    }

    result = calibration(journals)

    assert result.yours == []
    assert result.decided == 0
    assert [record.decided for record in result.wardmind] == [1]


def test_commit_and_likely_count_renewed_expanded_and_contracted_as_renewed():
    journals = {
        "a": journal_of(
            calls=[call_on(RENEWAL, CallKind.COMMIT)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.RENEWED)],
        ),
        "b": journal_of(
            calls=[call_on(RENEWAL, CallKind.COMMIT)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.EXPANDED)],
        ),
        "c": journal_of(
            calls=[call_on(RENEWAL, CallKind.COMMIT)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.CONTRACTED)],
        ),
        "d": journal_of(
            calls=[call_on(RENEWAL, CallKind.COMMIT)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED)],
        ),
        "e": journal_of(
            calls=[call_on(RENEWAL, CallKind.LIKELY)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.RENEWED)],
        ),
    }

    result = calibration(journals)

    assert result.yours == [
        CallRecord(
            label="Commit",
            decided=4,
            renewed=3,
            churned=1,
            sentence="When you said Commit, 3 of 4 renewed.",
        ),
        CallRecord(
            label="Likely",
            decided=1,
            renewed=1,
            churned=0,
            sentence="When you said Likely, 1 of 1 renewed.",
        ),
    ]
    assert result.decided == 5


def test_an_at_risk_call_counts_churn_not_renewals():
    journals = {
        "a": journal_of(
            calls=[call_on(RENEWAL, CallKind.AT_RISK)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED)],
        ),
        "b": journal_of(
            calls=[call_on(RENEWAL, CallKind.AT_RISK)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.RENEWED)],
        ),
        "c": journal_of(
            calls=[call_on(RENEWAL, CallKind.AT_RISK)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED)],
        ),
    }

    result = calibration(journals)

    assert result.yours == [
        CallRecord(
            label="At risk",
            decided=3,
            renewed=1,
            churned=2,
            sentence="When you said At risk, 2 of 3 churned.",
        ),
    ]
    assert result.decided == 3


def test_wardmind_records_group_each_outcome_by_its_own_health_and_count_uncalled_ones():
    journals = {
        "a": journal_of(outcomes=[outcome_on(RENEWAL, OutcomeKind.RENEWED, health=Health.HEALTHY)]),
        "b": journal_of(outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED, health=Health.HEALTHY)]),
        "c": journal_of(outcomes=[outcome_on(RENEWAL, OutcomeKind.EXPANDED, health=Health.WATCH)]),
        "d": journal_of(outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED, health=Health.AT_RISK)]),
        "e": journal_of(outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED, health=Health.AT_RISK)]),
        "f": journal_of(outcomes=[outcome_on(RENEWAL, OutcomeKind.RENEWED, health=Health.AT_RISK)]),
    }

    result = calibration(journals)

    assert result.wardmind == [
        CallRecord(
            label="Healthy",
            decided=2,
            renewed=1,
            churned=1,
            sentence="When Wardmind said Healthy, 1 of 2 renewed.",
        ),
        CallRecord(
            label="Watch",
            decided=1,
            renewed=1,
            churned=0,
            sentence="When Wardmind said Watch, 1 of 1 renewed.",
        ),
        CallRecord(
            label="At risk",
            decided=3,
            renewed=1,
            churned=2,
            sentence="When Wardmind said At risk, 2 of 3 churned.",
        ),
    ]
    assert result.yours == []
    assert result.decided == 0


def test_records_come_in_a_fixed_order_and_only_when_something_was_decided():
    later = ahead(50)
    journals = {
        "a": journal_of(
            calls=[call_on(RENEWAL, CallKind.AT_RISK)],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.CHURNED, health=Health.AT_RISK)],
        ),
        "b": journal_of(
            calls=[call_on(later, CallKind.COMMIT, call_id="c-00000002")],
            outcomes=[outcome_on(later, OutcomeKind.RENEWED, health=Health.HEALTHY)],
        ),
    }

    result = calibration(journals)

    assert [record.label for record in result.yours] == ["Commit", "At risk"]
    assert [record.label for record in result.wardmind] == ["Healthy", "At risk"]
    assert result.decided == 2


def test_only_the_current_call_for_a_renewal_is_checked_against_its_outcome():
    journals = {
        "acc-1": journal_of(
            calls=[
                call_on(RENEWAL, CallKind.COMMIT, call_id="c-00000001"),
                call_on(RENEWAL, CallKind.AT_RISK, call_id="c-00000002"),
            ],
            outcomes=[outcome_on(RENEWAL, OutcomeKind.RENEWED)],
        ),
    }

    result = calibration(journals)

    assert result.yours == [
        CallRecord(
            label="At risk",
            decided=1,
            renewed=1,
            churned=0,
            sentence="When you said At risk, 0 of 1 churned.",
        ),
    ]
    assert result.decided == 1
