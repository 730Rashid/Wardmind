from __future__ import annotations

import json
import random
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from cs_brief.core.config import (
    DEFAULT_SIGNAL_RULES,
    ArrScaling,
    ScoringConfig,
    SignalRule,
    Thresholds,
)
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.models import Account, Health, Milestones, Motion, Reason, Signal, SignalKind
from cs_brief.core.scoring import (
    arr_factor,
    classify_health,
    describe,
    latest_signals,
    normalise,
    score_account,
    score_book,
)

AS_OF = date(2026, 10, 19)
NOW = datetime(2026, 10, 18, 9, 0)
SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "demo_top10.json"
DEFAULT_ARR = ArrScaling()  # weight 0.3, floor 5,000, ceiling 500,000

FALLING = DEFAULT_SIGNAL_RULES[SignalKind.USAGE_TREND_30D]  # start 0, full -50
RISING = DEFAULT_SIGNAL_RULES[SignalKind.DAYS_SINCE_LAST_LOGIN]  # start 7, full 30

# Each kind at its `full` value, which is the strongest reading it can have.
FULL_TRIGGER: list[tuple[SignalKind, float, str]] = [
    (SignalKind.USAGE_TREND_30D, -50.0, "Active users down 50% in 30 days"),
    (SignalKind.DAYS_SINCE_LAST_LOGIN, 30.0, "No one has logged in for 30 days"),
    (SignalKind.OPEN_TICKETS_SEV1, 3.0, "3 open severity-1 tickets"),
    (SignalKind.NPS_LATEST, 0.0, "Latest NPS response was 0 out of 10"),
    (SignalKind.CHAMPION_LEFT, 1.0, "Champion has left the company"),
    (SignalKind.SEAT_UTILISATION, 30.0, "Only 30% of seats in use"),
    (SignalKind.BILLING_OVERDUE_DAYS, 60.0, "Invoice 60 days overdue"),
    (SignalKind.ONBOARDING_OVERDUE_MILESTONES, 3.0, "3 onboarding milestones overdue"),
]


def _account(id: str = "acc-x", name: str = "Test Co", arr: float = 50_000) -> Account:
    return Account(
        id=id,
        name=name,
        arr=arr,
        tier="Growth",
        owner="Owner",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 6, 1),
        milestones=Milestones(done=5, total=5),
    )


def _signal(
    kind: SignalKind,
    value: float,
    account_id: str = "acc-x",
    observed_at: datetime = NOW,
) -> Signal:
    return Signal(account_id=account_id, kind=kind, value=value, observed_at=observed_at)


def _demo_book(config: ScoringConfig | None = None) -> list:
    connector = DemoConnector(today=AS_OF)
    return score_book(
        connector.list_accounts(),
        connector.list_signals(),
        config or ScoringConfig(),
        AS_OF,
    )


# normalise


def test_normalise_falling_rule_maps_start_to_0_and_full_to_1():
    assert normalise(0, FALLING) == 0.0
    assert normalise(-50, FALLING) == 1.0
    assert normalise(-25, FALLING) == pytest.approx(0.5)


def test_normalise_rising_rule_maps_start_to_0_and_full_to_1():
    assert normalise(7, RISING) == 0.0
    assert normalise(30, RISING) == 1.0
    assert normalise(18.5, RISING) == pytest.approx(0.5)


def test_normalise_midpoint_is_half_in_both_directions():
    rising = SignalRule(weight=10, start=20, full=60)
    falling = SignalRule(weight=10, start=100, full=0)
    assert normalise(40, rising) == pytest.approx(0.5)
    assert normalise(50, falling) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("rule", "value", "expected"),
    [
        (FALLING, 10.0, 0.0),  # past start, the wrong way: clamped to 0
        (FALLING, -80.0, 1.0),  # past full: clamped to 1
        (RISING, 3.0, 0.0),
        (RISING, 40.0, 1.0),
    ],
    ids=["falling-below-start", "falling-beyond-full", "rising-below-start", "rising-beyond-full"],
)
def test_normalise_clamps_outside_the_range(rule, value, expected):
    assert normalise(value, rule) == expected


