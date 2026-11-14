"""The mirror: what the CSM keeps putting off, said back as a choice. The counts (FollowUp.moved
and AccountJournal.snoozed_on) and the two lines (slip_line, snooze_line) with their thresholds
and exact sentences, the snooze window's edge, a journal file from before the mirror, and the
web: the lines on the Today rows and the account page, and nothing shown below the thresholds.

The core tests use a temp store (as in test_journal.py). The web tests run through the real routes
against the temp folders from conftest, with hand-added accounts; the demo book only where its own
at-risk account is the point. Nothing here writes config/ or the user's profile.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, service
from wardmind.core.journal import (
    MAX_SNOOZE_DAYS_KEPT,
    SLIP_MOVES,
    SLIP_SNOOZE_WINDOW,
    AccountJournal,
    FollowUp,
    FollowUpStatus,
    Journal,
    JournalData,
    Snooze,
    slip_line,
    snooze_line,
)
from wardmind.core.models import Health
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import JOURNAL_FILE, LocalStore
from wardmind.web import app as web

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 9, 0)
LATER = datetime(2026, 11, 9, 10, 0)
ACC = "acc-1"
SLIP_TAIL = "Do it, hand it off or remove it."
SNOOZE_TAIL = "Decide: act on it, hand it off or let it go."
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def ahead(days: int) -> date:
    return TODAY + timedelta(days=days)


def refused(message: str):
    """A ValueError whose text is exactly this plain message."""
    return pytest.raises(ValueError, match=re.escape(message))


def _day(day: date) -> str:
    """'2 Oct 2026': no leading zero, a short month, the year."""
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


# --- Core: fixtures and builders ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


@pytest.fixture
def journal(store: LocalStore) -> Journal:
    return Journal(store)


def new_follow_up(journal: Journal, *, due: date, text: str = "Send the deck") -> FollowUp:
    return journal.add_follow_up(ACC, text, due, now=NOW, today=TODAY)


def snooze_on(journal: Journal, day: date, account: str = ACC) -> None:
    """A snooze made on `day` (its until date is checked against that day, not today)."""
    journal.snooze(account, day + timedelta(days=3), Health.WATCH, now=NOW, today=day)


def item_moved(moved: int, status: FollowUpStatus = FollowUpStatus.OPEN) -> FollowUp:
    """A follow-up with this many later moves, built directly for the line's own tests."""
    done_at = LATER if status is FollowUpStatus.DONE else None
    return FollowUp(
        id="f-00000001",
        text="Send the deck",
        due=ahead(2),
        status=status,
        created_at=NOW,
        done_at=done_at,
        moved=moved,
    )


# --- Core: the moved count ----------------------------------------------------------------------


def test_a_new_follow_up_has_no_moves_and_a_later_move_adds_one_each_time(journal):
    item = new_follow_up(journal, due=ahead(2))
    assert item.moved == 0

    once = journal.move_follow_up(ACC, item.id, ahead(5), today=TODAY)
    twice = journal.move_follow_up(ACC, item.id, ahead(9), today=TODAY)

    assert (once.moved, twice.moved) == (1, 2)
    assert journal.for_account(ACC).follow_ups[0].moved == 2


@pytest.mark.parametrize("day", [ahead(1), ahead(5)], ids=["earlier", "same-day"])
def test_a_move_earlier_or_to_the_same_day_does_not_count(journal, day):
    item = new_follow_up(journal, due=ahead(5))

    moved = journal.move_follow_up(ACC, item.id, day, today=TODAY)

    assert (moved.due, moved.moved) == (day, 0)


def test_a_move_back_earlier_keeps_the_count_it_already_has(journal):
    item = new_follow_up(journal, due=ahead(2))
    journal.move_follow_up(ACC, item.id, ahead(6), today=TODAY)

    back = journal.move_follow_up(ACC, item.id, ahead(3), today=TODAY)

    assert (back.due, back.moved) == (ahead(3), 1)


