"""Negotiation sheet: a one-page prep for the renewal call, awkward facts included.

What you have going for you, where you are exposed, what the customer is likely to ask for and
why, the last renewal, your own call against Wardmind's, and the recent contact. It is the
onboarding brief's pattern applied to the commercial conversation, and prints on one A4 page.

Seat counts and price per seat are not in the data Wardmind reads, so the sheet works from ARR
and the share of seats in use (and says so on the page).

Pure and deterministic: no I/O and no clock reads (callers pass `as_of`).

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before
changing them.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from wardmind.core.forecast import Disagreement, current_call, disagreement
from wardmind.core.growth import GrowthRadar
from wardmind.core.insights import Shelfware
from wardmind.core.journal import (
    AccountJournal,
    CallKind,
    OutcomeKind,
    activity_newest_first,
    last_contact,
    open_follow_ups,
)
from wardmind.core.models import Health, Motion, ScoredAccount, Signal, SignalKind
from wardmind.core.scoring import _whole

MAX_EXPOSURES = 5  # risk reasons listed (the score's largest first)
MAX_RECENT = 3  # contact entries listed
MAX_FOLLOW_UPS = 3  # open follow-ups listed
TENURE_FROM_YEARS = 2  # "A customer for N years" is a strength from this many whole years
QUIET_DAYS = 30  # no contact for this many days (or none at all) is an exposure
RECENT_CONTACT_DAYS = 14  # contact within this many days is a strength

# Thresholds for the likely asks (the readings that prompt each one).
ASK_SEV1_FROM = 1  # open severity-1 tickets
ASK_USAGE_DOWN_FROM = -20.0  # usage trend at or below this percent
ASK_BILLING_FROM = 30  # days an invoice is overdue


class LikelyAsk(BaseModel):
    ask: str  # sentence case, no full stop, e.g. "Fewer seats at a lower price"
    because: str  # a sentence with a full stop, e.g. "Only 62% of seats are in use."


class LastRenewal(BaseModel):
    kind: OutcomeKind
    renewal_date: date
    decided_on: date
    arr_before: float
    arr_after: float | None


class RecentContact(BaseModel):
    happened_on: date
    kind_label: str  # TouchKind.label, e.g. "Call"
    text: str  # the subject when there is one, else the first line of the text, cut to 120
    #            characters ending "…" when cut


class NegotiationSheet(BaseModel):
    account_id: str
    name: str
    owner: str
    arr: float
    renewal_date: date
    days_to_renewal: int  # renewal_date - as_of; negative when the date has passed
    health: Health
    motion: Motion
    risk: float
    customer_years: int | None  # whole years since start_date; None without a start_date
    seats_used: int | None  # SEAT_UTILISATION reading, whole percent half up; None without one
    unused_arr: float | None  # shelfware.unused_arr when shelfware is given, else None
    last_renewal: LastRenewal | None  # the outcome with the latest renewal_date, else None
    call: CallKind | None  # your call on this renewal (forecast.current_call)
    disagreement: Disagreement | None  # forecast.disagreement(call, health)
    strengths: list[str]  # sentences with full stops, in the order below
    exposures: list[str]  # sentences with full stops, in the order below
    asks: list[LikelyAsk]  # in the order below
    recent: list[RecentContact]  # newest first, at most MAX_RECENT; contact kinds only
    follow_ups: list[str]  # open follow-up texts, soonest due first, at most MAX_FOLLOW_UPS


def negotiation_sheet(
    scored: ScoredAccount,
    latest: dict[SignalKind, Signal],
    journal: AccountJournal,
    growth: GrowthRadar | None,
    shelfware: Shelfware | None,
    as_of: date,
) -> NegotiationSheet:
    """The sheet for scored.account's next renewal.

    strengths, in this order (each only when it applies):
      1. each growth reason's plain_english + "." (when growth is given and not held back)
      2. "No risk signals." when scored.reasons is empty
      3. f"A customer for {n} years." when customer_years >= TENURE_FROM_YEARS
      4. "They expanded at the last renewal." when last_renewal.kind is EXPANDED
      5. f"You were in touch {n} days ago." (n == 0 -> "You were in touch today.", n == 1 ->
         "You were in touch yesterday.") when the last contact (journal.last_contact) is within
         RECENT_CONTACT_DAYS of as_of

    exposures, in this order:
      1. the first MAX_EXPOSURES of scored.reasons, each plain_english + "."
      2. f"{unused}% of seats are unused." when shelfware is given (unused = unused_percent) and
         the reasons listed in 1 don't already include SEAT_UTILISATION
      3. "They contracted at the last renewal." / "They churned at the last renewal." when
         last_renewal.kind is CONTRACTED / CHURNED
      4. "No contact logged yet." when there is no contact at all, else f"No contact logged in
         {n} days." when the last contact is QUIET_DAYS or more before as_of

    asks, in this order (each only when it applies):
      1. shelfware given -> ("Fewer seats at a lower price",
                             f"Only {utilisation}% of seats are in use.")
      2. OPEN_TICKETS_SEV1 >= ASK_SEV1_FROM -> ("A credit or discount for the open issues",
            f"{n} severity-1 ticket is still open." / f"{n} severity-1 tickets are still open.")
      3. USAGE_TREND_30D <= ASK_USAGE_DOWN_FROM -> ("A shorter term or an exit clause",
            f"Active users are down {abs(t)}% in 30 days.")   (t whole, half up)
      4. BILLING_OVERDUE_DAYS >= ASK_BILLING_FROM -> ("Different payment terms",
            f"An invoice is {n} days overdue.")
      5. CHAMPION_LEFT >= 1 -> ("Time to bring in their new decision-maker",
            "Your champion has left.")
      6. last_renewal.kind is CONTRACTED -> ("Another reduction",
            "They contracted at the last renewal.")
    """
    account = scored.account
    last = _last_renewal(journal)
    found = current_call(journal, account.renewal_date)
    call = found.kind if found is not None else None
    latest_contact = last_contact(journal)
    # A contact dated after as_of (a mailbox clock running ahead) reads as today.
    days_since_contact = None if latest_contact is None else max((as_of - latest_contact).days, 0)
    years = _customer_years(account.start_date, as_of)
    seats = latest.get(SignalKind.SEAT_UTILISATION)

    strengths: list[str] = []
    if growth is not None and not growth.held_back:
        strengths.extend(f"{reason.plain_english}." for reason in growth.reasons)
    if not scored.reasons:
        strengths.append("No risk signals.")
    if years is not None and years >= TENURE_FROM_YEARS:
        strengths.append(f"A customer for {years} years.")
    if last is not None and last.kind is OutcomeKind.EXPANDED:
        strengths.append("They expanded at the last renewal.")
    if days_since_contact is not None and days_since_contact <= RECENT_CONTACT_DAYS:
        strengths.append(_touch_sentence(days_since_contact))

    listed = scored.reasons[:MAX_EXPOSURES]
    exposures = [f"{reason.plain_english}." for reason in listed]
    # The score's own seat reason already says how many seats are in use; don't say it twice.
    if shelfware is not None and all(r.kind is not SignalKind.SEAT_UTILISATION for r in listed):
        exposures.append(f"{shelfware.unused_percent}% of seats are unused.")
    if last is not None and last.kind is OutcomeKind.CONTRACTED:
        exposures.append("They contracted at the last renewal.")
    if last is not None and last.kind is OutcomeKind.CHURNED:
        exposures.append("They churned at the last renewal.")
    if days_since_contact is None:
        exposures.append("No contact logged yet.")
    elif days_since_contact >= QUIET_DAYS:
        exposures.append(f"No contact logged in {days_since_contact} days.")

    asks: list[LikelyAsk] = []
    if shelfware is not None:
        asks.append(
            LikelyAsk(
                ask="Fewer seats at a lower price",
                because=f"Only {shelfware.utilisation}% of seats are in use.",
            )
        )
    tickets = latest.get(SignalKind.OPEN_TICKETS_SEV1)
    if tickets is not None and tickets.value >= ASK_SEV1_FROM:
        count = _whole(tickets.value)
        because = (
            f"{count} severity-1 ticket is still open."
            if count == 1
            else f"{count} severity-1 tickets are still open."
        )
        asks.append(LikelyAsk(ask="A credit or discount for the open issues", because=because))
    usage = latest.get(SignalKind.USAGE_TREND_30D)
    if usage is not None and usage.value <= ASK_USAGE_DOWN_FROM:
        asks.append(
            LikelyAsk(
                ask="A shorter term or an exit clause",
                because=f"Active users are down {abs(_whole(usage.value))}% in 30 days.",
            )
        )
    overdue = latest.get(SignalKind.BILLING_OVERDUE_DAYS)
    if overdue is not None and overdue.value >= ASK_BILLING_FROM:
        asks.append(
            LikelyAsk(
                ask="Different payment terms",
                because=f"An invoice is {_whole(overdue.value)} days overdue.",
            )
        )
    champion = latest.get(SignalKind.CHAMPION_LEFT)
    if champion is not None and champion.value >= 1:
        asks.append(
            LikelyAsk(
                ask="Time to bring in their new decision-maker",
                because="Your champion has left.",
            )
        )
    if last is not None and last.kind is OutcomeKind.CONTRACTED:
        asks.append(
            LikelyAsk(ask="Another reduction", because="They contracted at the last renewal.")
        )

    recent = [
        RecentContact(
            happened_on=note.happened_on,
            kind_label=note.kind.label,
            text=_cut(note.subject.strip() or _first_line(note.text)),
        )
        for note in activity_newest_first(journal)
        if note.kind.is_contact
    ][:MAX_RECENT]
    follow_ups = [item.text for item in open_follow_ups(journal)][:MAX_FOLLOW_UPS]

    return NegotiationSheet(
        account_id=account.id,
        name=account.name,
        owner=account.owner,
        arr=account.arr,
        renewal_date=account.renewal_date,
        days_to_renewal=(account.renewal_date - as_of).days,
        health=scored.health,
        motion=scored.motion,
        risk=scored.risk,
        customer_years=years,
        seats_used=None if seats is None else _whole(seats.value),
        unused_arr=None if shelfware is None else shelfware.unused_arr,
        last_renewal=last,
        call=call,
        disagreement=disagreement(call, scored.health),
        strengths=strengths,
        exposures=exposures,
        asks=asks,
        recent=recent,
        follow_ups=follow_ups,
    )


_RECENT_TEXT = 120  # characters of a recent entry's text before it is cut with "…"


def _customer_years(start: date | None, as_of: date) -> int | None:
    """Whole years from start to as_of. A year counts once its anniversary has passed; the
    anniversary of 29 February in a year without one is 28 February. Never negative."""
    if start is None:
        return None
    years = as_of.year - start.year
    if _anniversary(start, as_of.year) > as_of:
        years -= 1
    return max(years, 0)


def _anniversary(start: date, year: int) -> date:
    try:
        return start.replace(year=year)
    except ValueError:  # 29 February, in a year without one
        return date(year, 2, 28)


def _last_renewal(journal: AccountJournal) -> LastRenewal | None:
    """The outcome with the latest renewal_date (ties: the later recorded_at), or None."""
    if not journal.outcomes:
        return None
    outcome = max(journal.outcomes, key=lambda item: (item.renewal_date, item.recorded_at))
    return LastRenewal(
        kind=outcome.kind,
        renewal_date=outcome.renewal_date,
        decided_on=outcome.decided_on,
        arr_before=outcome.arr_before,
        arr_after=outcome.arr_after,
    )


def _touch_sentence(days: int) -> str:
    if days == 0:
        return "You were in touch today."
    if days == 1:
        return "You were in touch yesterday."
    return f"You were in touch {days} days ago."


def _cut(text: str) -> str:
    """The text, or when longer than 120 characters its first 119 (rstripped) and "…"."""
    if len(text) <= _RECENT_TEXT:
        return text
    return text[: _RECENT_TEXT - 1].rstrip() + "…"


def _first_line(text: str) -> str:
    """The first line that has any content, stripped ("" when there is none)."""
    return next((line.strip() for line in text.split("\n") if line.strip()), "")
