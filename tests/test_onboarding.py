from __future__ import annotations

from datetime import timedelta

import pytest

from test_account_detail import AS_OF, _account, _detail, _reading, _row
from wardmind.core import onboarding, service
from wardmind.core.briefs import SIGNAL_LABELS, AccountDetail, suggest_action
from wardmind.core.config import MotionWindows, ScoringConfig
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Account, Milestones, Motion, Signal, SignalKind
from wardmind.core.onboarding import (
    ONBOARDING_SIGNALS,
    STANDARD_PLAN,
    AgendaItem,
    MilestoneStatus,
    OnboardingBrief,
    PlanMilestone,
    onboarding_brief,
    plan_for,
)

USAGE = SignalKind.USAGE_TREND_30D
DAYS = SignalKind.DAYS_SINCE_LAST_LOGIN
OPEN = SignalKind.OPEN_TICKETS_SEV1
NPS = SignalKind.NPS_LATEST
CHAMPION = SignalKind.CHAMPION_LEFT
SEATS = SignalKind.SEAT_UTILISATION
BILLING = SignalKind.BILLING_OVERDUE_DAYS
MILESTONES = SignalKind.ONBOARDING_OVERDUE_MILESTONES

# A value for each kind that puts the full weight on it, so one reading is one risk.
_TRIGGERS: dict[SignalKind, float] = {
    USAGE: -50,
    DAYS: 30,
    OPEN: 3,
    NPS: 0,
    CHAMPION: 1,
    SEATS: 30,
    BILLING: 60,
    MILESTONES: 3,
}

_LETTERS = {
    MilestoneStatus.DONE: "D",
    MilestoneStatus.OVERDUE: "O",
    MilestoneStatus.NEXT: "N",
    MilestoneStatus.UPCOMING: "U",
}

_CLOSING_ONBOARDING = "What would make the onboarding review a success for you?"
_CLOSING_NEXT = "What do you want to achieve next quarter?"
_WINS_QUESTION = "What has gone well so far?"
_NO_OTHER_RISKS = "No other risk signals so far."
_BANNED_CHARACTERS = ("\u2014", "\u00b7", "\u2192")  # em dash, middle dot, arrow


def _plan_account(done: int, total: int, overdue: int = 0) -> Account:
    milestones = Milestones(done=done, total=total, overdue=overdue)
    return _account("a").model_copy(update={"milestones": milestones})


def _letters(plan: list[PlanMilestone]) -> str:
    return "".join(_LETTERS[step.status] for step in plan)


def _detail_for(
    readings: list[Signal] | None = None,
    *,
    done: int = 4,
    total: int = 6,
    overdue: int = 0,
    days_since_start: int = 30,
    motion: Motion = Motion.ONBOARDING,
) -> AccountDetail:
    """Account 'a' with this plan, scored from `readings`. The motion is set directly, so tests
    can reach states the scoring would not pick, such as past the onboarding window."""
    account = _account("a", start_date=AS_OF - timedelta(days=days_since_start))
    account = account.model_copy(
        update={"milestones": Milestones(done=done, total=total, overdue=overdue)}
    )
    detail = _detail([account], readings or [], "a")
    scored = detail.scored.model_copy(
        update={
            "motion": motion,
            "suggested_action": suggest_action(motion, detail.scored.reasons),
        }
    )
    return detail.model_copy(update={"scored": scored})


def _brief(detail: AccountDetail) -> OnboardingBrief:
    return onboarding_brief(detail, MotionWindows())


def _topics(brief: OnboardingBrief) -> list[str]:
    return [item.topic for item in brief.agenda]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


# plan_for


@pytest.mark.parametrize("total", [1, 2, 5, 8])
def test_plan_has_one_step_per_milestone_in_the_standard_order(total):
    plan = plan_for(_plan_account(done=0, total=total))
    assert [step.position for step in plan] == list(range(1, total + 1))
    assert [step.name for step in plan] == [*STANDARD_PLAN[: total - 1], STANDARD_PLAN[-1]]


def test_no_milestones_gives_an_empty_plan():
    assert plan_for(_plan_account(done=0, total=0)) == []


def test_one_milestone_is_only_the_onboarding_review():
    plan = plan_for(_plan_account(done=0, total=1))
    assert [(step.position, step.name, step.status) for step in plan] == [
        (1, "Onboarding review", MilestoneStatus.NEXT)
    ]


