"""The My book screen: the summary, the filter sidebar, the cards (or the ranked table in the list
view) and the htmx partial."""

from __future__ import annotations

import html
import re
from datetime import date
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import service
from wardmind.core.briefs import weekly_brief
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Health, Motion
from wardmind.web import app as web


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
    """Ranks in page order, from the cards (the rank tile) or the list view's rank cells."""
    found = re.findall(
        r'rank-tile"><span class="visually-hidden">Rank </span>(\d+)<|class="cell-rank num">(\d+)<',
        markup,
    )
    return [int(card or row) for card, row in found]


def _without_since(markup: str) -> str:
    """The page without its "Since last week" panel: that panel links accounts too, but it is not a
    card or a row of the book."""
    return re.sub(r'<section class="since-panel".*?</section>', "", markup, flags=re.S)


def _account_links(markup: str) -> list[str]:
    tags = _tags(_without_since(markup), "a")
    return [a["href"] or "" for a in tags if a.get("class") == "account-link"]


def _checked(markup: str, name: str) -> list[str]:
    """The values of the ticked inputs called `name` (checkboxes and radios)."""
    return [
        t["value"] or "" for t in _tags(markup, "input") if t.get("name") == name and "checked" in t
    ]


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
    assert markup.count('class="account-card"') == 40
    assert _account_links(markup) == [f"/accounts/{item.account.id}" for item in book]
    assert _row_ranks(markup) == list(range(1, 41))
    assert "Showing 40 accounts" in text


def test_cards_show_health_motion_priority_arr_and_reason(client: TestClient) -> None:
    book = service.get_book()
    markup = client.get("/").text
    markup = markup[markup.index('id="results"') :]  # past the sidebar's own health tags

    labels = re.findall(r'health--\w+">\s*<svg.*?</svg>\s*([^<]+?)\s*</span>', markup, re.S)
    assert labels == [item.health.label for item in book]
    motions = re.findall(r'<li class="pill pill--(\w+)">', markup)
    assert motions == [item.motion.value for item in book]

    widths = re.findall(r'class="bar-fill" width="([^"]+)"', markup)
    assert [float(width) for width in widths] == [item.priority for item in book]
    first = book[0]
    assert f"Priority <strong>{web.whole(first.priority)}</strong>" in markup
    assert f"<strong>{web.money(first.account.arr)}</strong> ARR" in markup

    text = _text(client.get("/"))
    for item in book:
        others = len(item.reasons) - 1
        if others <= 0:
            assert f"{item.top_reason}." in text
        else:
            noun = "signal" if others == 1 else "signals"
            assert f"{item.top_reason}, and {others} other {noun}." in text


def test_the_list_view_keeps_the_ranked_table(client: TestClient) -> None:
    book = service.get_book()
    markup = client.get("/?view=list").text
    body = re.search(r"<tbody>(.*?)</tbody>", markup, re.S)
    assert body is not None
    assert len(re.findall(r"<tr\b", body.group(1))) == 40
    assert 'class="account-card"' not in markup
    assert _row_ranks(markup) == list(range(1, 41))
    assert f'aria-label="Priority {web.whole(book[0].priority)} of 100"' in markup
    multi = sum(1 for item in book if len(item.reasons) > 1)
    assert markup.count('class="more-signals"') == multi
    assert _checked(markup, "view") == ["list"]
    assert _checked(client.get("/").text, "view") == ["cards"]


def test_filter_counts_come_from_the_unfiltered_book(client: TestClient) -> None:
    # Motion (churn, renewal, onboarding), then health (at risk, watch, healthy).
    expected = ["20", "12", "8", "10", "12", "18"]
    for path in ["/", "/?motion=renewal", "/?health=watch", "/?q=zzz"]:
        markup = client.get(path).text
        assert re.findall(r'filter-count">(\d+)<', markup) == expected, path


def test_renewal_filter_shows_twelve_cards_and_ticks_its_box(client: TestClient) -> None:
    markup = client.get("/?motion=renewal").text
    assert markup.count('class="account-card"') == 12
    assert _checked(markup, "motion") == ["renewal"]
    assert _checked(markup, "health") == []


