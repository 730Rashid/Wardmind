"""The Today page's screen: the find box's narrowing to account names, the section index with its
counts, and the email threads waiting on replies.

journal_pages builds each section's rows and sentences from the core's view. This module narrows
that view to the accounts whose name contains the find text (before journal_pages sees it, so the
summary sentence describes what is shown), counts what is left for the index, and adds the threads
from journal.awaiting_replies. Nothing here reads a file: the route passes in the book and the
journals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from wardmind.core import journal
from wardmind.core.journal import AccountJournal, TodayView
from wardmind.core.models import ScoredAccount
from wardmind.web import formatting, journal_pages
from wardmind.web.journal_pages import TodayPage

WAITING_LIMIT = 10  # thread rows listed under "Waiting on replies"; the rest are counted
MAX_QUERY = 100  # characters kept from the find box
MAX_SUBJECT = 160  # characters of a thread's subject shown on its row
_NO_SUBJECT = "(No subject)"


def clean_query(text: str) -> str:
    """The find box's text on one line, stripped and cut to MAX_QUERY characters."""
    return " ".join(text.split())[:MAX_QUERY]


def _matches(name: str, needle: str) -> bool:
    """A case-insensitive substring match on the account's name; an empty needle matches all."""
    return not needle or needle in name.casefold()


@dataclass(frozen=True)
class WaitingRow:
    account_id: str
    name: str
    subject: str  # the latest email's subject, cut; "(No subject)" when it has none
    waiting_on_you: bool  # True: they wrote and you haven't answered yet
    label: str  # "Waiting on you" or "Waiting on them"
    line: str  # "They wrote 3 days ago" or "Sent 6 days ago, no reply yet"


@dataclass(frozen=True)
class IndexLink:
    anchor: str  # the section's id on the page
    label: str  # "Due", "Needs attention", ...
    count: int


@dataclass(frozen=True)
class TodayScreen:
    query: str  # the find text, cleaned; "" when no filter is set
    page: TodayPage  # the sections, narrowed to the find text
    waiting: list[WaitingRow]  # the first WAITING_LIMIT threads, waiting on you first
    waiting_more: int  # threads not listed
    waiting_total: int  # every thread that matches the find text
    index: list[IndexLink]  # the non-empty sections, in page order

    @property
    def empty(self) -> bool:
        return self.page.empty and self.waiting_total == 0


def narrow(view: TodayView, needle: str) -> TodayView:
    """The core's view with only the accounts whose name contains `needle` (already casefolded)."""
    if not needle:
        return view

    def keep(scored: ScoredAccount) -> bool:
        return _matches(scored.account.name, needle)

    return view.model_copy(
        update={
            "due": [item for item in view.due if keep(item.scored)],
            "attention": [item for item in view.attention if keep(item.scored)],
            "outcomes": [scored for scored in view.outcomes if keep(scored)],
            "upcoming": [item for item in view.upcoming if keep(item.scored)],
            "snoozed": [item for item in view.snoozed if keep(item.scored)],
        }
    )


def waiting_on_replies(
    book: list[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    today: date,
    needle: str = "",
) -> list[WaitingRow]:
    """Every email thread waiting on someone, across the book's accounts (journal entries for
    accounts outside the book are left out, as journal.today_view leaves them out). Waiting on you
    first, then the oldest first, then by account name and thread."""
    found: list[tuple[tuple[bool, date, str, str], WaitingRow]] = []
    for item in book:
        account = item.account
        if not _matches(account.name, needle):
            continue
        entry = journals.get(account.id)
        if entry is None:
            continue
        for status in journal.awaiting_replies(entry, today):
            ago = formatting.ago(status.days)
            row = WaitingRow(
                account_id=account.id,
                name=account.name,
                subject=journal_pages.cut_text(status.subject or _NO_SUBJECT, MAX_SUBJECT),
                waiting_on_you=not status.waiting_on_them,
                label="Waiting on them" if status.waiting_on_them else "Waiting on you",
                line=(
                    f"Sent {ago}, no reply yet" if status.waiting_on_them else f"They wrote {ago}"
                ),
            )
            key = (status.waiting_on_them, status.last_on, account.name.casefold(), status.thread)
            found.append((key, row))
    found.sort(key=lambda pair: pair[0])
    return [row for _, row in found]


def _index(page: TodayPage, waiting_total: int) -> list[IndexLink]:
    """The sections in page order, each with its count; a section with nothing is left out."""
    links = [
        IndexLink("today-due", "Due", len(page.due)),
        IndexLink("today-waiting", "Waiting on replies", waiting_total),
        IndexLink("today-attention", "Needs attention", len(page.attention)),
        IndexLink("today-renewals", "Renewals to record", len(page.renewals)),
        IndexLink("today-upcoming", "Coming up", len(page.upcoming)),
        IndexLink("today-snoozed", "Snoozed", len(page.snoozed)),
    ]
    return [link for link in links if link.count]


def screen(
    book: list[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    today: date,
    query: str = "",
) -> TodayScreen:
    """The Today page for the book and the journals, narrowed to the accounts whose name contains
    `query` (case-insensitive). With no query, nothing is narrowed."""
    text = clean_query(query)
    needle = text.casefold()
    view = journal.today_view(book, dict(journals), today)
    page = journal_pages.today_page(narrow(view, needle), today)
    threads = waiting_on_replies(book, journals, today, needle)
    return TodayScreen(
        query=text,
        page=page,
        waiting=threads[:WAITING_LIMIT],
        waiting_more=max(0, len(threads) - WAITING_LIMIT),
        waiting_total=len(threads),
        index=_index(page, len(threads)),
    )