def test_more_than_eight_milestones_are_capped_at_the_standard_plan():
    plan = plan_for(_plan_account(done=0, total=12))
    assert [step.name for step in plan] == list(STANDARD_PLAN)
    assert [step.position for step in plan] == list(range(1, 9))


@pytest.mark.parametrize(
    ("done", "overdue", "total", "letters"),
    [
        (0, 0, 1, "N"),
        (1, 0, 1, "D"),
        (0, 0, 5, "NUUUU"),
        (2, 0, 5, "DDNUU"),
        (2, 1, 5, "DDONU"),
        (1, 2, 5, "DOONU"),
        (2, 3, 5, "DDOOO"),  # no step is left for NEXT
        (5, 0, 5, "DDDDD"),
        (2, 1, 8, "DDONUUUU"),
    ],
)
def test_statuses_are_done_then_overdue_then_next_then_upcoming(done, overdue, total, letters):
    plan = plan_for(_plan_account(done=done, total=total, overdue=overdue))
    assert _letters(plan) == letters


def test_next_appears_once_unless_every_step_is_done_or_overdue():
    for total in range(1, 11):
        for done in range(total + 1):
            for overdue in range(total - done + 1):
                plan = plan_for(_plan_account(done=done, total=total, overdue=overdue))
                nexts = [step for step in plan if step.status is MilestoneStatus.NEXT]
                capped = min(total, len(STANDARD_PLAN))
                expected = 1 if done + overdue < capped else 0
                assert len(nexts) == expected, (done, overdue, total)


def test_the_last_step_is_always_the_onboarding_review():
    for total in range(1, 13):
        plan = plan_for(_plan_account(done=0, total=total))
        assert plan[-1].name == "Onboarding review", total


def test_done_and_overdue_are_clamped_to_the_eight_step_plan():
    # Ten milestones show eight steps, so nine done is eight done and nothing is left to be late.
    assert _letters(plan_for(_plan_account(done=9, total=10, overdue=1))) == "DDDDDDDD"
    # Five done leaves three of the eight steps, so at most three can be overdue.
    assert _letters(plan_for(_plan_account(done=5, total=10, overdue=5))) == "DDDDDOOO"


# onboarding_brief: fields


def test_the_brief_takes_the_account_facts_from_the_scored_account():
    detail = _detail_for([_reading("a", USAGE, -50)])
    brief = _brief(detail)
    scored = detail.scored
    assert brief.account == scored.account
    assert brief.health is scored.health
    assert brief.motion is Motion.ONBOARDING
    assert brief.in_onboarding is True
    assert brief.suggested_action == scored.suggested_action
    assert brief.milestones == plan_for(scored.account)


def test_day_counts_the_start_date_as_day_one_and_the_window_comes_from_the_windows():
    detail = _detail_for(days_since_start=79)
    brief = onboarding_brief(detail, MotionWindows(onboarding_days=60))
    assert brief.day == 80
    assert brief.window_days == 60


def test_risks_are_the_three_largest_reasons():
    readings = [
        _reading("a", USAGE, -50),  # 25
        _reading("a", OPEN, 3),  # 20
        _reading("a", DAYS, 30),  # 15
        _reading("a", CHAMPION, 1),  # 25, which sorts before usage on the tie
    ]
    detail = _detail_for(readings)
    brief = _brief(detail)
    assert [risk.kind for risk in brief.risks] == [CHAMPION, USAGE, OPEN]
    assert brief.risks == detail.scored.reasons[:3]


def test_signals_are_the_onboarding_rows_in_onboarding_order():
    readings = [_reading("a", kind, value) for kind, value in _TRIGGERS.items()]
    detail = _detail_for(readings)
    brief = _brief(detail)
    assert [row.kind for row in brief.signals] == list(ONBOARDING_SIGNALS)
    assert brief.signals == [_row(detail, kind) for kind in ONBOARDING_SIGNALS]


# onboarding_brief: summary


