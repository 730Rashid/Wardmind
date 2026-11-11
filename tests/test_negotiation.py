from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from wardmind.core.config import ScoringConfig
from wardmind.core.forecast import Disagreement
from wardmind.core.growth import GrowthBand, GrowthKind, GrowthRadar, GrowthReason
from wardmind.core.insights import Shelfware
from wardmind.core.journal import (
    AccountJournal,
    CallKind,
    FollowUp,
    FollowUpStatus,
    ForecastCall,
    Note,
    Outcome,
    OutcomeKind,
    TouchKind,
)
from wardmind.core.models import (
    Account,
    Health,
    Milestones,
    Motion,
    Reason,
    ScoredAccount,
    Signal,
    SignalKind,
)
from wardmind.core.negotiation import (
    MAX_EXPOSURES,
    MAX_FOLLOW_UPS,
    MAX_RECENT,
    LastRenewal,
    LikelyAsk,
    NegotiationSheet,
    RecentContact,
    negotiation_sheet,
)
from wardmind.core.scoring import score_account

AS_OF = date(2026, 10, 19)
READ_AT = datetime(2026, 10, 18, 9, 0)
RENEWAL = AS_OF + timedelta(days=200)

USAGE = SignalKind.USAGE_TREND_30D
OPEN = SignalKind.OPEN_TICKETS_SEV1
CHAMPION = SignalKind.CHAMPION_LEFT
SEATS = SignalKind.SEAT_UTILISATION
BILLING = SignalKind.BILLING_OVERDUE_DAYS


def _account(
    *,
    start_date: date | None = date(2024, 1, 1),
    renewal_date: date = RENEWAL,
    arr: float = 50_000,
) -> Account:
    return Account(
        id="acc-1",
        name="Acme",
        arr=arr,
        tier="Growth",
        owner="Dana",
        start_date=start_date,
        renewal_date=renewal_date,
        milestones=Milestones(done=4, total=4),
    )


def _scored(
    account: Account | None = None,
    *,
    health: Health = Health.HEALTHY,
    reasons: list[Reason] | None = None,
    risk: float = 0.0,
) -> ScoredAccount:
    return ScoredAccount(
        account=_account() if account is None else account,
        motion=Motion.RENEWAL,
        health=health,
        risk=risk,
        priority=0.0,
        reasons=[] if reasons is None else reasons,
    )


def _reason(kind: SignalKind, text: str, contribution: float = 10.0) -> Reason:
    return Reason(kind=kind, contribution=contribution, plain_english=text)


def _latest(values: dict[SignalKind, float]) -> dict[SignalKind, Signal]:
    return {
        kind: Signal(account_id="acc-1", kind=kind, value=value, observed_at=READ_AT)
        for kind, value in values.items()
    }


def _shelf(unused_percent: int = 38, unused_arr: float = 19_000.0) -> Shelfware:
    return Shelfware(
        utilisation=100 - unused_percent,
        unused_percent=unused_percent,
        unused_arr=unused_arr,
        days_to_renewal=200,
        renewing_soon=False,
        advice="Plan a seat review before the renewal, so a downsell isn't a surprise.",
    )


def _growth(*reasons: GrowthReason, held_back: bool = False) -> GrowthRadar:
    return GrowthRadar(
        score=0 if held_back else 40,
        band=GrowthBand.NONE if held_back else GrowthBand.SOME,
        reasons=list(reasons),
        held_back=held_back,
        sentence="Some room to grow.",
    )


def _grow(kind: GrowthKind, text: str, points: float = 10.0) -> GrowthReason:
    return GrowthReason(kind=kind, points=points, plain_english=text)


def _contact(
    happened_on: date,
    kind: TouchKind = TouchKind.CALL,
    *,
    text: str = "Talked through the plan",
    subject: str = "",
    created_at: datetime = READ_AT,
    note_id: str = "n-00000001",
) -> Note:
    return Note(
        id=note_id,
        kind=kind,
        text=text,
        happened_on=happened_on,
        created_at=created_at,
        subject=subject,
    )


