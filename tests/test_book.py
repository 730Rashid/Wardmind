"""The My book screen: the summary, the filter, the ranked table and the htmx partial."""

from __future__ import annotations

import html
import re
from datetime import date
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_brief.core import service
from cs_brief.core.briefs import weekly_brief
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.models import Health, Motion
from cs_brief.web import app as web


class _Tags(HTMLParser):
    """Collects the attributes of every start tag with one name."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.found: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == self.name:
            self.found.append(dict(attrs))


def _tags(markup: str, name: str) -> list[dict[str, str | None]]:
    parser = _Tags(name)
    parser.feed(markup)
    return parser.found


def _text(response: Any) -> str:
    """Page text with entities decoded, since Jinja escapes quotes and apostrophes."""
    return html.unescape(response.text)


def _row_ranks(markup: str) -> list[int]:
    return [int(rank) for rank in re.findall(r'class="cell-rank num">(\d+)<', markup)]


def _account_links(markup: str) -> list[str]:
    return [a["href"] or "" for a in _tags(markup, "a") if a.get("class") == "account-link"]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def test_full_page_has_the_summary_and_every_account(client: TestClient) -> None:
    book = service.get_book()
    brief = weekly_brief(book, as_of=date.today())
    at_risk = brief.counts_by_health[Health.AT_RISK]
    watching = brief.counts_by_health[Health.WATCH]
    assert (at_risk, watching) == (10, 12)

    response = client.get("/")
    assert response.status_code == 200
    text = _text(response)
    assert (
        f"{at_risk} accounts are at risk, carrying {web.money_short(brief.arr_at_risk)} of ARR."
        in text
    )
    assert f"{watching} more are worth watching this week." in text

    markup = response.text
    body = re.search(r"<tbody>(.*?)</tbody>", markup, re.S)
    assert body is not None
    assert len(re.findall(r"<tr\b", body.group(1))) == 40
    assert _account_links(markup) == [f"/accounts/{item.account.id}" for item in book]
    assert _row_ranks(markup)[0] == 1
    assert "Showing 40 accounts" in text


def test_rows_show_health_label_bar_and_reason(client: TestClient) -> None:
    book = service.get_book()
    markup = client.get("/").text

    labels = re.findall(r'health--\w+">\s*<svg.*?</svg>\s*([^<]+?)\s*</span>', markup, re.S)
    assert labels == [item.health.label for item in book]
    assert "At risk" in labels

    widths = re.findall(r'class="bar-fill" width="([^"]+)"', markup)
    assert [float(width) for width in widths] == [item.priority for item in book]
    assert f'aria-label="Priority {web.whole(book[0].priority)} of 100"' in markup

    text = _text(client.get("/"))
    for item in book:
        assert item.top_reason in text
    multi = sum(1 for item in book if len(item.reasons) > 1)
    assert markup.count('class="more-signals"') == multi


def test_motion_counts_come_from_the_unfiltered_book(client: TestClient) -> None:
    labels = ["All 40", "Churn 20", "Renewal 12", "Onboarding 8"]
    for path in ["/", "/?motion=renewal", "/?q=zzz"]:
        markup = client.get(path).text
        counts = re.findall(r'segment-count">(\d+)<', markup)
        assert counts == [label.split()[-1] for label in labels], path


def test_renewal_filter_shows_twelve_rows_and_checks_its_radio(client: TestClient) -> None:
    markup = client.get("/?motion=renewal").text
    body = re.search(r"<tbody>(.*?)</tbody>", markup, re.S)
    assert body is not None
    assert len(re.findall(r"<tr\b", body.group(1))) == 12
    radios = [t for t in _tags(markup, "input") if t.get("type") == "radio"]
    assert [t["value"] for t in radios if "checked" in t] == ["renewal"]


def test_unknown_motion_is_treated_as_all(client: TestClient) -> None:
    markup = client.get("/?motion=bogus").text
    assert len(_row_ranks(markup)) == 40
    radios = [t for t in _tags(markup, "input") if t.get("type") == "radio"]
    assert [t["value"] for t in radios if "checked" in t] == [""]


def test_search_matches_account_names_only(client: TestClient) -> None:
    book = service.get_book()
    expected = {item.account.name for item in book if "brindle" in item.account.name.lower()}
    assert len(expected) == 3

    markup = client.get("/?q=BRINDLE").text
    names = set(re.findall(r'href="/accounts/[^"]+">([^<]+)</a>', markup))
    assert names == expected
    assert len(_row_ranks(markup)) == 3


def test_search_with_no_match_shows_the_empty_copy_and_clear_link(client: TestClient) -> None:
    response = client.get("/?q=zzz")
    assert response.status_code == 200
    assert 'No accounts match "zzz".' in _text(response)
    assert re.search(r'<a href="/\?motion=">Clear search</a>', response.text)


def test_empty_motion_says_so(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    renewals = [item for item in service.get_book() if item.motion is Motion.RENEWAL]
    monkeypatch.setattr(service, "get_book", lambda: renewals)
    text = _text(client.get("/?motion=churn"))
    assert "No accounts are in churn right now." in text
    assert "Clear search" not in text


def test_htmx_request_gets_only_the_partial(client: TestClient) -> None:
    response = client.get("/?motion=churn", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    assert "<html" not in text
    assert 'id="results"' in text
    assert re.search(
        r'<span id="result-count" hx-swap-oob="innerHTML">Showing 20 accounts</span>', text
    )
    assert "book--intro" not in text
    assert "HX-Request" in response.headers["vary"]


def test_history_restore_gets_the_full_page(client: TestClient) -> None:
    headers = {"HX-Request": "true", "HX-History-Restore-Request": "true"}
    response = client.get("/?motion=churn", headers=headers)
    assert "<html" in response.text
    assert 'id="results"' in response.text
    assert "HX-Request" in response.headers["vary"]


def test_full_page_has_the_intro_class_and_no_oob_count(client: TestClient) -> None:
    response = client.get("/")
    assert "<html" in response.text
    assert "book--intro" in response.text
    assert "hx-swap-oob" not in response.text
    assert "HX-Request" in response.headers["vary"]


def test_ranks_stay_global_when_filtered(client: TestClient) -> None:
    book = service.get_book()
    position = {item.account.id: rank for rank, item in enumerate(book, start=1)}
    expected = [position[item.account.id] for item in book if item.motion is Motion.RENEWAL]
    assert _row_ranks(client.get("/?motion=renewal").text) == expected


@pytest.mark.parametrize(
    ("at_risk", "arr_at_risk", "watching", "first", "second"),
    [
        (0, 0.0, 0, "No accounts are at risk this week.", "Nothing else needs watching."),
        (0, 0.0, 1, "No accounts are at risk this week.", "1 account is worth watching this week."),
        (
            0,
            0.0,
            2,
            "No accounts are at risk this week.",
            "2 accounts are worth watching this week.",
        ),
        (1, 800, 0, "1 account is at risk, carrying $800 of ARR.", "Nothing else needs watching."),
        (
            1,
            800,
            1,
            "1 account is at risk, carrying $800 of ARR.",
            "1 more is worth watching this week.",
        ),
        (
            1,
            800,
            2,
            "1 account is at risk, carrying $800 of ARR.",
            "2 more are worth watching this week.",
        ),
        (
            2,
            1_400_000,
            0,
            "2 accounts are at risk, carrying $1.4M of ARR.",
            "Nothing else needs watching.",
        ),
        (
            2,
            1_400_000,
            1,
            "2 accounts are at risk, carrying $1.4M of ARR.",
            "1 more is worth watching this week.",
        ),
    ],
)
def test_summary_sentences_use_singular_and_plural(
    at_risk: int, arr_at_risk: float, watching: int, first: str, second: str
) -> None:
    assert web.summary_sentences(at_risk, arr_at_risk, watching) == (first, second)


def test_invalid_search_and_motion_render_without_error(client: TestClient) -> None:
    response = client.get("/?motion=%3Cscript%3E&q=%3Cb%3Ex%3C%2Fb%3E")
    assert response.status_code == 200
    assert "<b>x</b>" not in response.text
    assert "&lt;b&gt;x&lt;/b&gt;" in response.text


def test_no_js_submit_button_and_app_js_are_served(client: TestClient) -> None:
    markup = client.get("/").text
    assert re.search(r'<button type="submit" class="no-js-only">Show accounts</button>', markup)
    assert 'src="/static/js/app.js"' in markup
    response = client.get("/static/js/app.js")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == web.CSP


def test_accessibility_hooks_are_present(client: TestClient) -> None:
    markup = client.get("/").text
    assert '<caption class="visually-hidden">Accounts ranked by priority</caption>' in markup
    assert len(re.findall(r'<th scope="col"', markup)) == 7
    assert '<legend class="visually-hidden">Filter by motion</legend>' in markup
    labels = [t for t in _tags(markup, "label") if t.get("for") == "q"]
    assert len(labels) == 1
    assert '<p id="result-count" class="result-count" aria-live="polite">' in markup
    assert '<h1 class="visually-hidden">My book</h1>' in markup
