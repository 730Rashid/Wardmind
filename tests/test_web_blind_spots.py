"""Blind spots on My book: an account that looks Healthy only because its readings are missing or
out of date. Covers the Data group and its count, the data=blind filter (alone, with the other
filters, and in the htmx partial), the tag on cards and rows, and unknown values."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from wardmind.core import service
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Account, Milestones, Signal, SignalKind
from wardmind.web import app as web

ASTER = "aster"  # Healthy, two readings, Renewal motion: thin, so a blind spot
BASIL = "basil"  # Healthy, every reading fresh: not a blind spot
COBALT = "cobalt"  # Healthy, every reading 200 days old: a blind spot
DUNMORE = "dunmore"  # At risk, two readings: thin, but not Healthy, so not a blind spot
FENWICK = "fenwick"  # Watch, one reading: thin, but not Healthy, so not a blind spot
BLIND = {ASTER, COBALT}


class _Connector:
    """A connector with fixed accounts and readings, so each test controls the book."""

    name = "stub"

    def __init__(self, accounts: list[Account], signals: list[Signal]) -> None:
        self._accounts = accounts
        self._signals = signals

    def list_accounts(self) -> list[Account]:
        return list(self._accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        if account_id is None:
            return list(self._signals)
        return [signal for signal in self._signals if signal.account_id == account_id]


def _account(account_id: str, name: str, arr: float, renewal_in: int) -> Account:
    return Account(
        id=account_id,
        name=name,
        arr=arr,
        tier="Growth",
        owner="Dana Whitfield",
        renewal_date=date.today() + timedelta(days=renewal_in),
        milestones=Milestones(done=0, total=0),
    )


def _reading(account_id: str, kind: SignalKind, value: float, age_days: int) -> Signal:
    taken = datetime.combine(date.today() - timedelta(days=age_days), time(9, 0))
    return Signal(account_id=account_id, kind=kind, value=value, observed_at=taken)


def _steady(account_id: str, age_days: int) -> list[Signal]:
    """All eight kinds, every one benign, so the account is Healthy on a full set of readings."""
    values = {
        SignalKind.USAGE_TREND_30D: 10,
        SignalKind.DAYS_SINCE_LAST_LOGIN: 2,
        SignalKind.OPEN_TICKETS_SEV1: 0,
        SignalKind.NPS_LATEST: 9,
        SignalKind.CHAMPION_LEFT: 0,
        SignalKind.SEAT_UTILISATION: 80,
        SignalKind.BILLING_OVERDUE_DAYS: 0,
        SignalKind.ONBOARDING_OVERDUE_MILESTONES: 0,
    }
    return [_reading(account_id, kind, value, age_days) for kind, value in values.items()]


def _shown(markup: str) -> list[tuple[str, str]]:
    """(account id, markup) for each card or list row on the page, in page order. The "Since last
    week" panel links accounts too, but it is not a card or a row, so it is left out."""
    body = re.sub(r'<section class="since-panel".*?</section>', "", markup, flags=re.S)
    blocks = re.findall(
        r'<article class="account-card".*?</article>|<tr role="row">.*?</tr>', body, re.S
    )
    shown: list[tuple[str, str]] = []
    for block in blocks:
        link = re.search(r'class="account-link" href="/accounts/([^"]+)"', block)
        if link is not None:  # the list view's header row has no account link
            shown.append((link.group(1), block))
    return shown


def _ids(markup: str) -> list[str]:
    return [account_id for account_id, _ in _shown(markup)]


def _tagged(markup: str) -> set[str]:
    return {account_id for account_id, block in _shown(markup) if "Blind spot" in block}


def _data_group(markup: str) -> str | None:
    """The markup inside the Data fieldset, or None when the group is not on the page."""
    match = re.search(r"<legend>Data</legend>(.*?)</fieldset>", markup, re.S)
    return None if match is None else match.group(1)


def _legends(markup: str) -> list[str]:
    return re.findall(r"<legend>([^<]+)</legend>", markup)


def _counts(markup: str) -> list[str]:
    return re.findall(r'filter-count">(\d+)<', markup)


