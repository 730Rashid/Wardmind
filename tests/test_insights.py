from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from wardmind.core import service
from wardmind.core.briefs import SIGNAL_LABELS
from wardmind.core.config import (
    DEFAULT_SIGNAL_RULES,
    MotionWindows,
    ScoringConfig,
    SignalRule,
    Thresholds,
)
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.insights import (
    MIN_FORECAST_DAYS,
    SHELFWARE_MIN_UNUSED,
    STALE_AFTER_DAYS,
    Confidence,
    Fix,
    GoLiveStatus,
    PathDown,
    ScoreConfidence,
    Shelfware,
    book_confidence,
    golive_forecast,
    path_down,
    renewal_shelfware,
    score_confidence,
    shelfware,
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
from wardmind.core.scoring import latest_signals, score_account, score_book

AS_OF = date(2026, 10, 19)
READ_AT = datetime(2026, 10, 18, 9, 0)
WINDOWS = MotionWindows()  # onboarding 90 days, renewal window 120 days
EXPECT = "Expect a downsell ask; offering to right-size first keeps the conversation yours."
PLAN = "Plan a seat review before the renewal, so a downsell isn't a surprise."

USAGE = SignalKind.USAGE_TREND_30D
DAYS = SignalKind.DAYS_SINCE_LAST_LOGIN
OPEN = SignalKind.OPEN_TICKETS_SEV1
NPS = SignalKind.NPS_LATEST
CHAMPION = SignalKind.CHAMPION_LEFT
SEATS = SignalKind.SEAT_UTILISATION
BILLING = SignalKind.BILLING_OVERDUE_DAYS
MILESTONES = SignalKind.ONBOARDING_OVERDUE_MILESTONES

# A reading per kind that adds no risk, so an account built from these is Healthy.
NO_RISK: dict[SignalKind, float] = {
    USAGE: 0.0,
    DAYS: 7.0,
    OPEN: 0.0,
    NPS: 7.0,
    CHAMPION: 0.0,
    SEATS: 70.0,
    BILLING: 0.0,
    MILESTONES: 0.0,
}


def _account(
    id: str = "acc-x",
    *,
    name: str | None = None,
    arr: float = 50_000,
    start_date: date | None = date(2025, 1, 1),
    renewal_in_days: int = 400,
    done: int = 4,
    total: int = 4,
) -> Account:
    return Account(
        id=id,
        name=name or f"Account {id}",
        arr=arr,
        tier="Growth",
        owner="Owner",
        start_date=start_date,
        renewal_date=AS_OF + timedelta(days=renewal_in_days),
        milestones=Milestones(done=done, total=total),
    )


def _reading(kind: SignalKind, value: float, account_id: str = "acc-x") -> Signal:
    return Signal(account_id=account_id, kind=kind, value=value, observed_at=READ_AT)


def _latest(values: dict[SignalKind, float]) -> dict[SignalKind, Signal]:
    return {kind: _reading(kind, value) for kind, value in values.items()}


def _custom(rules: dict[SignalKind, SignalRule]) -> ScoringConfig:
    return ScoringConfig(signals={**DEFAULT_SIGNAL_RULES, **rules})


def _case(
    values: dict[SignalKind, float], config: ScoringConfig | None = None
) -> tuple[ScoredAccount, dict[SignalKind, Signal], ScoringConfig]:
    config = ScoringConfig() if config is None else config
    latest = _latest(values)
    scored = score_account(_account(), latest, config, AS_OF)
    return scored, latest, config


def _down(values: dict[SignalKind, float], config: ScoringConfig | None = None) -> PathDown | None:
    scored, latest, config = _case(values, config)
    return path_down(scored, latest, config, AS_OF)


def _rescored(
    scored: ScoredAccount,
    latest: dict[SignalKind, Signal],
    config: ScoringConfig,
    changes: dict[SignalKind, float],
) -> ScoredAccount:
    """The account re-scored with some readings replaced by new values."""
    readings = dict(latest)
    for kind, value in changes.items():
        readings[kind] = latest[kind].model_copy(update={"value": value})
    return score_account(scored.account, readings, config, AS_OF)


# path_down


def test_healthy_account_has_no_path_down():
    assert _down({}) is None
    assert _down({OPEN: 1.0}) is None  # 6.7 risk, still Healthy


def test_at_risk_partial_ticket_fix_comes_before_the_full_clear():
    result = _down({CHAMPION: 1.0, OPEN: 3.0})
    assert result is not None
    assert result.current is Health.AT_RISK
    assert result.target is Health.WATCH
    assert result.risk == 45.0
    assert result.threshold == 45  # thresholds.at_risk
    assert result.points_needed == 0.1
    tickets, champion = result.single
    assert tickets == Fix(
        kind=OPEN,
        label="Severity-1 tickets",
        now_text="3 open severity-1 tickets",
        goal_text="Close 1 of the 3 severity-1 tickets",
        target_value=2.0,
        points_off=6.7,
    )
    assert champion == Fix(
        kind=CHAMPION,
        label="Champion",
        now_text="Champion has left the company",
        goal_text="Find and confirm a new champion",
        target_value=0.0,
        points_off=25.0,
    )
    assert result.together == []
    assert result.not_enough == []
    assert result.reachable is True
    assert result.summary == "Close 1 of the 3 severity-1 tickets and this drops to Watch."


def test_watch_account_reaches_healthy_with_a_partial_usage_drop():
    result = _down({USAGE: -50.0})
    assert result is not None
    assert result.current is Health.WATCH
    assert result.target is Health.HEALTHY
    assert result.risk == 25.0
    assert result.threshold == 20  # thresholds.watch
    assert result.points_needed == 5.1
    assert result.single == [
        Fix(
            kind=USAGE,
            label="Usage trend",
            now_text="Active users down 50% in 30 days",
            goal_text="Get the 30-day usage drop down to 39% or less",
            target_value=-39.0,
            points_off=5.5,
        )
    ]
    assert result.summary == (
        "Get the 30-day usage drop down to 39% or less and this drops to Healthy."
    )


def test_single_fixes_are_ordered_by_share_of_range_moved_then_signal_order():
    result = _down({OPEN: 3.0, MILESTONES: 3.0, NPS: 0.0})
    assert result is not None
    assert result.risk == 55.0
    # Tickets and milestones each move 2/3 of their range; NPS moves 5/7 of its range. By
    # SignalKind order NPS would sit between the two, so this checks the share sort first.
    assert [fix.kind for fix in result.single] == [OPEN, MILESTONES, NPS]
    assert [fix.target_value for fix in result.single] == [1.0, 1.0, 5.0]
    assert [fix.points_off for fix in result.single] == [13.3, 13.3, 10.7]
    assert [fix.goal_text for fix in result.single] == [
        "Close 2 of the 3 severity-1 tickets",
        "Complete 2 of the 3 overdue milestones",
        "Get an NPS response of 5 or more",
    ]
    assert result.summary == "Close 2 of the 3 severity-1 tickets and this drops to Watch."


def test_together_when_no_single_change_is_enough():
    result = _down({USAGE: -50.0, CHAMPION: 1.0, BILLING: 60.0, OPEN: 3.0})
    assert result is not None
    assert result.risk == 90.0
    assert result.single == []
    assert [fix.kind for fix in result.together] == [USAGE, CHAMPION]  # SignalKind order
    assert [fix.target_value for fix in result.together] == [0.0, 0.0]
    assert [fix.points_off for fix in result.together] == [50.0, 50.0]
    assert result.points_needed == 45.1
    assert result.not_enough == ["Champion", "Usage trend", "Billing", "Severity-1 tickets"]
    assert result.reachable is True
    assert result.summary == (
        "No single fix is enough. Get active users back to flat or growing "
        "and find and confirm a new champion together would drop it to Watch."
    )


def test_together_takes_the_fewest_signals_even_when_a_bigger_set_moves_less():
    # Risk 78, so the account needs to fall under 45. Any one kind leaves it at least 57.
    # USAGE + CHAMPION (42 points) is the only pair that is enough. The three smaller kinds
    # together (36 points) are enough too, and their total share (1.5) is lower than the
    # pair's (2.0), but they are three signals, so the pair wins.
    config = _custom(
        {
            USAGE: SignalRule(weight=21, start=0, full=1),
            CHAMPION: SignalRule(weight=21, start=0, full=1),
            OPEN: SignalRule(weight=24, start=0, full=1),
            BILLING: SignalRule(weight=24, start=0, full=1),
            MILESTONES: SignalRule(weight=24, start=0, full=1),
        }
    )
    result = _down(
        {USAGE: 1.0, CHAMPION: 1.0, OPEN: 0.5, BILLING: 0.5, MILESTONES: 0.5},
        config,
    )
    assert result is not None
    assert result.risk == 78.0
    assert result.single == []
    assert [fix.kind for fix in result.together] == [USAGE, CHAMPION]
    assert [fix.points_off for fix in result.together] == [42.0, 42.0]


def test_together_ties_on_total_share_go_to_the_set_whose_kinds_come_first():
    # Four kinds, each worth 20 points at half its range, so every pair has the same share (1.0).
    config = _custom(
        {kind: SignalRule(weight=40, start=0, full=1) for kind in (USAGE, OPEN, CHAMPION, BILLING)}
    )
    result = _down({USAGE: 0.5, OPEN: 0.5, CHAMPION: 0.5, BILLING: 0.5}, config)
    assert result is not None
    assert result.risk == 80.0
    assert [fix.kind for fix in result.together] == [USAGE, OPEN]
    assert [fix.points_off for fix in result.together] == [40.0, 40.0]


def test_together_prefers_the_smallest_total_share_over_signal_order():
    # Every pair is enough. USAGE + OPEN comes first in SignalKind order, but OPEN + BILLING
    # moves less of its range (0.5 + 0.5 against 1.0 + 0.5), so it wins.
    config = _custom(
        {
            USAGE: SignalRule(weight=20, start=0, full=1),
            OPEN: SignalRule(weight=40, start=0, full=1),
            CHAMPION: SignalRule(weight=20, start=0, full=1),
            BILLING: SignalRule(weight=40, start=0, full=1),
        }
    )
    result = _down({USAGE: 1.0, OPEN: 0.5, CHAMPION: 1.0, BILLING: 0.5}, config)
    assert result is not None
    assert [fix.kind for fix in result.together] == [OPEN, BILLING]


def test_the_100_point_cap_is_counted_in_the_drop():
    # Five kinds add up to 110, capped to 100. Clearing USAGE, OPEN and CHAMPION leaves 40 (the
    # capped risk drops from 100 to 40), so the points off are 60, not the 70 uncapped.
    result = _down({USAGE: -50.0, CHAMPION: 1.0, BILLING: 60.0, OPEN: 3.0, MILESTONES: 3.0})
    assert result is not None
    assert result.risk == 100.0
    assert result.points_needed == 55.1
    assert result.single == []
    assert [fix.kind for fix in result.together] == [USAGE, OPEN, CHAMPION]
    assert [fix.points_off for fix in result.together] == [60.0, 60.0, 60.0]
    assert result.summary == (
        "No single fix is enough. Get active users back to flat or growing, "
        "close all 3 severity-1 tickets and find and confirm a new champion "
        "together would drop it to Watch."
    )


def test_not_reachable_when_healthy_is_out_of_reach():
    # With thresholds.watch at 0, risk 0 is still Watch, so no account can be Healthy.
    result = _down({OPEN: 1.0}, ScoringConfig(thresholds=Thresholds(watch=0)))
    assert result is not None
    assert result.current is Health.WATCH
    assert result.target is Health.HEALTHY
    assert result.risk == 6.7
    assert result.threshold == 0
    assert result.points_needed == 6.8
    assert result.single == []
    assert result.together == []
    assert result.not_enough == ["Severity-1 tickets"]
    assert result.reachable is False
    assert result.summary == "Clearing every signal still wouldn't drop it to Healthy."


@pytest.mark.parametrize(
    ("kind", "rule", "value", "goal"),
    [
        pytest.param(
            USAGE,
            SignalRule(weight=100, start=0, full=-1),
            -1.0,
            "Get active users back to flat or growing",
            id="usage-flat",
        ),
        pytest.param(
            USAGE,
            SignalRule(weight=100, start=0, full=-50),
            -50.0,
            "Get the 30-day usage drop down to 22% or less",
            id="usage-drop",
        ),
        pytest.param(
            DAYS,
            SignalRule(weight=100, start=7, full=30),
            30.0,
            "Get someone to log in",
            id="days-login",
        ),
        pytest.param(
            OPEN,
            SignalRule(weight=100, start=0, full=1),
            1.0,
            "Close the severity-1 ticket",
            id="tickets-one-to-zero",
        ),
        pytest.param(
            OPEN,
            SignalRule(weight=100, start=0, full=1),
            2.0,
            "Close both severity-1 tickets",
            id="tickets-both-to-zero",
        ),
        pytest.param(
            OPEN,
            SignalRule(weight=100, start=0, full=1),
            3.0,
            "Close all 3 severity-1 tickets",
            id="tickets-all-to-zero",
        ),
        pytest.param(
            OPEN,
            SignalRule(weight=100, start=0, full=10),
            8.0,
            "Close 4 of the 8 severity-1 tickets",
            id="tickets-some",
        ),
        pytest.param(
            NPS,
            SignalRule(weight=100, start=7, full=0),
            0.0,
            "Get an NPS response of 4 or more",
            id="nps",
        ),
        pytest.param(
            CHAMPION,
            SignalRule(weight=100, start=0, full=1),
            1.0,
            "Find and confirm a new champion",
            id="champion",
        ),
        pytest.param(
            SEATS,
            SignalRule(weight=100, start=70, full=30),
            30.0,
            "Get seat use up to 53%",
            id="seats",
        ),
        pytest.param(
            BILLING,
            SignalRule(weight=100, start=0, full=60),
            60.0,
            "Get the overdue invoice paid",
            id="billing",
        ),
        pytest.param(
            MILESTONES,
            SignalRule(weight=100, start=0, full=1),
            1.0,
            "Complete the overdue milestone",
            id="milestones-one-to-zero",
        ),
        pytest.param(
            MILESTONES,
            SignalRule(weight=100, start=0, full=1),
            2.0,
            "Complete both overdue milestones",
            id="milestones-both-to-zero",
        ),
        pytest.param(
            MILESTONES,
            SignalRule(weight=100, start=0, full=1),
            3.0,
            "Complete all 3 overdue milestones",
            id="milestones-all-to-zero",
        ),
        pytest.param(
            MILESTONES,
            SignalRule(weight=100, start=0, full=3),
            3.0,
            "Complete 2 of the 3 overdue milestones",
            id="milestones-some",
        ),
    ],
)
def test_goal_wording_for_each_branch(kind, rule, value, goal):
    result = _down({kind: value}, _custom({kind: rule}))
    assert result is not None
    assert [fix.goal_text for fix in result.single] == [goal]


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({CHAMPION: 1.0, OPEN: 3.0}, id="tickets-and-champion"),
        pytest.param({USAGE: -50.0}, id="usage-only"),
        pytest.param({OPEN: 3.0, MILESTONES: 3.0, NPS: 0.0}, id="three-way"),
    ],
)
def test_each_single_fix_is_enough_and_one_step_less_is_not(values):
    scored, latest, config = _case(values)
    result = path_down(scored, latest, config, AS_OF)
    assert result is not None
    assert result.single
    for fix in result.single:
        rule = config.signals[fix.kind]
        current = latest[fix.kind].value
        step_back = fix.target_value - (1 if rule.start > current else -1)
        enough = _rescored(scored, latest, config, {fix.kind: fix.target_value})
        one_less = _rescored(scored, latest, config, {fix.kind: step_back})
        assert enough.health in (result.target, Health.HEALTHY)
        assert one_less.health not in (result.target, Health.HEALTHY)
        assert fix.points_off == round(scored.risk - enough.risk, 1)