def test_several_motions_and_a_health_combine(client: TestClient) -> None:
    book = service.get_book()
    markup = client.get("/?motion=renewal&motion=onboarding").text
    assert markup.count('class="account-card"') == 20
    assert _checked(markup, "motion") == ["renewal", "onboarding"]

    markup = client.get("/?motion=churn&health=at_risk").text
    expected = [
        item.account.id
        for item in book
        if item.motion is Motion.CHURN and item.health is Health.AT_RISK
    ]
    assert _account_links(markup) == [f"/accounts/{account_id}" for account_id in expected]
    assert "Clear filters" in markup


def test_unknown_filter_values_are_ignored(client: TestClient) -> None:
    markup = client.get("/?motion=bogus&health=fine&topic=gossip&view=grid").text
    assert len(_row_ranks(markup)) == 40
    assert _checked(markup, "motion") == []
    assert _checked(markup, "view") == ["cards"]
    assert "Clear filters" not in markup


def test_search_matches_account_names_only(client: TestClient) -> None:
    book = service.get_book()
    expected = {item.account.name for item in book if "brindle" in item.account.name.lower()}
    assert len(expected) == 3

    markup = client.get("/?q=BRINDLE").text
    names = set(re.findall(r'href="/accounts/[^"]+">([^<]+)</a>', _without_since(markup)))
    assert names == expected
    assert len(_row_ranks(markup)) == 3


def test_search_with_no_match_shows_the_empty_copy_and_clear_link(client: TestClient) -> None:
    response = client.get("/?q=zzz")
    assert response.status_code == 200
    assert 'No accounts match "zzz".' in _text(response)
    assert '<a href="/">Clear search</a>' in response.text


def test_filters_with_no_match_say_so(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    renewals = [item for item in service.get_book() if item.motion is Motion.RENEWAL]
    monkeypatch.setattr(service, "get_book", lambda: renewals)
    text = _text(client.get("/?motion=churn"))
    assert "No accounts match these filters." in text
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
    assert _row_ranks(client.get("/?motion=renewal&view=list").text) == expected


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
    assert re.search(
        r'<button type="submit" class="button no-js-only">Show accounts</button>', markup
    )
    assert 'src="/static/js/app.js"' in markup
    response = client.get("/static/js/app.js")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == web.CSP


def test_accessibility_hooks_are_present(client: TestClient) -> None:
    markup = client.get("/").text
    assert '<h2 class="visually-hidden">Accounts ranked by priority</h2>' in markup
    assert '<h2 class="filters-title" id="filters-title">What needs your attention?</h2>' in markup
    assert "<legend>Motion</legend>" in markup and "<legend>Health</legend>" in markup
    assert '<legend class="visually-hidden">Show accounts as</legend>' in markup
    # Every card is named by its account's heading, and the rank is read out as "Rank 1".
    cards = [t for t in _tags(markup, "article") if t.get("class") == "account-card"]
    assert [t["aria-labelledby"] for t in cards][:2] == ["card-1", "card-2"]
    assert '<h3 class="card-title" id="card-1">' in markup
    # Each filter checkbox sits inside its label, so its name is the tag text and the count.
    boxes = [t for t in _tags(markup, "input") if t.get("type") == "checkbox"]
    assert len(boxes) == 6 and all(t.get("id") for t in boxes)
    labels = [t for t in _tags(markup, "label") if t.get("for") == "q"]
    assert len(labels) == 1
    assert '<p id="result-count" class="result-count" aria-live="polite">' in markup
    assert '<h1 class="visually-hidden">My book</h1>' in markup

    table = client.get("/?view=list").text
    assert '<caption class="visually-hidden">Accounts ranked by priority</caption>' in table
    assert len(re.findall(r'<th scope="col"', table)) == 7


def test_search_blur_does_not_trigger_a_refresh(client: TestClient) -> None:
    # A bare "change" trigger fires when the search box loses focus, re-rendering the results
    # just as Tab moves focus into them, which drops keyboard focus to <body>.
    form = _tags(client.get("/").text, "form")[0]
    triggers = [part.strip() for part in (form["hx-trigger"] or "").split(",")]
    assert "change" not in triggers
    assert "change from:.filter-input" in triggers
