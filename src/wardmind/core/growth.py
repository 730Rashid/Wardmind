"""Growth radar: a second, positive score for room to grow, with the same "why" as the risk score.

"Seats 96% used, active users up 22% in 30 days, NPS 9, and their site announced funding."
Account managers sort My book by it; the account page shows the reasons.

Pure and deterministic: no I/O and no clock reads (callers pass `as_of`). Each point comes from
the account's own latest readings or its own website's news, never from a model. An account
that is At risk is held back: growth talk with an unhappy customer backfires.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before
changing them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from wardmind.core.models import Health, ScoredAccount, Signal, SignalKind
from wardmind.core.research import Article, Topic
from wardmind.core.scoring import _whole

# Points each part can give; they add up to 100.
SEATS_POINTS = 35.0
USAGE_POINTS = 30.0
NPS_POINTS = 20.0
FUNDING_POINTS = 15.0

# Each reading earns points on a straight ramp from its floor (0 points) to its top (all points).
SEATS_FLOOR, SEATS_TOP = 80.0, 100.0  # seat utilisation, whole percent; 100% or more is full
USAGE_FLOOR, USAGE_TOP = 0.0, 30.0  # usage trend over 30 days, percent; +30% or more is full
NPS_FLOOR, NPS_TOP = 7.0, 10.0  # NPS 0-10; 7 or less earns nothing, 10 is full

FUNDING_DAYS = 180  # a funding headline counts for this many days after it was published
STRONG_FROM = 50  # score at or above: GrowthBand.STRONG
SOME_FROM = 25  # score at or above (and below STRONG_FROM): GrowthBand.SOME
MAX_HEADLINE = 80  # characters of a funding headline quoted in a reason (then "…")


class GrowthKind(StrEnum):
    """The parts of the growth score, in the order they are listed when points tie."""

    SEATS = "seats"
    USAGE = "usage"
    NPS = "nps"
    FUNDING = "funding"


class GrowthBand(StrEnum):
    STRONG = "strong"
    SOME = "some"
    NONE = "none"

    @property
    def label(self) -> str:
        return {"strong": "Ready to grow", "some": "Some room", "none": "No signs yet"}[self.value]


class FundingNews(BaseModel):
    """The newest funding headline from the account's own website (see funding_news)."""

    title: str
    published: date | None


class GrowthReason(BaseModel):
    kind: GrowthKind
    points: float = Field(gt=0)  # unrounded
    # Sentence case, no full stop:
    #   SEATS   -> f"Seats {u}% used"                      (u = whole percent, half up)
    #   USAGE   -> f"Active users up {t}% in 30 days"       (t = whole percent, half up)
    #   NPS     -> f"NPS {n}, a promoter" when n >= 9, else f"NPS {n}"   (n = whole, half up)
    #   FUNDING -> f'Their site announced funding: "{title}"'  (title cut to MAX_HEADLINE,
    #              ending "…" when cut)
    plain_english: str


class GrowthRadar(BaseModel):
    score: int = Field(ge=0, le=100)  # the points added up, rounded half up; 0 when held back
    band: GrowthBand  # from score: STRONG_FROM / SOME_FROM; NONE when held back
    reasons: list[GrowthReason]  # points > 0 only, most points first, ties in GrowthKind order;
    #                              empty when held back
    held_back: bool  # True when the account is At risk
    # One sentence ending in a full stop:
    #   held back      -> "Held back while this account is At risk. Fix the risk before talking
    #                      about growth."
    #   STRONG         -> f"Ready to grow: {top three reasons}."
    #   SOME           -> f"Some room to grow: {top three reasons}."
    #   NONE, score>0  -> f"No strong signs yet: {top three reasons}."
    #   score == 0     -> "No signs of room to grow yet."
    # The reasons are joined "a", "a and b", "a, b and c", each with its first letter lower-cased
    # unless it starts with an acronym (so "Ready to grow: seats 96% used, active users up 22% in
    # 30 days and NPS 9, a promoter.").
    sentence: str


def funding_news(articles: Sequence[Article], as_of: date) -> FundingNews | None:
    """The newest article tagged Topic.FUNDING whose published day is within FUNDING_DAYS before
    as_of (as_of - FUNDING_DAYS <= published <= as_of). Undated funding articles are used only
    when no dated one qualifies; the first undated one wins. None when nothing qualifies."""
    earliest = as_of - timedelta(days=FUNDING_DAYS)
    funding = [article for article in articles if Topic.FUNDING in article.topics]
    dated = [
        article
        for article in funding
        if article.published is not None and earliest <= article.published <= as_of
    ]
    if dated:
        # max() keeps the first of several articles on the same day, so the feed order breaks ties.
        newest = max(dated, key=lambda article: article.published or date.min)
        return FundingNews(title=newest.title, published=newest.published)
    undated = [article for article in funding if article.published is None]
    if undated:
        return FundingNews(title=undated[0].title, published=None)
    return None