# score_confidence


def _relevant(motion: Motion) -> list[SignalKind]:
    """The kinds that count toward confidence: milestones only count during onboarding."""
    return [kind for kind in SignalKind if motion is Motion.ONBOARDING or kind is not MILESTONES]


def _dated(
    kind: SignalKind, days_old: int, value: float | None = None, account_id: str = "acc-x"
) -> Signal:
    """A reading taken `days_old` days before AS_OF (a negative number means after it)."""
    taken = datetime.combine(AS_OF - timedelta(days=days_old), time(9, 0))
    reading = NO_RISK[kind] if value is None else value
    return Signal(account_id=account_id, kind=kind, value=reading, observed_at=taken)


def _motion_account(motion: Motion) -> Account:
    if motion is Motion.ONBOARDING:
        return _account(start_date=AS_OF - timedelta(days=10), done=2, total=5)
    if motion is Motion.RENEWAL:
        return _account(renewal_in_days=30)
    return _account()  # churn: started long ago, renewal far off


def _confidence(motion: Motion, readings: list[Signal]) -> ScoreConfidence:
    latest = {reading.kind: reading for reading in readings}
    scored = score_account(_motion_account(motion), latest, ScoringConfig(), AS_OF)
    assert scored.motion is motion
    return score_confidence(scored, latest, AS_OF)