def test_a_move_later_than_an_overdue_date_counts_even_when_it_only_reaches_today(journal):
    item = journal.add_follow_up(ACC, "Overdue", ago(3), now=NOW, today=ago(3))

    moved = journal.move_follow_up(ACC, item.id, TODAY, today=TODAY)

    assert moved.moved == 1


def test_a_done_follow_up_cannot_move_so_its_count_stays_as_it_was(journal):
    item = new_follow_up(journal, due=ahead(2))
    journal.move_follow_up(ACC, item.id, ahead(5), today=TODAY)
    journal.complete_follow_up(ACC, item.id, now=LATER)

    with refused("That follow-up is already done."):
        journal.move_follow_up(ACC, item.id, ahead(9), today=TODAY)

    kept = journal.for_account(ACC).follow_ups[0]
    assert (kept.due, kept.moved) == (ahead(5), 1)


def test_done_and_reopened_keep_the_count(journal):
    item = new_follow_up(journal, due=ahead(2))
    for day in (ahead(3), ahead(4), ahead(5)):
        journal.move_follow_up(ACC, item.id, day, today=TODAY)
    journal.complete_follow_up(ACC, item.id, now=LATER)

    reopened = journal.reopen_follow_up(ACC, item.id)

    assert (reopened.status, reopened.moved) == (FollowUpStatus.OPEN, 3)


def test_linking_accounts_keeps_a_moved_follow_up_its_count(journal):
    item = journal.add_follow_up("acc-a", "Moved", ahead(2), now=NOW, today=TODAY)
    for day in (ahead(3), ahead(4), ahead(5)):
        journal.move_follow_up("acc-a", item.id, day, today=TODAY)

    journal.move_account("acc-a", "acc-b")

    (kept,) = journal.for_account("acc-b").follow_ups
    assert kept.moved == 3


# --- Core: the snooze days (AccountJournal.snoozed_on) -----------------------------------------


def test_each_snooze_adds_its_day_and_two_on_the_same_day_both_count(journal):
    snooze_on(journal, TODAY)
    snooze_on(journal, TODAY)

    assert journal.for_account(ACC).snoozed_on == [TODAY, TODAY]


def test_snooze_days_stay_in_ascending_order_when_an_earlier_day_is_recorded_later(journal):
    snooze_on(journal, TODAY)
    snooze_on(journal, ago(4))
    snooze_on(journal, ago(9))

    assert journal.for_account(ACC).snoozed_on == [ago(9), ago(4), TODAY]


def test_only_the_newest_snooze_days_are_kept_and_the_oldest_are_dropped(journal):
    for back in range(MAX_SNOOZE_DAYS_KEPT + 2, 0, -1):
        snooze_on(journal, ago(back))

    kept = journal.for_account(ACC).snoozed_on
    assert kept == [ago(back) for back in range(MAX_SNOOZE_DAYS_KEPT, 0, -1)]


def test_waking_an_account_keeps_its_snooze_days(journal):
    snooze_on(journal, ago(2))

    assert journal.wake(ACC) is True

    entry = journal.for_account(ACC)
    assert entry.snooze is None
    assert entry.snoozed_on == [ago(2)]


def test_linking_accounts_keeps_both_sides_snooze_days_in_order(journal):
    snooze_on(journal, ago(3), account="acc-a")
    snooze_on(journal, ago(1), account="acc-b")
    snooze_on(journal, TODAY, account="acc-b")

    journal.move_account("acc-a", "acc-b")

    assert journal.for_account("acc-b").snoozed_on == [ago(3), ago(1), TODAY]


def test_linking_keeps_only_the_newest_snooze_days_across_both_accounts(journal):
    for back in range(30, 15, -1):
        snooze_on(journal, ago(back), account="acc-a")
    for back in range(15, 0, -1):
        snooze_on(journal, ago(back), account="acc-b")

    journal.move_account("acc-a", "acc-b")

    kept = journal.for_account("acc-b").snoozed_on
    assert kept == [ago(back) for back in range(MAX_SNOOZE_DAYS_KEPT, 0, -1)]


# --- Core: slip_line (a follow-up moved later three times or more) -----------------------------


