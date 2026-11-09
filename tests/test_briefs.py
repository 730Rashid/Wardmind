from __future__ import annotations

from datetime import date, timedelta

import pytest

from wardmind.core import briefs
from wardmind.core.briefs import explain, suggest_action, weekly_brief
from wardmind.core.config import ScoringConfig
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import (
    Account,
    Health,
    Milestones,
    Motion,
    Reason,
    ScoredAccount,
    SignalKind,
)
from wardmind.core.scoring import score_book

AS_OF = date(2026, 10, 19)
ALL_PAIRS = [(motion, kind) for motion in Motion for kind in SignalKind]
NO_ACTION = "No action needed this week."


def _reason(kind: SignalKind, text: str = "A reason", contribution: float = 10.0) -> Reason:
    return Reason(kind=kind, contribution=contribution, plain_english=text)


def _scored(
    id: str,
    *,
    motion: Motion = Motion.CHURN,
    health: Health = Health.HEALTHY,
    arr: float = 50_000,
    renewal_in_days: int = 400,
    reasons: tuple[Reason, ...] = (),
) -> ScoredAccount:
    account = Account(
        id=id,
        name=f"Account {id}",
        arr=arr,
        tier="Growth",
        owner="Owner",
        start_date=date(2024, 1, 1),
        renewal_date=AS_OF + timedelta(days=renewal_in_days),
        milestones=Milestones(done=4, total=4),
    )
    risk = 50.0 if health is Health.AT_RISK else 0.0
    return ScoredAccount(
        account=account,
        motion=motion,
        health=health,
        risk=risk,
        priority=risk,
        reasons=list(reasons),
    )


def _demo_book() -> list[ScoredAccount]:
    connector = DemoConnector(today=AS_OF)
    return score_book(connector.list_accounts(), connector.list_signals(), ScoringConfig(), AS_OF)


# suggest_action


@pytest.mark.parametrize("motion", list(Motion))
def test_no_reasons_means_no_action_needed(motion):
    assert suggest_action(motion, []) == NO_ACTION


@pytest.mark.parametrize(
    ("motion", "kind", "action"),
    [
        (
            Motion.RENEWAL,
            SignalKind.CHAMPION_LEFT,
            "Find the new decision-maker and meet them before the renewal conversation.",
        ),
        (
            Motion.CHURN,
            SignalKind.USAGE_TREND_30D,
            "Book a usage review with the admin to find what stopped working.",
        ),
        (
            Motion.ONBOARDING,
            SignalKind.ONBOARDING_OVERDUE_MILESTONES,
            "Reset the onboarding plan with the customer and agree new milestone dates.",
        ),
        (
            Motion.CHURN,
            SignalKind.BILLING_OVERDUE_DAYS,
            "Ask finance to confirm the invoice reached the right billing contact.",
        ),
    ],
    ids=["renewal-champion", "churn-usage", "onboarding-milestones", "churn-billing"],
)
def test_table_entries_match_the_brief(motion, kind, action):
    assert suggest_action(motion, [_reason(kind)]) == action


@pytest.mark.parametrize("kind", list(SignalKind))
def test_onboarding_has_its_own_action_for_every_signal(kind):
    # An onboarding account whose top problem is, say, severity-1 tickets needs a ticket action,
    # not the generic onboarding check-in.
    action = suggest_action(Motion.ONBOARDING, [_reason(kind)])
    assert action != briefs._FALLBACK_ACTIONS[Motion.ONBOARDING]


@pytest.mark.parametrize(("motion", "kind"), ALL_PAIRS)
def test_every_action_is_one_short_imperative_sentence(motion, kind):
    action = suggest_action(motion, [_reason(kind)])
    assert action.endswith(".")
    assert action.count(".") == 1
    assert len(action.split()) <= 16
    assert action.isascii()
    assert action[0].isupper()


@pytest.mark.parametrize("motion", list(Motion))
def test_every_motion_has_a_fallback_action(motion, monkeypatch):
    # With the rule table empty, every lookup must fall back to the motion's own action.
    monkeypatch.setattr(briefs, "_ACTIONS", {})
    action = suggest_action(motion, [_reason(SignalKind.NPS_LATEST)])
    assert action == briefs._FALLBACK_ACTIONS[motion]
    assert action.endswith(".")
    assert len(action.split()) <= 16


def test_only_the_top_reason_decides_the_action():
    champion = _reason(SignalKind.CHAMPION_LEFT)
    usage = _reason(SignalKind.USAGE_TREND_30D)
    assert suggest_action(Motion.RENEWAL, [champion, usage]) == suggest_action(
        Motion.RENEWAL, [champion]
    )
    assert suggest_action(Motion.RENEWAL, [usage, champion]) == suggest_action(
        Motion.RENEWAL, [usage]
    )


# weekly_brief


