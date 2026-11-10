"""The Today page and what feeds it: its sections and their order, the summary sentence and its
plurals, the empty state, the actions on it, the nav badge and the Today landing setting, My book's
chips and "Since last week" panel (demo changes, and the first-day message on Your data), and the
weekly brief's "Since last week" and "Follow-ups due this week".

Runs through the real routes against the temp folders from conftest. Accounts are added by hand, so
the only network-free data is the demo book, used where its real changes are the point.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, journal, service
from wardmind.core.models import Health
from wardmind.core.settings import DataSource, Landing, Settings, load_settings, save_settings
from wardmind.web import app as web
from wardmind.web import journal_pages

ORIGIN = {"origin": "http://127.0.0.1"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)
HTMX = {"HX-Request": "true"}


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))
    service.reset_connectors()


@pytest.fixture(autouse=True)
def mine() -> None:
    _use(DataSource.MINE)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _account(name: str, renewal: date) -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": "$150,000", "renewal_date": renewal.isoformat()},
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _late_follow_up(account_id: str, text: str, days_late: int) -> None:
    """A follow-up due `days_late` days before today. The core refuses past dates for today, so the
    test pins the day the follow-up was written on: the same way the core's own tests do."""
    due = TODAY - timedelta(days=days_late)
    service.get_journal().add_follow_up(account_id, text, due, now=NOW, today=due)


def _section(page: str, section_id: str) -> str:
    match = re.search(rf'<section[^>]* id="{section_id}"[^>]*>(.*?)</section>', page, re.S)
    assert match is not None, section_id
    return match.group(1)


# --- The Today page ---------------------------------------------------------------------------


def test_today_lists_each_section_in_order_with_its_items(client: TestClient) -> None:
    late = _account("Late Follow", TODAY + timedelta(days=200))
    _late_follow_up(late, "Send the usage pack", 2)
    renewal = _account("Renewal Due", TODAY - timedelta(days=12))
    upcoming = _account("Coming Up", TODAY + timedelta(days=200))
    service.get_journal().add_follow_up(
        upcoming, "Book the QBR", TODAY + timedelta(days=3), now=NOW, today=TODAY
    )
    snoozed = _account("Quiet Account", TODAY + timedelta(days=200))
    service.get_journal().snooze(
        snoozed, TODAY + timedelta(days=7), Health.HEALTHY, now=NOW, today=TODAY
    )

    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    positions = [
        page.index(f'id="{name}"')
        for name in (
            "today-due",
            "today-renewals",
            "today-upcoming",
            "today-snoozed",
        )
    ]
    assert positions == sorted(positions)

    due = _section(page, "today-due")
    assert "Late Follow" in due and "Send the usage pack" in due
    assert "2 days late" in due and "due--late" in due
    assert "Tomorrow" in due and "+1 week" in due and "Done" in due

    renewals = _section(page, "today-renewals")
    assert "Renewal Due" in renewals
    assert "Renewal date passed 12 days ago" in renewals
    assert f"/accounts/{renewal}#outcome" in renewals and "Record the outcome" in renewals

    coming = _section(page, "today-upcoming")
    assert "Coming Up" in coming and "Book the QBR" in coming and "Due in 3 days" in coming

    snoozed_section = _section(page, "today-snoozed")
    assert "Quiet Account" in snoozed_section
    assert journal_pages.short_day(TODAY + timedelta(days=7), TODAY) in snoozed_section
    assert "Wake" in snoozed_section


def test_the_summary_sentence_counts_and_pluralises(client: TestClient) -> None:
    for number in range(3):
        account = _account(f"Due {number}", TODAY + timedelta(days=200))
        _late_follow_up(account, f"Task {number}", 1)
    renewal = _account("Renewal Only", TODAY - timedelta(days=5))
    assert renewal

    page = _text(client.get("/today"))
    assert "3 follow-ups due and 1 renewal to record." in page
    assert "You haven't recorded a contact in the last 7 days." in page