@pytest.mark.parametrize("moved", [0, 1, SLIP_MOVES - 1])
def test_slip_line_is_silent_below_three_later_moves(moved):
    assert slip_line(item_moved(moved)) is None


@pytest.mark.parametrize("moved", [SLIP_MOVES, SLIP_MOVES + 1, 9])
def test_slip_line_names_the_count_for_an_open_follow_up(moved):
    assert slip_line(item_moved(moved)) == f"Moved {moved} times. {SLIP_TAIL}"


def test_slip_line_gives_the_exact_sentence():
    assert slip_line(item_moved(4)) == "Moved 4 times. Do it, hand it off or remove it."


def test_slip_line_is_silent_for_a_done_follow_up_however_often_it_moved():
    assert slip_line(item_moved(5, FollowUpStatus.DONE)) is None


# --- Core: snooze_line (an account snoozed three times or more in 90 days) ---------------------


def test_snooze_line_is_silent_below_three_snoozes_in_the_window():
    two = AccountJournal(snoozed_on=[ago(10), ago(5)])

    assert snooze_line(two, TODAY) is None
    assert snooze_line(AccountJournal(), TODAY) is None


def test_snooze_line_gives_the_exact_sentence_with_the_day_without_a_leading_zero():
    entry = AccountJournal(snoozed_on=[date(2026, 10, 2), date(2026, 10, 20), date(2026, 11, 1)])

    assert snooze_line(entry, TODAY) == (
        "Snoozed 3 times since 2 Oct 2026. Decide: act on it, hand it off or let it go."
    )


def test_snooze_line_counts_only_the_window_and_dates_from_the_earliest_counted_snooze():
    entry = AccountJournal(snoozed_on=[ago(120), ago(80), ago(5), TODAY])

    assert snooze_line(entry, TODAY) == (f"Snoozed 3 times since 21 Aug 2026. {SNOOZE_TAIL}")


def test_the_window_edge_counts_a_snooze_89_days_back_and_not_one_90_days_back():
    """The window is the days within (today - SLIP_SNOOZE_WINDOW, today]: a snooze exactly
    SLIP_SNOOZE_WINDOW - 1 days ago counts, and one exactly SLIP_SNOOZE_WINDOW days ago doesn't."""
    just_inside = AccountJournal(snoozed_on=[ago(SLIP_SNOOZE_WINDOW - 1), ago(10), TODAY])
    assert snooze_line(just_inside, TODAY) == (f"Snoozed 3 times since 12 Aug 2026. {SNOOZE_TAIL}")

    just_outside = AccountJournal(snoozed_on=[ago(SLIP_SNOOZE_WINDOW), ago(10), TODAY])
    assert snooze_line(just_outside, TODAY) is None

    # Only the 89-days-back one and the two recent ones count: the 90-days-back one is left out,
    # so the date is 12 Aug, not 11 Aug.
    mixed = AccountJournal(
        snoozed_on=[ago(SLIP_SNOOZE_WINDOW), ago(SLIP_SNOOZE_WINDOW - 1), ago(10), TODAY]
    )
    assert snooze_line(mixed, TODAY) == (f"Snoozed 3 times since 12 Aug 2026. {SNOOZE_TAIL}")


def test_a_snooze_made_today_counts_toward_the_three():
    entry = AccountJournal(snoozed_on=[TODAY, TODAY, TODAY])

    assert snooze_line(entry, TODAY) == f"Snoozed 3 times since 9 Nov 2026. {SNOOZE_TAIL}"


def test_a_snooze_dated_after_today_is_not_counted():
    entry = AccountJournal(snoozed_on=[ahead(1), TODAY, TODAY])

    assert snooze_line(entry, TODAY) is None


def test_snoozes_older_than_the_window_do_not_count():
    entry = AccountJournal(snoozed_on=[ago(200), ago(150), ago(120)])

    assert snooze_line(entry, TODAY) is None


# --- Core: a journal file from before the mirror -----------------------------------------------