def test_totals_and_counts_on_the_demo_book():
    book = _demo_book()
    brief = weekly_brief(book, AS_OF)
    assert brief.as_of == AS_OF
    assert brief.total_accounts == 40
    assert brief.total_arr == pytest.approx(sum(item.account.arr for item in book))
    assert brief.arr_at_risk == pytest.approx(
        sum(item.account.arr for item in book if item.health is Health.AT_RISK)
    )
    assert brief.counts_by_health == {Health.AT_RISK: 10, Health.WATCH: 12, Health.HEALTHY: 18}
    assert brief.counts_by_motion == {Motion.ONBOARDING: 8, Motion.RENEWAL: 12, Motion.CHURN: 20}


def test_counts_sum_to_the_total_and_every_key_is_present():
    book = [
        _scored("a", health=Health.AT_RISK, motion=Motion.RENEWAL, renewal_in_days=10),
        _scored("b", health=Health.WATCH, motion=Motion.CHURN),
    ]
    brief = weekly_brief(book, AS_OF)
    assert sum(brief.counts_by_health.values()) == brief.total_accounts == 2
    assert sum(brief.counts_by_motion.values()) == brief.total_accounts == 2
    assert set(brief.counts_by_health) == set(Health)
    assert set(brief.counts_by_motion) == set(Motion)
    assert brief.counts_by_health[Health.HEALTHY] == 0
    assert brief.counts_by_motion[Motion.ONBOARDING] == 0


def test_an_empty_book_gives_zeros_and_no_call_outs():
    brief = weekly_brief([], AS_OF)
    assert brief.total_accounts == 0
    assert brief.total_arr == 0.0
    assert brief.arr_at_risk == 0.0
    assert brief.counts_by_health == dict.fromkeys(Health, 0)
    assert brief.counts_by_motion == dict.fromkeys(Motion, 0)
    assert brief.top == []
    assert brief.notable == []


def test_arr_at_risk_counts_only_at_risk_accounts():
    book = [
        _scored("a", health=Health.AT_RISK, arr=120_000),
        _scored("b", health=Health.AT_RISK, arr=30_500),
        _scored("c", health=Health.WATCH, arr=999_000),
        _scored("d", health=Health.HEALTHY, arr=1_000),
    ]
    brief = weekly_brief(book, AS_OF)
    assert brief.arr_at_risk == 150_500
    assert brief.total_arr == 1_150_500
    assert brief.arr_by_health == {
        Health.AT_RISK: 150_500,
        Health.WATCH: 999_000,
        Health.HEALTHY: 1_000,
    }
    assert weekly_brief([], AS_OF).arr_by_health == dict.fromkeys(Health, 0.0)


def test_top_is_the_first_n_of_the_book():
    book = _demo_book()
    assert weekly_brief(book, AS_OF, top_n=3).top == book[:3]
    assert len(weekly_brief(book, AS_OF).top) == 10
    assert weekly_brief(book, AS_OF, top_n=100).top == book


def test_no_call_outs_for_a_quiet_book():
    assert weekly_brief([_scored("a"), _scored("b")], AS_OF).notable == []


def test_renewal_call_out_counts_renewals_in_the_next_30_days():
    book = [
        _scored("a", motion=Motion.RENEWAL, renewal_in_days=0),
        _scored("b", motion=Motion.RENEWAL, renewal_in_days=30),
        _scored("c", motion=Motion.RENEWAL, renewal_in_days=31),
        _scored("d", motion=Motion.RENEWAL, renewal_in_days=-1),
        _scored("e", motion=Motion.CHURN, renewal_in_days=10),
    ]
    assert weekly_brief(book, AS_OF).notable == ["2 renewals due in the next 30 days"]


def test_single_renewal_call_out_is_singular():
    book = [_scored("a", motion=Motion.RENEWAL, renewal_in_days=12)]
    assert weekly_brief(book, AS_OF).notable == ["1 renewal due in the next 30 days"]


def test_champion_call_out_plural_and_singular():
    champion = (_reason(SignalKind.CHAMPION_LEFT, "Champion has left the company"),)
    one = [_scored("a", reasons=champion)]
    two = [_scored("a", reasons=champion), _scored("b", reasons=champion)]
    assert weekly_brief(one, AS_OF).notable == ["1 champion has left"]
    assert weekly_brief(two, AS_OF).notable == ["2 champions have left"]


def test_sev1_call_out_plural_and_singular():
    sev1 = (_reason(SignalKind.OPEN_TICKETS_SEV1, "3 open severity-1 tickets"),)
    one = [_scored("a", reasons=sev1)]
    two = [_scored("a", reasons=sev1), _scored("b", reasons=sev1)]
    assert weekly_brief(one, AS_OF).notable == ["1 account with open severity-1 tickets"]
    assert weekly_brief(two, AS_OF).notable == ["2 accounts with open severity-1 tickets"]


