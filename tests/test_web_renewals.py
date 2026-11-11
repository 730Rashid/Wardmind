"""The renewals page and the account page's "Your call" box: the summary, the four totals and the
ARR bar, one row per renewal (soonest first) with its health, days and call buttons, the
disagreement filter, how the calls held up, the save route (kinds, clear, refusals, back targets,
origin, unknown account), the account aside (shown, current call, hidden once the renewal date has
passed) and the demo data kept apart from the CSM's own.

Runs through the real routes against the temp folders from conftest, with hand-added accounts (no
network). The date is pinned in the web app, so the day counts never flake at midnight.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, forecast, service
from wardmind.core import journal as journal_module
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.journal import CallKind, OutcomeInput, OutcomeKind
from wardmind.core.models import Account, Health, Milestones, Motion, ScoredAccount, SignalKind
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import DEMO_JOURNAL_FILE, JOURNAL_FILE, LocalStore
from wardmind.web import app as web
from wardmind.web import renewal_pages

ORIGIN = {"origin": "http://127.0.0.1"}
EVIL_ORIGIN = {"origin": "http://evil.example"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
STATIC_CSS = web.STATIC_DIR / "css"
APP_CSS = STATIC_CSS / "app.css"
PENDING_CSS = STATIC_CSS / "app.css"  # the renewals rules were merged into it
PINNED = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
SOON = PINNED + timedelta(days=12)
LATER = PINNED + timedelta(days=40)
FAR = PINNED + timedelta(days=200)  # outside the 120-day window
PAST = PINNED - timedelta(days=10)


class _PinnedDate(date):
    """Stands in for `date` in the web app, so date.today() is PINNED for the whole test."""

    @classmethod
    def today(cls) -> date:
        return PINNED


@pytest.fixture(autouse=True)
def pinned_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "date", _PinnedDate)
    _use(DataSource.MINE)


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))
    service.reset_connectors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(client: TestClient, path: str, data: dict[str, str] | None = None, **extra: Any) -> Any:
    headers = extra.pop("headers", ORIGIN)
    return client.post(path, data=data or {}, headers=headers, follow_redirects=False, **extra)


def _account(name: str, renewal: date, arr: str = "$150,000") -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": arr, "renewal_date": renewal.isoformat()},
        today=PINNED,
    )
    service.reset_connectors()
    return account.id


def _reading(account_id: str, kind: SignalKind, raw: str) -> None:
    editing.add_reading(service.get_store(), service.get_combined(), account_id, kind, raw, now=NOW)
    service.reset_connectors()


def _at_risk(name: str, renewal: date, arr: str = "$150,000") -> str:
    """An account whose readings put it At risk (risk 90 of 100, past the 45 line)."""
    account_id = _account(name, renewal, arr)
    _reading(account_id, SignalKind.CHAMPION_LEFT, "yes")
    _reading(account_id, SignalKind.OPEN_TICKETS_SEV1, "3")
    _reading(account_id, SignalKind.USAGE_TREND_30D, "-50")
    _reading(account_id, SignalKind.BILLING_OVERDUE_DAYS, "60")
    return account_id


def _journal(account_id: str) -> journal_module.AccountJournal:
    return service.account_journal(account_id)


def _call_path(account_id: str) -> str:
    return f"/accounts/{account_id}/call"


def _row(page: str, account_id: str) -> str:
    """The markup of one renewal row, from its opening tag to its closing one."""
    match = re.search(
        rf'<li class="today-row renewal-row" id="row-{re.escape(account_id)}">(.*?)</li>',
        page,
        re.S,
    )
    assert match is not None, f"no row for {account_id}"
    return match.group(0)


def _row_order(page: str) -> list[str]:
    return re.findall(r'<li class="today-row renewal-row" id="row-([^"]+)">', page)


def _buttons(row: str) -> list[tuple[str, str, str]]:
    """(kind, label, aria-pressed) for each call button in one row."""
    found = re.findall(
        r'name="kind" value="(\w+)">\s*<input type="hidden" name="back" value="renewals">\s*'
        r'(?:<input type="hidden" name="show" value="disagree">\s*)?'
        r'<button type="submit" class="button button--small call-choice" '
        r'aria-pressed="(true|false)">(\w[\w ]*?)<span',
        row,
    )
    return [(kind, label, pressed) for kind, pressed, label in found]


def _notice(page: str) -> str:
    match = re.search(r'<p class="status-line" role="status">([^<]*)</p>', page)
    return match.group(1) if match else ""


# --- The page: summary, totals and bar ----------------------------------------------------------


def test_renewals_page_is_in_the_layout_with_its_nav_link_current(client: TestClient) -> None:
    response = client.get("/renewals")
    assert response.status_code == 200
    assert "<title>Renewals - Wardmind</title>" in response.text
    assert 'href="/renewals" aria-current="page"' in response.text
    _clean(response)


def test_an_empty_book_says_so_and_shows_no_totals(client: TestClient) -> None:
    response = client.get("/renewals")
    page = _text(response)
    assert "No accounts in your book yet." in page
    assert "Add accounts in My book, and their renewals will show here." in page
    assert 'class="renewal-totals"' not in response.text
    _clean(response)


def test_no_renewal_in_the_window_is_a_plain_empty_state(client: TestClient) -> None:
    _account("Far Off Ltd", FAR)
    response = client.get("/renewals")
    page = _text(response)
    assert "No renewals in the next 120 days." in page
    assert "A renewal shows here when its date is in the next 120 days." in page
    assert "Settings > Scoring" in page
    assert 'class="renewal-totals"' not in response.text
    assert "Far Off Ltd" not in page
    _clean(response)


def test_the_summary_counts_the_renewals_their_arr_and_the_calls(client: TestClient) -> None:
    first = _account("Acme", SOON, "$400,000")
    _account("Beta", LATER, "$1,000,000")
    _account("Gamma", LATER, "$0")
    _post(client, _call_path(first), {"kind": "commit", "back": "renewals"})
    page = _text(client.get("/renewals"))
    assert (
        "3 renewals in the next 120 days, carrying $1.4M of ARR. You've called 1 of them." in page
    )


def test_the_summary_when_every_renewal_is_called_or_none_is(client: TestClient) -> None:
    first = _account("Acme", SOON)
    second = _account("Beta", LATER)
    page = _text(client.get("/renewals"))
    assert (
        "2 renewals in the next 120 days, carrying $300K of ARR. You haven't called them yet."
        in page
    )
    _post(client, _call_path(first), {"kind": "likely", "back": "renewals"})
    _post(client, _call_path(second), {"kind": "at_risk", "back": "renewals"})
    page = _text(client.get("/renewals"))
    assert "You've called all of them." in page


def test_the_summary_for_one_renewal_talks_about_it(client: TestClient) -> None:
    _account("Acme", SOON)
    page = _text(client.get("/renewals"))
    assert (
        "1 renewal in the next 120 days, carrying $150K of ARR. You haven't called it yet." in page
    )


def test_a_renewal_due_today_says_renews_today(client: TestClient) -> None:
    _account("Acme", PINNED)
    page = _text(client.get("/renewals"))
    assert "1 renewal in the next 120 days, carrying $150K of ARR." in page
    assert "Renews today, 11 Nov 2026." in page


def test_the_four_totals_come_in_order_with_their_count_and_arr(client: TestClient) -> None:
    commit = _account("Acme", SOON, "$400,000")
    likely = _account("Beta", LATER, "$100,000")
    _account("Gamma", LATER, "$50,000")
    _post(client, _call_path(commit), {"kind": "commit", "back": "renewals"})
    _post(client, _call_path(likely), {"kind": "likely", "back": "renewals"})
    response = client.get("/renewals")
    totals = re.findall(
        r'renewal-key--(\w+)" aria-hidden="true"></span>(\w[\w ]*?)</span>', response.text
    )
    assert totals == [
        ("commit", "Commit"),
        ("likely", "Likely"),
        ("at_risk", "At risk"),
        ("none", "Not called"),
    ]
    page = _text(response)
    assert "1 renewal, $400K ARR" in page
    assert "1 renewal, $100K ARR" in page
    assert "0 renewals, $0 ARR" in page
    assert "1 renewal, $50K ARR" in page


def test_the_arr_bar_is_placed_by_the_server_with_no_style_attribute(client: TestClient) -> None:
    _account("Acme", SOON, "$300,000")
    _account("Beta", LATER, "$100,000")
    response = client.get("/renewals")
    assert '<svg class="renewal-bar"' in response.text
    segments = re.findall(
        r'<rect x="([\d.]+)" width="([\d.]+)" height="24" '
        r'class="renewal-seg renewal-seg--(\w+)"[^>]*/>',
        response.text,
    )
    assert [slot for _, _, slot in segments] == ["none"]  # both accounts are not called yet
    assert segments[0][:2] == ("0.0", "100.0")
    assert "ARR by call: $400K not called" in response.text
    _clean(response)


def test_the_bar_is_split_by_call_and_runs_edge_to_edge(client: TestClient) -> None:
    commit = _account("Acme", SOON, "$300,000")
    _account("Beta", LATER, "$100,000")
    _post(client, _call_path(commit), {"kind": "commit", "back": "renewals"})
    response = client.get("/renewals")
    segments = re.findall(
        r'<rect x="([\d.]+)" width="([\d.]+)" height="24" '
        r'class="renewal-seg renewal-seg--(\w+)"[^>]*/>',
        response.text,
    )
    assert [slot for _, _, slot in segments] == ["commit", "none"]
    assert float(segments[0][0]) == 0.0
    assert float(segments[0][1]) == 75.0
    assert float(segments[1][0]) == 75.0
    assert round(float(segments[1][0]) + float(segments[1][1]), 2) == 100.0
    assert "ARR by call: $300K commit, $100K not called" in response.text


def test_a_window_with_no_arr_draws_no_bar(client: TestClient) -> None:
    _account("Acme", SOON, "$0")
    response = client.get("/renewals")
    assert "renewal-bar" not in response.text
    assert "1 renewal in the next 120 days, carrying $0 of ARR." in _text(response)


# --- Rows: order, content, buttons --------------------------------------------------------------


def test_rows_run_soonest_first_then_by_name(client: TestClient) -> None:
    late = _account("Aardvark", LATER)
    same_day_b = _account("bravo", SOON)
    same_day_a = _account("Alpha", SOON)
    early = _account("Zulu", PINNED + timedelta(days=2))
    response = client.get("/renewals")
    assert _row_order(response.text) == [early, same_day_a, same_day_b, late]


def test_each_row_has_the_logo_name_health_days_date_arr_and_negotiation_link(
    client: TestClient,
) -> None:
    account = _at_risk("Acme Robotics", SOON, "$184,000")
    response = client.get("/renewals")
    row = _row(response.text, account)
    assert f'href="/accounts/{account}"' in row
    assert ">Acme Robotics</a>" in row
    assert "Renews in 12 days, 23 Nov 2026. $184,000 ARR." in row
    assert 'class="health health--at_risk"' in row
    assert "At risk</span>" in row
    assert f'href="/accounts/{account}/renewal-sheet"' in row
    assert "Negotiation sheet" in row
    assert 'class="logo logo--md logo--mono' in row  # no logo saved: the monogram stands in


def test_days_read_as_one_day_or_several(client: TestClient) -> None:
    tomorrow = _account("Acme", PINNED + timedelta(days=1))
    _account("Beta", LATER)
    page = _text(client.get("/renewals"))
    assert "Renews in 1 day, 12 Nov 2026." in page
    assert f'id="row-{tomorrow}"' in page
    assert "Renews in 40 days, 21 Dec 2026." in page


def test_each_row_has_three_call_buttons_and_only_the_current_one_is_pressed(
    client: TestClient,
) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "likely", "back": "renewals"})
    response = client.get("/renewals")
    buttons = _buttons(_row(response.text, account))
    assert buttons == [
        ("commit", "Commit", "false"),
        ("likely", "Likely", "true"),
        ("at_risk", "At risk", "false"),
    ]
    assert "You called Likely." in _text(response)


def test_an_uncalled_row_says_so_and_has_no_clear(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = client.get("/renewals")
    row = _row(response.text, account)
    assert all(pressed == "false" for _, _, pressed in _buttons(row))
    assert "Not called yet." in row
    assert "Clear" not in row


def test_a_called_row_offers_clear(client: TestClient) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "commit", "back": "renewals"})
    row = _row(client.get("/renewals").text, account)
    assert 'name="kind" value="clear"' in row
    assert ">Clear<span" in row


def test_the_call_buttons_are_small_post_forms_to_the_call_route(client: TestClient) -> None:
    account = _account("Acme", SOON)
    row = _row(client.get("/renewals").text, account)
    forms = re.findall(r'<form class="inline-form" method="post" action="([^"]+)">', row)
    assert forms == [f"/accounts/{account}/call"] * 3
    assert row.count('name="back" value="renewals"') == 3


def test_each_row_is_one_group_with_a_name_for_its_buttons(client: TestClient) -> None:
    account = _account("Acme", SOON)
    row = _row(client.get("/renewals").text, account)
    assert 'role="group" aria-label="Your call on Acme"' in row
    assert "for Acme</span>" in row


# --- Disagreements ------------------------------------------------------------------------------


def test_a_commit_on_an_at_risk_account_disagrees_and_says_what_to_check(
    client: TestClient,
) -> None:
    account = _at_risk("Acme", SOON)
    _post(client, _call_path(account), {"kind": "commit", "back": "renewals"})
    response = client.get("/renewals")
    page = _text(response)
    assert "1 call disagrees with Wardmind's score." in page
    assert 'href="/renewals?show=disagree">Show only these</a>' in response.text
    assert "Wardmind says At risk. Check what it sees before you commit." in _row(
        response.text, account
    )


def test_an_at_risk_call_on_a_healthy_account_disagrees_the_other_way(client: TestClient) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "at_risk", "back": "renewals"})
    page = _text(client.get("/renewals"))
    assert "Wardmind sees no risk signals. Note what you know that it doesn't." in page


def test_with_no_disagreement_the_line_says_so_and_offers_no_filter(client: TestClient) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "commit", "back": "renewals"})
    response = client.get("/renewals")
    assert "No calls disagree with Wardmind's score." in _text(response)
    assert "show=disagree" not in response.text


def test_the_disagreement_filter_lists_only_those_rows_and_links_back(client: TestClient) -> None:
    agree = _account("Agree", SOON)
    disagree = _at_risk("Disagree", LATER)
    _post(client, _call_path(agree), {"kind": "commit", "back": "renewals"})
    _post(client, _call_path(disagree), {"kind": "commit", "back": "renewals"})
    unanswered = _account("Unanswered", PINNED + timedelta(days=60))
    response = client.get("/renewals?show=disagree")
    assert response.status_code == 200
    assert _row_order(response.text) == [disagree]
    page = _text(response)
    assert 'href="/renewals">Show all renewals</a>' in response.text
    assert "1 call disagrees with Wardmind's score." in page
    assert f'id="row-{unanswered}"' not in response.text
    assert f'id="row-{agree}"' not in response.text


def test_the_filter_keeps_the_totals_for_the_whole_window(client: TestClient) -> None:
    disagree = _at_risk("Disagree", SOON)
    _account("Other", LATER, "$50,000")
    _post(client, _call_path(disagree), {"kind": "commit", "back": "renewals"})
    page = _text(client.get("/renewals?show=disagree"))
    assert "1 renewal, $150K ARR" in page
    assert "1 renewal, $50K ARR" in page


def test_the_filter_carries_into_the_call_forms_as_a_hidden_field(client: TestClient) -> None:
    disagree = _at_risk("Disagree", SOON)
    _post(client, _call_path(disagree), {"kind": "commit", "back": "renewals"})
    response = client.get("/renewals?show=disagree")
    row = _row(response.text, disagree)
    assert row.count('<input type="hidden" name="show" value="disagree">') == 4  # 3 buttons + Clear


def test_a_filter_with_nothing_to_show_says_so(client: TestClient) -> None:
    _account("Acme", SOON)
    response = client.get("/renewals?show=disagree")
    assert "No calls disagree with Wardmind's score." in _text(response)
    assert _row_order(response.text) == []
    assert 'href="/renewals">Show all renewals</a>' in response.text


def test_an_unknown_show_value_shows_every_row(client: TestClient) -> None:
    _account("Acme", SOON)
    assert _row_order(client.get("/renewals?show=everything").text) != []


# --- How the calls held up ----------------------------------------------------------------------


def test_with_nothing_decided_the_calibration_says_when_it_will_show(client: TestClient) -> None:
    response = client.get("/renewals")
    page = _text(response)
    assert "How your calls held up" in page
    assert (
        "When renewals you've called are decided, this shows how your calls held up. "
        "Record each outcome on the account page." in page
    )
    assert "Wardmind's score</h3>" not in response.text


def test_the_calibration_gives_your_sentences_then_wardmind_under_a_heading(
    client: TestClient,
) -> None:
    account = _account("Acme", PAST)
    scored = _scored(account)
    service.get_journal().make_call(account, CallKind.COMMIT, scored, now=NOW, today=PAST)
    service.record_outcome(
        account,
        OutcomeInput(
            kind=OutcomeKind.RENEWED, renewal_date=PAST, decided_on=PINNED - timedelta(days=1)
        ),
        today=PINNED,
    )
    response = client.get("/renewals")
    page = _text(response)
    assert "When you said Commit, 1 of 1 renewed." in page
    assert "When Wardmind said Healthy, 1 of 1 renewed." in page
    assert page.index("When you said Commit") < page.index("When Wardmind said Healthy")
    assert "Wardmind's score</h3>" in response.text
    assert "When renewals you've called are decided" not in page


def test_the_calibration_stays_on_the_page_when_no_renewal_is_in_the_window(
    client: TestClient,
) -> None:
    account = _account("Acme", PAST)
    service.get_journal().make_call(
        account, CallKind.AT_RISK, _scored(account), now=NOW, today=PAST
    )
    service.record_outcome(
        account,
        OutcomeInput(
            kind=OutcomeKind.CHURNED, renewal_date=PAST, decided_on=PINNED - timedelta(days=1)
        ),
        today=PINNED,
    )
    page = _text(client.get("/renewals"))
    assert "No renewals in the next 120 days." in page
    assert "When you said At risk, 1 of 1 churned." in page


def _scored(account_id: str) -> ScoredAccount:
    found = service.find_account(service.get_book(as_of=PINNED), account_id)
    assert found is not None
    return found


# --- The save route -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("posted", "kind"),
    [("commit", CallKind.COMMIT), ("likely", CallKind.LIKELY), ("at_risk", CallKind.AT_RISK)],
)
def test_each_kind_is_saved_for_the_renewal_and_returns_to_its_row(
    client: TestClient, posted: str, kind: CallKind
) -> None:
    account = _account("Acme", SOON, "$150,000")
    response = _post(client, _call_path(account), {"kind": posted, "back": "renewals"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/renewals?call=call-saved#row-{account}"
    (saved,) = _journal(account).calls
    assert saved.kind is kind
    assert saved.renewal_date == SOON
    assert saved.made_on == PINNED
    assert saved.arr == 150_000
    assert _notice(_text(client.get(response.headers["location"]))) == "Call saved."


def test_a_second_call_on_the_same_renewal_replaces_the_first(client: TestClient) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "commit", "back": "renewals"})
    _post(client, _call_path(account), {"kind": "at_risk", "back": "renewals"})
    assert [entry.kind for entry in _journal(account).calls] == [CallKind.AT_RISK]


def test_back_account_returns_to_the_call_box(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = _post(client, _call_path(account), {"kind": "likely", "back": "account"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{account}?call=call-saved#call"


@pytest.mark.parametrize("back", ["", "today", "https://evil.example/", "RENEWALS"])
def test_any_other_back_value_means_the_account_page(client: TestClient, back: str) -> None:
    account = _account("Acme", SOON)
    response = _post(client, _call_path(account), {"kind": "likely", "back": back})
    assert response.headers["location"] == f"/accounts/{account}?call=call-saved#call"


def test_the_disagreement_filter_is_kept_through_the_redirect(client: TestClient) -> None:
    account = _at_risk("Acme", SOON)
    response = _post(
        client,
        _call_path(account),
        {"kind": "commit", "back": "renewals", "show": "disagree"},
    )
    assert response.headers["location"] == (
        f"/renewals?show=disagree&call=call-saved#row-{account}"
    )


def test_a_show_value_other_than_disagree_is_not_kept(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = _post(
        client, _call_path(account), {"kind": "commit", "back": "renewals", "show": "all"}
    )
    assert response.headers["location"] == f"/renewals?call=call-saved#row-{account}"


def test_clear_removes_the_call_and_says_so(client: TestClient) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "commit", "back": "renewals"})
    response = _post(client, _call_path(account), {"kind": "clear", "back": "renewals"})
    assert response.headers["location"] == f"/renewals?call=call-cleared#row-{account}"
    assert _journal(account).calls == []
    assert "Call cleared." in _text(client.get(response.headers["location"]))


def test_clear_with_no_call_still_ends_with_no_call(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = _post(client, _call_path(account), {"kind": "clear", "back": "account"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{account}?call=call-cleared#call"
    assert _journal(account).calls == []


@pytest.mark.parametrize("kind", ["", "maybe", "<script>alert(1)</script>", "COMMIT"])
def test_an_unknown_kind_gets_the_fixed_reason_and_nothing_is_saved(
    client: TestClient, kind: str
) -> None:
    account = _account("Acme", SOON)
    response = _post(client, _call_path(account), {"kind": kind, "back": "renewals"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/renewals?call=call-pick#row-{account}"
    assert "script" not in response.headers["location"]
    assert _journal(account).calls == []
    page = _text(client.get(response.headers["location"]))
    assert "Pick Commit, Likely or At risk." in page
    assert "alert(1)" not in page


def test_a_missing_kind_is_the_same_refusal(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = _post(client, _call_path(account), {"back": "account"})
    assert response.headers["location"] == f"/accounts/{account}?call=call-pick#call"


def test_an_unknown_account_is_a_404_in_the_layout(client: TestClient) -> None:
    response = _post(client, "/accounts/nope/call", {"kind": "commit", "back": "renewals"})
    assert response.status_code == 404
    assert "No account with id nope." in _text(response)
    assert "Back to My book" in _text(response)


def test_a_call_on_a_renewal_that_has_passed_is_refused(client: TestClient) -> None:
    account = _account("Acme", PAST)
    response = _post(client, _call_path(account), {"kind": "commit", "back": "account"})
    assert response.headers["location"] == f"/accounts/{account}?call=call-passed#call"
    assert _journal(account).calls == []
    assert "That renewal date has passed. Record the outcome instead." in _text(
        client.get(response.headers["location"])
    )


def test_clear_still_works_on_a_renewal_that_has_passed(client: TestClient) -> None:
    account = _account("Acme", PAST)
    service.get_journal().make_call(account, CallKind.LIKELY, _scored(account), now=NOW, today=PAST)
    response = _post(client, _call_path(account), {"kind": "clear", "back": "account"})
    assert response.headers["location"] == f"/accounts/{account}?call=call-cleared#call"
    assert _journal(account).calls == []


def test_a_post_from_another_site_is_refused_and_saves_nothing(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = _post(
        client,
        _call_path(account),
        {"kind": "commit", "back": "renewals"},
        headers=EVIL_ORIGIN,
    )
    assert response.status_code == 403
    assert _journal(account).calls == []


def test_an_unknown_notice_code_on_the_page_shows_nothing(client: TestClient) -> None:
    _account("Acme", SOON)
    response = client.get("/renewals?call=<b>hi</b>")
    assert response.status_code == 200
    assert _notice(response.text) == ""
    assert "<b>hi</b>" not in response.text


# --- The account page's "Your call" box ---------------------------------------------------------


def test_the_account_box_shows_the_choices_with_the_current_one_checked(
    client: TestClient,
) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "likely", "back": "renewals"})
    response = client.get(f"/accounts/{account}")
    assert response.status_code == 200
    box = re.search(r'<section class="call-box" id="call".*?</section>', response.text, re.S)
    assert box is not None
    markup = box.group(0)
    assert '<h2 class="section-title" id="call-heading">Your call</h2>' in markup
    assert (
        "Your own forecast for the renewal on 23 Nov 2026. Wardmind keeps it private and later "
        "shows how your calls held up." in _text(response)
    )
    assert 'name="kind" value="likely" checked' in markup
    assert (
        'name="kind" value="commit">' in markup
        and 'name="kind" value="commit" checked' not in markup
    )
    assert f'action="/accounts/{account}/call"' in markup
    assert 'name="back" value="account"' in markup
    assert 'href="/renewals">All renewals</a>' in markup
    _clean(response)


def test_the_account_box_says_what_the_current_call_is_and_offers_clear(
    client: TestClient,
) -> None:
    account = _account("Acme", SOON)
    _post(client, _call_path(account), {"kind": "commit", "back": "renewals"})
    page = _text(client.get(f"/accounts/{account}"))
    assert "You called Commit on 11 Nov." in page
    assert 'name="kind" value="clear"' in page


def test_the_account_box_with_no_call_has_no_clear_and_no_checked_choice(
    client: TestClient,
) -> None:
    account = _account("Acme", SOON)
    response = client.get(f"/accounts/{account}")
    assert 'id="call"' in response.text
    assert "checked" not in response.text.split('id="call"', 1)[1].split("</section>", 1)[0]
    assert 'name="kind" value="clear"' not in response.text
    assert "You called" not in _text(response)


def test_a_disagreeing_call_is_explained_in_the_account_box(client: TestClient) -> None:
    account = _at_risk("Acme", SOON)
    _post(client, _call_path(account), {"kind": "commit", "back": "renewals"})
    box = _text(client.get(f"/accounts/{account}"))
    assert "Wardmind says At risk. Check what it sees before you commit." in box


def test_the_account_box_is_hidden_once_the_renewal_date_has_passed(client: TestClient) -> None:
    account = _account("Acme", PAST)
    response = client.get(f"/accounts/{account}")
    assert response.status_code == 200
    assert 'id="call"' not in response.text
    assert "Your own forecast" not in _text(response)
    assert "Record the outcome" in _text(response)  # the outcome form takes over


def test_the_account_box_is_shown_on_the_renewal_date_itself(client: TestClient) -> None:
    account = _account("Acme", PINNED)
    assert 'id="call"' in client.get(f"/accounts/{account}").text


def test_the_account_page_names_a_saved_call_and_a_clear(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = _post(client, _call_path(account), {"kind": "commit", "back": "account"})
    page = _text(client.get(response.headers["location"]))
    assert "Call saved." in page
    response = _post(client, _call_path(account), {"kind": "clear", "back": "account"})
    assert "Call cleared." in _text(client.get(response.headers["location"]))


def test_the_account_page_names_a_refused_call(client: TestClient) -> None:
    account = _account("Acme", SOON)
    response = _post(client, _call_path(account), {"kind": "soon", "back": "account"})
    assert "Pick Commit, Likely or At risk." in _text(client.get(response.headers["location"]))


# --- Demo data ----------------------------------------------------------------------------------


def test_demo_calls_are_kept_in_the_demo_journal_and_not_in_your_own(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.DEMO)
    upcoming = [
        account for account in DemoConnector().list_accounts() if account.renewal_date >= PINNED
    ]
    assert upcoming, "the demo book has no renewal on or after the pinned day"
    target = upcoming[0].id
    response = _post(client, _call_path(target), {"kind": "commit", "back": "account"})
    assert response.status_code == 303
    assert (store.root / DEMO_JOURNAL_FILE).exists()
    assert not (store.root / JOURNAL_FILE).exists()
    assert "You called Commit on 11 Nov." in _text(client.get(f"/accounts/{target}"))


def test_the_demo_renewals_page_renders(client: TestClient) -> None:
    _use(DataSource.DEMO)
    response = client.get("/renewals")
    assert response.status_code == 200
    _clean(response)


# --- Pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("back", "show", "code", "location"),
    [
        ("renewals", "", "call-saved", "/renewals?call=call-saved#row-acc-1"),
        (
            "renewals",
            "disagree",
            "call-cleared",
            "/renewals?show=disagree&call=call-cleared#row-acc-1",
        ),
        ("renewals", "other", "call-pick", "/renewals?call=call-pick#row-acc-1"),
        ("account", "disagree", "call-saved", "/accounts/acc-1?call=call-saved#call"),
        ("", "", "call-saved", "/accounts/acc-1?call=call-saved#call"),
    ],
)
def test_call_redirect_targets(back: str, show: str, code: str, location: str) -> None:
    assert renewal_pages.call_redirect(back, "acc-1", code, show) == location


def test_a_row_anchor_with_a_slash_in_its_id_is_percent_encoded_in_the_redirect() -> None:
    location = renewal_pages.call_redirect("renewals", "a/b", "call-saved", "")
    assert location.endswith("#row-a%2Fb")
    assert renewal_pages.row_anchor("a/b") == "row-a/b"


def test_renewal_and_call_dates_read_in_plain_words() -> None:
    assert renewal_pages.renewal_day(date(2027, 1, 19)) == "19 Jan 2027"
    assert renewal_pages.call_day(date(2026, 11, 3), PINNED) == "3 Nov"
    assert renewal_pages.call_day(date(2025, 11, 3), PINNED) == "3 Nov 2025"


def test_the_notice_codes_map_to_fixed_sentences() -> None:
    assert renewal_pages.call_notice("call-saved") == "Call saved."
    assert renewal_pages.call_notice("call-cleared") == "Call cleared."
    assert renewal_pages.call_notice("call-pick") == "Pick Commit, Likely or At risk."
    assert renewal_pages.call_notice("made-up") == ""


def test_the_account_call_box_is_hidden_from_the_day_after_the_renewal() -> None:
    account = _pure_account("Acme", PINNED - timedelta(days=1))
    assert renewal_pages.account_call(account, None, PINNED).visible is False
    assert renewal_pages.account_call(_pure_account("Acme", PINNED), None, PINNED).visible is True


def test_the_account_call_box_reads_the_current_call_and_its_choices() -> None:
    account = _pure_account("Acme", SOON)
    call = _pure_call(CallKind.LIKELY, made_on=date(2026, 10, 30))
    box = renewal_pages.account_call(account, call, PINNED)
    assert box.current == "You called Likely on 30 Oct."
    assert [(choice.value, choice.label, choice.selected) for choice in box.choices] == [
        ("commit", "Commit", False),
        ("likely", "Likely", True),
        ("at_risk", "At risk", False),
    ]


def test_the_bar_has_no_gap_and_covers_the_whole_width() -> None:
    view = _pure_view(
        [
            ("Acme", SOON, Health.HEALTHY, CallKind.COMMIT, 300_000),
            ("Beta", LATER, Health.HEALTHY, None, 100_000),
            ("Gamma", LATER, Health.HEALTHY, CallKind.AT_RISK, 50_000),
        ]
    )
    page = _pure_page(view)
    segments = page.segments
    assert [segment.slot for segment in segments] == ["commit", "at_risk", "none"]
    assert segments[0].x == 0.0
    for before, after in zip(segments, segments[1:], strict=False):
        assert round(before.x + before.width, 2) == after.x
    assert round(segments[-1].x + segments[-1].width, 2) == 100.0


def test_totals_are_always_the_four_calls_even_with_no_renewals() -> None:
    page = _pure_page(_pure_view([]))
    assert [total.slot for total in page.totals] == ["commit", "likely", "at_risk", "none"]
    assert page.empty is True
    assert page.segments == []


def test_a_one_day_window_and_no_window_read_in_plain_words() -> None:
    assert _pure_page(_pure_view([]), window_days=1).summary == "No renewals in the next day."
    assert _pure_page(_pure_view([]), window_days=0).summary == "No renewals today."


def test_the_disagreement_line_counts_in_words() -> None:
    one = _pure_page(_pure_view([("Acme", SOON, Health.HEALTHY, CallKind.AT_RISK, 1.0)]))
    assert one.disagree_line == "1 call disagrees with Wardmind's score."
    two = _pure_page(
        _pure_view(
            [
                ("Acme", SOON, Health.HEALTHY, CallKind.AT_RISK, 1.0),
                ("Beta", LATER, Health.HEALTHY, CallKind.AT_RISK, 1.0),
            ]
        )
    )
    assert two.disagree_line == "2 calls disagree with Wardmind's score."


# --- Templates, CSS and line endings ------------------------------------------------------------


def test_the_renewals_templates_have_no_inline_script_or_style() -> None:
    for name in ("renewals.html", "accounts/_call.html"):
        text = (web.TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert not INLINE.search(text), name


@pytest.mark.parametrize(
    "path",
    [
        web.TEMPLATES_DIR / "renewals.html",
        web.TEMPLATES_DIR / "accounts" / "_call.html",
        PENDING_CSS,
        Path(renewal_pages.__file__),
        Path(__file__),
    ],
)
def test_new_files_use_lf_line_endings(path: Path) -> None:
    assert b"\r" not in path.read_bytes()


# --- Helpers for the pure tests -----------------------------------------------------------------


def _pure_account(name: str, renewal: date, arr: float = 150_000) -> Account:
    return Account(
        id=f"acc-{name.lower()}",
        name=name,
        arr=arr,
        tier="Growth",
        owner="Sam",
        renewal_date=renewal,
        milestones=Milestones(done=0, total=0),
    )


def _pure_call(kind: CallKind, *, made_on: date) -> journal_module.ForecastCall:
    return journal_module.ForecastCall(
        id="c-00000001",
        kind=kind,
        renewal_date=SOON,
        made_on=made_on,
        health=Health.HEALTHY,
        risk=0.0,
        arr=150_000,
        recorded_at=datetime(2026, 10, 30, 9, 0),
    )


def _pure_view(
    specs: list[tuple[str, date, Health, CallKind | None, float]],
) -> forecast.ForecastView:
    """A forecast view built from (name, renewal, health, call, arr) rows, with the calls made on
    the same day as the renewal's journal entry."""
    book: list[ScoredAccount] = []
    journals: dict[str, journal_module.AccountJournal] = {}
    for name, renewal, health, call, arr in specs:
        account = _pure_account(name, renewal, arr)
        book.append(
            ScoredAccount(
                account=account,
                motion=Motion.RENEWAL,
                health=health,
                risk=0.0,
                priority=0.0,
                reasons=[],
            )
        )
        if call is not None:
            entry = journal_module.ForecastCall(
                id="c-00000002",
                kind=call,
                renewal_date=renewal,
                made_on=PINNED,
                health=health,
                risk=0.0,
                arr=arr,
                recorded_at=datetime(2026, 11, 11, 9, 0),
            )
            journals[account.id] = journal_module.AccountJournal(calls=[entry])
    return forecast.forecast_view(book, journals, PINNED, 120)


def _pure_page(view: forecast.ForecastView, window_days: int = 120) -> renewal_pages.RenewalsPage:
    return renewal_pages.renewals_page(
        view,
        forecast.calibration({}),
        window_days=window_days,
        book_size=len(view.rows) or 1,
        disagree_only=False,
        notice="",
        money_short=web.money_short,
    )