def test_a_journal_file_from_before_the_mirror_loads_with_no_moves_and_no_snooze_days(
    store, journal
):
    data = JournalData(
        by_account={
            ACC: AccountJournal(
                follow_ups=[
                    FollowUp(id="f-00000001", text="Old", due=ahead(2), created_at=NOW),
                ],
                snooze=Snooze(until=ahead(5), health=Health.WATCH, created_at=NOW),
            )
        }
    )
    raw = json.loads(data.model_dump_json())
    for entry in raw["by_account"].values():
        entry.pop("snoozed_on", None)
        for item in entry["follow_ups"]:
            item.pop("moved", None)
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_text(json.dumps(raw), encoding="utf-8")

    loaded = Journal(store).for_account(ACC)
    assert loaded.follow_ups[0].moved == 0
    assert loaded.snoozed_on == []
    assert slip_line(loaded.follow_ups[0]) is None
    assert snooze_line(loaded, TODAY) is None

    moved = journal.move_follow_up(ACC, loaded.follow_ups[0].id, ahead(4), today=TODAY)
    assert moved.moved == 1
    snooze_on(journal, TODAY)
    assert Journal(store).for_account(ACC).snoozed_on == [TODAY]


# --- Web: fixtures and helpers -----------------------------------------------------------------

ORIGIN = {"origin": "http://127.0.0.1"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
WEB_DAY = date.today()  # the web app reads date.today()
WEB_NOW = datetime.now(UTC).replace(tzinfo=None)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(client: TestClient, path: str, data: dict[str, str]) -> Any:
    return client.post(path, data=data, headers=ORIGIN, follow_redirects=False)


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))
    service.reset_connectors()


def _section(page: str, section_id: str) -> str:
    match = re.search(rf'<section[^>]* id="{section_id}"[^>]*>(.*?)</section>', page, re.S)
    assert match is not None, section_id
    return match.group(1)


def _account(name: str) -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": name,
            "arr": "$150,000",
            "renewal_date": (WEB_DAY + timedelta(days=200)).isoformat(),
        },
        today=WEB_DAY,
    )
    service.reset_connectors()
    return account.id


def _slipped(account_id: str, text: str, *, first: date, moves: int) -> str:
    """A follow-up due `first`, then moved later `moves` times, a day each time. Returns its id."""
    store_journal = service.get_journal()
    item = store_journal.add_follow_up(account_id, text, first, now=WEB_NOW, today=WEB_DAY)
    for step in range(1, moves + 1):
        store_journal.move_follow_up(
            account_id, item.id, first + timedelta(days=step), today=WEB_DAY
        )
    return item.id


def _snooze_times(account_id: str, times: int) -> None:
    for _ in range(times):
        service.get_journal().snooze(
            account_id, WEB_DAY + timedelta(days=7), Health.AT_RISK, now=WEB_NOW, today=WEB_DAY
        )


def _demo_attention_account(client: TestClient) -> str:
    """The first account the demo book lists under Needs attention (at risk, nothing open)."""
    attention = _section(_text(client.get("/today")), "today-attention")
    match = re.search(r'href="/accounts/([^"#?]+)"', attention)
    assert match is not None
    return match.group(1)


@pytest.fixture(autouse=True)
def mine() -> None:
    _use(DataSource.MINE)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


# --- Web: the Today rows -----------------------------------------------------------------------


def test_a_due_row_shows_the_follow_up_mirror_after_three_later_moves(client: TestClient):
    account = _account("Slipping Due")
    item_id = _slipped(account, "Send the usage pack", first=WEB_DAY, moves=3)
    # An earlier move doesn't count, and brings it back to today's due list.
    service.get_journal().move_follow_up(account, item_id, WEB_DAY, today=WEB_DAY)

    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    due = _section(_text(response), "today-due")
    assert "Send the usage pack" in due
    assert f"Moved 3 times. {SLIP_TAIL}" in due


def test_a_coming_up_row_shows_the_follow_up_mirror(client: TestClient):
    account = _account("Slipping Next")
    _slipped(account, "Book the QBR", first=WEB_DAY + timedelta(days=2), moves=3)

    coming = _section(_text(client.get("/today")), "today-upcoming")
    assert "Book the QBR" in coming
    assert f"Moved 3 times. {SLIP_TAIL}" in coming


