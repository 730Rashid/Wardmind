"""The account page's insight blocks, drawn from core/insights.py: how sure the score is, reading
ages in the breakdown, what would it take, the go-live forecast and unused seats.

Most cases use a stub connector with readings dated from today, so the ages are exact. The demo
book is used where the question is "what does a real account look like".
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import insights, scoring, service
from wardmind.core.briefs import SIGNAL_LABELS
from wardmind.core.config import load_scoring_config
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.insights import Confidence, Fix, GoLiveStatus, PathDown
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
from wardmind.web import formatting

USAGE = SignalKind.USAGE_TREND_30D
LOGIN = SignalKind.DAYS_SINCE_LAST_LOGIN
TICKETS = SignalKind.OPEN_TICKETS_SEV1
SEATS = SignalKind.SEAT_UTILISATION
TEMPLATES = Path(web.TEMPLATES_DIR)


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


def _block(markup: str, pattern: str) -> str:
    match = re.search(pattern, markup, re.S)
    assert match is not None, pattern
    return match.group(1)


def _plain(fragment: str) -> str:
    """Text of a fragment: tags removed, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _account(
    account_id: str = "acc-1",
    *,
    arr: float = 100_000.0,
    start_days_ago: int | None = None,
    renewal_in_days: int = 200,
    milestones: Milestones | None = None,
) -> Account:
    today = date.today()
    return Account(
        id=account_id,
        name=f"Account {account_id}",
        arr=arr,
        tier="Growth",
        owner="Owner",
        start_date=None if start_days_ago is None else today - timedelta(days=start_days_ago),
        renewal_date=today + timedelta(days=renewal_in_days),
        milestones=milestones or Milestones(done=4, total=4),
    )


def _reading(account_id: str, kind: SignalKind, value: float, days_ago: int = 0) -> Signal:
    observed = datetime.combine(date.today() - timedelta(days=days_ago), time(9, 0))
    return Signal(
        account_id=account_id, kind=kind, value=value, observed_at=observed, source="test"
    )


def _scored(accounts: list[Account], signals: list[Signal], account_id: str) -> ScoredAccount:
    book = scoring.score_book(accounts, signals, load_scoring_config(), date.today())
    return next(item for item in book if item.account.id == account_id)


def _latest(signals: list[Signal], account_id: str) -> dict[SignalKind, Signal]:
    return scoring.latest_signals(signals).get(account_id, {})


@pytest.fixture
def demo_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _stub_client(
    monkeypatch: pytest.MonkeyPatch, accounts: list[Account], signals: list[Signal] | None = None
) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: _StubConnector(accounts, signals))
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _demo_item(predicate: Any) -> ScoredAccount:
    found = [item for item in service.get_book() if predicate(item)]
    assert found, "the demo book has no account of that kind"
    return found[0]


def _demo_path(item: ScoredAccount) -> PathDown | None:
    latest = _latest(DemoConnector().list_signals(item.account.id), item.account.id)
    return insights.path_down(item, latest, load_scoring_config(), date.today())


def _and_join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


# --- What would it take ----------------------------------------------------------------------


def test_an_at_risk_account_shows_what_would_it_take_with_the_path_summary(
    demo_client: TestClient,
):
    item = _demo_item(lambda found: found.health is Health.AT_RISK)
    path = _demo_path(item)
    assert path is not None and path.reachable

    response = demo_client.get(f"/accounts/{item.account.id}")
    assert response.status_code == 200
    markup = response.text
    score = _block(markup, r'<section class="account-section score"[^>]*>(.*?)</section>')
    block = _block(score, r'<div class="path-down">(.*?)</div>')

    assert _plain(_block(block, r'<h3 class="path-title"[^>]*>(.*?)</h3>')) == "What would it take"
    expected = path.summary
    if path.together:
        expected = (
            f"No single fix is enough. These {len(path.together)} together would drop it to "
            f"{path.target.label}:"
        )
    assert _plain(_block(block, r'<p class="path-summary">(.*?)</p>')) == expected
    assert score.index('class="risk-sentence"') < score.index('class="path-down"')