def test_the_summary_says_one_of_each_and_counts_this_weeks_contacts(client: TestClient) -> None:
    lone = _account("Solo Due", TODAY + timedelta(days=200))
    _late_follow_up(lone, "Only one", 0)
    service.get_journal().add_note(
        lone, journal.TouchKind.CALL, "Talked", TODAY, now=NOW, today=TODAY
    )
    other = _account("Second Contact", TODAY + timedelta(days=200))
    service.get_journal().add_note(
        other, journal.TouchKind.MEETING, "Met", TODAY - timedelta(days=2), now=NOW, today=TODAY
    )
    page = _text(client.get("/today"))
    assert "1 follow-up due." in page
    assert "You contacted 2 accounts in the last 7 days." in page


def test_a_book_with_nothing_due_shows_the_empty_state(client: TestClient) -> None:
    _account("Nothing Wrong", TODAY + timedelta(days=200))
    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    assert "Nothing needs you today." in page
    assert 'href="/"' in page and "Go to My book" in page
    for section in ("today-due", "today-attention", "today-renewals", "today-upcoming"):
        assert f'id="{section}"' not in page


def test_demo_accounts_at_risk_with_no_journal_need_attention(client: TestClient) -> None:
    _use(DataSource.DEMO)
    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    attention = _section(_text(response), "today-attention")
    assert "At risk" in attention
    assert "Add follow-up" in attention and "Snooze 7 days" in attention
    assert "Log a call" in attention and "#notes" in attention


def test_the_add_follow_up_on_today_posts_the_suggested_step_for_a_week(
    client: TestClient,
) -> None:
    _use(DataSource.DEMO)
    attention = _section(_text(client.get("/today")), "today-attention")
    match = re.search(r'name="text" value="([^"]*)"', attention)
    assert match is not None
    assert match.group(1) != ""
    assert 'name="days" value="7"' in attention
    assert 'name="back" value="today"' in attention


# --- The actions on Today --------------------------------------------------------------------


def test_done_on_today_offers_undo_there_and_undo_reopens(client: TestClient) -> None:
    account = _account("Done From Today", TODAY + timedelta(days=200))
    service.get_journal().add_follow_up(account, "Close it out", TODAY, now=NOW, today=TODAY)
    (item,) = journal.open_follow_ups(service.account_journal(account))

    done = _post(client, f"/accounts/{account}/follow-ups/{item.id}/done", {"back": "today"})
    assert done.status_code == 303
    assert done.headers["location"] == f"/today?journal=done&undo={item.id}"

    page = _text(client.get(done.headers["location"]))
    assert "Marked done." in page
    assert f"/accounts/{account}/follow-ups/{item.id}/undo" in page
    assert 'name="back" value="today"' in page

    undone = _post(client, f"/accounts/{account}/follow-ups/{item.id}/undo", {"back": "today"})
    assert undone.headers["location"] == "/today?journal=reopened"
    assert journal.open_follow_ups(service.account_journal(account))[0].id == item.id


def test_tomorrow_and_plus_one_week_move_a_due_follow_up(client: TestClient) -> None:
    account = _account("Moving On", TODAY + timedelta(days=200))
    service.get_journal().add_follow_up(account, "Move me", TODAY, now=NOW, today=TODAY)
    (item,) = journal.open_follow_ups(service.account_journal(account))

    tomorrow = _post(
        client,
        f"/accounts/{account}/follow-ups/{item.id}/move",
        {"back": "today", "days": "1"},
    )
    assert tomorrow.status_code == 303
    assert tomorrow.headers["location"] == (
        f"/today?journal=moved&day={(TODAY + timedelta(days=1)).isoformat()}"
    )
    assert journal.open_follow_ups(service.account_journal(account))[0].due == (
        TODAY + timedelta(days=1)
    )

    week = _post(
        client,
        f"/accounts/{account}/follow-ups/{item.id}/move",
        {"back": "today", "days": "7"},
    )
    assert week.status_code == 303
    assert journal.open_follow_ups(service.account_journal(account))[0].due == (
        TODAY + timedelta(days=7)
    )