def test_full_when_every_relevant_signal_was_read_today():
    readings = [_dated(kind, 0) for kind in _relevant(Motion.CHURN)]
    result = _confidence(Motion.CHURN, readings)
    assert result.level is Confidence.FULL
    assert result.relevant == 7
    assert result.fresh == 7
    assert result.missing == []
    assert result.stale == []
    assert result.oldest_age_days == 0
    assert result.sentence == "Based on all 7 signals, all read today."
    assert result.blind_spot is False


def test_onboarding_counts_the_milestones_signal_as_relevant():
    everything = _confidence(Motion.ONBOARDING, [_dated(kind, 0) for kind in SignalKind])
    assert everything.relevant == 8
    assert everything.level is Confidence.FULL
    assert everything.sentence == "Based on all 8 signals, all read today."

    without = _confidence(
        Motion.ONBOARDING,
        [_dated(kind, 0) for kind in SignalKind if kind is not MILESTONES],
    )
    assert without.relevant == 8
    assert without.missing == ["Onboarding milestones"]
    assert without.level is Confidence.PARTIAL
    assert without.sentence == "Based on 7 of 8 signals. No reading for Onboarding milestones."


def test_outside_onboarding_an_old_milestones_reading_does_not_count():
    readings = [_dated(kind, 0) for kind in _relevant(Motion.CHURN)]
    readings.append(_dated(MILESTONES, 400))
    result = _confidence(Motion.CHURN, readings)
    assert result.level is Confidence.FULL
    assert result.relevant == 7
    assert result.oldest_age_days == 0
    assert result.stale == []
    assert result.ages[list(SignalKind).index(MILESTONES)].stale is True  # still reported