# classify_health


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (0.0, Health.HEALTHY),
        (19.9, Health.HEALTHY),
        (20.0, Health.WATCH),
        (44.9, Health.WATCH),
        (45.0, Health.AT_RISK),
        (100.0, Health.AT_RISK),
    ],
)
def test_health_boundaries_are_inclusive(risk, expected):
    assert classify_health(risk, Thresholds()) is expected


# arr_factor


def test_arr_factor_at_floor_is_the_minimum():
    assert arr_factor(5_000, DEFAULT_ARR) == pytest.approx(0.7)


def test_arr_factor_at_ceiling_is_one():
    assert arr_factor(500_000, DEFAULT_ARR) == pytest.approx(1.0)


def test_arr_factor_below_floor_is_clamped_to_the_minimum():
    assert arr_factor(1_000, DEFAULT_ARR) == pytest.approx(0.7)


def test_arr_factor_above_ceiling_is_clamped_to_one():
    assert arr_factor(2_000_000, DEFAULT_ARR) == pytest.approx(1.0)


def test_arr_factor_with_zero_weight_is_always_one():
    flat = ArrScaling(weight=0)
    for arr in (0, 1_000, 50_000, 10_000_000):
        assert arr_factor(arr, flat) == 1.0


def test_arr_factor_for_zero_arr_uses_the_minimum():
    assert arr_factor(0, DEFAULT_ARR) == pytest.approx(0.7)


def test_arr_factor_is_log_scaled():
    # 50,000 is the geometric midpoint of 5,000 and 500,000, so arr_norm is 0.5.
    assert arr_factor(50_000, DEFAULT_ARR) == pytest.approx(0.85)


# describe


@pytest.mark.parametrize(
    ("kind", "value", "text"),
    [
        (SignalKind.USAGE_TREND_30D, -38.0, "Active users down 38% in 30 days"),
        (SignalKind.USAGE_TREND_30D, 12.0, "Active users up 12% in 30 days"),
        (SignalKind.USAGE_TREND_30D, 12.5, "Active users up 13% in 30 days"),
        (SignalKind.USAGE_TREND_30D, 0.0, "Active users flat over 30 days"),
        (SignalKind.USAGE_TREND_30D, -0.4, "Active users flat over 30 days"),
        (SignalKind.DAYS_SINCE_LAST_LOGIN, 0.0, "Last login today"),
        (SignalKind.DAYS_SINCE_LAST_LOGIN, 1.0, "No one has logged in for 1 day"),
        (SignalKind.DAYS_SINCE_LAST_LOGIN, 23.0, "No one has logged in for 23 days"),
        (SignalKind.OPEN_TICKETS_SEV1, 0.0, "No open severity-1 tickets"),
        (SignalKind.OPEN_TICKETS_SEV1, 1.0, "1 open severity-1 ticket"),
        (SignalKind.OPEN_TICKETS_SEV1, 3.0, "3 open severity-1 tickets"),
        (SignalKind.NPS_LATEST, 4.0, "Latest NPS response was 4 out of 10"),
        (SignalKind.CHAMPION_LEFT, 0.0, "Champion still in place"),
        (SignalKind.CHAMPION_LEFT, 1.0, "Champion has left the company"),
        (SignalKind.SEAT_UTILISATION, 34.0, "Only 34% of seats in use"),
        (SignalKind.SEAT_UTILISATION, 84.0, "84% of seats in use"),
        (SignalKind.SEAT_UTILISATION, 69.6, "70% of seats in use"),  # rounds up past the cut-off
        (SignalKind.BILLING_OVERDUE_DAYS, 0.0, "Billing up to date"),
        (SignalKind.BILLING_OVERDUE_DAYS, 1.0, "Invoice 1 day overdue"),
        (SignalKind.BILLING_OVERDUE_DAYS, 45.0, "Invoice 45 days overdue"),
        (SignalKind.ONBOARDING_OVERDUE_MILESTONES, 0.0, "Onboarding milestones on track"),
        (SignalKind.ONBOARDING_OVERDUE_MILESTONES, 1.0, "1 onboarding milestone overdue"),
        (SignalKind.ONBOARDING_OVERDUE_MILESTONES, 2.0, "2 onboarding milestones overdue"),
    ],
)
def test_describe_wording(kind, value, text):
    assert describe(kind, value) == text