def _in_touch() -> AccountJournal:
    """A journal with contact yesterday, so no contact exposure or strength muddies a test."""
    return AccountJournal(notes=[_contact(AS_OF - timedelta(days=1))])


def _outcome(
    kind: OutcomeKind,
    renewal_date: date,
    *,
    recorded_at: datetime = READ_AT,
    decided_on: date | None = None,
    arr_before: float = 50_000,
    arr_after: float | None = None,
) -> Outcome:
    return Outcome(
        id="o-00000001",
        kind=kind,
        renewal_date=renewal_date,
        decided_on=renewal_date - timedelta(days=10) if decided_on is None else decided_on,
        arr_before=arr_before,
        arr_after=arr_after,
        risk=0.0,
        priority=0.0,
        health=Health.HEALTHY,
        motion=Motion.RENEWAL,
        recorded_at=recorded_at,
    )


def _call(kind: CallKind, renewal_date: date = RENEWAL) -> ForecastCall:
    return ForecastCall(
        id="c-00000001",
        kind=kind,
        renewal_date=renewal_date,
        made_on=AS_OF,
        health=Health.HEALTHY,
        risk=0.0,
        arr=50_000,
        recorded_at=READ_AT,
    )


def _follow_up(
    text: str,
    due: date,
    *,
    status: FollowUpStatus = FollowUpStatus.OPEN,
    created_at: datetime = READ_AT,
) -> FollowUp:
    return FollowUp(
        id="f-00000001",
        text=text,
        due=due,
        status=status,
        created_at=created_at,
        done_at=READ_AT if status is FollowUpStatus.DONE else None,
    )


def _sheet(
    scored: ScoredAccount | None = None,
    *,
    latest: dict[SignalKind, Signal] | None = None,
    journal: AccountJournal | None = None,
    growth: GrowthRadar | None = None,
    shelfware: Shelfware | None = None,
    as_of: date = AS_OF,
) -> NegotiationSheet:
    return negotiation_sheet(
        _scored() if scored is None else scored,
        {} if latest is None else latest,
        AccountJournal() if journal is None else journal,
        growth,
        shelfware,
        as_of,
    )


def _recent_text(text: str, subject: str = "") -> str:
    journal = AccountJournal(
        notes=[_contact(AS_OF - timedelta(days=1), text=text, subject=subject)]
    )
    return _sheet(journal=journal).recent[0].text


# --- Plain fields -------------------------------------------------------------------------


def test_plain_fields_come_from_the_account_and_the_score():
    scored = _scored(_account(arr=72_000), health=Health.WATCH, risk=27.5)
    sheet = _sheet(scored)
    assert sheet.account_id == "acc-1"
    assert sheet.name == "Acme"
    assert sheet.owner == "Dana"
    assert sheet.arr == 72_000
    assert sheet.renewal_date == RENEWAL
    assert sheet.health is Health.WATCH
    assert sheet.motion is Motion.RENEWAL
    assert sheet.risk == 27.5


@pytest.mark.parametrize(
    ("renewal", "days"),
    [
        (AS_OF + timedelta(days=5), 5),
        (AS_OF, 0),
        (AS_OF - timedelta(days=5), -5),
    ],
)
def test_days_to_renewal_is_negative_once_the_date_has_passed(renewal, days):
    sheet = _sheet(_scored(_account(renewal_date=renewal)))
    assert sheet.days_to_renewal == days


@pytest.mark.parametrize(
    ("value", "seats"),
    [(62.5, 63), (62.4, 62), (100.5, 101), (70.0, 70)],
)
def test_seats_used_is_the_whole_percent_half_up(value, seats):
    sheet = _sheet(latest=_latest({SEATS: value}))
    assert sheet.seats_used == seats


def test_seats_used_is_none_without_a_reading():
    assert _sheet().seats_used is None


def test_unused_arr_comes_from_shelfware_or_is_none():
    assert _sheet(shelfware=_shelf(unused_arr=19_000.0)).unused_arr == 19_000.0
    assert _sheet().unused_arr is None