@pytest.mark.parametrize(
    ("motion", "fresh", "level"),
    [
        pytest.param(Motion.ONBOARDING, 4, Confidence.THIN, id="onboarding-4-of-8-thin"),
        pytest.param(Motion.ONBOARDING, 5, Confidence.PARTIAL, id="onboarding-5-of-8-partial"),
        pytest.param(Motion.ONBOARDING, 8, Confidence.FULL, id="onboarding-8-of-8-full"),
        pytest.param(Motion.CHURN, 3, Confidence.THIN, id="churn-3-of-7-thin"),
        pytest.param(Motion.CHURN, 4, Confidence.PARTIAL, id="churn-4-of-7-partial"),
        pytest.param(Motion.CHURN, 7, Confidence.FULL, id="churn-7-of-7-full"),
        pytest.param(Motion.RENEWAL, 3, Confidence.THIN, id="renewal-3-of-7-thin"),
    ],
)
def test_level_boundaries(motion, fresh, level):
    readings = [_dated(kind, 0) for kind in _relevant(motion)[:fresh]]
    result = _confidence(motion, readings)
    assert result.fresh == fresh
    assert result.level is level


@pytest.mark.parametrize("kind", list(SignalKind), ids=[kind.value for kind in SignalKind])
def test_a_reading_goes_stale_one_day_after_its_limit(kind):
    limit = STALE_AFTER_DAYS[kind]
    others = [_dated(other, 0) for other in SignalKind if other is not kind]
    at_limit = _confidence(Motion.ONBOARDING, [*others, _dated(kind, limit)])
    assert at_limit.stale == []
    assert at_limit.level is Confidence.FULL

    past = _confidence(Motion.ONBOARDING, [*others, _dated(kind, limit + 1)])
    assert past.stale == [SIGNAL_LABELS[kind]]
    assert past.fresh == 7
    assert past.ages[list(SignalKind).index(kind)].stale is True