def _ticked(markup: str, name: str) -> list[str]:
    """The values of the ticked inputs called `name` (checkboxes and radios)."""
    values: list[str] = []
    for tag in re.findall(r"<input\b[^>]*>", markup):
        value = re.search(r'value="([^"]*)"', tag)
        if f'name="{name}"' in tag and " checked" in tag and value is not None:
            values.append(value.group(1))
    return values


@pytest.fixture
def book_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    accounts = [
        _account(ASTER, "Aster Lane", 120_000, renewal_in=40),  # Renewal motion
        _account(BASIL, "Basil Works", 90_000, renewal_in=300),
        _account(COBALT, "Cobalt Yard", 60_000, renewal_in=300),
        _account(DUNMORE, "Dunmore Clinic", 150_000, renewal_in=300),
        _account(FENWICK, "Fenwick Watch", 40_000, renewal_in=300),
    ]
    signals = [
        _reading(ASTER, SignalKind.USAGE_TREND_30D, 5, age_days=3),
        _reading(ASTER, SignalKind.DAYS_SINCE_LAST_LOGIN, 1, age_days=3),
        *_steady(BASIL, age_days=1),
        *_steady(COBALT, age_days=200),
        _reading(DUNMORE, SignalKind.CHAMPION_LEFT, 1, age_days=2),  # 25 points
        _reading(DUNMORE, SignalKind.OPEN_TICKETS_SEV1, 3, age_days=2),  # 20 points: at risk
        _reading(FENWICK, SignalKind.USAGE_TREND_30D, -40, age_days=2),  # 20 points: watch
    ]
    monkeypatch.setattr(service, "get_connector", lambda: _Connector(accounts, signals))
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def demo_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def test_the_book_fixture_has_the_blind_spots_we_expect(book_client: TestClient) -> None:
    book = service.get_book()
    healths = {item.account.id: item.health.value for item in book}
    assert healths == {
        ASTER: "healthy",
        BASIL: "healthy",
        COBALT: "healthy",
        DUNMORE: "at_risk",
        FENWICK: "watch",
    }
    markup = book_client.get("/").text
    assert _ids(markup) == [item.account.id for item in book]


def test_demo_data_has_no_blind_spots_so_no_data_group(demo_client: TestClient) -> None:
    markup = demo_client.get("/").text
    assert _data_group(markup) is None
    assert 'name="data"' not in markup
    assert _legends(markup) == ["Motion", "Health"]
    assert _tagged(markup) == set()


def test_blind_filter_on_demo_data_shows_the_empty_copy(demo_client: TestClient) -> None:
    markup = demo_client.get("/?data=blind").text
    assert _ids(markup) == []
    assert "No accounts match these filters." in markup
    assert "Clear filters" in markup


def test_data_group_sits_after_health_and_shows_the_book_count(book_client: TestClient) -> None:
    markup = book_client.get("/").text
    assert _legends(markup) == ["Motion", "Health", "Data"]
    group = _data_group(markup)
    assert group is not None
    assert _counts(group) == ["2"]
    assert 'value="blind"' in group
    assert "Blind spots" in group
    assert _ticked(markup, "data") == []


def test_counts_come_from_the_whole_book_whatever_is_ticked(book_client: TestClient) -> None:
    # Motion (churn, renewal, onboarding), health (at risk, watch, healthy), then Data.
    expected = ["4", "1", "0", "1", "1", "3", "2"]
    for path in ["/", "/?data=blind", "/?motion=churn&data=blind", "/?health=healthy"]:
        assert _counts(book_client.get(path).text) == expected, path


