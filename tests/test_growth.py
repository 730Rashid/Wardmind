"""Pure tests of core/growth.py: the funding news window, the four ramps and their points, the
reasons and their sentences, the score and its bands, and the held-back rule. No I/O, no clock."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from wardmind.core.growth import (
    FUNDING_DAYS,
    FUNDING_POINTS,
    MAX_HEADLINE,
    NPS_POINTS,
    SEATS_POINTS,
    USAGE_POINTS,
    FundingNews,
    GrowthBand,
    GrowthKind,
    GrowthRadar,
    book_growth,
    funding_news,
    growth_radar,
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
from wardmind.core.research import Article, Topic

AS_OF = date(2026, 10, 22)
SEEN = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
SEATS = SignalKind.SEAT_UTILISATION
USAGE = SignalKind.USAGE_TREND_30D
NPS = SignalKind.NPS_LATEST
TITLE = "Acme raises Series B to hire support"
FUNDED = f'Their site announced funding: "{TITLE}"'
FUNDED_MID_SENTENCE = f'their site announced funding: "{TITLE}"'
NO_SIGNS = "No signs of room to grow yet."
HELD_BACK = "Held back while this account is At risk. Fix the risk before talking about growth."


def _scored(health: Health = Health.HEALTHY, account_id: str = "acc-1") -> ScoredAccount:
    account = Account(
        id=account_id,
        name="Acme Corporation",
        arr=120_000,
        tier="Growth",
        owner="Sam Lee",
        renewal_date=date(2027, 1, 10),
        milestones=Milestones(done=0, total=0),
        domain="acme.example",
    )
    return ScoredAccount(
        account=account,
        motion=Motion.RENEWAL,
        health=health,
        risk=0,
        priority=0,
        reasons=[],
    )


def _latest(values: dict[SignalKind, float]) -> dict[SignalKind, Signal]:
    return {
        kind: Signal(account_id="acc-1", kind=kind, value=value, observed_at=SEEN)
        for kind, value in values.items()
    }


def _news(title: str = TITLE, published: date | None = date(2026, 10, 5)) -> FundingNews:
    return FundingNews(title=title, published=published)


def _radar(
    values: dict[SignalKind, float] | None = None,
    funding: FundingNews | None = None,
    health: Health = Health.HEALTHY,
) -> GrowthRadar:
    return growth_radar(_scored(health), _latest(values or {}), funding)


def _article(
    title: str = TITLE,
    published: date | None = AS_OF - timedelta(days=30),
    topics: tuple[Topic, ...] = (Topic.FUNDING,),
) -> Article:
    return Article(
        title=title,
        url="https://acme.example/news/item",
        published=published,
        topics=list(topics),
    )


def _points(radar: GrowthRadar, kind: GrowthKind) -> float:
    return next(reason.points for reason in radar.reasons if reason.kind is kind)


def _kinds(radar: GrowthRadar) -> list[GrowthKind]:
    return [reason.kind for reason in radar.reasons]


def _texts(radar: GrowthRadar) -> list[str]:
    return [reason.plain_english for reason in radar.reasons]


# --- funding_news: the window -----------------------------------------------------------------


def test_a_funding_article_inside_the_window_is_the_news() -> None:
    article = _article(published=AS_OF - timedelta(days=17))
    assert funding_news([article], AS_OF) == FundingNews(
        title=TITLE, published=AS_OF - timedelta(days=17)
    )


def test_exactly_funding_days_before_as_of_still_counts() -> None:
    day = AS_OF - timedelta(days=FUNDING_DAYS)
    assert funding_news([_article(published=day)], AS_OF) == FundingNews(title=TITLE, published=day)


def test_one_day_more_than_the_window_does_not_count() -> None:
    too_old = AS_OF - timedelta(days=FUNDING_DAYS + 1)
    assert funding_news([_article(published=too_old)], AS_OF) is None


def test_an_article_published_on_as_of_counts() -> None:
    assert funding_news([_article(published=AS_OF)], AS_OF) == FundingNews(
        title=TITLE, published=AS_OF
    )


def test_a_future_dated_article_does_not_count() -> None:
    assert funding_news([_article(published=AS_OF + timedelta(days=1))], AS_OF) is None


def test_the_window_moves_with_as_of() -> None:
    day = AS_OF - timedelta(days=FUNDING_DAYS)
    article = _article(published=day)
    assert funding_news([article], AS_OF) is not None
    assert funding_news([article], AS_OF + timedelta(days=1)) is None


def test_the_whole_title_is_kept_in_the_news_even_when_long() -> None:
    long_title = "Acme raises " + "x" * 200
    news = funding_news([_article(title=long_title, published=AS_OF)], AS_OF)
    assert news is not None
    assert news.title == long_title


# --- funding_news: which articles --------------------------------------------------------------


@pytest.mark.parametrize(
    "topics",
    [(Topic.LEADERSHIP,), (Topic.ACQUISITION,), (Topic.RESTRUCTURING,), ()],
    ids=["leadership", "acquisition", "restructuring", "untagged"],
)
def test_articles_that_are_not_about_funding_are_ignored(topics: tuple[Topic, ...]) -> None:
    article = _article(topics=topics, published=AS_OF)
    assert funding_news([article], AS_OF) is None


def test_a_funding_article_tagged_with_other_topics_too_counts() -> None:
    article = _article(topics=(Topic.ACQUISITION, Topic.FUNDING), published=AS_OF)
    assert funding_news([article], AS_OF) == FundingNews(title=TITLE, published=AS_OF)


def test_the_newest_dated_funding_article_wins_whatever_the_order() -> None:
    older = _article("Acme raises a seed round", AS_OF - timedelta(days=100))
    newer = _article("Acme raises Series B", AS_OF - timedelta(days=10))
    expected = FundingNews(title="Acme raises Series B", published=AS_OF - timedelta(days=10))
    assert funding_news([older, newer], AS_OF) == expected
    assert funding_news([newer, older], AS_OF) == expected


def test_on_the_same_day_the_first_article_in_the_list_wins() -> None:
    day = AS_OF - timedelta(days=5)
    first = _article("Acme raises Series A", day)
    second = _article("Acme raises Series B", day)
    assert funding_news([first, second], AS_OF) == FundingNews(
        title="Acme raises Series A", published=day
    )


def test_a_dated_article_beats_an_undated_one() -> None:
    undated = _article("Acme raises Series C", None)
    dated = _article(TITLE, AS_OF - timedelta(days=5))
    expected = FundingNews(title=TITLE, published=AS_OF - timedelta(days=5))
    assert funding_news([undated, dated], AS_OF) == expected


def test_an_undated_article_is_used_when_no_dated_one_qualifies() -> None:
    too_old = _article("Acme raises Series A", AS_OF - timedelta(days=FUNDING_DAYS + 1))
    future = _article("Acme raises Series Z", AS_OF + timedelta(days=3))
    undated = _article("Acme raises Series B", None)
    assert funding_news([too_old, future, undated], AS_OF) == FundingNews(
        title="Acme raises Series B", published=None
    )


def test_the_first_undated_article_wins() -> None:
    first = _article("Acme raises Series A", None)
    second = _article("Acme raises Series B", None)
    assert funding_news([first, second], AS_OF) == FundingNews(
        title="Acme raises Series A", published=None
    )


def test_an_undated_non_funding_article_is_ignored() -> None:
    article = _article("Acme names a new CTO", None, topics=(Topic.LEADERSHIP,))
    assert funding_news([article], AS_OF) is None


def test_no_articles_gives_no_news() -> None:
    assert funding_news([], AS_OF) is None


# --- growth_radar: the four ramps --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "points"),
    [
        pytest.param(79.9, None, id="below-floor"),
        pytest.param(80.0, None, id="at-floor"),
        pytest.param(85.0, 8.75, id="quarter-way"),
        pytest.param(90.0, 17.5, id="midpoint"),
        pytest.param(100.0, 35.0, id="at-top"),
        pytest.param(150.0, 35.0, id="above-top-capped"),
    ],
)
def test_seats_ramp_from_80_to_100_percent(value: float, points: float | None) -> None:
    radar = _radar({SEATS: value})
    if points is None:
        assert GrowthKind.SEATS not in _kinds(radar)
    else:
        assert _points(radar, GrowthKind.SEATS) == pytest.approx(points)


@pytest.mark.parametrize(
    ("value", "points"),
    [
        pytest.param(-5.0, None, id="below-floor"),
        pytest.param(0.0, None, id="at-floor"),
        pytest.param(15.0, 15.0, id="midpoint"),
        pytest.param(30.0, 30.0, id="at-top"),
        pytest.param(45.0, 30.0, id="above-top-capped"),
    ],
)
def test_usage_ramp_from_0_to_30_percent(value: float, points: float | None) -> None:
    radar = _radar({USAGE: value})
    if points is None:
        assert GrowthKind.USAGE not in _kinds(radar)
    else:
        assert _points(radar, GrowthKind.USAGE) == pytest.approx(points)


@pytest.mark.parametrize(
    ("value", "points"),
    [
        pytest.param(6.0, None, id="below-floor"),
        pytest.param(7.0, None, id="at-floor"),
        pytest.param(8.5, 10.0, id="midpoint"),
        pytest.param(10.0, 20.0, id="at-top"),
        pytest.param(12.0, 20.0, id="above-top-capped"),
    ],
)
def test_nps_ramp_from_7_to_10(value: float, points: float | None) -> None:
    radar = _radar({NPS: value})
    if points is None:
        assert GrowthKind.NPS not in _kinds(radar)
    else:
        assert _points(radar, GrowthKind.NPS) == pytest.approx(points)


def test_funding_adds_its_points_only_when_there_is_news() -> None:
    assert _points(_radar({}, _news()), GrowthKind.FUNDING) == FUNDING_POINTS
    assert _kinds(_radar({}, None)) == []


def test_the_four_parts_can_add_up_to_one_hundred() -> None:
    assert SEATS_POINTS + USAGE_POINTS + NPS_POINTS + FUNDING_POINTS == 100
    radar = _radar({SEATS: 100.0, USAGE: 30.0, NPS: 10.0}, _news())
    assert radar.score == 100
    assert radar.band is GrowthBand.STRONG


def test_a_part_with_no_reading_earns_nothing() -> None:
    radar = _radar({USAGE: 10.0})
    assert _kinds(radar) == [GrowthKind.USAGE]


# --- growth_radar: reasons ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "text"),
    [
        pytest.param({SEATS: 95.5}, "Seats 96% used", id="seats-half-up"),
        pytest.param({SEATS: 80.4}, "Seats 80% used", id="seats-rounds-down"),
        pytest.param({USAGE: 21.5}, "Active users up 22% in 30 days", id="usage-half-up"),
        pytest.param({USAGE: 10.0}, "Active users up 10% in 30 days", id="usage-plain"),
        pytest.param({NPS: 7.5}, "NPS 8", id="nps-half-up-no-promoter"),
        pytest.param({NPS: 8.4}, "NPS 8", id="nps-below-promoter"),
        pytest.param({NPS: 8.5}, "NPS 9, a promoter", id="nps-half-up-to-promoter"),
        pytest.param({NPS: 9.0}, "NPS 9, a promoter", id="nps-promoter"),
        pytest.param({NPS: 10.0}, "NPS 10, a promoter", id="nps-top"),
    ],
)
def test_reason_wording_for_each_part(values: dict[SignalKind, float], text: str) -> None:
    radar = _radar(values)
    assert _texts(radar) == [text]


def test_the_funding_reason_quotes_the_headline() -> None:
    assert _texts(_radar({}, _news())) == [FUNDED]


def test_a_title_of_exactly_max_headline_characters_is_not_cut() -> None:
    title = "Acme raises money " + "x" * (MAX_HEADLINE - len("Acme raises money "))
    assert len(title) == MAX_HEADLINE
    assert _texts(_radar({}, _news(title=title))) == [f'Their site announced funding: "{title}"']


def test_a_longer_title_is_cut_so_the_ellipsis_ends_it_at_max_headline_characters() -> None:
    title = "Acme raises money " + "x" * 100
    text = _texts(_radar({}, _news(title=title)))[0]
    quoted = text.removeprefix('Their site announced funding: "').removesuffix('"')
    assert quoted == title[: MAX_HEADLINE - 1] + "…"
    assert len(quoted) == MAX_HEADLINE


def test_the_cut_drops_a_space_left_before_the_ellipsis() -> None:
    title = "a" * 78 + " " + "b" * 20
    text = _texts(_radar({}, _news(title=title)))[0]
    assert text == f'Their site announced funding: "{"a" * 78}…"'


def test_reasons_are_listed_most_points_first() -> None:
    radar = _radar({SEATS: 90.0, USAGE: 10.0}, _news())
    assert _kinds(radar) == [GrowthKind.SEATS, GrowthKind.FUNDING, GrowthKind.USAGE]


def test_a_tie_goes_in_growth_kind_order_not_insertion_order() -> None:
    radar = _radar({USAGE: 17.5, SEATS: 90.0})
    assert _kinds(radar) == [GrowthKind.SEATS, GrowthKind.USAGE]


def test_a_tie_between_usage_and_funding_puts_usage_first() -> None:
    radar = _radar({USAGE: 15.0}, _news())
    assert _points(radar, GrowthKind.USAGE) == _points(radar, GrowthKind.FUNDING)
    assert _kinds(radar) == [GrowthKind.USAGE, GrowthKind.FUNDING]


def test_a_tie_between_nps_and_funding_puts_nps_first() -> None:
    radar = _radar({NPS: 9.25}, _news())
    assert _points(radar, GrowthKind.NPS) == _points(radar, GrowthKind.FUNDING) == 15.0
    assert _kinds(radar) == [GrowthKind.NPS, GrowthKind.FUNDING]


def test_a_tie_between_usage_and_nps_puts_usage_first() -> None:
    radar = _radar({NPS: 10.0, USAGE: 20.0})
    assert _kinds(radar) == [GrowthKind.USAGE, GrowthKind.NPS]


# --- growth_radar: score and bands -------------------------------------------------------------


def test_the_score_rounds_half_up() -> None:
    radar = _radar({SEATS: 90.0})  # 17.5 points
    assert radar.score == 18
    assert radar.band is GrowthBand.NONE


def test_a_true_half_is_not_lost_to_float_error() -> None:
    # 1.4 + 0.1 is 1.5, but the float sum is 1.4999999999999951; the score must still be 2.
    radar = _radar({SEATS: 80.8, USAGE: 0.1})
    assert radar.score == 2


def test_half_up_can_lift_an_account_across_a_band_line() -> None:
    radar = _radar({USAGE: 9.5}, _news())  # 24.5 points
    assert radar.score == 25
    assert radar.band is GrowthBand.SOME


def test_just_under_half_stays_down() -> None:
    radar = _radar({USAGE: 9.4}, _news())  # 24.4 points
    assert radar.score == 24
    assert radar.band is GrowthBand.NONE


@pytest.mark.parametrize(
    ("values", "funding", "score", "band"),
    [
        pytest.param({USAGE: 9.0}, _news(), 24, GrowthBand.NONE, id="24-is-none"),
        pytest.param({USAGE: 10.0}, _news(), 25, GrowthBand.SOME, id="25-is-some"),
        pytest.param({SEATS: 100.0, USAGE: 14.0}, None, 49, GrowthBand.SOME, id="49-is-some"),
        pytest.param({SEATS: 100.0, USAGE: 14.5}, None, 50, GrowthBand.STRONG, id="49.5-is-50"),
        pytest.param({SEATS: 100.0, USAGE: 15.0}, None, 50, GrowthBand.STRONG, id="50-is-strong"),
        pytest.param({}, None, 0, GrowthBand.NONE, id="nothing-is-none"),
        pytest.param({SEATS: 80.0}, None, 0, GrowthBand.NONE, id="at-floor-is-zero"),
    ],
)
def test_bands_change_at_25_and_50(
    values: dict[SignalKind, float],
    funding: FundingNews | None,
    score: int,
    band: GrowthBand,
) -> None:
    radar = _radar(values, funding)
    assert radar.score == score
    assert radar.band is band
    assert radar.held_back is False


def test_band_labels() -> None:
    assert GrowthBand.STRONG.label == "Ready to grow"
    assert GrowthBand.SOME.label == "Some room"
    assert GrowthBand.NONE.label == "No signs yet"


# --- growth_radar: sentences -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "funding", "sentence"),
    [
        pytest.param(
            {SEATS: 96.0, USAGE: 22.0, NPS: 9.0},
            None,
            "Ready to grow: seats 96% used, active users up 22% in 30 days and NPS 9, a promoter.",
            id="strong-three-reasons-keeps-nps-capitals",
        ),
        pytest.param(
            {SEATS: 100.0, USAGE: 30.0, NPS: 10.0},
            _news(),
            "Ready to grow: seats 100% used, active users up 30% in 30 days and NPS 10, "
            "a promoter.",
            id="four-reasons-names-only-the-top-three",
        ),
        pytest.param(
            {SEATS: 96.0, USAGE: 22.0, NPS: 9.0},
            _news(),
            "Ready to grow: seats 96% used, active users up 22% in 30 days and "
            f"{FUNDED_MID_SENTENCE}.",
            id="funding-can-be-one-of-the-top-three",
        ),
        pytest.param(
            {SEATS: 90.0, USAGE: 10.0},
            None,
            "Some room to grow: seats 90% used and active users up 10% in 30 days.",
            id="some-two-reasons",
        ),
        pytest.param(
            {SEATS: 100.0},
            None,
            "Some room to grow: seats 100% used.",
            id="some-one-reason",
        ),
        pytest.param(
            {USAGE: 20.0},
            _news(),
            f"Some room to grow: active users up 20% in 30 days and {FUNDED_MID_SENTENCE}.",
            id="some-funding-lower-cased-after-the-first",
        ),
        pytest.param(
            {USAGE: 9.0},
            _news(),
            f"No strong signs yet: {FUNDED_MID_SENTENCE} and active users up 9% in 30 days.",
            id="none-funding-first-is-lower-cased-too",
        ),
        pytest.param(
            {NPS: 8.0},
            None,
            "No strong signs yet: NPS 8.",
            id="none-one-reason",
        ),
        pytest.param(
            {USAGE: 5.0, NPS: 8.0},
            None,
            "No strong signs yet: NPS 8 and active users up 5% in 30 days.",
            id="none-two-reasons",
        ),
        pytest.param(
            {SEATS: 81.0, USAGE: 5.0, NPS: 8.0},
            None,
            "No strong signs yet: NPS 8, active users up 5% in 30 days and seats 81% used.",
            id="none-three-reasons-lower-cases-seats",
        ),
        pytest.param({}, None, NO_SIGNS, id="no-readings"),
    ],
)
def test_the_sentence_names_the_top_reasons(
    values: dict[SignalKind, float], funding: FundingNews | None, sentence: str
) -> None:
    assert _radar(values, funding).sentence == sentence


def test_a_tiny_score_says_no_signs_even_though_a_reason_is_listed() -> None:
    # Points > 0 still list the seats reason (0.175 points), but the score rounds to 0, so the
    # sentence says there are no signs yet and names nothing.
    radar = _radar({SEATS: 80.1})
    assert radar.score == 0
    assert radar.band is GrowthBand.NONE
    assert _kinds(radar) == [GrowthKind.SEATS]
    assert radar.sentence == NO_SIGNS


# --- growth_radar: held back -------------------------------------------------------------------


def test_an_account_at_risk_is_held_back_whatever_its_readings_and_funding() -> None:
    radar = _radar({SEATS: 100.0, USAGE: 30.0, NPS: 10.0}, _news(), Health.AT_RISK)
    assert radar == GrowthRadar(
        score=0,
        band=GrowthBand.NONE,
        reasons=[],
        held_back=True,
        sentence=HELD_BACK,
    )


def test_an_account_at_risk_with_no_readings_is_held_back_too() -> None:
    radar = _radar({}, None, Health.AT_RISK)
    assert radar.held_back is True
    assert radar.sentence == HELD_BACK


@pytest.mark.parametrize("health", [Health.WATCH, Health.HEALTHY], ids=["watch", "healthy"])
def test_watch_and_healthy_accounts_are_not_held_back(health: Health) -> None:
    radar = _radar({SEATS: 100.0, USAGE: 30.0, NPS: 10.0}, _news(), health)
    assert radar.held_back is False
    assert radar.score == 100
    assert radar.sentence.startswith("Ready to grow: ")


# --- book_growth -------------------------------------------------------------------------------


def test_book_growth_is_keyed_by_account_id_with_defaults_for_missing_data() -> None:
    book = [
        _scored(account_id="a"),
        _scored(account_id="b"),
        _scored(account_id="c", health=Health.AT_RISK),
        _scored(account_id="d"),
    ]
    latest = {"a": _latest({SEATS: 90.0}), "c": _latest({USAGE: 30.0})}
    funding = {"b": _news()}

    result = book_growth(book, latest, funding)

    assert set(result) == {"a", "b", "c", "d"}
    assert result["a"] == growth_radar(book[0], latest["a"], None)
    assert result["b"] == growth_radar(book[1], {}, funding["b"])
    assert _kinds(result["b"]) == [GrowthKind.FUNDING]
    assert result["c"].held_back is True
    assert result["c"].score == 0
    assert result["d"].score == 0
    assert result["d"].reasons == []
    assert result["d"].sentence == NO_SIGNS


def test_book_growth_of_an_empty_book_is_empty() -> None:
    assert book_growth([], {}, {}) == {}