def test_a_reading_dated_after_as_of_has_age_zero():
    readings = [_dated(USAGE, -3)]
    readings += [_dated(kind, 0) for kind in _relevant(Motion.CHURN) if kind is not USAGE]
    result = _confidence(Motion.CHURN, readings)
    assert result.ages[0].age_days == 0
    assert result.oldest_age_days == 0
    assert result.level is Confidence.FULL


def test_full_with_an_older_reading_says_how_many_days_old_it_is():
    readings = [_dated(kind, 0) for kind in _relevant(Motion.CHURN)[:-1]]
    readings.append(_dated(BILLING, 12))
    result = _confidence(Motion.CHURN, readings)
    assert result.level is Confidence.FULL
    assert result.sentence == "Based on all 7 signals. The oldest reading is 12 days old."


def test_full_with_a_one_day_old_reading_uses_the_singular():
    readings = [_dated(kind, 0) for kind in _relevant(Motion.CHURN)[:-1]]
    readings.append(_dated(BILLING, 1))
    result = _confidence(Motion.CHURN, readings)
    assert result.sentence == "Based on all 7 signals. The oldest reading is 1 day old."


def test_missing_and_out_of_date_lists_keep_signal_order():
    readings = [
        _dated(USAGE, 0),
        _dated(OPEN, 0),
        _dated(SEATS, 0),
        _dated(NPS, 200),  # limit 180
        _dated(CHAMPION, 100),  # limit 90
        _dated(BILLING, 31),  # limit 30
    ]
    result = _confidence(Motion.ONBOARDING, readings)
    assert result.missing == ["Last login", "Onboarding milestones"]
    assert result.stale == ["NPS", "Champion", "Billing"]
    assert result.fresh == 3
    assert result.level is Confidence.THIN
    assert result.sentence == (
        "Based on 3 of 8 signals. No reading for Last login and Onboarding milestones. "
        "Out of date: NPS, Champion and Billing."
    )


def test_one_missing_and_one_stale_each_read_as_a_single_item():
    readings = [
        _dated(kind, 100 if kind is CHAMPION else 0)
        for kind in _relevant(Motion.CHURN)
        if kind is not DAYS
    ]
    result = _confidence(Motion.CHURN, readings)
    assert result.level is Confidence.PARTIAL
    assert result.sentence == (
        "Based on 5 of 7 signals. No reading for Last login. Out of date: Champion."
    )


def test_no_readings_at_all_lists_every_relevant_label_in_order():
    result = _confidence(Motion.CHURN, [])
    assert result.level is Confidence.THIN
    assert result.oldest_age_days is None
    assert result.sentence == (
        "Based on 0 of 7 signals. No reading for Usage trend, Last login, Severity-1 tickets, "
        "NPS, Champion, Seats in use and Billing."
    )