def test_a_single_fix_has_no_other_ways_list(demo_client: TestClient):
    item = _demo_item(
        lambda found: (
            found.health is Health.AT_RISK
            and _demo_path(found) is not None
            and len(_demo_path(found).single) == 1
        )
    )
    path = _demo_path(item)
    assert path is not None

    block = _block(
        demo_client.get(f"/accounts/{item.account.id}").text,
        r'<div class="path-down">(.*?)</div>',
    )
    assert _plain(_block(block, r'<p class="path-summary">(.*?)</p>')) == path.summary
    assert "Other ways to get there:" not in block


def test_together_fixes_are_listed_each_with_its_reading(demo_client: TestClient):
    item = _demo_item(
        lambda found: (
            found.health is Health.AT_RISK
            and _demo_path(found) is not None
            and not _demo_path(found).single
            and len(_demo_path(found).together) > 1
        )
    )
    path = _demo_path(item)
    assert path is not None

    block = _block(
        demo_client.get(f"/accounts/{item.account.id}").text,
        r'<div class="path-down">(.*?)</div>',
    )
    assert _plain(_block(block, r'<p class="path-summary">(.*?)</p>')) == (
        f"No single fix is enough. These {len(path.together)} together would drop it to "
        f"{path.target.label}:"
    )
    rows = re.findall(r'<ul class="journal-list path-list">(.*?)</ul>', block, re.S)
    assert len(rows) == 1
    items = [_plain(row) for row in re.findall(r"<li[^>]*>(.*?)</li>", rows[0], re.S)]
    assert items == [f"{fix.goal_text}. Now: {fix.now_text}." for fix in path.together]
    # With no single fix, "not enough on its own" would only repeat the opening sentence.
    assert 'class="path-muted"' not in block
    assert _plain(_block(block, r'<p class="path-note">(.*?)</p>')) == (
        "This is the score's arithmetic. Fixing the cause is what keeps it there."
    )