@pytest.mark.parametrize(
    ("kwargs", "opening"),
    [
        (
            {"done": 2, "overdue": 2, "days_since_start": 80},
            "Behind on the plan with 10 days left.",
        ),
        ({"done": 2, "overdue": 2, "days_since_start": 89}, "Behind on the plan with 1 day left."),
        (
            {"done": 2, "overdue": 2, "days_since_start": 90},
            "Behind on the plan and past the 90-day window.",
        ),
        ({"done": 4, "days_since_start": 30}, "On track: 4 of 6 steps done with 60 days left."),
        ({"done": 4, "days_since_start": 89}, "On track: 4 of 6 steps done with 1 day left."),
        (
            {"done": 4, "days_since_start": 90},
            "On track: 4 of 6 steps done, past the 90-day window.",
        ),
        (
            {"done": 1, "total": 1, "days_since_start": 30},
            "On track: 1 of 1 step done with 60 days left.",
        ),
        ({"done": 4, "motion": Motion.CHURN}, "4 of 6 onboarding steps done."),
        ({"done": 1, "total": 1, "motion": Motion.RENEWAL}, "1 of 1 onboarding step done."),
    ],
)
def test_summary_opening_sentence_for_each_state(kwargs, opening):
    brief = _brief(_detail_for([_reading("a", DAYS, 30)], **kwargs))
    assert brief.summary == f"{opening} No one has logged in for 30 days."


def test_the_second_sentence_skips_the_milestone_risk():
    readings = [_reading("a", MILESTONES, 3), _reading("a", DAYS, 30)]  # 20 then 15
    brief = _brief(_detail_for(readings, overdue=2))
    assert brief.risks[0].kind is MILESTONES
    assert brief.summary.endswith(" No one has logged in for 30 days.")


def test_the_second_sentence_says_so_when_there_is_no_other_risk():
    only_milestones = _brief(_detail_for([_reading("a", MILESTONES, 1)], overdue=1))
    no_risks = _brief(_detail_for())
    assert only_milestones.summary.endswith(f" {_NO_OTHER_RISKS}")
    assert no_risks.summary.endswith(f" {_NO_OTHER_RISKS}")


# onboarding_brief: agenda


def test_agenda_is_progress_then_the_risks_then_the_review():
    readings = [_reading("a", USAGE, -50), _reading("a", OPEN, 3), _reading("a", DAYS, 30)]
    brief = _brief(_detail_for(readings))
    assert _topics(brief) == [
        "Progress on the plan",
        "Usage trend",
        "Severity-1 tickets",
        "Last login",
        "The onboarding review",
    ]
    assert brief.agenda[1] == AgendaItem(
        topic="Usage trend",
        detail="Active users down 50% in 30 days. "
        "Ask which teams have slowed down and what changed for them.",
    )


@pytest.mark.parametrize("kind", [kind for kind in SignalKind if kind is not MILESTONES])
def test_each_risk_gets_its_label_talking_point_and_question(kind):
    brief = _brief(_detail_for([_reading("a", kind, _TRIGGERS[kind])]))
    risk = brief.risks[0]
    assert risk.kind is kind
    assert brief.agenda[1] == AgendaItem(
        topic=SIGNAL_LABELS[kind],
        detail=f"{risk.plain_english}. {onboarding._TALKING_POINTS[kind]}",
    )
    assert onboarding._QUESTIONS[kind] in brief.questions


def test_the_milestone_risk_is_neither_an_agenda_item_nor_a_risk_question():
    brief = _brief(_detail_for([_reading("a", MILESTONES, 3)], overdue=2))
    assert SIGNAL_LABELS[MILESTONES] not in _topics(brief)
    assert onboarding._QUESTIONS[MILESTONES] not in brief.questions


def test_wins_pads_the_agenda_to_three_when_there_are_no_risks():
    brief = _brief(_detail_for())
    assert _topics(brief) == ["Progress on the plan", "Wins so far", "The onboarding review"]


def test_outside_onboarding_with_no_risks_the_agenda_looks_to_next_quarter():
    brief = _brief(_detail_for(motion=Motion.CHURN))
    assert _topics(brief) == ["Progress on the plan", "Wins so far", "What comes next"]
    assert brief.agenda[-1].detail == "Agree the goals for the next quarter."


def test_outside_onboarding_one_risk_gets_wins_but_no_next_quarter_item():
    brief = _brief(_detail_for([_reading("a", DAYS, 30)], motion=Motion.RENEWAL))
    assert _topics(brief) == ["Progress on the plan", "Last login", "Wins so far"]


def test_outside_onboarding_enough_risks_means_no_filler():
    readings = [_reading("a", USAGE, -50), _reading("a", DAYS, 30)]
    brief = _brief(_detail_for(readings, motion=Motion.CHURN))
    assert _topics(brief) == ["Progress on the plan", "Usage trend", "Last login"]