def test_add_follow_up_and_snooze_on_today_return_there(client: TestClient) -> None:
    _use(DataSource.DEMO)
    added = _post(
        client,
        "/accounts/acc-001/follow-ups",
        {"back": "today", "text": "Ring them", "days": "7"},
    )
    assert added.headers["location"] == "/today?journal=follow-up-added"
    snoozed = _post(client, "/accounts/acc-001/snooze", {"back": "today", "days": "7"})
    assert snoozed.headers["location"].startswith("/today?journal=snoozed&day=")
    page = _text(client.get(snoozed.headers["location"]))
    assert "Snoozed until" in page


def test_wake_on_today_ends_the_snooze(client: TestClient) -> None:
    account = _account("Wake Here", TODAY + timedelta(days=200))
    service.get_journal().snooze(
        account, TODAY + timedelta(days=5), Health.HEALTHY, now=NOW, today=TODAY
    )
    response = _post(client, f"/accounts/{account}/wake", {"back": "today"})
    assert response.headers["location"] == "/today?journal=woke"
    assert service.account_journal(account).snooze is None


def test_a_bogus_back_value_on_today_actions_goes_to_the_account(client: TestClient) -> None:
    account = _account("Bogus Back", TODAY + timedelta(days=200))
    service.get_journal().add_follow_up(account, "Stay", TODAY, now=NOW, today=TODAY)
    (item,) = journal.open_follow_ups(service.account_journal(account))
    response = _post(
        client, f"/accounts/{account}/follow-ups/{item.id}/done", {"back": "//evil.example"}
    )
    assert response.headers["location"].startswith(f"/accounts/{account}?journal=done")


def test_a_missing_follow_up_on_today_is_a_friendly_page(client: TestClient) -> None:
    account = _account("Missing Item", TODAY + timedelta(days=200))
    response = _post(client, f"/accounts/{account}/follow-ups/f-00000000/done", {"back": "today"})
    assert response.status_code == 404
    _clean(response)
    page = _text(response)
    assert "That follow-up isn't in this account." in page
    assert 'href="/today"' in page and "Back to Today" in page


# --- The nav badge and the Today landing ----------------------------------------------------


def _today_link(page: str) -> str:
    match = re.search(r'<a class="nav-link" href="/today"[^>]*>.*?</a>', page, re.S)
    assert match is not None
    return match.group(0)


def test_the_nav_badge_counts_follow_ups_due_today_or_earlier(client: TestClient) -> None:
    assert "nav-badge" not in _today_link(client.get("/").text)

    account = _account("Badge Count", TODAY + timedelta(days=200))
    _late_follow_up(account, "Late one", 1)
    service.get_journal().add_follow_up(account, "Due today", TODAY, now=NOW, today=TODAY)
    service.get_journal().add_follow_up(
        account, "Later", TODAY + timedelta(days=4), now=NOW, today=TODAY
    )

    link = _today_link(client.get("/").text)
    assert 'aria-label="2 waiting">2</span>' in link
    assert link.index("Today") < link.index("nav-badge")
    first_link = re.search(r'<li><a class="nav-link" href="/today"', client.get("/").text)
    assert first_link is not None, "Today is the first nav item"