def test_a_real_score_gives_a_sheet_with_its_reasons_and_asks():
    account = _account(start_date=None)
    latest = _latest({USAGE: -38.0, OPEN: 2.0, CHAMPION: 1.0})
    scored = score_account(account, latest, ScoringConfig(), AS_OF)
    sheet = _sheet(scored, latest=latest)
    assert sheet.health is scored.health
    assert sheet.exposures[: len(scored.reasons)] == [
        f"{reason.plain_english}." for reason in scored.reasons
    ]
    assert [ask.ask for ask in sheet.asks] == [
        "A credit or discount for the open issues",
        "A shorter term or an exit clause",
        "Time to bring in their new decision-maker",
    ]


# --- Customer years -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "as_of", "years"),
    [
        (date(2020, 3, 1), date(2026, 2, 28), 5),  # the sixth anniversary is tomorrow
        (date(2020, 3, 1), date(2026, 3, 1), 6),  # on the anniversary: counted
        (date(2024, 2, 29), date(2026, 2, 27), 1),  # 28 February stands in for 29 February
        (date(2024, 2, 29), date(2026, 2, 28), 2),
        (date(2024, 2, 29), date(2028, 2, 28), 3),  # 2028 has a 29 February
        (date(2024, 2, 29), date(2028, 2, 29), 4),
        (AS_OF, AS_OF, 0),
        (date(2026, 12, 1), AS_OF, 0),  # started after as_of: never negative
    ],
)
def test_customer_years_count_only_passed_anniversaries(start, as_of, years):
    sheet = _sheet(_scored(_account(start_date=start)), as_of=as_of)
    assert sheet.customer_years == years


def test_customer_years_is_none_without_a_start_date():
    sheet = _sheet(_scored(_account(start_date=None)))
    assert sheet.customer_years is None
    assert not any(line.startswith("A customer") for line in sheet.strengths)


def test_tenure_is_a_strength_from_two_whole_years():
    one_year = _sheet(_scored(_account(start_date=date(2024, 10, 20))))
    two_years = _sheet(_scored(_account(start_date=date(2024, 10, 19))))
    assert not any(line.startswith("A customer") for line in one_year.strengths)
    assert "A customer for 2 years." in two_years.strengths


# --- Strengths ----------------------------------------------------------------------------


def test_strengths_follow_the_contract_order():
    scored = _scored(_account(start_date=date(2024, 1, 1)))  # no reasons: "No risk signals."
    growth = _growth(_grow(GrowthKind.SEATS, "Seats 96% used"))
    journal = AccountJournal(
        notes=[_contact(AS_OF - timedelta(days=1))],
        outcomes=[_outcome(OutcomeKind.EXPANDED, AS_OF - timedelta(days=30))],
    )
    sheet = _sheet(scored, journal=journal, growth=growth)
    assert sheet.strengths == [
        "Seats 96% used.",
        "No risk signals.",
        "A customer for 2 years.",
        "They expanded at the last renewal.",
        "You were in touch yesterday.",
    ]


def test_each_growth_reason_is_a_strength_in_the_radar_order():
    growth = _growth(
        _grow(GrowthKind.SEATS, "Seats 96% used", 35.0),
        _grow(GrowthKind.USAGE, "Active users up 22% in 30 days", 20.0),
        _grow(GrowthKind.NPS, "NPS 9, a promoter", 10.0),
    )
    scored = _scored(
        _account(start_date=None), reasons=[_reason(USAGE, "Active users down 5% in 30 days")]
    )
    sheet = _sheet(scored, growth=growth)
    assert sheet.strengths == [
        "Seats 96% used.",
        "Active users up 22% in 30 days.",
        "NPS 9, a promoter.",
    ]


def test_held_back_growth_adds_no_strengths():
    growth = _growth(_grow(GrowthKind.SEATS, "Seats 96% used"), held_back=True)
    scored = _scored(
        _account(start_date=None),
        health=Health.AT_RISK,
        reasons=[_reason(USAGE, "Active users down 38% in 30 days", 30.0)],
    )
    assert _sheet(scored, growth=growth).strengths == []


