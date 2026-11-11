"""The renewals page and the account page's "Your call" box, in words and bars (core/forecast.py).

Pure: nothing here reads the clock, a file or the config. The caller passes in the view it built,
today's window and the money formatter, so every branch can be tested. The routes in app.py read
the journal, save the call and redirect with one of the fixed notice codes below.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlencode

from wardmind.core import forecast
from wardmind.core.journal import CallKind, ForecastCall
from wardmind.core.models import Account, Health

# What a save can redirect with. The URL carries the code, never the text a CSM typed.
CALL_SAVED = "call-saved"
CALL_CLEARED = "call-cleared"
CALL_PICK = "call-pick"
CALL_PASSED = "call-passed"
CALL_NOTICES: dict[str, str] = {
    CALL_SAVED: "Call saved.",
    CALL_CLEARED: "Call cleared.",
    CALL_PICK: "Pick Commit, Likely or At risk.",
    CALL_PASSED: "That renewal date has passed. Record the outcome instead.",
}
CLEAR = "clear"  # the posted kind that removes the call
CHOICES: tuple[CallKind, ...] = (CallKind.COMMIT, CallKind.LIKELY, CallKind.AT_RISK)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class CallChoice:
    value: str  # the posted kind: "commit", "likely" or "at_risk"
    label: str  # "Commit", "Likely", "At risk"
    selected: bool  # the account's current call


@dataclass(frozen=True)
class RenewalRow:
    account_id: str
    anchor: str  # the row's element id, see row_anchor
    name: str
    health: Health
    when: str  # "Renews in 12 days, 19 Jan 2027"
    arr: float
    call_text: str  # "You called Likely.", or "Not called yet."
    choices: list[CallChoice]
    can_clear: bool
    disagreement: str  # Disagreement.text, or ""


@dataclass(frozen=True)
class CallTotal:
    slot: str  # "commit", "likely", "at_risk" or "none": the class of its key and segment
    label: str  # "Commit", ..., "Not called"
    count_text: str  # "3 renewals"
    arr: float


@dataclass(frozen=True)
class BarSegment:
    slot: str
    x: float  # percent of the bar, 0 to 100
    width: float  # percent of the bar


@dataclass(frozen=True)
class RenewalsPage:
    summary: str
    empty: bool  # no renewal in the window: the page shows empty_text instead of the lists
    empty_text: str
    totals: list[CallTotal]  # Commit, Likely, At risk, Not called: always four
    segments: list[BarSegment]  # the stacked ARR bar; empty when no ARR is in the window
    bar_label: str
    disagree_line: str  # "2 calls disagree with Wardmind's score."
    filter_href: str  # the link beside the disagreement line, or ""
    filter_text: str
    showing_disagree: bool
    rows: list[RenewalRow]  # soonest first; only the disagreeing ones when showing_disagree
    notice: str
    calibration_yours: list[str]  # one sentence per kind of call that has an outcome
    calibration_wardmind: list[str]  # one sentence per health that has an outcome


@dataclass(frozen=True)
class AccountCall:
    visible: bool  # False once the renewal date has passed: the outcome form takes over
    renewal_text: str  # "19 Jan 2027"
    current: str  # "You called Likely on 3 Nov.", or ""
    choices: list[CallChoice]


def call_notice(code: str) -> str:
    """The sentence for a save's notice code, or "" for any other value."""
    return CALL_NOTICES.get(code, "")


def row_anchor(account_id: str) -> str:
    """The id of a renewal row. Redirects to a row add the id, percent-encoded, after the #."""
    return f"row-{account_id}"


def call_redirect(back: str, account_id: str, code: str, show: str) -> str:
    """Where a save goes. 'renewals' returns to the renewals page at the row (keeping the
    disagreement filter when it was shown); any other value returns to the account page's call
    box. The notice code is the only thing in the query."""
    if back == "renewals":
        params: dict[str, str] = {"show": "disagree"} if show == "disagree" else {}
        params["call"] = code
        return f"/renewals?{urlencode(params)}#{quote(row_anchor(account_id), safe='')}"
    return f"/accounts/{quote(account_id, safe='')}?{urlencode({'call': code})}#call"