def test_the_today_landing_is_offered_and_opens_the_product_name(client: TestClient) -> None:
    appearance = client.get("/settings/appearance").text
    assert 'name="landing" value="today"' in appearance
    response = client.post(
        "/settings/appearance",
        data={"theme": "system", "accent": "plum", "density": "comfortable", "landing": "today"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_settings().landing is Landing.TODAY
    assert 'class="brand" href="/today"' in client.get("/settings/appearance").text


# --- My book: chips, and "Since last week" -------------------------------------------------


def test_my_book_chips_show_the_snooze_the_late_follow_up_and_the_contact(
    client: TestClient,
) -> None:
    account = _account("Chipped Account", TODAY + timedelta(days=200))
    _late_follow_up(account, "Chase the sign-off", 3)
    service.get_journal().add_note(
        account,
        journal.TouchKind.CALL,
        "Old call",
        TODAY - timedelta(days=12),
        now=NOW,
        today=TODAY,
    )
    service.get_journal().snooze(
        account, TODAY + timedelta(days=7), Health.HEALTHY, now=NOW, today=TODAY
    )

    page = _text(client.get("/"))
    snooze = journal_pages.short_day(TODAY + timedelta(days=7), TODAY)
    assert f"Snoozed until {snooze}" in page
    assert 'class="chip chip--late"' in page
    assert "Follow-up 3 days late" in page
    assert '<span class="card-contact">Contacted 12 days ago</span>' in page


def test_my_book_says_what_changed_since_last_week_in_demo_mode(client: TestClient) -> None:
    _use(DataSource.DEMO)
    page = _text(client.get("/"))
    assert '<h2 class="section-title" id="since-heading">Since last week</h2>' in page
    # Five of the demo's twenty-four changes are listed; the rest are a link to the brief.
    assert page.count('class="since-row"') == 5
    assert "Moved from" in page
    assert "See all in the weekly brief" in page


def test_the_since_panel_is_left_out_of_an_htmx_refresh(client: TestClient) -> None:
    _use(DataSource.DEMO)
    refresh = client.get("/", headers=HTMX)
    assert 'id="since-heading"' not in refresh.text
    assert 'class="since-row"' not in refresh.text


def test_your_data_first_day_says_when_history_started(client: TestClient) -> None:
    _account("First Day", TODAY + timedelta(days=200))
    page = _text(client.get("/"))
    started = journal_pages.short_day(TODAY, TODAY)
    later = journal_pages.short_day(TODAY + timedelta(days=7), TODAY)
    assert (
        f"Wardmind started keeping score history on {started}. "
        f"From {later} this shows what changed each week."
    ) in page


# --- The weekly brief -----------------------------------------------------------------------


def test_the_brief_has_since_last_week_and_the_follow_ups_due_this_week(client: TestClient) -> None:
    account = _account("Brief Follow", TODAY + timedelta(days=200))
    service.get_journal().add_follow_up(
        account, "Brief item", TODAY + timedelta(days=2), now=NOW, today=TODAY
    )
    _late_follow_up(account, "Overdue brief item", 1)

    response = client.get("/brief")
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    assert '<h2 class="section-title" id="since-heading">Since last week</h2>' in page
    assert '<h2 class="section-title" id="due-heading">Follow-ups due this week</h2>' in page
    rows = re.findall(r'<li class="due-row">(.*?)</li>', page, re.S)
    assert len(rows) == 2
    assert "Overdue brief item" in rows[0] and "1 day late" in rows[0]
    assert "Brief item" in rows[1] and "Due in 2 days" in rows[1]
    assert all("Brief Follow" in row for row in rows)


def test_the_brief_lists_eight_follow_ups_and_says_how_many_more(client: TestClient) -> None:
    account = _account("Many Follow", TODAY + timedelta(days=200))
    for number in range(10):
        service.get_journal().add_follow_up(
            account, f"Item {number}", TODAY + timedelta(days=1), now=NOW, today=TODAY
        )
    page = _text(client.get("/brief"))
    assert page.count('class="due-row"') == 8
    assert "and 2 more" in page


def test_the_brief_says_nothing_is_due_when_it_is_quiet(client: TestClient) -> None:
    _account("Quiet Brief", TODAY + timedelta(days=200))
    page = _text(client.get("/brief"))
    assert "Nothing is due in the next seven days." in page


def test_the_today_and_book_and_brief_pages_have_no_inline_style(client: TestClient) -> None:
    account = _account("Style Check", TODAY + timedelta(days=200))
    _late_follow_up(account, "Style", 1)
    for path in ("/today", "/", "/brief"):
        response = client.get(path)
        assert response.status_code == 200
        _clean(response)
    assert "Since last week" in _text(client.get("/brief"))


def test_the_brief_and_book_show_the_since_rows_from_real_demo_changes(client: TestClient) -> None:
    _use(DataSource.DEMO)
    book_page = client.get("/").text
    brief_page = client.get("/brief").text
    assert brief_page.count('class="since-row"') == 12
    assert "and 12 more" in _text(client.get("/brief"))
    assert book_page.count('class="since-row"') == 5
    assert "Moved from" in brief_page
    book = service.get_book()
    assert len(book) == 40
    assert sum(1 for item in book if item.health is Health.AT_RISK) == 10
