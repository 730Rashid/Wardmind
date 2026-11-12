"""The redesigned Today page and My book's logos: the section index and its counts, the find box
that narrows every section to account names, the threads waiting on replies, one primary action per
row with its More disclosure, the logo or monogram on each row, and the rank that My book keeps.

Runs through the real routes against the temp folders from conftest, with hand-added accounts (no
network). The date is pinned in the web app, so the day-count sentences never flake at midnight.
The demo book is used only where its own health is the point (the Needs attention section).
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, journal, service
from wardmind.core.journal import Direction, LoggedEmail
from wardmind.core.logos import ImageType, LogoInfo, LogoSource
from wardmind.core.models import Health
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
PINNED = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 9, 0)  # naive, as the journal keeps times
FAR = PINNED + timedelta(days=200)  # a renewal well away from today


class _PinnedDate(date):
    """Stands in for `date` in the web app, so date.today() is PINNED for the whole test."""

    @classmethod
    def today(cls) -> date:
        return PINNED


@pytest.fixture(autouse=True)
def pinned_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "date", _PinnedDate)
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _account(name: str, renewal: date = FAR) -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": "$150,000", "renewal_date": renewal.isoformat()},
        today=PINNED,
    )
    service.reset_connectors()
    return account.id


def _late_follow_up(account_id: str, text: str, days_late: int) -> None:
    due = PINNED - timedelta(days=days_late)
    service.get_journal().add_follow_up(account_id, text, due, now=NOW, today=due)


def _mail(account_id: str, subject: str, days_ago: int, direction: Direction, message: str) -> None:
    service.get_journal().log_email(
        account_id,
        LoggedEmail(
            message_id=f"{message}@example.com",
            subject=subject,
            sent_on=PINNED - timedelta(days=days_ago),
            direction=direction,
            by="",
            people=["client@example.com"],
            snippet=f"Body of {subject}",
        ),
        now=NOW,
    )


def _section(page: str, section_id: str) -> str:
    match = re.search(rf'<section[^>]* id="{section_id}"[^>]*>(.*?)</section>', page, re.S)
    assert match is not None, section_id
    return match.group(1)


def _rows(section: str) -> list[str]:
    return re.findall(r'<li class="today-row">(.*?)</li>', section, re.S)


def _index(page: str) -> list[tuple[str, str, int]]:
    """(anchor, label, count) for each link in the section index, in page order."""
    nav = re.search(
        r'<nav class="today-index" aria-label="Today\'s sections">(.*?)</nav>', page, re.S
    )
    if nav is None:
        return []
    links = re.findall(
        r'<a href="#(today-[a-z]+)">([^<]+?) <span class="today-index-count">(\d+)</span></a>',
        nav.group(1),
    )
    return [(anchor, label, int(count)) for anchor, label, count in links]


# --- The section index ---------------------------------------------------------------------


def test_the_index_is_a_labelled_nav_that_links_to_each_section_and_counts_its_rows(
    client: TestClient,
) -> None:
    late = _account("Late Follow")
    _late_follow_up(late, "Send the usage pack", 2)
    _late_follow_up(late, "Chase the sign-off", 0)
    _account("Renewal Due", PINNED - timedelta(days=12))  # its renewal is past: renewals section
    upcoming = _account("Coming Up")
    service.get_journal().add_follow_up(
        upcoming, "Book the QBR", PINNED + timedelta(days=3), now=NOW, today=PINNED
    )
    snoozed = _account("Quiet Account")
    service.get_journal().snooze(
        snoozed, PINNED + timedelta(days=7), Health.HEALTHY, now=NOW, today=PINNED
    )
    _mail(late, "Usage pack thread", 6, Direction.SENT, "sent-usage")

    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    page = _text(response)

    assert 'aria-label="Today\'s sections"' in page
    assert _index(page) == [
        ("today-due", "Due", 2),
        ("today-waiting", "Waiting on replies", 1),
        ("today-renewals", "Renewals to record", 1),
        ("today-upcoming", "Coming up", 1),
        ("today-snoozed", "Snoozed", 1),
    ]


def test_each_section_id_on_the_page_is_in_the_index_and_its_count_is_its_rows(
    client: TestClient,
) -> None:
    for number in range(2):
        account = _account(f"Due {number}")
        _late_follow_up(account, f"Task {number}", 1)
    _mail(_account("Thread Client"), "Plan", 3, Direction.RECEIVED, "received-plan")
    _account("Renewal Only", PINNED - timedelta(days=5))

    page = _text(client.get("/today"))
    sections = re.findall(r'<section[^>]* id="(today-[a-z]+)"', page)
    index = _index(page)
    assert [anchor for anchor, _, _ in index] == sections
    for anchor, _label, count in index:
        assert len(_rows(_section(page, anchor))) == count, anchor


def test_an_empty_book_has_no_index_at_all(client: TestClient) -> None:
    page = _text(client.get("/today"))
    assert "today-index" not in page
    assert "Nothing needs you today." in page


def test_needs_attention_counts_its_rows_from_the_demo_book(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.DEMO))
    service.reset_connectors()
    view = journal.today_view(service.get_book(as_of=PINNED), service.get_journal().all(), PINNED)
    page = _text(client.get("/today"))
    assert ("today-attention", "Needs attention", len(view.attention)) in _index(page)
    assert len(_rows(_section(page, "today-attention"))) == len(view.attention)


# --- The find box ---------------------------------------------------------------------------


def test_find_narrows_every_section_and_the_index_to_matching_names(client: TestClient) -> None:
    acme = _account("Acme Corp")
    _late_follow_up(acme, "Acme follow-up", 1)
    beacon = _account("Beacon Ltd")
    _late_follow_up(beacon, "Beacon follow-up", 1)
    _account("Acme Renewals", PINNED - timedelta(days=12))
    _mail(beacon, "Beacon plan", 6, Direction.SENT, "beacon-plan")

    page = _text(client.get("/today", params={"q": "acme"}))
    assert 'value="acme"' in page
    assert 'Showing accounts matching "acme".' in page
    assert '<a href="/today">Clear</a>' in page

    due = _section(page, "today-due")
    assert "Acme Corp" in due and "Acme follow-up" in due
    assert "Beacon" not in due
    renewals = _section(page, "today-renewals")
    assert "Acme Renewals" in renewals
    assert "Beacon" not in page
    assert _index(page) == [
        ("today-due", "Due", 1),
        ("today-renewals", "Renewals to record", 1),
    ]
    # The summary describes what is shown. The contact line counts the whole book, so it is hidden.
    assert "1 follow-up due and 1 renewal to record." in page
    assert "You haven't recorded a contact" not in page


def test_find_is_case_insensitive_and_ignores_the_spaces_around_it(client: TestClient) -> None:
    _late_follow_up(_account("Beacon Ltd"), "Beacon task", 1)
    _late_follow_up(_account("Acme Corp"), "Acme task", 1)

    page = _text(client.get("/today", params={"q": "  BEACON  "}))
    assert 'Showing accounts matching "BEACON".' in page
    assert "Beacon task" in page and "Acme task" not in page


def test_find_with_no_match_says_so_and_clear_brings_every_section_back(client: TestClient) -> None:
    _late_follow_up(_account("Acme Corp"), "Acme task", 1)

    response = client.get("/today", params={"q": "zzz"})
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    assert 'No accounts match "zzz".' in page
    assert 'href="/today">Clear search</a>' in page
    assert "Nothing needs you today." not in page
    assert 'id="today-due"' not in page and "today-index" not in page

    cleared = _text(client.get("/today"))
    assert "Acme task" in cleared
    assert "Showing accounts matching" not in cleared


def test_find_text_is_escaped_and_never_runs(client: TestClient) -> None:
    response = client.get("/today", params={"q": "<b>x</b>"})
    assert response.status_code == 200
    _clean(response)
    assert "<b>x</b>" not in response.text
    assert "&lt;b&gt;x&lt;/b&gt;" in response.text


def test_the_find_box_is_labelled_and_sits_in_a_search_form(client: TestClient) -> None:
    page = client.get("/today").text
    assert '<form class="today-find" method="get" action="/today" role="search">' in page
    assert '<label for="today-find-q">Find an account</label>' in page
    assert 'id="today-find-q" name="q"' in page


# --- Waiting on replies ----------------------------------------------------------------------


def test_waiting_on_replies_lists_threads_waiting_on_them_and_on_you(client: TestClient) -> None:
    sent = _account("Thread Client")
    _mail(sent, "Renewal plan", 6, Direction.SENT, "renewal-plan")
    wrote = _account("Question Client")
    _mail(wrote, "Pricing question", 3, Direction.RECEIVED, "pricing-question")
    too_new = _account("Fresh Client")
    _mail(too_new, "Just sent", 2, Direction.SENT, "just-sent")  # under five days: not waiting

    response = client.get("/today")
    _clean(response)
    section = _section(_text(response), "today-waiting")
    rows = _rows(section)
    assert len(rows) == 2
    # Waiting on you first, then the thread that has waited longest.
    assert "Question Client" in rows[0] and "Pricing question" in rows[0]
    assert "Waiting on you" in rows[0] and "They wrote 3 days ago" in rows[0]
    assert "Thread Client" in rows[1] and "Renewal plan" in rows[1]
    assert "Waiting on them" in rows[1] and "Sent 6 days ago, no reply yet" in rows[1]
    assert f'href="/accounts/{wrote}#activity"' in rows[0]
    assert f'href="/accounts/{sent}#activity"' in rows[1]
    assert "Fresh Client" not in section


def test_waiting_on_replies_sits_after_due_and_before_needs_attention(client: TestClient) -> None:
    account = _account("Order Check")
    _late_follow_up(account, "Order task", 1)
    _mail(account, "Order thread", 3, Direction.RECEIVED, "order-thread")

    page = _text(client.get("/today"))
    positions = [page.index(f'id="{name}"') for name in ("today-due", "today-waiting")]
    assert positions == sorted(positions)


def test_waiting_rows_are_limited_to_ten_and_the_rest_are_counted(client: TestClient) -> None:
    for number in range(12):
        account = _account(f"Client {number:02d}")
        _mail(account, f"Question {number:02d}", 3, Direction.RECEIVED, f"question-{number}")

    page = _text(client.get("/today"))
    assert ("today-waiting", "Waiting on replies", 12) in _index(page)
    assert len(_rows(_section(page, "today-waiting"))) == 10
    assert "And 2 more threads are waiting." in page


def test_the_find_box_narrows_the_waiting_threads_too(client: TestClient) -> None:
    _mail(_account("Acme Thread"), "Acme question", 3, Direction.RECEIVED, "acme-question")
    _mail(_account("Other Thread"), "Other question", 3, Direction.RECEIVED, "other-question")

    page = _text(client.get("/today", params={"q": "acme"}))
    section = _section(page, "today-waiting")
    assert len(_rows(section)) == 1
    assert "Acme Thread" in section and "Other Thread" not in section


def test_a_waiting_row_has_one_open_link_and_no_more_disclosure(client: TestClient) -> None:
    account = _account("Open Me")
    _mail(account, "Open thread", 3, Direction.RECEIVED, "open-thread")

    section = _section(_text(client.get("/today")), "today-waiting")
    (row,) = _rows(section)
    assert "<details" not in row
    assert f'href="/accounts/{account}#activity"' in row
    assert ">Open<" in row


# --- One primary action per row, the rest under More -----------------------------------------


def test_a_due_row_has_done_as_its_primary_and_the_moves_under_more(client: TestClient) -> None:
    account = _account("Due Row")
    _late_follow_up(account, "Move me", 1)
    (item,) = journal.open_follow_ups(service.account_journal(account))

    section = _section(_text(client.get("/today")), "today-due")
    (row,) = _rows(section)
    primary = row[: row.index("<details")]
    assert f'action="/accounts/{account}/follow-ups/{item.id}/done"' in primary
    assert 'name="back" value="today"' in primary
    assert primary.count("<button") == 1 and ">Done<" in primary
    more = row[row.index("<details") :]
    assert more.count("<summary>") == 1
    assert f'action="/accounts/{account}/follow-ups/{item.id}/move"' in more
    assert '<input type="hidden" name="days" value="1">' in more
    assert '<input type="hidden" name="days" value="7">' in more
    assert ">Tomorrow<" in more and ">+1 week<" in more
    assert "Done" not in more


def test_a_needs_attention_row_has_add_follow_up_and_snooze_and_call_under_more(
    client: TestClient,
) -> None:
    save_settings(Settings(source=DataSource.DEMO))
    service.reset_connectors()
    section = _section(_text(client.get("/today")), "today-attention")
    rows = _rows(section)
    assert rows
    for row in rows:
        primary = row[: row.index("<details")]
        more = row[row.index("<details") :]
        assert primary.count("<button") == 1 and "Add follow-up" in primary
        assert re.search(r'action="/accounts/[^"]+/follow-ups"', primary)
        assert 'name="back" value="today"' in primary and 'name="days" value="7"' in primary
        assert re.search(r'action="/accounts/[^"]+/snooze"', more)
        assert re.search(r'href="/accounts/[^"]+#activity"', more)
        assert "Snooze 7 days" in more and "Log a call" in more


def test_the_repeated_buttons_carry_the_account_name_for_screen_readers(client: TestClient) -> None:
    account = _account("Named Client")
    _late_follow_up(account, "Name check", 1)

    section = _section(_text(client.get("/today")), "today-due")
    row = _rows(section)[0]
    assert 'Done<span class="visually-hidden"> for Named Client</span>' in row
    assert 'More<span class="visually-hidden"> actions for Named Client</span>' in row


def test_renewal_snoozed_and_coming_up_rows_keep_their_single_action(client: TestClient) -> None:
    renewal = _account("Renewal Row", PINNED - timedelta(days=12))
    upcoming = _account("Upcoming Row")
    service.get_journal().add_follow_up(
        upcoming, "Upcoming task", PINNED + timedelta(days=2), now=NOW, today=PINNED
    )
    snoozed = _account("Snoozed Row")
    service.get_journal().snooze(
        snoozed, PINNED + timedelta(days=7), Health.HEALTHY, now=NOW, today=PINNED
    )

    page = _text(client.get("/today"))
    (renewal_row,) = _rows(_section(page, "today-renewals"))
    assert f'href="/accounts/{renewal}#outcome"' in renewal_row
    assert "Record the outcome" in renewal_row and "<details" not in renewal_row

    (coming_row,) = _rows(_section(page, "today-upcoming"))
    assert 'name="back" value="today"' in coming_row and ">Done<" in coming_row
    assert "<details" not in coming_row

    (snoozed_row,) = _rows(_section(page, "today-snoozed"))
    assert f'action="/accounts/{snoozed}/wake"' in snoozed_row and ">Wake<" in snoozed_row
    assert "<details" not in snoozed_row


# --- Logos and monograms --------------------------------------------------------------------


def test_today_rows_show_a_monogram_when_the_account_has_no_logo(client: TestClient) -> None:
    account = _account("Acme Corp")
    _late_follow_up(account, "Monogram check", 1)

    response = client.get("/today")
    section = _section(_text(response), "today-due")
    (row,) = _rows(section)
    assert 'class="logo logo--md logo--mono logo--' in row
    assert ">A</span>" in row  # "Corp" is a company word, so the initials are the first word's
    assert 'aria-hidden="true"' in row.split("today-row-body")[0]


def test_today_rows_show_the_saved_logo_when_there_is_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _account("Logo Client")
    _late_follow_up(account, "Logo check", 1)
    saved = LogoInfo(
        account_id=account,
        file="logo.png",
        content_type=ImageType.PNG,
        source=LogoSource.UPLOAD,
        saved_at=NOW,
    )
    monkeypatch.setattr(service, "all_logos", lambda: {account: saved})

    response = client.get("/today")
    _clean(response)
    (row,) = _rows(_section(_text(response), "today-due"))
    assert f'<img class="logo logo--md" src="/logos/{account}"' in row
    assert 'alt=""' in row


def test_my_book_rows_lead_with_a_logo_and_keep_each_rank(client: TestClient) -> None:
    _account("Brindle Works")
    _account("Cobalt Freight")

    markup = client.get("/?view=list").text
    body = re.search(r"<tbody>(.*?)</tbody>", markup, re.S)
    assert body is not None
    rows = re.findall(r"<tr role=\"row\">(.*?)</tr>", body.group(1), re.S)
    assert len(rows) == 2
    for row in rows:
        account_cell = re.search(
            r'<th scope="row" role="rowheader" class="cell-account">(.*?)</th>', row, re.S
        )
        assert account_cell is not None
        assert "logo logo--md" in account_cell.group(1)
        assert 'class="rank-tile"' in account_cell.group(1)
    assert re.findall(r'rank-tile"><span class="visually-hidden">Rank </span>(\d+)<', markup) == [
        "1",
        "2",
    ]


def test_my_book_cards_show_a_logo_beside_each_rank(client: TestClient) -> None:
    _account("Card One")
    _account("Card Two")

    markup = client.get("/").text
    cards = re.findall(r'<article class="account-card".*?</article>', markup, re.S)
    assert len(cards) == 2
    for card in cards:
        top = card.split('<h3 class="card-name"')[0]
        assert 'class="card-id"' in top
        assert "logo logo--md" in top
        assert 'rank-tile"><span class="visually-hidden">Rank </span>' in top
    assert re.findall(r'rank-tile"><span class="visually-hidden">Rank </span>(\d+)<', markup) == [
        "1",
        "2",
    ]


def test_my_book_logos_and_today_render_with_no_inline_markup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _account("Markup Check")
    _late_follow_up(account, "Markup", 1)
    monkeypatch.setattr(
        service,
        "all_logos",
        lambda: {
            account: LogoInfo(
                account_id=account,
                file="markup.png",
                content_type=ImageType.PNG,
                source=LogoSource.UPLOAD,
                saved_at=NOW,
            )
        },
    )
    for path in ("/today", "/", "/?view=list"):
        response = client.get(path)
        assert response.status_code == 200
        _clean(response)


def test_the_list_view_puts_the_account_first_and_has_six_column_headers(
    client: TestClient,
) -> None:
    _account("Column Check")
    table = _text(client.get("/?view=list"))
    headers = re.findall(r'<th scope="col" role="columnheader"[^>]*>([^<]+)</th>', table)
    assert headers == ["Account", "Health", "Priority", "Motion", "ARR", "Why it's on your list"]


def test_an_action_from_a_narrowed_today_keeps_the_find_text(client: TestClient) -> None:
    account = _account("Kept Find")
    _late_follow_up(account, "Close me", 1)
    (item,) = journal.open_follow_ups(service.account_journal(account))

    section = _section(_text(client.get("/today?q=kept")), "today-due")
    assert '<input type="hidden" name="q" value="kept">' in section

    path = f"/accounts/{account}/follow-ups/{item.id}/done"
    response = _post(client, path, {"back": "today", "q": "  kept  "})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/today?")
    assert "q=kept" in response.headers["location"]


def test_an_action_from_an_unfiltered_today_adds_no_find_text(client: TestClient) -> None:
    account = _account("No Find")
    _late_follow_up(account, "Close me too", 1)
    (item,) = journal.open_follow_ups(service.account_journal(account))

    assert 'name="q" value=' not in _section(_text(client.get("/today")), "today-due")
    response = _post(client, f"/accounts/{account}/follow-ups/{item.id}/done", {"back": "today"})
    assert response.status_code == 303
    assert "q=" not in response.headers["location"]
