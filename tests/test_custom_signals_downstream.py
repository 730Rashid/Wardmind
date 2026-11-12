"""Custom signals below the score: the account's breakdown and timeline, the action for the top
reason, the onboarding brief, "what would it take", how sure the score is, negotiation and growth,
and history and journal snapshots.

The book has two signals of its own: "API calls per week" (unit "calls", with a question and an
action) and "Reports exported" (with neither). A reading of a signal the book has removed reads as
"Old metric"."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from functools import partial

from wardmind.core import briefs, service, signal_info
from wardmind.core.briefs import SIGNAL_LABELS, account_detail, suggest_action
from wardmind.core.config import CustomSignal, MotionWindows, ScoringConfig, SignalRule
from wardmind.core.growth import growth_radar
from wardmind.core.history import BookSnapshot, snapshot_of
from wardmind.core.insights import STALE_AFTER_DAYS, _first_enough, path_down, score_confidence
from wardmind.core.journal import AccountJournal, Outcome, OutcomeKind
from wardmind.core.models import (
    Account,
    CustomKind,
    Health,
    Milestones,
    Motion,
    Reason,
    Signal,
    SignalKind,
)
from wardmind.core.negotiation import negotiation_sheet
from wardmind.core.onboarding import onboarding_brief
from wardmind.core.scoring import latest_signals, score_account, score_book

AS_OF = date(2026, 10, 19)
READ_AT = datetime(2026, 10, 18, 9, 0)

USAGE = SignalKind.USAGE_TREND_30D
CHAMPION = SignalKind.CHAMPION_LEFT
API = CustomKind.of("api_calls")
REPORTS = CustomKind.of("reports_exported")
OLD = CustomKind.of("old_metric")

API_QUESTION = "Which integrations have stopped calling the API?"
API_ACTION = "Check the API keys with the customer's developer lead this week."
CLOSING_ONBOARDING = "What would make the onboarding review a success for you?"

# API calls: full risk at 50 a week, none from 500 up. Reports: full risk at 0, none from 4 up.
CONFIG = ScoringConfig(
    custom=[
        CustomSignal(
            id="api_calls",
            name="API calls per week",
            unit="calls",
            rule=SignalRule(weight=30, start=500, full=50),
            question=API_QUESTION,
            action=API_ACTION,
        ),
        CustomSignal(
            id="reports_exported",
            name="Reports exported",
            rule=SignalRule(weight=10, start=4, full=0),
        ),
    ]
)


def _account(
    id: str,
    *,
    start_date: date = date(2025, 1, 1),
    renewal_in_days: int = 200,
    done: int = 4,
    total: int = 4,
) -> Account:
    return Account(
        id=id,
        name=f"Account {id}",
        arr=50_000,
        tier="Growth",
        owner="Owner",
        start_date=start_date,
        renewal_date=AS_OF + timedelta(days=renewal_in_days),
        milestones=Milestones(done=done, total=total),
    )


def _reading(
    account_id: str, kind: SignalKind | CustomKind, value: float, observed_at: datetime = READ_AT
) -> Signal:
    return Signal(
        account_id=account_id, kind=kind, value=value, observed_at=observed_at, source="test"
    )


def _detail(
    accounts: list[Account],
    signals: list[Signal],
    account_id: str,
    config: ScoringConfig = CONFIG,
):
    book = score_book(accounts, signals, config, AS_OF)
    detail = account_detail(book, account_id, signals, config, AS_OF)
    assert detail is not None
    return detail


def _row(detail, kind):
    return next(row for row in detail.breakdown if row.kind == kind)


class _Connector:
    """The two calls get_book makes, over fixed accounts and readings."""

    name = "test"

    def __init__(self, accounts: list[Account], signals: list[Signal]) -> None:
        self._accounts = accounts
        self._signals = signals

    def list_accounts(self) -> list[Account]:
        return list(self._accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        return [s for s in self._signals if account_id is None or s.account_id == account_id]


def test_the_built_in_tables_are_the_signal_info_tables():
    assert SIGNAL_LABELS is signal_info.BUILTIN_LABELS
    assert STALE_AFTER_DAYS is signal_info.BUILTIN_STALE_DAYS


# Breakdown and timeline


def test_breakdown_has_a_row_for_each_signal_the_book_scores_with_its_label():
    signals = [_reading("a", API, 120), _reading("a", USAGE, -10)]
    detail = _detail([_account("a")], signals, "a")

    assert len(detail.breakdown) == len(SignalKind) + 2
    assert {row.kind for row in detail.breakdown} == {*SignalKind, API, REPORTS}
    api = _row(detail, API)
    assert api.label == "API calls per week"
    assert api.value_text == "API calls per week: 120 calls"
    assert api.contribution == 25.3
    assert api.max_points == 30
    assert api.observed_at == READ_AT


def test_a_custom_signal_with_no_reading_shows_like_a_built_in_with_none():
    detail = _detail([_account("a")], [_reading("a", API, 120)], "a")

    reports = _row(detail, REPORTS)
    champion = _row(detail, CHAMPION)
    assert reports.label == "Reports exported"
    assert reports.max_points == 10
    assert (reports.value_text, reports.contribution, reports.observed_at) == (
        "No reading",
        0,
        None,
    )
    assert (champion.value_text, champion.contribution, champion.observed_at) == (
        "No reading",
        0,
        None,
    )


def test_non_zero_breakdown_rows_match_the_reasons_with_custom_signals():
    signals = [_reading("a", API, 120), _reading("a", USAGE, -10), _reading("a", REPORTS, 1)]
    detail = _detail([_account("a")], signals, "a")

    rows = [(row.kind, row.contribution) for row in detail.breakdown if row.contribution > 0]
    assert rows == [(reason.kind, reason.contribution) for reason in detail.scored.reasons]
    assert [reason.kind for reason in detail.scored.reasons] == [API, REPORTS, USAGE]


def test_the_timeline_labels_each_custom_reading_and_flags_only_the_latest():
    earlier = READ_AT - timedelta(days=14)
    signals = [_reading("a", API, 480, earlier), _reading("a", API, 120)]
    detail = _detail([_account("a")], signals, "a")

    newest, older = [entry for entry in detail.timeline if entry.kind == API]
    assert newest.label == older.label == "API calls per week"
    assert newest.plain_english == "API calls per week: 120 calls"
    assert older.plain_english == "API calls per week: 480 calls"
    assert newest.is_latest and not older.is_latest
    assert older.contribution == 1.3


# The action for the top reason


def test_the_top_custom_reason_gives_its_own_action():
    signals = [_reading("a", API, 120), _reading("a", USAGE, -10)]
    detail = _detail([_account("a")], signals, "a")

    assert detail.scored.reasons[0].kind == API
    action = suggest_action(detail.scored.motion, detail.scored.reasons, CONFIG)
    assert action == API_ACTION


def test_get_book_gives_the_top_custom_reason_its_action():
    connector = _Connector([_account("a")], [_reading("a", API, 120), _reading("a", USAGE, -10)])

    book = service.get_book(connector, CONFIG, AS_OF)

    assert book[0].suggested_action == API_ACTION


def test_a_custom_signal_without_an_action_gets_one_made_from_its_name():
    detail = _detail([_account("a")], [_reading("a", REPORTS, 0)], "a")

    assert detail.scored.reasons[0].kind == REPORTS
    assert suggest_action(detail.scored.motion, detail.scored.reasons, CONFIG) == (
        "Find out what is behind the reports exported reading and agree a fix."
    )


def test_without_a_config_a_custom_top_reason_gets_the_motions_fallback():
    detail = _detail([_account("a")], [_reading("a", API, 120)], "a")

    fallback = briefs._FALLBACK_ACTIONS[detail.scored.motion]
    assert suggest_action(detail.scored.motion, detail.scored.reasons) == fallback


# The onboarding brief


def _onboarding_account() -> Account:
    # Started 30 days ago with 4 of 6 milestones done: inside the onboarding window, not finished.
    return _account("a", start_date=AS_OF - timedelta(days=30), done=4, total=6)


def test_the_onboarding_brief_asks_the_custom_question_and_talks_the_custom_action():
    signals = [_reading("a", API, 120), _reading("a", REPORTS, 1)]
    detail = _detail([_onboarding_account()], signals, "a")

    brief = onboarding_brief(detail, MotionWindows(), CONFIG)

    assert brief.in_onboarding
    assert [risk.kind for risk in brief.risks] == [API, REPORTS]
    assert brief.questions == [
        "Who owns Pilot team trained, and when will it be done?",
        API_QUESTION,
        "What is behind the change in reports exported?",
        CLOSING_ONBOARDING,
    ]
    api_item = next(item for item in brief.agenda if item.topic == "API calls per week")
    assert api_item.detail == f"API calls per week: 120 calls. {API_ACTION}"


def test_a_custom_question_typed_without_a_question_mark_gets_one():
    config = ScoringConfig(
        custom=[
            CustomSignal(
                id="api_calls",
                name="API calls per week",
                unit="calls",
                rule=SignalRule(weight=30, start=500, full=50),
                question="Ask who owns the API keys.",
            )
        ]
    )
    detail = _detail([_onboarding_account()], [_reading("a", API, 120)], "a", config)

    brief = onboarding_brief(detail, MotionWindows(), config)

    assert "Ask who owns the API keys?" in brief.questions


def test_without_a_config_the_brief_still_reads_a_custom_risk_by_its_id():
    detail = _detail([_onboarding_account()], [_reading("a", API, 120)], "a")

    brief = onboarding_brief(detail, MotionWindows())

    assert "What is behind the change in api calls?" in brief.questions
    assert brief.questions[-1] == CLOSING_ONBOARDING


# What would it take


def test_what_would_it_take_names_the_custom_signal_and_the_reading_that_fixes_it():
    # 50 calls a week is full risk (30 points); a champion who left adds 25. That is At risk.
    signals = [_reading("a", API, 50), _reading("a", CHAMPION, 1)]
    scored = score_book([_account("a")], signals, CONFIG, AS_OF)[0]
    assert scored.health is Health.AT_RISK

    path = path_down(scored, latest_signals(signals)["a"], CONFIG, AS_OF)

    assert path is not None
    assert [fix.kind for fix in path.single] == [API, CHAMPION]
    api_fix = path.single[0]
    assert api_fix.label == "API calls per week"
    assert api_fix.now_text == "API calls per week: 50 calls"
    assert api_fix.goal_text == "Get API calls per week to 201 calls or more"
    assert path.summary == "Get API calls per week to 201 calls or more and this drops to Watch."
    assert path.not_enough == []


def test_what_would_it_take_says_down_to_for_a_signal_that_is_better_when_lower():
    days = CustomSignal(
        id="days_idle",
        name="Days since export",
        unit="days",
        rule=SignalRule(weight=30, start=7, full=67),
    )
    config = ScoringConfig(custom=[days])
    signals = [_reading("a", days.kind, 67), _reading("a", CHAMPION, 1)]
    scored = score_book([_account("a")], signals, config, AS_OF)[0]

    path = path_down(scored, latest_signals(signals)["a"], config, AS_OF)

    assert path is not None
    assert path.single[0].label == "Days since export"
    assert path.single[0].goal_text == "Get Days since export down to 46 days or less"


def _walk(value: float, start: float) -> Iterator[float]:
    """Every reading from value toward start, one whole unit at a time, then start itself: the
    search the bisection in insights must agree with."""
    if start > value:
        whole = math.floor(value) + 1
        while whole < start:
            yield float(whole)
            whole += 1
    else:
        whole = math.ceil(value) - 1
        while whole > start:
            yield float(whole)
            whole -= 1
    yield start


def _clears(reading: float, *, rising: bool, threshold: float) -> bool:
    """A test that turns from False to True once along the way (the shape insights relies on)."""
    return reading >= threshold if rising else reading <= threshold


def test_the_bisection_finds_the_first_reading_a_unit_by_unit_walk_would():
    values = [-3.5, 0.0, 0.5, 7.0, 7.5, 50.0, 66.5, 120.0]
    starts = [-20.0, 0.0, 2.5, 7.0, 51.0, 500.5]
    thresholds = [-30, -3.5, 0, 0.25, 3, 7, 7.5, 20, 45.5, 50, 66.5, 100, 400, 500.5, 1000]
    for value in values:
        for start in starts:
            if start == value:
                continue
            rising = start > value
            for threshold in thresholds:
                clears = partial(_clears, rising=rising, threshold=threshold)
                expected = next((step for step in _walk(value, start) if clears(step)), None)
                assert _first_enough(value, start, clears) == expected


def test_a_very_wide_range_gives_the_first_enough_reading_quickly():
    wide = CustomSignal(
        id="rows_synced",
        name="Rows synced",
        unit="rows",
        rule=SignalRule(weight=30, start=1_000_000, full=50),
    )
    config = ScoringConfig(custom=[wide])
    signals = [_reading("a", wide.kind, 50), _reading("a", CHAMPION, 1)]
    scored = score_book([_account("a")], signals, config, AS_OF)[0]
    assert scored.health is Health.AT_RISK

    path = path_down(scored, latest_signals(signals)["a"], config, AS_OF)

    assert path is not None
    target = path.single[0].target_value
    assert path.single[0].kind == wide.kind

    def health_at(reading: float) -> Health:
        readings = {
            wide.kind: _reading("a", wide.kind, reading),
            CHAMPION: _reading("a", CHAMPION, 1),
        }
        return score_account(scored.account, readings, config, AS_OF).health

    assert health_at(target) is not Health.AT_RISK
    assert health_at(target - 1) is Health.AT_RISK


def test_how_sure_counts_the_books_own_signals_and_flags_an_old_one():
    signals = [
        _reading("a", API, 120, READ_AT),
        _reading("a", REPORTS, 1, AS_OF - timedelta(days=45)),
    ]
    scored = score_book([_account("a")], signals, CONFIG, AS_OF)[0]

    confidence = score_confidence(scored, latest_signals(signals)["a"], AS_OF, CONFIG)

    labels = [age.label for age in confidence.ages]
    assert len(labels) == len(SignalKind) + 2
    assert labels[-2:] == ["API calls per week", "Reports exported"]
    assert confidence.relevant == len(SignalKind) + 2 - 1  # every kind but onboarding
    assert confidence.stale == ["Reports exported"]
    assert "Reports exported" not in confidence.missing
    assert "Out of date: Reports exported." in confidence.sentence
    report_age = next(age for age in confidence.ages if age.kind == REPORTS)
    assert report_age.stale is True
    assert next(age for age in confidence.ages if age.kind == API).stale is False


def test_growth_and_negotiation_read_the_books_own_signals_without_failing():
    signals = [_reading("a", API, 120), _reading("a", USAGE, -10), _reading("a", REPORTS, 1)]
    scored = score_book([_account("a")], signals, CONFIG, AS_OF)[0]
    latest = latest_signals(signals)["a"]
    built_in_only = {
        kind: reading for kind, reading in latest.items() if isinstance(kind, SignalKind)
    }

    assert growth_radar(scored, latest, None) == growth_radar(scored, built_in_only, None)
    sheet = negotiation_sheet(scored, latest, AccountJournal(), None, None, AS_OF)
    assert "API calls per week: 120 calls." in sheet.exposures


# A removed signal


def test_a_reading_of_a_removed_signal_reads_by_its_name_everywhere():
    config = ScoringConfig(custom=[CONFIG.custom[0]])  # the old metric is no longer in the book
    signals = [_reading("a", API, 120), _reading("a", OLD, 12, READ_AT - timedelta(days=3))]
    book = score_book([_account("a")], signals, config, AS_OF)
    scored = book[0]
    latest = latest_signals(signals)["a"]

    detail = account_detail(book, "a", signals, config, AS_OF)
    assert OLD not in {row.kind for row in detail.breakdown}
    old_entry = next(entry for entry in detail.timeline if entry.kind == OLD)
    assert old_entry.label == "Old metric"
    assert old_entry.plain_english == "Old metric: 12"
    assert old_entry.contribution == 0.0
    assert old_entry.is_latest
    assert all(reason.kind != OLD for reason in scored.reasons)

    removed_reason = Reason(kind=OLD, contribution=5.0, plain_english="Old metric: 12")
    assert suggest_action(scored.motion, [removed_reason], config) == (
        "Find out what is behind the old metric reading and agree a fix."
    )

    path = path_down(scored, latest, config, AS_OF)
    assert path is not None
    assert [fix.label for fix in path.single] == ["API calls per week"]
    assert path.not_enough == []

    confidence = score_confidence(scored, latest, AS_OF, config)
    assert "Old metric" not in [age.label for age in confidence.ages]
    unnamed = score_confidence(scored, latest, AS_OF)
    assert "Old metric" in [age.label for age in unnamed.ages]

    snapshot = snapshot_of(book, signals, AS_OF, READ_AT)
    assert snapshot.accounts["a"].signals[OLD] == 12


# Snapshots and outcomes


def test_a_snapshot_round_trips_a_custom_key_through_json():
    signals = [_reading("a", API, 120), _reading("a", USAGE, -10)]
    book = score_book([_account("a")], signals, CONFIG, AS_OF)
    snapshot = snapshot_of(book, signals, AS_OF, READ_AT)

    text = snapshot.model_dump_json()
    assert '"custom:api_calls":120.0' in text
    loaded = BookSnapshot.model_validate_json(text)

    entry = loaded.accounts["a"]
    assert entry.signals == {API: 120.0, USAGE: -10.0}
    assert {type(key) for key in entry.signals} == {CustomKind, SignalKind}
    assert snapshot.accounts["a"].signals == entry.signals


def test_a_snapshot_with_only_built_in_keys_still_loads():
    signals = [_reading("a", USAGE, -10)]
    book = score_book([_account("a")], signals, CONFIG, AS_OF)
    payload = json.loads(snapshot_of(book, signals, AS_OF, READ_AT).model_dump_json())
    payload["accounts"]["a"]["signals"] = {"usage_trend_30d": -10.0}

    loaded = BookSnapshot.model_validate_json(json.dumps(payload))

    assert loaded.accounts["a"].signals == {USAGE: -10.0}
    assert {type(key) for key in loaded.accounts["a"].signals} == {SignalKind}


def test_an_outcome_keeps_custom_readings_through_json():
    outcome = Outcome(
        id="o-1a2b3c4d",
        kind=OutcomeKind.RENEWED,
        renewal_date=AS_OF + timedelta(days=200),
        decided_on=AS_OF,
        arr_before=50_000,
        risk=37.8,
        priority=20.0,
        health=Health.WATCH,
        motion=Motion.CHURN,
        signals={USAGE: -10.0, API: 120.0},
        recorded_at=READ_AT,
    )

    loaded = Outcome.model_validate_json(outcome.model_dump_json())

    assert loaded.signals == {USAGE: -10.0, API: 120.0}
    assert {type(key) for key in loaded.signals} == {SignalKind, CustomKind}