@pytest.mark.parametrize(
    ("kind", "value", "text"),
    FULL_TRIGGER,
    ids=[kind.value for kind, _, _ in FULL_TRIGGER],
)
def test_fully_triggered_signal_contributes_its_weight(kind, value, text):
    weight = DEFAULT_SIGNAL_RULES[kind].weight
    scored = score_account(_account(), {kind: _signal(kind, value)}, ScoringConfig(), AS_OF)
    assert scored.risk == pytest.approx(weight)
    assert scored.reasons == [Reason(kind=kind, contribution=weight, plain_english=text)]


# score_account


def test_risk_is_capped_at_100_while_reasons_keep_their_contributions():
    signals = {kind: _signal(kind, value) for kind, value, _ in FULL_TRIGGER}
    scored = score_account(_account(arr=500_000), signals, ScoringConfig(), AS_OF)
    assert scored.risk == 100.0
    assert sum(r.contribution for r in scored.reasons) == pytest.approx(150.0)
    assert scored.priority == 100.0
    assert scored.health is Health.AT_RISK


def test_account_with_no_signals_has_zero_risk_and_no_reasons():
    scored = score_account(_account(), {}, ScoringConfig(), AS_OF)
    assert scored.risk == 0.0
    assert scored.priority == 0.0
    assert scored.health is Health.HEALTHY
    assert scored.reasons == []
    assert scored.suggested_action == ""


def test_priority_is_risk_scaled_by_arr_factor():
    signals = {SignalKind.OPEN_TICKETS_SEV1: _signal(SignalKind.OPEN_TICKETS_SEV1, 3.0)}  # 20
    small = score_account(_account(arr=5_000), signals, ScoringConfig(), AS_OF)
    assert small.risk == pytest.approx(20.0)
    assert small.priority == pytest.approx(14.0)  # 20 * 0.7


def test_reasons_exclude_zero_contributions_and_are_sorted():
    signals = {
        SignalKind.USAGE_TREND_30D: _signal(SignalKind.USAGE_TREND_30D, -50.0),  # 25.0
        SignalKind.DAYS_SINCE_LAST_LOGIN: _signal(SignalKind.DAYS_SINCE_LAST_LOGIN, 7.0),  # 0
        SignalKind.OPEN_TICKETS_SEV1: _signal(SignalKind.OPEN_TICKETS_SEV1, 1.0),  # 6.7
        SignalKind.NPS_LATEST: _signal(SignalKind.NPS_LATEST, 7.0),  # 0
    }
    scored = score_account(_account(), signals, ScoringConfig(), AS_OF)
    assert [r.kind for r in scored.reasons] == [
        SignalKind.USAGE_TREND_30D,
        SignalKind.OPEN_TICKETS_SEV1,
    ]
    assert [r.contribution for r in scored.reasons] == [pytest.approx(25.0), pytest.approx(6.7)]
    assert scored.risk == pytest.approx(31.7)


def test_equal_contributions_sort_by_kind_value():
    signals = {
        SignalKind.USAGE_TREND_30D: _signal(SignalKind.USAGE_TREND_30D, -50.0),  # 25.0
        SignalKind.CHAMPION_LEFT: _signal(SignalKind.CHAMPION_LEFT, 1.0),  # 25.0
    }
    scored = score_account(_account(), signals, ScoringConfig(), AS_OF)
    assert [r.kind for r in scored.reasons] == [
        SignalKind.CHAMPION_LEFT,  # "champion_left" sorts before "usage_trend_30d"
        SignalKind.USAGE_TREND_30D,
    ]


def test_changing_a_weight_in_a_custom_config_changes_the_risk():
    signals = {SignalKind.OPEN_TICKETS_SEV1: _signal(SignalKind.OPEN_TICKETS_SEV1, 1.0)}
    default = score_account(_account(), signals, ScoringConfig(), AS_OF)
    assert default.risk == pytest.approx(6.7)  # 20 * (1 / 3)

    custom = ScoringConfig.model_validate(
        {"signals": {"open_tickets_sev1": {"weight": 40, "start": 0, "full": 3}}}
    )
    assert score_account(_account(), signals, custom, AS_OF).risk == pytest.approx(13.3)