def renewals_page(
    view: forecast.ForecastView,
    calibration: forecast.Calibration,
    *,
    window_days: int,
    book_size: int,
    disagree_only: bool,
    notice: str,
    money_short: Callable[[float], str],
) -> RenewalsPage:
    """The renewals page: the summary, the four totals, the ARR bar, the disagreements, one row per
    renewal and how the calls held up. `money_short` is the app's compact money formatter."""
    window = _window_text(window_days)
    empty = not view.rows
    if book_size == 0:
        summary = "No accounts in your book yet."
        empty_text = "Add accounts in My book, and their renewals will show here."
    elif empty:
        summary = f"No renewals {window}."
        empty_text = (
            f"A renewal shows here when its date is {window}. "
            "The window is set in Settings > Scoring."
        )
    else:
        summary = _summary(view, window, money_short)
        empty_text = ""
    totals = [
        CallTotal(
            slot=_slot(total.call),
            label=total.call.label if total.call is not None else "Not called",
            count_text=_count(total.count, "renewal", "renewals"),
            arr=total.arr,
        )
        for total in view.totals
    ]
    bar_label = _bar_label(totals, money_short)
    disagreements = view.disagreements
    if disagree_only:
        filter_href, filter_text = "/renewals", "Show all renewals"
    elif disagreements > 0:
        filter_href, filter_text = "/renewals?show=disagree", "Show only these"
    else:
        filter_href, filter_text = "", ""
    listed = (
        [row for row in view.rows if row.disagreement is not None] if disagree_only else view.rows
    )
    return RenewalsPage(
        summary=summary,
        empty=empty,
        empty_text=empty_text,
        totals=totals,
        segments=_segments([(total.slot, total.arr) for total in totals]),
        bar_label=bar_label,
        disagree_line=_disagree_line(disagreements),
        filter_href=filter_href,
        filter_text=filter_text,
        showing_disagree=disagree_only,
        rows=[_row(item) for item in listed],
        notice=notice,
        calibration_yours=[record.sentence for record in calibration.yours],
        calibration_wardmind=[record.sentence for record in calibration.wardmind],
    )


def account_call(account: Account, call: ForecastCall | None, today: date) -> AccountCall:
    """The account page's "Your call" box. It is hidden once the renewal date is behind today."""
    current = ""
    if call is not None:
        current = f"You called {call.kind.label} on {call_day(call.made_on, today)}."
    return AccountCall(
        visible=account.renewal_date >= today,
        renewal_text=renewal_day(account.renewal_date),
        current=current,
        choices=_choices(call.kind if call is not None else None),
    )


def renewal_day(day: date) -> str:
    """'19 Jan 2027': a renewal always carries its year."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def call_day(day: date, today: date) -> str:
    """'3 Nov', with the year only when it isn't this year: '3 Nov 2025'."""
    label = f"{day.day} {_MONTHS[day.month - 1]}"
    return label if day.year == today.year else f"{label} {day.year}"


# --- Helpers (private) --------------------------------------------------------------------------


def _window_text(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "in the next day"
    return f"in the next {days} days"


def _count(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _summary(view: forecast.ForecastView, window: str, money_short: Callable[[float], str]) -> str:
    count = len(view.rows)
    called = sum(1 for row in view.rows if row.call is not None)
    first = (
        f"{_count(count, 'renewal', 'renewals')} {window}, "
        f"carrying {money_short(view.arr_total)} of ARR."
    )
    if called == 0:
        second = f"You haven't called {'it' if count == 1 else 'them'} yet."
    elif called == count:
        second = f"You've called {'it' if count == 1 else 'all of them'}."
    else:
        second = f"You've called {called} of them."
    return f"{first} {second}"


def _slot(call: CallKind | None) -> str:
    return "none" if call is None else call.value


def _bar_label(totals: Sequence[CallTotal], money_short: Callable[[float], str]) -> str:
    """'ARR by call: $1.2M commit, $300K likely'. Calls with no ARR are left out."""
    parts = [f"{money_short(total.arr)} {total.label.lower()}" for total in totals if total.arr > 0]
    if not parts:
        return "ARR by call: no ARR in the window"
    return "ARR by call: " + ", ".join(parts)


def _segments(arrs: Sequence[tuple[str, float]]) -> list[BarSegment]:
    """The stacked bar: one segment per slot with ARR, in the order given. The edges are rounded
    to two decimals and the last segment runs to 100, so the bar has no gap at its end."""
    total = sum(arr for _, arr in arrs)
    if total <= 0:
        return []
    bands = [(slot, arr) for slot, arr in arrs if arr > 0]
    starts: list[float] = []
    running = 0.0
    for _, arr in bands:
        starts.append(round(running / total * 100, 2))
        running += arr
    segments: list[BarSegment] = []
    for index, (slot, _) in enumerate(bands):
        start = starts[index]
        if index + 1 < len(bands):
            width = round(starts[index + 1] - start, 2)
        else:
            width = round(100 - start, 2)
        segments.append(BarSegment(slot=slot, x=start, width=width))
    return segments


def _disagree_line(count: int) -> str:
    if count == 0:
        return "No calls disagree with Wardmind's score."
    if count == 1:
        return "1 call disagrees with Wardmind's score."
    return f"{count} calls disagree with Wardmind's score."


def _renews(days: int) -> str:
    if days == 0:
        return "Renews today"
    if days == 1:
        return "Renews in 1 day"
    return f"Renews in {days} days"


def _choices(selected: CallKind | None) -> list[CallChoice]:
    return [
        CallChoice(value=kind.value, label=kind.label, selected=kind is selected)
        for kind in CHOICES
    ]


def _row(item: forecast.ForecastRow) -> RenewalRow:
    called = item.call
    return RenewalRow(
        account_id=item.account_id,
        anchor=row_anchor(item.account_id),
        name=item.name,
        health=item.health,
        when=f"{_renews(item.days_to_renewal)}, {renewal_day(item.renewal_date)}",
        arr=item.arr,
        call_text=f"You called {called.label}." if called is not None else "Not called yet.",
        choices=_choices(called),
        can_clear=called is not None,
        disagreement=item.disagreement.text if item.disagreement is not None else "",
    )