def test_the_review_item_names_the_window_day():
    brief = onboarding_brief(_detail_for(), MotionWindows(onboarding_days=60))
    assert brief.agenda[-1] == AgendaItem(
        topic="The onboarding review", detail="Confirm what success looks like by day 60."
    )


@pytest.mark.parametrize(
    ("done", "total", "overdue", "detail"),
    [
        (1, 6, 1, "1 of 6 steps are done. Admins set up is overdue. Agree a new date."),
        (
            1,
            6,
            2,
            "1 of 6 steps are done. Admins set up and Data imported are overdue. Agree new dates.",
        ),
        (
            0,
            6,
            3,
            "0 of 6 steps are done. Kickoff call, Admins set up and Data imported are overdue. "
            "Agree new dates.",
        ),
        (
            0,
            8,
            4,
            "0 of 8 steps are done. Kickoff call, Admins set up, Data imported and "
            "Integrations connected are overdue. Agree new dates.",
        ),
        (
            4,
            6,
            0,
            "4 of 6 steps are done. Next up is Pilot team trained. Agree who owns it and when.",
        ),
        (6, 6, 0, "Every step is done."),
        (1, 1, 0, "Every step is done."),
        (0, 1, 1, "0 of 1 step is done. Onboarding review is overdue. Agree a new date."),
    ],
)
def test_progress_detail_wording(done, total, overdue, detail):
    brief = _brief(_detail_for(done=done, total=total, overdue=overdue))
    assert brief.agenda[0] == AgendaItem(topic="Progress on the plan", detail=detail)


def test_the_agenda_is_capped_and_risk_items_give_way_first(monkeypatch):
    # Three risks never overflow the cap of six on their own, so lower the cap to see the rule.
    monkeypatch.setattr(onboarding, "_MAX_AGENDA", 4)
    readings = [_reading("a", USAGE, -50), _reading("a", OPEN, 3), _reading("a", DAYS, 30)]
    brief = _brief(_detail_for(readings))
    assert _topics(brief) == [
        "Progress on the plan",
        "Usage trend",
        "Severity-1 tickets",
        "The onboarding review",
    ]


# onboarding_brief: questions


def test_an_overdue_step_is_asked_about_first():
    brief = _brief(_detail_for(done=1, total=6, overdue=1))
    assert brief.questions[0] == "What is blocking Admins set up?"


def test_the_next_step_is_asked_about_when_nothing_is_overdue():
    brief = _brief(_detail_for())
    assert brief.questions[0] == "Who owns Pilot team trained, and when will it be done?"


def test_risk_questions_follow_the_plan_question_and_the_closing_comes_last():
    readings = [_reading("a", USAGE, -50), _reading("a", DAYS, 30)]
    brief = _brief(_detail_for(readings, done=1, total=6, overdue=1))
    assert brief.questions == [
        "What is blocking Admins set up?",
        "Which teams have stopped using it, and what changed for them?",
        "Who on your team should be logging in this week?",
        _CLOSING_ONBOARDING,
    ]


def test_the_closing_question_depends_on_the_motion():
    assert _brief(_detail_for()).questions[-1] == _CLOSING_ONBOARDING
    assert _brief(_detail_for(motion=Motion.CHURN)).questions[-1] == _CLOSING_NEXT


def test_duplicate_questions_are_asked_once(monkeypatch):
    monkeypatch.setitem(onboarding._QUESTIONS, DAYS, onboarding._QUESTIONS[USAGE])
    readings = [_reading("a", USAGE, -50), _reading("a", DAYS, 30)]
    brief = _brief(_detail_for(readings))
    assert brief.questions.count(onboarding._QUESTIONS[USAGE]) == 1
    assert len(brief.questions) == len(set(brief.questions))


def test_three_risks_and_an_overdue_step_make_five_questions_with_the_closing_last():
    readings = [_reading("a", USAGE, -50), _reading("a", OPEN, 3), _reading("a", DAYS, 30)]
    brief = _brief(_detail_for(readings, done=1, total=6, overdue=1))
    assert len(brief.questions) == 5
    assert brief.questions[-1] == _CLOSING_ONBOARDING


