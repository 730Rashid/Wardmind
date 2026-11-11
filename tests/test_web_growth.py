"""Room to grow on the web: the account aside's section, the band tags on My book's rows and cards,
and the Sort by switch (sort=growth, the htmx partial, and the filters and search it combines with).

The score itself is tested in test_growth.py; here the page shows what growth.py decides. Funding
news is set by monkeypatching the service helpers, so no test depends on the date or on a website.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wardmind.core import growth, service
from wardmind.core.config import load_scoring_config
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.growth import FundingNews, GrowthBand, GrowthRadar
from wardmind.core.models import (
    Account,
    Health,
    Milestones,
    Motion,
    ScoredAccount,
    Signal,
    SignalKind,
)
from wardmind.web import app as web

SEATS = SignalKind.SEAT_UTILISATION
USAGE = SignalKind.USAGE_TREND_30D
NPS = SignalKind.NPS_LATEST
CSS_DIR = Path(web.__file__).parent / "static" / "css"
APP_CSS = CSS_DIR / "app.css"
PENDING_CSS = CSS_DIR / "app.css"  # the growth rules were merged into it
TEMPLATE_DIR = Path(web.TEMPLATES_DIR)
GROWTH_FILES = [
    TEMPLATE_DIR / "accounts" / "_growth.html",
    TEMPLATE_DIR / "book.html",
    TEMPLATE_DIR / "partials" / "book_results.html",
    PENDING_CSS,
    Path(__file__),
]


class _StubConnector:
    """A connector with fixed accounts and signals, so a test controls the page's data."""

    name = "stub"

    def __init__(self, accounts: list[Account], signals: list[Signal] | None = None) -> None:
        self._accounts = accounts
        self._signals = signals or []

    def list_accounts(self) -> list[Account]:
        return list(self._accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        if account_id is None:
            return list(self._signals)
        return [signal for signal in self._signals if signal.account_id == account_id]


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


def _block(markup: str, pattern: str) -> str:
    match = re.search(pattern, markup, re.S)
    assert match is not None, pattern
    return match.group(0)


def _texts(fragment: str, pattern: str) -> list[str]:
    """The text each match of `pattern` captures, with entities decoded (Jinja escapes quotes)."""
    return [html.unescape(text) for text in re.findall(pattern, fragment)]


def _growth_section(markup: str) -> str:
    return _block(markup, r'<section class="growth"[^>]*>.*?</section>')


def _without_since(markup: str) -> str:
    """The page without its "Since last week" panel, which links accounts but is not a row."""
    return re.sub(r'<section class="since-panel".*?</section>', "", markup, flags=re.S)


def _row_ranks(markup: str) -> list[int]:
    """Priority ranks in page order, from the rank tile of each card or list row."""
    found = re.findall(
        r'rank-tile"><span class="visually-hidden">Rank </span>(\d+)<', _without_since(markup)
    )
    return [int(rank) for rank in found]


def _growth_tags(markup: str) -> list[tuple[str, str]]:
    """(band, words) of each growth tag on the page, in page order."""
    return re.findall(r'growth-tag growth-tag--(\w+)">([^<]+)</span>', markup)


def _checked(markup: str, name: str) -> list[str]:
    """The values of the ticked inputs called `name` (checkboxes and radios)."""
    return [
        tag["value"] or ""
        for tag in _tags(markup, "input")
        if tag.get("name") == name and "checked" in tag
    ]


def _form_pairs(markup: str) -> list[tuple[str, str]]:
    """What the browser sends when the book's form goes as it stands: each ticked box or radio,
    and the search text."""
    pairs: list[tuple[str, str]] = []
    for tag in _tags(markup, "input"):
        name = tag.get("name")
        if not name:
            continue
        if tag.get("type") in ("checkbox", "radio") and "checked" in tag:
            pairs.append((name, tag.get("value") or "on"))
        elif tag.get("type") == "search":
            pairs.append((name, tag.get("value") or ""))
    return pairs


def _growth_order(
    book: list[ScoredAccount],
    radars: dict[str, GrowthRadar],
    keep: Callable[[ScoredAccount], bool] = lambda item: True,
) -> list[int]:
    """The priority ranks (1-based) of the kept accounts, most room to grow first. The sort is
    stable, so accounts with the same score stay in priority order."""
    kept = [(rank, item) for rank, item in enumerate(book, start=1) if keep(item)]
    ordered = sorted(kept, key=lambda pair: -radars[pair[1].account.id].score)
    return [rank for rank, _item in ordered]


def _book_radars(
    book: list[ScoredAccount], funding: dict[str, FundingNews] | None = None
) -> dict[str, GrowthRadar]:
    """Room to grow for the book as the demo connector has it, with no funding news unless given."""
    return growth.book_growth(
        book, service.latest_book_signals(DemoConnector()), funding if funding is not None else {}
    )


def _account(account_id: str) -> Account:
    return Account(
        id=account_id,
        name=f"Account {account_id}",
        arr=100_000.0,
        tier="Growth",
        owner="Owner",
        start_date=None,
        renewal_date=date.today() + timedelta(days=200),
        milestones=Milestones(done=4, total=4),
    )


def _reading(account_id: str, kind: SignalKind, value: float) -> Signal:
    observed = datetime.combine(date.today(), time(9, 0))
    return Signal(
        account_id=account_id, kind=kind, value=value, observed_at=observed, source="test"
    )


def _stub_client(
    monkeypatch: pytest.MonkeyPatch, accounts: list[Account], signals: list[Signal] | None = None
) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: _StubConnector(accounts, signals))
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def book() -> list[ScoredAccount]:
    """The demo book in priority order, read through the same connector the pages use."""
    return service.get_book(DemoConnector(), load_scoring_config(), date.today())