def test_two_later_moves_show_no_follow_up_mirror(client: TestClient):
    account = _account("Two Moves")
    _slipped(account, "Only twice", first=WEB_DAY + timedelta(days=2), moves=2)

    page = _text(client.get("/today"))
    assert "Only twice" in _section(page, "today-upcoming")
    assert SLIP_TAIL not in page


def test_moves_earlier_than_the_due_date_show_no_follow_up_mirror(client: TestClient):
    account = _account("Earlier Moves")
    item = service.get_journal().add_follow_up(
        account, "Pulled in", WEB_DAY + timedelta(days=6), now=WEB_NOW, today=WEB_DAY
    )
    for day in (5, 4, 3):
        service.get_journal().move_follow_up(
            account, item.id, WEB_DAY + timedelta(days=day), today=WEB_DAY
        )

    page = _text(client.get("/today"))
    assert "Pulled in" in _section(page, "today-upcoming")
    assert SLIP_TAIL not in page


def test_a_needs_attention_row_shows_the_snooze_mirror_once_three_snoozes_are_recorded(
    client: TestClient,
):
    _use(DataSource.DEMO)
    account = _demo_attention_account(client)
    _snooze_times(account, 3)
    service.get_journal().wake(account)

    attention = _section(_text(client.get("/today")), "today-attention")
    assert f'href="/accounts/{account}"' in attention
    assert f"Snoozed 3 times since {_day(WEB_DAY)}. {SNOOZE_TAIL}" in attention


def test_a_snoozed_row_shows_the_snooze_mirror(client: TestClient):
    _use(DataSource.DEMO)
    account = _demo_attention_account(client)
    _snooze_times(account, 3)

    snoozed = _section(_text(client.get("/today")), "today-snoozed")
    assert f'href="/accounts/{account}"' in snoozed
    assert f"Snoozed 3 times since {_day(WEB_DAY)}. {SNOOZE_TAIL}" in snoozed


def test_two_snoozes_show_no_snooze_mirror_on_today(client: TestClient):
    _use(DataSource.DEMO)
    account = _demo_attention_account(client)
    _snooze_times(account, 2)

    page = _text(client.get("/today"))
    assert f'href="/accounts/{account}"' in _section(page, "today-snoozed")
    assert SNOOZE_TAIL not in page


# --- Web: the account page ---------------------------------------------------------------------


def test_the_account_page_shows_the_follow_up_mirror_under_the_item(client: TestClient):
    account = _account("Account Mirror")
    item_id = _slipped(account, "Send the deck", first=WEB_DAY + timedelta(days=2), moves=0)
    for step in (3, 4, 5):
        response = _post(
            client,
            f"/accounts/{account}/follow-ups/{item_id}/move",
            {"due": (WEB_DAY + timedelta(days=step)).isoformat(), "back": "account"},
        )
        assert response.status_code == 303

    page = _text(client.get(f"/accounts/{account}"))
    follow_ups = _section(page, "follow-ups")
    assert "Send the deck" in follow_ups
    assert f"Moved 3 times. {SLIP_TAIL}" in follow_ups


def test_the_account_page_shows_the_snooze_mirror_by_the_snooze_controls(client: TestClient):
    account = _account("Snoozed Often")
    _snooze_times(account, 3)

    response = client.get(f"/accounts/{account}")
    assert response.status_code == 200
    _clean(response)
    next_step = _section(_text(response), "next-step")
    assert f"Snoozed 3 times since {_day(WEB_DAY)}. {SNOOZE_TAIL}" in next_step
    assert "Wake now" in next_step


def test_the_account_page_shows_nothing_below_the_thresholds(client: TestClient):
    account = _account("Below Thresholds")
    _slipped(account, "Twice moved", first=WEB_DAY + timedelta(days=2), moves=2)
    _snooze_times(account, 2)

    page = _text(client.get(f"/accounts/{account}"))
    assert "Twice moved" in page
    assert SLIP_TAIL not in page
    assert SNOOZE_TAIL not in page