def test_the_question_cap_keeps_the_closing_question_last(monkeypatch):
    monkeypatch.setattr(onboarding, "_MAX_QUESTIONS", 3)
    readings = [_reading("a", USAGE, -50), _reading("a", OPEN, 3), _reading("a", DAYS, 30)]
    brief = _brief(_detail_for(readings, done=1, total=6, overdue=1))
    assert brief.questions == [
        "What is blocking Admins set up?",
        "Which teams have stopped using it, and what changed for them?",
        _CLOSING_ONBOARDING,
    ]


def test_at_least_two_questions_are_asked_with_the_wins_question_when_needed():
    brief = _brief(_detail_for(done=6, total=6))
    assert brief.questions == [_WINS_QUESTION, _CLOSING_ONBOARDING]
    outside = _brief(_detail_for(done=6, total=6, motion=Motion.CHURN))
    assert outside.questions == [_WINS_QUESTION, _CLOSING_NEXT]


def test_one_planned_question_is_enough_without_the_wins_question():
    brief = _brief(_detail_for())
    assert brief.questions == [
        "Who owns Pilot team trained, and when will it be done?",
        _CLOSING_ONBOARDING,
    ]


def test_every_question_ends_with_a_question_mark_and_every_signal_has_its_copy():
    assert set(onboarding._TALKING_POINTS) == set(SignalKind)
    assert set(onboarding._QUESTIONS) == set(SignalKind)
    assert all(text.endswith(".") for text in onboarding._TALKING_POINTS.values())
    assert all(text.endswith("?") for text in onboarding._QUESTIONS.values())


# demo checks


@pytest.fixture(scope="module")
def demo():
    connector = DemoConnector(today=AS_OF)
    config = ScoringConfig()
    book = service.get_book(connector=connector, config=config, as_of=AS_OF)
    return connector, config, book


def _demo_brief(demo, account_id: str) -> OnboardingBrief:
    connector, config, _ = demo
    brief = service.get_onboarding_brief(
        account_id, connector=connector, config=config, as_of=AS_OF
    )
    assert brief is not None, account_id
    return brief


def test_onboarding_accounts_are_the_eight_demo_onboarding_accounts_in_book_order(demo):
    _, _, book = demo
    accounts = service.onboarding_accounts(book)
    assert len(accounts) == 8
    assert accounts == [item for item in book if item.motion is Motion.ONBOARDING]


def test_every_demo_onboarding_brief_is_complete_and_plain(demo):
    _, _, book = demo
    for item in service.onboarding_accounts(book):
        account_id = item.account.id
        brief = _demo_brief(demo, account_id)
        assert brief.in_onboarding, account_id
        assert 3 <= len(brief.agenda) <= 6, account_id
        assert 2 <= len(brief.questions) <= 5, account_id
        assert brief.summary.endswith("."), account_id
        assert all(step.detail.endswith(".") for step in brief.agenda), account_id
        assert all(question.endswith("?") for question in brief.questions), account_id
        for text in _strings(brief.model_dump(mode="json")):
            assert not any(char in text for char in _BANNED_CHARACTERS), text


def test_the_top_onboarding_brief_reads_as_expected(demo):
    _, _, book = demo
    top = service.onboarding_accounts(book)[0]
    brief = _demo_brief(demo, top.account.id)
    assert brief.account.name == "Dunmere Legal"
    assert brief.summary == "Behind on the plan with 62 days left. Invoice 60 days overdue."
    assert _topics(brief) == [
        "Progress on the plan",
        "Billing",
        "Last login",
        "Usage trend",
        "The onboarding review",
    ]
    assert brief.questions[0] == "What is blocking Data imported?"


def test_get_onboarding_brief_is_the_brief_of_the_account_detail(demo):
    connector, config, _ = demo
    detail = service.get_account_detail("acc-033", connector=connector, config=config, as_of=AS_OF)
    assert detail is not None
    assert _demo_brief(demo, "acc-033") == onboarding_brief(detail, config.motion)


def test_an_account_outside_onboarding_still_gets_a_brief(demo):
    _, _, book = demo
    item = next(item for item in book if item.motion is not Motion.ONBOARDING)
    brief = _demo_brief(demo, item.account.id)
    assert brief.in_onboarding is False
    assert brief.questions[-1] == _CLOSING_NEXT


def test_an_unknown_account_id_has_no_brief(demo):
    connector, config, _ = demo
    brief = service.get_onboarding_brief("acc-999", connector=connector, config=config, as_of=AS_OF)
    assert brief is None