def test_no_growth_radar_and_an_empty_radar_add_no_strengths():
    scored = _scored(_account(start_date=None), reasons=[_reason(USAGE, "Flat", 5.0)])
    assert _sheet(scored, growth=None).strengths == []
    assert _sheet(scored, growth=_growth()).strengths == []


def test_no_risk_signals_is_a_strength_only_without_reasons():
    clean = _sheet(_scored(_account(start_date=None)))
    assert clean.strengths == ["No risk signals."]
    risky = _sheet(
        _scored(_account(start_date=None), reasons=[_reason(USAGE, "Flat over 30 days", 5.0)])
    )
    assert "No risk signals." not in risky.strengths


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (OutcomeKind.EXPANDED, True),
        (OutcomeKind.RENEWED, False),
        (OutcomeKind.CONTRACTED, False),
        (OutcomeKind.CHURNED, False),
    ],
)
def test_expanded_at_the_last_renewal_is_a_strength(kind, expected):
    journal = AccountJournal(outcomes=[_outcome(kind, AS_OF - timedelta(days=30))])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert ("They expanded at the last renewal." in sheet.strengths) is expected


@pytest.mark.parametrize(
    ("days_ago", "sentence"),
    [
        (0, "You were in touch today."),
        (1, "You were in touch yesterday."),
        (2, "You were in touch 2 days ago."),
        (14, "You were in touch 14 days ago."),
    ],
)
def test_recent_contact_is_a_strength_with_the_day_in_words(days_ago, sentence):
    journal = AccountJournal(notes=[_contact(AS_OF - timedelta(days=days_ago))])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert sheet.strengths == ["No risk signals.", sentence]


@pytest.mark.parametrize(("days_ago", "touched"), [(14, True), (15, False)])
def test_contact_is_a_strength_up_to_fourteen_days(days_ago, touched):
    journal = AccountJournal(notes=[_contact(AS_OF - timedelta(days=days_ago))])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert any(line.startswith("You were in touch") for line in sheet.strengths) is touched


def test_contact_dated_after_as_of_reads_as_today():
    journal = AccountJournal(notes=[_contact(AS_OF + timedelta(days=1))])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert "You were in touch today." in sheet.strengths
    assert not any("No contact" in line for line in sheet.exposures)


def test_the_latest_contact_of_any_contact_kind_counts():
    journal = AccountJournal(
        notes=[
            _contact(AS_OF - timedelta(days=20), TouchKind.CALL, note_id="n-a"),
            _contact(AS_OF - timedelta(days=3), TouchKind.EMAIL, note_id="n-b"),
            _contact(AS_OF - timedelta(days=10), TouchKind.NOTE, note_id="n-c"),
        ]
    )
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert "You were in touch 3 days ago." in sheet.strengths
    assert sheet.exposures == []


# --- Exposures ----------------------------------------------------------------------------


def test_exposures_list_the_first_five_reasons_in_score_order():
    reasons = [_reason(USAGE, f"Reason {number}", 10.0 - number) for number in range(1, 7)]
    sheet = _sheet(_scored(_account(start_date=None), reasons=reasons), journal=_in_touch())
    assert sheet.exposures == [f"Reason {number}." for number in range(1, 6)]
    assert MAX_EXPOSURES == 5


def test_unused_seats_are_an_exposure_only_with_shelfware():
    with_shelf = _sheet(
        _scored(_account(start_date=None)), journal=_in_touch(), shelfware=_shelf(38)
    )
    assert with_shelf.exposures == ["38% of seats are unused."]
    without = _sheet(_scored(_account(start_date=None)), journal=_in_touch())
    assert without.exposures == []


@pytest.mark.parametrize(
    ("kind", "exposures"),
    [
        (OutcomeKind.CONTRACTED, ["They contracted at the last renewal."]),
        (OutcomeKind.CHURNED, ["They churned at the last renewal."]),
        (OutcomeKind.RENEWED, []),
        (OutcomeKind.EXPANDED, []),
    ],
)
def test_contracted_or_churned_at_the_last_renewal_is_an_exposure(kind, exposures):
    journal = AccountJournal(
        notes=_in_touch().notes, outcomes=[_outcome(kind, AS_OF - timedelta(days=30))]
    )
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert sheet.exposures == exposures