def growth_radar(
    scored: ScoredAccount,
    latest: dict[SignalKind, Signal],
    funding: FundingNews | None,
) -> GrowthRadar:
    """The account's room to grow.

    Points per part (a part with no reading earns nothing):
      SEATS   SEAT_UTILISATION reading on the SEATS ramp, times SEATS_POINTS
      USAGE   USAGE_TREND_30D reading on the USAGE ramp, times USAGE_POINTS
      NPS     NPS_LATEST reading on the NPS ramp, times NPS_POINTS
      FUNDING FUNDING_POINTS when `funding` is not None (the caller checked its age)
    A ramp is min(max((value - floor) / (top - floor), 0), 1).

    Held back (scored.health is Health.AT_RISK): score 0, band NONE, reasons [], held_back True.
    """
    if scored.health is Health.AT_RISK:
        return GrowthRadar(
            score=0,
            band=GrowthBand.NONE,
            reasons=[],
            held_back=True,
            sentence=_HELD_BACK_SENTENCE,
        )
    reasons = _reasons(latest, funding)
    # Rounded to 9 places first, so float error in the sum can't push a true .5 the wrong way.
    score = _whole(round(sum(reason.points for reason in reasons), 9))
    band = _band(score)
    return GrowthRadar(
        score=score,
        band=band,
        reasons=reasons,
        held_back=False,
        sentence=_sentence(score, band, reasons),
    )


def book_growth(
    book: Sequence[ScoredAccount],
    latest_by_account: dict[str, dict[SignalKind, Signal]],
    funding: dict[str, FundingNews],
) -> dict[str, GrowthRadar]:
    """growth_radar for every account in the book, keyed by account id (an account with no
    readings gets {} as its latest; one missing from `funding` gets None)."""
    return {
        item.account.id: growth_radar(
            item, latest_by_account.get(item.account.id, {}), funding.get(item.account.id)
        )
        for item in book
    }


# --- Private helpers -------------------------------------------------------------------------

_PROMOTER_FROM = 9  # a whole NPS at or above this is a promoter
_NAMED_REASONS = 3  # the sentence names this many reasons, most points first
_HELD_BACK_SENTENCE = (
    "Held back while this account is At risk. Fix the risk before talking about growth."
)
_NO_SIGNS_SENTENCE = "No signs of room to grow yet."
_LEADS: dict[GrowthBand, str] = {
    GrowthBand.STRONG: "Ready to grow",
    GrowthBand.SOME: "Some room to grow",
    GrowthBand.NONE: "No strong signs yet",
}
_KIND_ORDER = tuple(GrowthKind)


def _ramp(value: float, floor: float, top: float) -> float:
    """0 at or below floor, 1 at or above top, a straight line between the two."""
    return min(max((value - floor) / (top - floor), 0.0), 1.0)


def _add(found: list[GrowthReason], kind: GrowthKind, points: float, text: str) -> None:
    """Adds a reason only when its part earns points."""
    if points > 0:
        found.append(GrowthReason(kind=kind, points=points, plain_english=text))


def _reasons(latest: dict[SignalKind, Signal], funding: FundingNews | None) -> list[GrowthReason]:
    """The parts that earn points, most points first, ties in GrowthKind order."""
    found: list[GrowthReason] = []
    seats = latest.get(SignalKind.SEAT_UTILISATION)
    if seats is not None:
        points = _ramp(seats.value, SEATS_FLOOR, SEATS_TOP) * SEATS_POINTS
        _add(found, GrowthKind.SEATS, points, f"Seats {_whole(seats.value)}% used")
    usage = latest.get(SignalKind.USAGE_TREND_30D)
    if usage is not None:
        points = _ramp(usage.value, USAGE_FLOOR, USAGE_TOP) * USAGE_POINTS
        text = f"Active users up {_whole(usage.value)}% in 30 days"
        _add(found, GrowthKind.USAGE, points, text)
    nps = latest.get(SignalKind.NPS_LATEST)
    if nps is not None:
        whole = _whole(nps.value)
        text = f"NPS {whole}, a promoter" if whole >= _PROMOTER_FROM else f"NPS {whole}"
        _add(found, GrowthKind.NPS, _ramp(nps.value, NPS_FLOOR, NPS_TOP) * NPS_POINTS, text)
    if funding is not None:
        text = f'Their site announced funding: "{_headline(funding.title)}"'
        _add(found, GrowthKind.FUNDING, FUNDING_POINTS, text)
    # Points are compared rounded, so two parts that are equal on paper tie for real.
    found.sort(key=lambda reason: (-round(reason.points, 9), _KIND_ORDER.index(reason.kind)))
    return found


def _headline(title: str) -> str:
    """The title itself when it fits MAX_HEADLINE characters, else cut so that the kept text
    and the "…" together are MAX_HEADLINE characters."""
    if len(title) <= MAX_HEADLINE:
        return title
    return title[: MAX_HEADLINE - 1].rstrip() + "…"


def _band(score: int) -> GrowthBand:
    if score >= STRONG_FROM:
        return GrowthBand.STRONG
    if score >= SOME_FROM:
        return GrowthBand.SOME
    return GrowthBand.NONE


def _sentence(score: int, band: GrowthBand, reasons: list[GrowthReason]) -> str:
    if score == 0:
        return _NO_SIGNS_SENTENCE
    named = reasons[:_NAMED_REASONS]
    texts = [_lower_first(reason.plain_english) for reason in named]
    return f"{_LEADS[band]}: {_join(texts)}."


def _join(items: list[str]) -> str:
    """A list as prose: "A", "A and B", "A, B and C"."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _lower_first(text: str) -> str:
    """The first letter lower-cased so a reason reads mid-sentence. An acronym such as "NPS"
    keeps its capitals."""
    if text[:2].isupper():
        return text
    return text[:1].lower() + text[1:]