# latest_signals and score_book


def test_latest_signal_wins_over_an_older_worse_reading():
    older = _signal(SignalKind.USAGE_TREND_30D, -50.0, observed_at=NOW - timedelta(days=15))
    newer = _signal(SignalKind.USAGE_TREND_30D, 5.0, observed_at=NOW)
    assert latest_signals([older, newer])["acc-x"][SignalKind.USAGE_TREND_30D] == newer
    assert latest_signals([newer, older])["acc-x"][SignalKind.USAGE_TREND_30D] == newer
    for order in ([older, newer], [newer, older]):
        scored = score_book([_account()], order, ScoringConfig(), AS_OF)[0]
        assert scored.risk == 0.0
        assert scored.reasons == []


def test_same_timestamp_resolves_the_same_way_in_either_order():
    a = _signal(SignalKind.OPEN_TICKETS_SEV1, 1.0)
    b = _signal(SignalKind.OPEN_TICKETS_SEV1, 2.0)
    assert latest_signals([a, b]) == latest_signals([b, a])


def test_signals_for_unknown_accounts_are_ignored():
    stray = [_signal(SignalKind.USAGE_TREND_30D, -50.0, account_id="ghost")]
    book = score_book([_account(id="acc-x")], stray, ScoringConfig(), AS_OF)
    assert [s.account.id for s in book] == ["acc-x"]
    assert book[0].risk == 0.0


def test_priority_ranks_a_small_account_at_risk_above_a_large_healthy_one():
    big_healthy = _account(id="big", name="Big", arr=500_000)
    small_at_risk = _account(id="small", name="Small", arr=6_000)
    signals = [_signal(kind, value, account_id="small") for kind, value, _ in FULL_TRIGGER]
    book = score_book([big_healthy, small_at_risk], signals, ScoringConfig(), AS_OF)
    assert [s.account.id for s in book] == ["small", "big"]


def test_book_tie_breaks_by_priority_then_arr_then_name():
    accounts = [
        _account(id="a", name="Beta", arr=100_000),
        _account(id="b", name="Alpha", arr=100_000),
        _account(id="c", name="Gamma", arr=200_000),
        _account(id="d", name="Delta", arr=50_000),
    ]
    # No signals, so every priority is 0 and only ARR then name can order the book.
    book = score_book(accounts, [], ScoringConfig(), AS_OF)
    assert [s.account.id for s in book] == ["c", "b", "a", "d"]


def test_scoring_twice_gives_equal_results():
    assert _demo_book() == _demo_book()


def test_shuffled_input_gives_an_identical_book():
    connector = DemoConnector(today=AS_OF)
    accounts = connector.list_accounts()
    signals = connector.list_signals()
    expected = score_book(accounts, signals, ScoringConfig(), AS_OF)

    rng = random.Random(7)
    shuffled_accounts = accounts[:]
    shuffled_signals = signals[:]
    rng.shuffle(shuffled_accounts)
    rng.shuffle(shuffled_signals)
    assert score_book(shuffled_accounts, shuffled_signals, ScoringConfig(), AS_OF) == expected


# Demo book cross-checks


def test_demo_book_health_counts():
    counts = Counter(s.health for s in _demo_book())
    assert counts == {Health.HEALTHY: 18, Health.WATCH: 12, Health.AT_RISK: 10}


def test_demo_book_motion_counts():
    counts = Counter(s.motion for s in _demo_book())
    assert counts == {Motion.ONBOARDING: 8, Motion.RENEWAL: 12, Motion.CHURN: 20}


def test_demo_top10_matches_snapshot():
    top10 = [
        {
            "id": s.account.id,
            "priority": s.priority,
            "health": s.health.value,
            "motion": s.motion.value,
        }
        for s in _demo_book()[:10]
    ]
    if not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(top10, indent=2) + "\n"
        SNAPSHOT.write_bytes(text.encode("utf-8"))  # bytes, so the line endings stay LF on Windows
        pytest.fail(f"Snapshot was missing and has been written to {SNAPSHOT}. Rerun pytest.")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert top10 == expected