def test_unused_seats_are_not_repeated_when_the_score_already_names_the_seat_reading():
    reasons = [_reason(SignalKind.SEAT_UTILISATION, "Only 62% of seats in use", 12.0)]
    sheet = _sheet(
        _scored(_account(start_date=None), reasons=reasons),
        journal=_in_touch(),
        shelfware=_shelf(38),
    )
    assert sheet.exposures == ["Only 62% of seats in use."]
    # The ask still names the seats: it is what the customer will raise.
    assert [ask.ask for ask in sheet.asks] == ["Fewer seats at a lower price"]


def test_exposures_follow_the_contract_order():
    reasons = [
        _reason(USAGE, "Active users down 38% in 30 days", 30.0),
        _reason(OPEN, "2 open severity-1 tickets", 15.0),
    ]
    journal = AccountJournal(
        outcomes=[_outcome(OutcomeKind.CONTRACTED, AS_OF - timedelta(days=30))]
    )
    sheet = _sheet(
        _scored(_account(start_date=None), reasons=reasons),
        journal=journal,
        shelfware=_shelf(38),
    )
    assert sheet.exposures == [
        "Active users down 38% in 30 days.",
        "2 open severity-1 tickets.",
        "38% of seats are unused.",
        "They contracted at the last renewal.",
        "No contact logged yet.",
    ]


def test_no_contact_at_all_is_an_exposure():
    sheet = _sheet(_scored(_account(start_date=None)))
    assert sheet.exposures == ["No contact logged yet."]


def test_plain_notes_are_not_contact():
    journal = AccountJournal(notes=[_contact(AS_OF - timedelta(days=1), TouchKind.NOTE)])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert sheet.exposures == ["No contact logged yet."]
    assert sheet.strengths == ["No risk signals."]
    assert sheet.recent == []


@pytest.mark.parametrize(
    ("days_ago", "exposure"),
    [
        (29, None),
        (30, "No contact logged in 30 days."),
        (45, "No contact logged in 45 days."),
    ],
)
def test_quiet_for_thirty_days_is_an_exposure(days_ago, exposure):
    journal = AccountJournal(notes=[_contact(AS_OF - timedelta(days=days_ago))])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert sheet.exposures == ([] if exposure is None else [exposure])


# --- Likely asks --------------------------------------------------------------------------


def test_shelfware_gives_the_seat_ask():
    sheet = _sheet(_scored(_account(start_date=None)), shelfware=_shelf(38))
    assert sheet.asks == [
        LikelyAsk(ask="Fewer seats at a lower price", because="Only 62% of seats are in use.")
    ]


@pytest.mark.parametrize(
    ("tickets", "because"),
    [
        (0.0, None),
        (0.9, None),
        (1.0, "1 severity-1 ticket is still open."),
        (2.0, "2 severity-1 tickets are still open."),
    ],
)
def test_open_severity_one_tickets_ask_for_a_credit(tickets, because):
    sheet = _sheet(_scored(_account(start_date=None)), latest=_latest({OPEN: tickets}))
    expected = (
        []
        if because is None
        else [LikelyAsk(ask="A credit or discount for the open issues", because=because)]
    )
    assert sheet.asks == expected


@pytest.mark.parametrize(
    ("usage", "because"),
    [
        (-20.0, "Active users are down 20% in 30 days."),  # at the threshold: asks
        (-20.4, "Active users are down 20% in 30 days."),
        (-38.0, "Active users are down 38% in 30 days."),
        (-19.5, None),  # the raw reading is compared, so -19.5 does not ask
        (0.0, None),
    ],
)
def test_usage_down_twenty_percent_asks_for_a_shorter_term(usage, because):
    sheet = _sheet(_scored(_account(start_date=None)), latest=_latest({USAGE: usage}))
    expected = (
        []
        if because is None
        else [LikelyAsk(ask="A shorter term or an exit clause", because=because)]
    )
    assert sheet.asks == expected