def test_ages_cover_every_kind_in_signal_order():
    result = _confidence(Motion.CHURN, [_dated(USAGE, 2)])
    assert [age.kind for age in result.ages] == list(SignalKind)
    assert [age.label for age in result.ages] == [SIGNAL_LABELS[kind] for kind in SignalKind]
    assert result.ages[0].age_days == 2
    assert result.ages[0].stale is False
    assert result.ages[1].age_days is None
    assert result.ages[1].stale is False


@pytest.mark.parametrize(
    ("readings", "blind"),
    [
        pytest.param([_dated(USAGE, 0)], True, id="healthy-thin-is-a-blind-spot"),
        pytest.param(
            [_dated(CHAMPION, 0, 1.0), _dated(BILLING, 0, 60.0)],
            False,
            id="at-risk-thin-is-not",
        ),
        pytest.param([_dated(OPEN, 0, 3.0)], False, id="watch-thin-is-not"),
        pytest.param(
            [_dated(kind, 0) for kind in _relevant(Motion.CHURN)[:4]],
            False,
            id="healthy-partial-is-not",
        ),
    ],
)
def test_blind_spot_only_for_healthy_and_thin(readings, blind):
    assert _confidence(Motion.CHURN, readings).blind_spot is blind


def test_book_confidence_covers_every_account_and_a_missing_one_has_no_readings():
    readings = [_dated(kind, 0, account_id="acc-a") for kind in SignalKind]
    book = score_book([_account("acc-a"), _account("acc-b")], readings, ScoringConfig(), AS_OF)
    by_account = latest_signals(readings)
    result = book_confidence(book, by_account, AS_OF)

    assert set(result) == {"acc-a", "acc-b"}
    assert result["acc-a"].level is Confidence.FULL
    assert result["acc-b"].fresh == 0
    assert result["acc-b"].missing == [SIGNAL_LABELS[kind] for kind in _relevant(Motion.CHURN)]
    for item in book:
        expected = score_confidence(item, by_account.get(item.account.id, {}), AS_OF)
        assert result[item.account.id] == expected


# golive_forecast


def _on_day(day: int, done: int, total: int) -> Account:
    """An onboarding account on `day` (the start date is day 1)."""
    return _account(start_date=AS_OF - timedelta(days=day - 1), done=done, total=total)


def test_no_forecast_without_a_start_date_with_no_milestones_or_once_complete():
    assert golive_forecast(_account(start_date=None, done=1, total=4), AS_OF, WINDOWS) is None
    assert golive_forecast(_on_day(30, 0, 0), AS_OF, WINDOWS) is None
    assert golive_forecast(_on_day(30, 4, 4), AS_OF, WINDOWS) is None
    future = _account(start_date=AS_OF + timedelta(days=1), done=1, total=4)
    assert golive_forecast(future, AS_OF, WINDOWS) is None


def test_start_date_is_day_one_and_too_early_before_the_minimum():
    first_day = golive_forecast(_on_day(1, 0, 4), AS_OF, WINDOWS)
    assert first_day is not None
    assert first_day.day == 1
    assert first_day.status is GoLiveStatus.TOO_EARLY
    assert first_day.sentence == "Day 1 of onboarding. A forecast appears after 14 days."


def test_day_13_is_too_early_and_day_14_gets_a_forecast():
    early = golive_forecast(_on_day(MIN_FORECAST_DAYS - 1, 2, 4), AS_OF, WINDOWS)
    assert early is not None
    assert early.status is GoLiveStatus.TOO_EARLY
    assert early.day == 13
    assert early.sentence == "Day 13 of onboarding. A forecast appears after 14 days."
    assert (early.projected_day, early.projected_date, early.overrun_days) == (None, None, None)

    first = golive_forecast(_on_day(MIN_FORECAST_DAYS, 2, 4), AS_OF, WINDOWS)
    assert first is not None
    assert first.day == 14
    assert first.status is GoLiveStatus.ON_TRACK
    assert first.projected_day == 28
    assert first.sentence == "At this pace onboarding ends on day 28 of 90, with 62 days to spare."


def test_no_pace_when_nothing_is_done_after_the_minimum_days():
    result = golive_forecast(_on_day(14, 0, 4), AS_OF, WINDOWS)
    assert result is not None
    assert result.status is GoLiveStatus.NO_PACE
    assert result.day == 14
    assert (
        result.sentence == "No milestones done after 14 days, so there's no pace to forecast from."
    )
    assert (result.projected_day, result.projected_date, result.overrun_days) == (None, None, None)


def test_on_track_with_spare_days_gives_a_projected_date():
    start = AS_OF - timedelta(days=29)
    result = golive_forecast(_on_day(30, 3, 6), AS_OF, WINDOWS)
    assert result is not None
    assert result.status is GoLiveStatus.ON_TRACK
    assert result.projected_day == 60
    assert result.overrun_days == -30
    assert result.projected_date == start + timedelta(days=59)
    assert result.sentence == "At this pace onboarding ends on day 60 of 90, with 30 days to spare."