def test_the_blind_filter_keeps_exactly_the_blind_spots(book_client: TestClient) -> None:
    book = service.get_book()
    expected = [item.account.id for item in book if item.account.id in BLIND]
    assert expected == [ASTER, COBALT]

    markup = book_client.get("/?data=blind").text
    assert _ids(markup) == expected
    assert _ticked(markup, "data") == ["blind"]
    assert "Clear filters" in markup
    assert "Showing 2 accounts" in markup


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("data=blind&health=healthy", [ASTER, COBALT]),
        ("data=blind&health=at_risk", []),
        ("data=blind&health=watch&health=healthy", [ASTER, COBALT]),
        ("data=blind&motion=renewal", [ASTER]),
        ("data=blind&motion=churn", [COBALT]),
        ("data=blind&motion=churn&health=healthy", [COBALT]),
        ("data=blind&q=cobalt", [COBALT]),
        ("data=blind&q=zzz", []),
        ("data=blind&view=list&health=healthy", [ASTER, COBALT]),
    ],
)
def test_the_blind_filter_combines_with_the_other_filters(
    book_client: TestClient, query: str, expected: list[str]
) -> None:
    markup = book_client.get(f"/?{query}").text
    assert _ids(markup) == expected
    if not expected:
        assert "No accounts match" in markup


def test_the_tag_marks_blind_spot_cards_and_only_those(book_client: TestClient) -> None:
    markup = book_client.get("/").text
    assert _tagged(markup) == BLIND
    for account_id, block in _shown(markup):
        if account_id not in BLIND:
            assert "Blind spot" not in block
            continue
        assert 'class="health-status"' in block
        assert "health--healthy" in block
        # The tag comes after the health badge it sits beside, and its explanation is hidden text.
        assert block.index("health--healthy") < block.index("Blind spot")
        assert (
            '<span class="visually-hidden"> — healthy may only mean data is missing or out of date'
            in block
        )


def test_the_tag_marks_blind_spot_rows_in_the_list_view(book_client: TestClient) -> None:
    markup = book_client.get("/?view=list").text
    assert _ids(markup) == [item.account.id for item in service.get_book()]
    assert _tagged(markup) == BLIND
    assert _ticked(markup, "view") == ["list"]
    for account_id, block in _shown(markup):
        if account_id in BLIND:
            assert 'class="health-status"' in block
            assert "healthy may only mean data is missing" in block


def test_htmx_refresh_returns_only_the_blind_results(book_client: TestClient) -> None:
    response = book_client.get("/?data=blind", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    assert "<html" not in text
    assert "<legend>" not in text
    assert _ids(text) == [ASTER, COBALT]
    assert _tagged(text) == BLIND
    assert re.search(
        r'<span id="result-count" hx-swap-oob="innerHTML">Showing 2 accounts</span>', text
    )
    assert "HX-Request" in response.headers["vary"]


def test_the_filter_form_pushes_the_url_and_the_checkbox_refreshes_the_results(
    book_client: TestClient,
) -> None:
    markup = book_client.get("/").text
    form = re.search(r'<form id="book-filter"[^>]*>', markup)
    assert form is not None
    assert 'method="get"' in form.group(0) and 'action="/"' in form.group(0)
    assert 'hx-get="/"' in form.group(0) and 'hx-push-url="true"' in form.group(0)
    assert "change from:.filter-input" in form.group(0)
    box = re.search(r'<input[^>]*name="data"[^>]*>', markup)
    assert box is not None
    assert "filter-input" in box.group(0)


def test_a_ticked_blind_box_comes_back_ticked_without_javascript(
    book_client: TestClient,
) -> None:
    # The no-JS form submits data=blind; the reloaded page must keep the box ticked.
    markup = book_client.get("/?data=blind").text
    assert _ticked(markup, "data") == ["blind"]
    assert _counts(_data_group(markup) or "") == ["2"]


@pytest.mark.parametrize("query", ["data=stale", "data=BLIND", "data=", "data=%3Cscript%3E"])
def test_an_unknown_data_value_is_ignored(book_client: TestClient, query: str) -> None:
    book = service.get_book()
    markup = book_client.get(f"/?{query}").text
    assert _ids(markup) == [item.account.id for item in book]
    assert _ticked(markup, "data") == []
    assert "Clear filters" not in markup
    assert _counts(_data_group(markup) or "") == ["2"]


def test_unknown_data_values_are_dropped_but_blind_still_applies(book_client: TestClient) -> None:
    markup = book_client.get("/?data=stale&data=blind&data=nope").text
    assert _ids(markup) == [ASTER, COBALT]
    assert _ticked(markup, "data") == ["blind"]