@pytest.fixture
def radars(book: list[ScoredAccount]) -> dict[str, GrowthRadar]:
    return _book_radars(book)


def _expected_tags(
    book: list[ScoredAccount], radars: dict[str, GrowthRadar]
) -> list[tuple[str, str]]:
    """The tags a page shows: Ready to grow and Some room, in book order."""
    tags = []
    for item in book:
        radar = radars[item.account.id]
        if radar.band in (GrowthBand.STRONG, GrowthBand.SOME):
            tags.append((radar.band.value, radar.band.label))
    return tags


# --- The account aside -----------------------------------------------------------------------


def test_a_strong_account_is_ready_to_grow_with_its_three_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _stub_client(
        monkeypatch,
        [_account("acc-1")],
        [_reading("acc-1", SEATS, 96), _reading("acc-1", USAGE, 22), _reading("acc-1", NPS, 9)],
    )
    section = _growth_section(client.get("/accounts/acc-1").text)

    assert '<span class="growth-tag growth-tag--strong">Ready to grow</span>' in section
    # The reasons are listed, so the sentence that names them is not repeated above the list.
    assert _texts(section, r"<p>([^<]+)</p>") == []
    assert _texts(section, r"<li>([^<]+)</li>") == [
        "Seats 96% used",
        "Active users up 22% in 30 days",
        "NPS 9, a promoter",
    ]
    assert 'class="growth-caption"' in section
    assert "From seat use, the usage trend, NPS and funding news on their own website." in section
    assert "growth-held" not in section


def test_a_some_room_account_says_so_with_its_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub_client(
        monkeypatch,
        [_account("acc-1")],
        [_reading("acc-1", SEATS, 90), _reading("acc-1", USAGE, 10)],
    )
    section = _growth_section(client.get("/accounts/acc-1").text)

    assert '<span class="growth-tag growth-tag--some">Some room</span>' in section
    assert _texts(section, r"<p>([^<]+)</p>") == []
    assert _texts(section, r"<li>([^<]+)</li>") == [
        "Seats 90% used",
        "Active users up 10% in 30 days",
    ]


def test_a_weak_account_with_one_reading_says_no_strong_signs_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _stub_client(monkeypatch, [_account("acc-1")], [_reading("acc-1", SEATS, 85)])
    section = _growth_section(client.get("/accounts/acc-1").text)

    assert '<span class="growth-tag growth-tag--none">No signs yet</span>' in section
    assert _texts(section, r"<p>([^<]+)</p>") == []
    assert _texts(section, r"<li>([^<]+)</li>") == ["Seats 85% used"]


def test_an_account_with_no_readings_has_no_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub_client(monkeypatch, [_account("acc-1")])
    section = _growth_section(client.get("/accounts/acc-1").text)

    assert '<span class="growth-tag growth-tag--none">No signs yet</span>' in section
    assert _texts(section, r"<p>([^<]+)</p>") == ["No signs of room to grow yet."]
    assert "growth-reasons" not in section


def test_funding_news_from_the_website_is_one_more_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _stub_client(monkeypatch, [_account("acc-1")], [_reading("acc-1", SEATS, 96)])
    news = FundingNews(
        title="Acme raises Series B to hire support", published=date.today() - timedelta(days=10)
    )
    monkeypatch.setattr(service, "account_funding", lambda account, now=None: news)

    section = _growth_section(client.get("/accounts/acc-1").text)

    # Seats 28 points plus funding 15 is 43: Some room, with the headline as the second reason.
    assert '<span class="growth-tag growth-tag--some">Some room</span>' in section
    assert _texts(section, r"<li>([^<]+)</li>") == [
        "Seats 96% used",
        'Their site announced funding: "Acme raises Series B to hire support"',
    ]