@pytest.mark.parametrize(
    ("overdue", "because"),
    [
        (30.0, "An invoice is 30 days overdue."),
        (45.0, "An invoice is 45 days overdue."),
        (29.5, None),  # below the threshold: 29.5 would round to 30, but the raw reading decides
        (0.0, None),
    ],
)
def test_thirty_days_overdue_asks_for_different_payment_terms(overdue, because):
    sheet = _sheet(_scored(_account(start_date=None)), latest=_latest({BILLING: overdue}))
    expected = (
        [] if because is None else [LikelyAsk(ask="Different payment terms", because=because)]
    )
    assert sheet.asks == expected


@pytest.mark.parametrize(("left", "asks"), [(1.0, True), (0.0, False)])
def test_a_champion_who_left_asks_for_a_new_decision_maker(left, asks):
    sheet = _sheet(_scored(_account(start_date=None)), latest=_latest({CHAMPION: left}))
    expected = (
        [
            LikelyAsk(
                ask="Time to bring in their new decision-maker",
                because="Your champion has left.",
            )
        ]
        if asks
        else []
    )
    assert sheet.asks == expected


@pytest.mark.parametrize(
    ("kind", "asks"),
    [
        (OutcomeKind.CONTRACTED, True),
        (OutcomeKind.RENEWED, False),
        (OutcomeKind.EXPANDED, False),
        (OutcomeKind.CHURNED, False),
    ],
)
def test_contracted_at_the_last_renewal_asks_for_another_reduction(kind, asks):
    journal = AccountJournal(outcomes=[_outcome(kind, AS_OF - timedelta(days=30))])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    expected = (
        [LikelyAsk(ask="Another reduction", because="They contracted at the last renewal.")]
        if asks
        else []
    )
    assert sheet.asks == expected


def test_all_asks_come_in_the_contract_order():
    journal = AccountJournal(
        outcomes=[_outcome(OutcomeKind.CONTRACTED, AS_OF - timedelta(days=30))]
    )
    sheet = _sheet(
        _scored(_account(start_date=None)),
        latest=_latest({OPEN: 2.0, USAGE: -25.0, BILLING: 45.0, CHAMPION: 1.0}),
        journal=journal,
        shelfware=_shelf(38),
    )
    assert [ask.ask for ask in sheet.asks] == [
        "Fewer seats at a lower price",
        "A credit or discount for the open issues",
        "A shorter term or an exit clause",
        "Different payment terms",
        "Time to bring in their new decision-maker",
        "Another reduction",
    ]
    assert [ask.because for ask in sheet.asks] == [
        "Only 62% of seats are in use.",
        "2 severity-1 tickets are still open.",
        "Active users are down 25% in 30 days.",
        "An invoice is 45 days overdue.",
        "Your champion has left.",
        "They contracted at the last renewal.",
    ]


def test_a_clean_account_has_no_asks():
    assert _sheet(_scored(_account(start_date=None))).asks == []


# --- The last renewal ---------------------------------------------------------------------


def test_last_renewal_is_none_without_outcomes():
    assert _sheet().last_renewal is None


def test_last_renewal_is_the_latest_renewal_date_not_the_latest_recorded():
    journal = AccountJournal(
        outcomes=[
            _outcome(
                OutcomeKind.CHURNED,
                AS_OF - timedelta(days=365),
                recorded_at=datetime(2026, 1, 5, 9, 0),
            ),
            _outcome(
                OutcomeKind.EXPANDED,
                AS_OF - timedelta(days=5),
                recorded_at=datetime(2025, 11, 1, 9, 0),
            ),
        ]
    )
    last = _sheet(journal=journal).last_renewal
    assert last is not None
    assert last.kind is OutcomeKind.EXPANDED
    assert last.renewal_date == AS_OF - timedelta(days=5)


def test_last_renewal_ties_go_to_the_later_recording():
    renewal = AS_OF - timedelta(days=30)
    journal = AccountJournal(
        outcomes=[
            _outcome(OutcomeKind.CONTRACTED, renewal, recorded_at=datetime(2026, 10, 2, 9, 0)),
            _outcome(OutcomeKind.RENEWED, renewal, recorded_at=datetime(2026, 10, 1, 9, 0)),
        ]
    )
    last = _sheet(journal=journal).last_renewal
    assert last is not None
    assert last.kind is OutcomeKind.CONTRACTED