def test_other_ways_and_the_not_enough_line_are_drawn_from_the_path(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    fixes = [
        Fix(
            kind=USAGE,
            label=SIGNAL_LABELS[USAGE],
            now_text="Active users down 38% in 30 days",
            goal_text="Get active users back to flat or growing",
            target_value=0,
            points_off=12.0,
        ),
        Fix(
            kind=LOGIN,
            label=SIGNAL_LABELS[LOGIN],
            now_text="No one has logged in for 45 days",
            goal_text="Get someone to log in",
            target_value=0,
            points_off=9.0,
        ),
        Fix(
            kind=TICKETS,
            label=SIGNAL_LABELS[TICKETS],
            now_text="1 open severity-1 ticket",
            goal_text="Close the severity-1 ticket",
            target_value=0,
            points_off=6.0,
        ),
    ]
    fake = PathDown(
        current=Health.AT_RISK,
        target=Health.WATCH,
        risk=60.0,
        threshold=45,
        points_needed=15.1,
        single=fixes,
        together=[],
        not_enough=["Champion", "NPS", "Billing"],
        reachable=True,
        summary="Get active users back to flat or growing and this drops to Watch.",
    )
    monkeypatch.setattr(insights, "path_down", lambda *args, **kwargs: fake)
    item = _demo_item(lambda found: found.health is Health.AT_RISK)

    block = _block(
        demo_client.get(f"/accounts/{item.account.id}").text,
        r'<div class="path-down">(.*?)</div>',
    )
    assert _plain(_block(block, r'<p class="path-label">(.*?)</p>')) == ("Other ways to get there:")
    items = re.findall(r'<ul class="journal-list path-list">(.*?)</ul>', block, re.S)
    assert len(items) == 1
    assert [_plain(row) for row in re.findall(r"<li[^>]*>(.*?)</li>", items[0], re.S)] == [
        "Get someone to log in. Now: No one has logged in for 45 days.",
        "Close the severity-1 ticket. Now: 1 open severity-1 ticket.",
    ]
    assert _plain(_block(block, r'<p class="path-muted">(.*?)</p>')) == (
        "Not enough on its own: Champion, NPS and Billing."
    )


def test_a_healthy_account_has_no_what_would_it_take(demo_client: TestClient):
    item = _demo_item(lambda found: found.health is Health.HEALTHY)
    markup = demo_client.get(f"/accounts/{item.account.id}").text
    assert "What would it take" not in markup
    assert 'class="path-down"' not in markup


# --- How sure this score is ------------------------------------------------------------------


def test_a_full_data_account_says_so_under_the_score_heading(demo_client: TestClient):
    item = _demo_item(lambda found: found.health is Health.HEALTHY)
    confidence = insights.score_confidence(
        item, _latest(DemoConnector().list_signals(item.account.id), item.account.id), date.today()
    )
    assert confidence.level is Confidence.FULL

    markup = demo_client.get(f"/accounts/{item.account.id}").text
    assert re.search(r'id="score-heading">[^<]*</h2>\s*<p class="confidence">', markup)
    line = _block(markup, r'<p class="confidence">(.*?)</p>')
    assert _plain(line) == f"Full data {confidence.sentence}"
    assert "Healthy may only mean data is missing." not in line


def test_a_blind_spot_is_healthy_on_thin_data_and_says_so(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    today_accounts = [_account("acc-blind")]
    readings = [_reading("acc-blind", USAGE, 4, days_ago=3), _reading("acc-blind", LOGIN, 2)]
    item = _scored(today_accounts, readings, "acc-blind")
    assert item.health is Health.HEALTHY
    confidence = insights.score_confidence(item, _latest(readings, "acc-blind"), date.today())
    assert confidence.level is Confidence.THIN and confidence.blind_spot

    client = _stub_client(monkeypatch, today_accounts, readings)
    markup = client.get("/accounts/acc-blind").text
    line = _block(markup, r'<p class="confidence[^"]*">(.*?)</p>')
    assert _plain(line) == (
        f"Blind spot {confidence.sentence} Healthy may only mean data is missing."
    )
    assert "What would it take" not in markup


@pytest.mark.parametrize(
    ("readings", "label"),
    [
        (
            [_reading("acc-thin", TICKETS, 3)],
            "Thin data",
        ),
        (
            [
                _reading("acc-thin", TICKETS, 3),
                _reading("acc-thin", LOGIN, 60),
                _reading("acc-thin", USAGE, -40),
                _reading("acc-thin", SEATS, 50),
            ],
            "Partial data",
        ),
    ],
)
def test_thin_and_partial_data_on_an_account_that_is_not_healthy(
    monkeypatch: pytest.MonkeyPatch, readings: list[Signal], label: str
):
    accounts = [_account("acc-thin")]
    item = _scored(accounts, readings, "acc-thin")
    assert item.health is not Health.HEALTHY
    confidence = insights.score_confidence(item, _latest(readings, "acc-thin"), date.today())
    assert confidence.level.label == label and not confidence.blind_spot

    client = _stub_client(monkeypatch, accounts, readings)
    line = _block(client.get("/accounts/acc-thin").text, r'<p class="confidence[^"]*">(.*?)</p>')
    assert _plain(line) == f"{label} {confidence.sentence}"
    assert "Healthy may only mean data is missing." not in line


def test_a_stale_reading_is_out_of_date_under_its_row_and_in_the_line(
    monkeypatch: pytest.MonkeyPatch,
):
    accounts = [_account("acc-stale")]
    readings = [
        _reading("acc-stale", USAGE, -10, days_ago=45),
        _reading("acc-stale", LOGIN, 1, days_ago=1),
        _reading("acc-stale", TICKETS, 0, days_ago=2),
    ]
    confidence = insights.score_confidence(
        _scored(accounts, readings, "acc-stale"), _latest(readings, "acc-stale"), date.today()
    )
    assert confidence.stale == [SIGNAL_LABELS[USAGE]]

    client = _stub_client(monkeypatch, accounts, readings)
    markup = client.get("/accounts/acc-stale").text
    stale = _block(markup, r'<span class="reading-age reading-age--stale">(.*?)</span>')
    assert _plain(stale) == "Out of date: read 45 days ago"
    assert f"Out of date: {SIGNAL_LABELS[USAGE]}." in _plain(
        _block(markup, r'<p class="confidence[^"]*">(.*?)</p>')
    )


# --- Reading ages in the breakdown -----------------------------------------------------------


def test_each_breakdown_row_says_how_old_its_reading_is(monkeypatch: pytest.MonkeyPatch):
    accounts = [_account("acc-ages")]
    readings = [
        _reading("acc-ages", USAGE, 0, days_ago=0),
        _reading("acc-ages", LOGIN, 0, days_ago=1),
        _reading("acc-ages", TICKETS, 0, days_ago=5),
    ]
    client = _stub_client(monkeypatch, accounts, readings)
    markup = client.get("/accounts/acc-ages").text
    table = _block(markup, r'<table class="breakdown">(.*?)</table>')
    cells = {
        label: _plain(cell)
        for label, cell in re.findall(
            r'<th scope="row" class="cell-signal">([^<]+)</th>\s*'
            r'<td class="cell-reading">(.*?)</td>',
            table,
            re.S,
        )
    }

    assert len(cells) == 8
    assert cells[SIGNAL_LABELS[USAGE]].endswith("Read today")
    assert cells[SIGNAL_LABELS[LOGIN]].endswith("Read yesterday")
    assert cells[SIGNAL_LABELS[TICKETS]].endswith("Read 5 days ago")
    assert "Read" not in cells[SIGNAL_LABELS[SEATS]]
    assert cells[SIGNAL_LABELS[SEATS]] == "No reading"
    assert len(re.findall(r'<span class="reading-age', table)) == 3


# --- Go-live forecast ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start_days_ago", "done", "status"),
    [
        (20, 2, GoLiveStatus.ON_TRACK),
        (80, 1, GoLiveStatus.LATE),
        (20, 0, GoLiveStatus.NO_PACE),
        (5, 1, GoLiveStatus.TOO_EARLY),
    ],
)
def test_an_onboarding_account_has_a_go_live_forecast(
    monkeypatch: pytest.MonkeyPatch,
    start_days_ago: int,
    done: int,
    status: GoLiveStatus,
):
    today = date.today()
    account = _account(
        "acc-onb",
        start_days_ago=start_days_ago,
        milestones=Milestones(done=done, total=6),
    )
    forecast = insights.golive_forecast(account, today, load_scoring_config().motion)
    assert forecast is not None and forecast.status is status
    assert _scored([account], [], "acc-onb").motion is Motion.ONBOARDING

    client = _stub_client(monkeypatch, [account])
    markup = client.get("/accounts/acc-onb").text
    section = _block(markup, r'<section class="account-section golive"[^>]*>(.*?)</section>')
    assert _plain(_block(section, r'<h2 class="section-title"[^>]*>(.*?)</h2>')) == (
        "Go-live forecast"
    )
    assert _plain(_block(section, r'<p class="golive-status">(.*?)</p>')) == status.label
    assert _plain(_block(section, r'<p class="golive-sentence">(.*?)</p>')) == forecast.sentence
    assert "Assumes the remaining milestones take as long" in section
    if forecast.projected_date is None:
        assert "Projected finish" not in section
    else:
        assert _plain(_block(section, r'<p class="golive-projected">(.*?)</p>')) == (
            f"Projected finish: {formatting.day_label(forecast.projected_date, today)}"
        )


def test_a_completed_onboarding_has_no_go_live_section(monkeypatch: pytest.MonkeyPatch):
    account = _account("acc-done", start_days_ago=20, milestones=Milestones(done=6, total=6))
    client = _stub_client(monkeypatch, [account])
    markup = client.get("/accounts/acc-done").text
    assert 'class="account-section golive"' not in markup
    assert "Go-live forecast" not in markup


def test_a_demo_onboarding_account_has_the_go_live_section(demo_client: TestClient):
    item = _demo_item(lambda found: found.motion is Motion.ONBOARDING)
    forecast = insights.golive_forecast(item.account, date.today(), load_scoring_config().motion)
    assert forecast is not None
    markup = demo_client.get(f"/accounts/{item.account.id}").text
    assert 'class="account-section golive"' in markup
    assert markup.index('class="account-section golive"') < markup.index(
        'class="account-section history"'
    )


# --- Unused seats ----------------------------------------------------------------------------


def test_low_seat_use_shows_the_unused_seats_box_with_the_money(monkeypatch: pytest.MonkeyPatch):
    accounts = [_account("acc-seats", arr=100_000.0, renewal_in_days=30)]
    readings = [_reading("acc-seats", SEATS, 50)]
    shelfware = insights.shelfware(
        accounts[0], _latest(readings, "acc-seats"), date.today(), load_scoring_config().motion
    )
    assert shelfware is not None and shelfware.unused_percent == 50

    client = _stub_client(monkeypatch, accounts, readings)
    markup = client.get("/accounts/acc-seats").text
    aside = _block(markup, r'<aside class="account-aside"[^>]*>(.*?)</aside>')
    box = _block(aside, r'<section class="shelfware"[^>]*>(.*?)</section>')
    assert _plain(_block(box, r'<h2 class="section-title"[^>]*>(.*?)</h2>')) == "Unused seats"
    assert _plain(_block(box, r'<p class="shelfware-lead">(.*?)</p>')) == (
        f"About {web.money(shelfware.unused_arr)} of this contract pays for seats no one uses "
        f"({shelfware.unused_percent}% of seats)."
    )
    assert "About $50,000 of this contract" in _plain(box)
    assert shelfware.advice in _plain(box)
    assert "Assumes a flat price per seat." in box
    # Placed after the facts list and before the renewal outcome.
    assert aside.index('<dl class="facts-list">') < aside.index('class="shelfware"')
    assert aside.index('class="shelfware"') < aside.index('class="outcome-box"')


def test_high_seat_use_has_no_unused_seats_box(monkeypatch: pytest.MonkeyPatch):
    accounts = [_account("acc-full", arr=100_000.0)]
    client = _stub_client(monkeypatch, accounts, [_reading("acc-full", SEATS, 90)])
    markup = client.get("/accounts/acc-full").text
    assert 'class="shelfware"' not in markup
    assert "Unused seats" not in markup


# --- Other pages and the templates -----------------------------------------------------------


def test_the_onboarding_brief_table_has_the_same_reading_ages(demo_client: TestClient):
    item = _demo_item(lambda found: found.motion is Motion.ONBOARDING)
    brief = demo_client.get(f"/onboarding/{item.account.id}").text
    # Row-for-row equality with the account page is test_onboarding_pages' job; here, the ages.
    assert "reading-age" in _block(brief, r'<table class="breakdown">(.*?)</table>')


def test_the_new_templates_have_no_style_attributes_or_scripts():
    names = [
        "account.html",
        "partials/breakdown_table.html",
        "accounts/_confidence.html",
        "accounts/_path_down.html",
        "accounts/_golive.html",
        "accounts/_shelfware.html",
    ]
    for name in names:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=", source), name
        assert "<script" not in source.lower(), name


def test_the_rendered_insight_blocks_have_no_style_attributes(demo_client: TestClient):
    item = _demo_item(lambda found: found.health is Health.AT_RISK)
    markup = demo_client.get(f"/accounts/{item.account.id}").text
    assert not re.search(r"\sstyle\s*=", markup)
    assert not re.search(r"<script(?![^>]*\ssrc=)", markup, flags=re.IGNORECASE)


def test_an_onboarding_account_has_no_unused_seats_box(demo_client: TestClient):
    # Seats are still being rolled out during onboarding, so unused ones aren't a downsell risk.
    item = _demo_item(lambda found: found.motion is Motion.ONBOARDING)
    assert "Unused seats" not in demo_client.get(f"/accounts/{item.account.id}").text