def test_on_track_right_on_time_has_zero_overrun():
    result = golive_forecast(_on_day(30, 2, 6), AS_OF, WINDOWS)
    assert result is not None
    assert result.status is GoLiveStatus.ON_TRACK
    assert result.projected_day == 90
    assert result.overrun_days == 0
    assert result.sentence == "At this pace onboarding ends on day 90 of 90, right on time."


def test_late_by_one_day_uses_the_singular():
    result = golive_forecast(_on_day(34, 3, 8), AS_OF, WINDOWS)
    assert result is not None
    assert result.status is GoLiveStatus.LATE
    assert result.projected_day == 91
    assert result.overrun_days == 1
    assert result.sentence == "At this pace onboarding ends on day 91 of 90, 1 day late."


def test_late_by_many_days_uses_the_plural():
    result = golive_forecast(_on_day(60, 2, 6), AS_OF, WINDOWS)
    assert result is not None
    assert result.status is GoLiveStatus.LATE
    assert result.projected_day == 180
    assert result.sentence == "At this pace onboarding ends on day 180 of 90, 90 days late."


def test_past_the_window_is_still_forecast_and_late():
    result = golive_forecast(_on_day(200, 2, 6), AS_OF, WINDOWS)
    assert result is not None
    assert result.status is GoLiveStatus.LATE
    assert result.window_days == 90
    assert result.projected_day == 600
    assert result.overrun_days == 510


def test_projected_day_rounds_up_in_integer_maths():
    fractional = golive_forecast(_on_day(20, 3, 5), AS_OF, WINDOWS)  # 100 / 3 = 33.3 -> 34
    assert fractional is not None
    assert fractional.projected_day == 34
    exact = golive_forecast(_on_day(20, 4, 6), AS_OF, WINDOWS)  # 120 / 4 = 30 exactly
    assert exact is not None
    assert exact.projected_day == 30


def test_the_window_comes_from_the_motion_windows():
    result = golive_forecast(_on_day(20, 4, 6), AS_OF, MotionWindows(onboarding_days=20))
    assert result is not None
    assert result.window_days == 20
    assert result.status is GoLiveStatus.LATE
    assert result.sentence == "At this pace onboarding ends on day 30 of 20, 10 days late."


# shelfware


def _shelf(
    utilisation: float | None, *, arr: float = 50_000, renewal_in_days: int = 60
) -> Shelfware | None:
    account = _account(arr=arr, renewal_in_days=renewal_in_days)
    latest = {} if utilisation is None else {SEATS: _reading(SEATS, utilisation)}
    return shelfware(account, latest, AS_OF, WINDOWS)


def test_no_shelfware_without_a_seat_reading_or_with_no_arr():
    assert _shelf(None) is None
    assert _shelf(40.0, arr=0) is None


@pytest.mark.parametrize("utilisation", [81.0, 100.0, 150.0])
def test_no_shelfware_when_less_than_the_minimum_is_unused(utilisation):
    assert _shelf(utilisation) is None


def test_exactly_the_minimum_unused_is_flagged():
    flagged = _shelf(80.0)
    assert flagged is not None
    assert flagged.unused_percent == SHELFWARE_MIN_UNUSED
    assert flagged.unused_arr == 10_000.0


def test_utilisation_is_a_whole_percent_before_the_test():
    assert _shelf(80.4) is not None  # 80% in use, 20% unused
    assert _shelf(80.5) is None  # rounds to 81% in use
    rounded = _shelf(34.6)
    assert rounded is not None
    assert rounded.utilisation == 35
    assert rounded.unused_percent == 65


@pytest.mark.parametrize(
    ("arr", "utilisation", "unused_arr"),
    [
        pytest.param(50_000, 35, 32_500.0, id="exact-hundreds"),
        pytest.param(12_345, 40, 7_400.0, id="7407-rounds-down"),
        pytest.param(12_345, 60, 4_900.0, id="4938-rounds-down"),
        pytest.param(10_100, 50, 5_100.0, id="5050-half-rounds-up"),
    ],
)
def test_unused_arr_rounds_to_the_nearest_hundred(arr, utilisation, unused_arr):
    result = _shelf(utilisation, arr=arr)
    assert result is not None
    assert result.unused_arr == unused_arr