def test_last_renewal_carries_the_outcome_fields():
    journal = AccountJournal(
        outcomes=[
            _outcome(
                OutcomeKind.CONTRACTED,
                date(2026, 6, 1),
                decided_on=date(2026, 5, 20),
                arr_before=40_000,
                arr_after=30_000,
            )
        ]
    )
    assert _sheet(journal=journal).last_renewal == LastRenewal(
        kind=OutcomeKind.CONTRACTED,
        renewal_date=date(2026, 6, 1),
        decided_on=date(2026, 5, 20),
        arr_before=40_000,
        arr_after=30_000,
    )


# --- Your call against the score ----------------------------------------------------------


def test_no_call_means_no_call_and_no_disagreement():
    sheet = _sheet(_scored(_account(start_date=None), health=Health.AT_RISK))
    assert sheet.call is None
    assert sheet.disagreement is None


def test_your_call_on_this_renewal_is_shown():
    journal = AccountJournal(calls=[_call(CallKind.LIKELY)])
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert sheet.call is CallKind.LIKELY


def test_the_last_call_on_the_renewal_date_wins():
    journal = AccountJournal(calls=[_call(CallKind.COMMIT), _call(CallKind.AT_RISK)])
    sheet = _sheet(_scored(_account(start_date=None), health=Health.HEALTHY), journal=journal)
    assert sheet.call is CallKind.AT_RISK
    assert sheet.disagreement is Disagreement.CALL_LOWER


def test_a_call_on_an_older_renewal_date_is_not_this_renewal():
    journal = AccountJournal(calls=[_call(CallKind.AT_RISK, RENEWAL - timedelta(days=365))])
    sheet = _sheet(_scored(_account(start_date=None), health=Health.HEALTHY), journal=journal)
    assert sheet.call is None
    assert sheet.disagreement is None


def test_a_stale_call_does_not_override_this_renewals_call():
    journal = AccountJournal(
        calls=[_call(CallKind.COMMIT), _call(CallKind.AT_RISK, RENEWAL - timedelta(days=365))]
    )
    sheet = _sheet(_scored(_account(start_date=None), health=Health.HEALTHY), journal=journal)
    assert sheet.call is CallKind.COMMIT
    assert sheet.disagreement is None


@pytest.mark.parametrize(
    ("call", "health", "expected"),
    [
        (CallKind.COMMIT, Health.AT_RISK, Disagreement.CALL_HIGHER),
        (CallKind.LIKELY, Health.AT_RISK, Disagreement.CALL_HIGHER),
        (CallKind.AT_RISK, Health.HEALTHY, Disagreement.CALL_LOWER),
        (CallKind.COMMIT, Health.WATCH, None),
        (CallKind.COMMIT, Health.HEALTHY, None),
        (CallKind.LIKELY, Health.WATCH, None),
        (CallKind.LIKELY, Health.HEALTHY, None),
        (CallKind.AT_RISK, Health.WATCH, None),
        (CallKind.AT_RISK, Health.AT_RISK, None),
    ],
)
def test_disagreement_between_your_call_and_the_score(call, health, expected):
    journal = AccountJournal(calls=[_call(call)])
    sheet = _sheet(_scored(_account(start_date=None), health=health), journal=journal)
    assert sheet.call is call
    assert sheet.disagreement is expected


# --- Recent contact -----------------------------------------------------------------------


def test_recent_is_contact_only_newest_first_and_at_most_three():
    journal = AccountJournal(
        notes=[
            _contact(AS_OF - timedelta(days=2), TouchKind.CALL, text="Call", note_id="n-1"),
            _contact(AS_OF - timedelta(days=1), TouchKind.NOTE, text="Plain", note_id="n-2"),
            _contact(AS_OF - timedelta(days=5), TouchKind.MEETING, text="Meeting", note_id="n-3"),
            _contact(AS_OF - timedelta(days=1), TouchKind.EMAIL, text="Email", note_id="n-4"),
            _contact(AS_OF - timedelta(days=9), TouchKind.OTHER, text="Other", note_id="n-5"),
        ]
    )
    sheet = _sheet(_scored(_account(start_date=None)), journal=journal)
    assert [(item.kind_label, item.text) for item in sheet.recent] == [
        ("Email", "Email"),
        ("Call", "Call"),
        ("Meeting", "Meeting"),
    ]
    assert len(sheet.recent) == MAX_RECENT