def test_an_account_at_risk_is_held_back_to_one_muted_sentence(client: TestClient) -> None:
    book = service.get_book()
    at_risk = next(item for item in book if item.health is Health.AT_RISK)

    markup = client.get(f"/accounts/{at_risk.account.id}").text
    section = _growth_section(markup)

    assert re.findall(r'<p class="growth-held">([^<]+)</p>', section) == [
        "Held back while this account is At risk. Fix the risk before talking about growth."
    ]
    assert "growth-tag" not in section
    assert "growth-reasons" not in section
    assert "growth-caption" not in section


def test_the_growth_section_sits_in_the_account_aside(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _stub_client(monkeypatch, [_account("acc-1")], [_reading("acc-1", SEATS, 96)])
    markup = client.get("/accounts/acc-1").text

    assert '<h2 class="section-title" id="growth-heading">Room to grow</h2>' in markup
    aside = _block(markup, r'<aside class="account-aside".*?</aside>')
    assert '<section class="growth" aria-labelledby="growth-heading">' in aside


# --- My book: the tags -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/?view=list"])
def test_ready_and_some_accounts_carry_a_tag_on_cards_and_rows(
    client: TestClient,
    book: list[ScoredAccount],
    radars: dict[str, GrowthRadar],
    path: str,
) -> None:
    expected = _expected_tags(book, radars)
    assert expected  # the demo book has accounts with room to grow, so the tags are tested
    assert _growth_tags(client.get(path).text) == expected


def test_the_tag_sits_beside_the_health_badge_on_a_card(client: TestClient) -> None:
    markup = client.get("/").text
    blocks = re.findall(r'<span class="health-status">(.*?)</span>\s*</div>', markup, re.S)
    tagged = [block for block in blocks if "growth-tag" in block]
    assert tagged
    for block in tagged:
        assert block.index('class="health health--') < block.index("growth-tag")


def test_sort_does_not_change_which_accounts_are_tagged(
    client: TestClient, book: list[ScoredAccount], radars: dict[str, GrowthRadar]
) -> None:
    tagged = Counter(_growth_tags(client.get("/?sort=growth").text))
    assert tagged == Counter(_expected_tags(book, radars))


def test_funding_news_changes_the_tags_the_book_shows(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    book: list[ScoredAccount],
) -> None:
    news = FundingNews(title="Funding headline", published=date.today())
    funding = {item.account.id: news for item in book}
    monkeypatch.setattr(service, "book_funding", lambda now=None: funding)

    markup = client.get("/?sort=growth").text

    radars = _book_radars(book, funding)
    assert Counter(_growth_tags(markup)) == Counter(_expected_tags(book, radars))
    assert _row_ranks(markup) == _growth_order(book, radars)


# --- My book: the sort -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/?sort=growth", "/?sort=growth&view=list"])
def test_room_to_grow_orders_by_score_then_priority(
    client: TestClient,
    book: list[ScoredAccount],
    radars: dict[str, GrowthRadar],
    path: str,
) -> None:
    markup = client.get(path).text

    assert _row_ranks(markup) == _growth_order(book, radars)
    scores = [radars[book[rank - 1].account.id].score for rank in _row_ranks(markup)]
    assert scores == sorted(scores, reverse=True)


def test_the_default_order_is_still_priority(client: TestClient) -> None:
    for path in ["/", "/?sort=priority", "/?view=list"]:
        assert _row_ranks(client.get(path).text) == list(range(1, 41)), path


@pytest.mark.parametrize("value", ["bogus", "", "GROWTH", "growth-now"])
def test_an_unknown_sort_is_priority(client: TestClient, value: str) -> None:
    markup = client.get("/", params={"sort": value}).text

    assert _row_ranks(markup) == list(range(1, 41))
    assert _checked(markup, "sort") == ["priority"]


def test_the_sort_switch_shows_which_order_is_on(client: TestClient) -> None:
    assert _checked(client.get("/").text, "sort") == ["priority"]
    assert _checked(client.get("/?sort=growth").text, "sort") == ["growth"]
    assert _checked(client.get("/?sort=priority&view=list").text, "sort") == ["priority"]


def test_the_sort_switch_is_part_of_the_book_form(client: TestClient) -> None:
    markup = client.get("/").text
    form_start = markup.index('<form id="book-filter"')
    form_end = markup.index("</form>", form_start)
    for radio in ('name="sort" id="sort-priority"', 'name="sort" id="sort-growth"'):
        assert form_start < markup.index(radio) < form_end
    assert '<legend class="visually-hidden">Sort by</legend>' in markup
    assert '<label for="sort-growth">Room to grow</label>' in markup
    assert '<label for="sort-priority">Priority</label>' in markup


def test_the_sort_switch_keeps_the_other_filters_and_search(
    client: TestClient, book: list[ScoredAccount], radars: dict[str, GrowthRadar]
) -> None:
    page = client.get("/?motion=renewal&view=list&q=brindle").text
    pairs = [(name, value) for name, value in _form_pairs(page) if name != "sort"]
    pairs.append(("sort", "growth"))

    sent = client.get("/", params=pairs).text

    assert _checked(sent, "motion") == ["renewal"]
    assert _checked(sent, "view") == ["list"]
    assert _checked(sent, "sort") == ["growth"]
    assert 'value="brindle"' in sent
    expected = _growth_order(
        book,
        radars,
        lambda item: item.motion is Motion.RENEWAL and "brindle" in item.account.name.lower(),
    )
    assert _row_ranks(sent) == expected


@pytest.mark.parametrize(
    ("query", "keep"),
    [
        ("motion=renewal", lambda item: item.motion is Motion.RENEWAL),
        ("health=healthy", lambda item: item.health is Health.HEALTHY),
        ("q=brindle", lambda item: "brindle" in item.account.name.lower()),
    ],
)
def test_sort_combines_with_filters_and_search(
    client: TestClient,
    book: list[ScoredAccount],
    radars: dict[str, GrowthRadar],
    query: str,
    keep: Callable[[ScoredAccount], bool],
) -> None:
    markup = client.get(f"/?{query}&sort=growth").text

    assert _row_ranks(markup) == _growth_order(book, radars, keep)


def test_the_sort_heading_and_caption_say_room_to_grow(client: TestClient) -> None:
    cards = client.get("/?sort=growth").text
    assert '<h2 class="visually-hidden">Accounts ranked by room to grow</h2>' in cards
    table = client.get("/?sort=growth&view=list").text
    assert '<caption class="visually-hidden">Accounts ranked by room to grow</caption>' in table


def test_the_default_heading_and_caption_still_say_priority(client: TestClient) -> None:
    assert '<h2 class="visually-hidden">Accounts ranked by priority</h2>' in client.get("/").text
    table = client.get("/?view=list").text
    assert '<caption class="visually-hidden">Accounts ranked by priority</caption>' in table


def test_an_empty_result_under_the_sort_keeps_its_copy(client: TestClient) -> None:
    response = client.get("/?q=zzz&sort=growth")
    assert response.status_code == 200
    assert 'No accounts match "zzz".' in response.text
    assert 'class="growth-tag' not in response.text
    assert '<a href="/">Clear search</a>' in response.text


def test_an_htmx_refresh_honours_the_sort(
    client: TestClient, book: list[ScoredAccount], radars: dict[str, GrowthRadar]
) -> None:
    response = client.get("/?sort=growth", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="results"' in response.text
    assert _row_ranks(response.text) == _growth_order(book, radars)
    assert '<span id="result-count" hx-swap-oob="innerHTML">Showing 40 accounts</span>' in (
        response.text
    )
    assert "HX-Request" in response.headers["vary"]


def test_an_htmx_refresh_with_an_unknown_sort_is_priority(client: TestClient) -> None:
    response = client.get("/?sort=bogus", headers={"HX-Request": "true"})
    assert _row_ranks(response.text) == list(range(1, 41))


def test_the_history_restore_gets_the_sorted_full_page(
    client: TestClient, book: list[ScoredAccount], radars: dict[str, GrowthRadar]
) -> None:
    headers = {"HX-Request": "true", "HX-History-Restore-Request": "true"}
    response = client.get("/?sort=growth", headers=headers)
    assert "<html" in response.text
    assert _row_ranks(response.text) == _growth_order(book, radars)


# --- Shape and rules -------------------------------------------------------------------------


def test_the_growth_markup_has_no_inline_style_or_script(client: TestClient) -> None:
    pages = [
        client.get("/?sort=growth").text,
        client.get("/?sort=growth&view=list").text,
        client.get("/accounts/acc-002").text,
    ]
    for markup in pages:
        assert not re.search(r"\sstyle=", markup)
        assert not re.search(r"\son[a-z]+=", markup, re.I)
        assert not re.search(r"<script(?![^>]*\bsrc=)", markup)


def test_the_growth_copy_has_no_arrows(client: TestClient) -> None:
    section = _growth_section(client.get("/accounts/acc-002").text)
    assert "→" not in section
    assert "→" not in client.get("/?sort=growth").text


@pytest.mark.parametrize("path", GROWTH_FILES, ids=lambda path: path.name)
def test_the_files_use_lf_line_endings(path: Path) -> None:
    assert b"\r" not in path.read_bytes()


def test_the_growth_tag_and_section_have_their_rules_in_the_pending_css() -> None:
    css = PENDING_CSS.read_text(encoding="utf-8")
    for selector in (".growth {", ".growth-tag {", ".growth-tag--strong", ".growth-tag--some"):
        assert selector in css
    assert ".sort-label" in css