def test_onboarding_call_out_plural_and_singular():
    behind = (_reason(SignalKind.ONBOARDING_OVERDUE_MILESTONES, "2 onboarding milestones overdue"),)
    one = [_scored("a", motion=Motion.ONBOARDING, reasons=behind)]
    two = [
        _scored("a", motion=Motion.ONBOARDING, reasons=behind),
        _scored("b", motion=Motion.ONBOARDING, reasons=behind),
    ]
    assert weekly_brief(one, AS_OF).notable == ["1 onboarding account behind on milestones"]
    assert weekly_brief(two, AS_OF).notable == ["2 onboarding accounts behind on milestones"]


def test_onboarding_milestones_only_count_during_onboarding():
    behind = (_reason(SignalKind.ONBOARDING_OVERDUE_MILESTONES, "1 onboarding milestone overdue"),)
    book = [_scored("a", motion=Motion.CHURN, reasons=behind)]
    assert weekly_brief(book, AS_OF).notable == []


def test_call_outs_come_in_a_fixed_order():
    book = [
        _scored(
            "a",
            motion=Motion.ONBOARDING,
            reasons=(
                _reason(SignalKind.ONBOARDING_OVERDUE_MILESTONES, "1 onboarding milestone overdue"),
            ),
        ),
        _scored(
            "b",
            motion=Motion.CHURN,
            reasons=(
                _reason(SignalKind.OPEN_TICKETS_SEV1, "1 open severity-1 ticket"),
                _reason(SignalKind.CHAMPION_LEFT, "Champion has left the company"),
            ),
        ),
        _scored("c", motion=Motion.RENEWAL, renewal_in_days=5),
    ]
    assert weekly_brief(book, AS_OF).notable == [
        "1 renewal due in the next 30 days",
        "1 champion has left",
        "1 account with open severity-1 tickets",
        "1 onboarding account behind on milestones",
    ]


# explain


def test_explain_copies_the_fields_across():
    usage = _reason(SignalKind.USAGE_TREND_30D, "Active users down 38% in 30 days", 25.0)
    scored = _scored("acc-9", health=Health.AT_RISK, reasons=(usage,))
    scored = scored.model_copy(
        update={"suggested_action": "Book a usage review.", "risk": 62.5, "priority": 40.0}
    )
    explanation = explain(scored)
    assert explanation.account_id == "acc-9"
    assert explanation.name == "Account acc-9"
    assert explanation.motion is Motion.CHURN
    assert explanation.health is Health.AT_RISK
    assert explanation.risk == 62.5
    assert explanation.priority == 40.0
    assert explanation.reasons == [usage]
    assert explanation.suggested_action == "Book a usage review."


def test_explain_with_no_reasons():
    explanation = explain(_scored("a", health=Health.HEALTHY, motion=Motion.CHURN))
    assert explanation.summary == "Healthy and on churn watch. No risk signals."


@pytest.mark.parametrize(
    ("health", "motion", "opening"),
    [
        (Health.AT_RISK, Motion.ONBOARDING, "At risk and in onboarding."),
        (Health.WATCH, Motion.RENEWAL, "Watch and up for renewal."),
        (Health.HEALTHY, Motion.CHURN, "Healthy and on churn watch."),
    ],
)
def test_explain_opening_names_health_and_motion(health, motion, opening):
    scored = _scored("a", health=health, motion=motion, reasons=(_reason(SignalKind.NPS_LATEST),))
    assert explain(scored).summary.startswith(opening + " ")


def test_explain_with_one_reason():
    usage = _reason(SignalKind.USAGE_TREND_30D, "Active users down 38% in 30 days")
    scored = _scored("a", health=Health.AT_RISK, motion=Motion.RENEWAL, reasons=(usage,))
    assert explain(scored).summary == (
        "At risk and up for renewal. Active users down 38% in 30 days."
    )


def test_explain_with_two_or_more_reasons_uses_the_top_two():
    reasons = (
        _reason(SignalKind.USAGE_TREND_30D, "Active users down 38% in 30 days"),
        _reason(SignalKind.CHAMPION_LEFT, "Champion has left the company"),
        _reason(SignalKind.OPEN_TICKETS_SEV1, "3 open severity-1 tickets"),
    )
    scored = _scored("a", health=Health.WATCH, motion=Motion.ONBOARDING, reasons=reasons)
    assert explain(scored).summary == (
        "Watch and in onboarding. Active users down 38% in 30 days, "
        "and champion has left the company."
    )


def test_explain_never_doubles_the_full_stop():
    reasons = (
        _reason(SignalKind.CHAMPION_LEFT, "Champion has left the company."),
        _reason(SignalKind.OPEN_TICKETS_SEV1, "3 open severity-1 tickets."),
    )
    summary = explain(_scored("a", health=Health.AT_RISK, reasons=reasons)).summary
    assert ".." not in summary
    assert summary == (
        "At risk and on churn watch. Champion has left the company, and 3 open severity-1 tickets."
    )