def test_same_day_contacts_list_the_latest_created_first():
    journal = AccountJournal(
        notes=[
            _contact(
                AS_OF - timedelta(days=3),
                text="Earlier",
                created_at=datetime(2026, 10, 18, 9, 0),
                note_id="n-a",
            ),
            _contact(
                AS_OF - timedelta(days=3),
                text="Later",
                created_at=datetime(2026, 10, 18, 10, 0),
                note_id="n-b",
            ),
        ]
    )
    sheet = _sheet(journal=journal)
    assert [item.text for item in sheet.recent] == ["Later", "Earlier"]


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        (TouchKind.CALL, "Call"),
        (TouchKind.MEETING, "Meeting"),
        (TouchKind.EMAIL, "Email"),
        (TouchKind.OTHER, "Other"),
    ],
)
def test_recent_entries_carry_the_kind_label(kind, label):
    journal = AccountJournal(notes=[_contact(AS_OF - timedelta(days=1), kind)])
    assert _sheet(journal=journal).recent == [
        RecentContact(
            happened_on=AS_OF - timedelta(days=1),
            kind_label=label,
            text="Talked through the plan",
        )
    ]


def test_recent_text_is_the_subject_when_there_is_one():
    journal = AccountJournal(
        notes=[
            _contact(
                AS_OF - timedelta(days=1),
                TouchKind.EMAIL,
                text="A long body of the email",
                subject="Renewal plan",
            )
        ]
    )
    assert _sheet(journal=journal).recent == [
        RecentContact(
            happened_on=AS_OF - timedelta(days=1), kind_label="Email", text="Renewal plan"
        )
    ]


def test_recent_text_is_the_first_line_without_a_subject():
    assert _recent_text("Agreed the pilot\nSecond line about pricing") == "Agreed the pilot"


def test_a_text_of_exactly_120_characters_is_not_cut():
    line = "y" * 120
    assert _recent_text(line) == line


def test_a_longer_text_is_cut_to_119_characters_and_an_ellipsis():
    text = _recent_text("x" * 121)
    assert text == "x" * 119 + "…"
    assert len(text) == 120


def test_the_cut_drops_spaces_before_the_ellipsis():
    line = "a" * 118 + " " + "b" * 20  # the first 119 characters end in a space
    assert _recent_text(line) == "a" * 118 + "…"


def test_a_long_first_line_is_cut_before_the_next_line():
    assert _recent_text("z" * 130 + "\nsecond line") == "z" * 119 + "…"


def test_a_long_subject_is_cut_too():
    assert _recent_text("body", subject="s" * 200) == "s" * 119 + "…"


def test_recent_is_empty_without_contact():
    assert _sheet().recent == []


# --- Open follow-ups ----------------------------------------------------------------------


def test_open_follow_ups_soonest_first_then_created_first_and_at_most_three():
    journal = AccountJournal(
        follow_ups=[
            _follow_up("Send the ROI deck", AS_OF + timedelta(days=10)),
            _follow_up("Call the sponsor", AS_OF - timedelta(days=2)),
            _follow_up("Done already", AS_OF - timedelta(days=20), status=FollowUpStatus.DONE),
            _follow_up(
                "Chase legal", AS_OF + timedelta(days=3), created_at=datetime(2026, 10, 18, 10, 0)
            ),
            _follow_up("Book the QBR", AS_OF + timedelta(days=3), created_at=READ_AT),
            _follow_up("Much later", AS_OF + timedelta(days=30)),
        ]
    )
    sheet = _sheet(journal=journal)
    assert sheet.follow_ups == ["Call the sponsor", "Book the QBR", "Chase legal"]
    assert len(sheet.follow_ups) == MAX_FOLLOW_UPS