@pytest.mark.parametrize(
    ("days", "renewing_soon", "advice"),
    [
        pytest.param(-1, False, PLAN, id="passed"),
        pytest.param(0, True, f"Renewal is today. {EXPECT}", id="today"),
        pytest.param(1, True, f"Renewal is in 1 day. {EXPECT}", id="one-day"),
        pytest.param(120, True, f"Renewal is in 120 days. {EXPECT}", id="window-edge"),
        pytest.param(121, False, PLAN, id="past-window"),
    ],
)
def test_renewing_soon_window_and_advice(days, renewing_soon, advice):
    result = _shelf(40.0, renewal_in_days=days)
    assert result is not None
    assert result.days_to_renewal == days
    assert result.renewing_soon is renewing_soon
    assert result.advice == advice


# renewal_shelfware

Entry = tuple[str, str, float, float | None, int]  # id, name, arr, utilisation, renewal in days


def _shelf_book(*entries: Entry) -> tuple[list[ScoredAccount], dict[str, dict[SignalKind, Signal]]]:
    accounts: list[Account] = []
    readings: list[Signal] = []
    for acc_id, name, arr, utilisation, days in entries:
        accounts.append(_account(acc_id, name=name, arr=arr, renewal_in_days=days))
        if utilisation is not None:
            readings.append(_reading(SEATS, utilisation, account_id=acc_id))
    book = score_book(accounts, readings, ScoringConfig(), AS_OF)
    return book, latest_signals(readings)


def test_only_accounts_renewing_soon_with_shelfware_are_listed():
    book, by_account = _shelf_book(
        ("a", "Alpha", 100_000, 40.0, 30),  # renewing, 60% unused
        ("b", "Beta", 100_000, 10.0, 300),  # not renewing soon
        ("c", "Gamma", 100_000, 95.0, 30),  # renewing, but nearly all in use
        ("d", "Delta", 100_000, 40.0, -5),  # renewal already passed
        ("e", "Echo", 100_000, None, 30),  # renewing, but no seat reading
    )
    summary = renewal_shelfware(book, by_account, AS_OF, WINDOWS)
    assert [row.account_id for row in summary.rows] == ["a"]
    assert summary.count == 1
    assert summary.total_unused_arr == 60_000.0


def test_rows_order_by_unused_arr_then_name_then_id():
    book, by_account = _shelf_book(
        ("d", "Delta", 200_000, 0.0, 30),  # 100% unused: 200,000
        ("b", "Beta", 100_000, 50.0, 30),  # 50,000
        ("z", "Alpha", 100_000, 50.0, 30),  # 50,000, earlier name but later id
        ("g2", "Gamma", 100_000, 50.0, 30),  # 50,000, same name as g1
        ("g1", "Gamma", 100_000, 50.0, 30),  # 50,000
    )
    summary = renewal_shelfware(book, by_account, AS_OF, WINDOWS)
    assert [row.account_id for row in summary.rows] == ["d", "z", "b", "g1", "g2"]
    assert summary.count == 5
    assert summary.total_unused_arr == 400_000.0


def test_limit_caps_the_rows_but_count_and_total_cover_every_account():
    book, by_account = _shelf_book(
        ("d", "Delta", 200_000, 0.0, 30),
        ("b", "Beta", 100_000, 50.0, 30),
        ("z", "Alpha", 100_000, 50.0, 30),
        ("g2", "Gamma", 100_000, 50.0, 30),
        ("g1", "Gamma", 100_000, 50.0, 30),
    )
    summary = renewal_shelfware(book, by_account, AS_OF, WINDOWS, limit=2)
    assert [row.account_id for row in summary.rows] == ["d", "z"]
    assert summary.count == 5
    assert summary.total_unused_arr == 400_000.0

    none_shown = renewal_shelfware(book, by_account, AS_OF, WINDOWS, limit=0)
    assert none_shown.rows == []
    assert none_shown.count == 5


def test_rows_carry_the_account_health_and_motion():
    book, by_account = _shelf_book(("a", "Alpha", 100_000, 40.0, 30))
    summary = renewal_shelfware(book, by_account, AS_OF, WINDOWS)
    (row,) = summary.rows
    assert row.name == "Alpha"
    assert row.health is book[0].health
    assert row.motion is Motion.RENEWAL
    assert row.shelfware == _shelf(40.0, arr=100_000, renewal_in_days=30)


# service


def test_latest_book_signals_has_every_demo_account_with_all_eight_kinds():
    connector = DemoConnector(today=AS_OF)
    latest = service.latest_book_signals(connector)
    accounts = connector.list_accounts()
    assert len(latest) == len(accounts) == 40
    for account in accounts:
        assert set(latest[account.id]) == set(SignalKind)


def test_latest_book_signals_is_the_latest_reading_per_kind():
    connector = DemoConnector(today=AS_OF)
    assert service.latest_book_signals(connector) == latest_signals(connector.list_signals())


def test_latest_book_signals_defaults_to_the_demo_connector():
    latest = service.latest_book_signals()
    assert sorted(latest) == sorted(account.id for account in DemoConnector().list_accounts())
