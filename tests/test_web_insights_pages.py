"""The go-live forecast on the onboarding pages (the picker's Forecast column and the brief's
go-live block) and the unused seats on upcoming renewals in the weekly brief. The expected values
come from the insights functions and the demo book, computed here, so they follow the data."""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import insights, service
from wardmind.core.config import MotionWindows, load_scoring_config
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.insights import GoLiveForecast, GoLiveStatus, ShelfwareSummary
from wardmind.core.models import Account, Milestones, Motion, Signal, SignalKind
from wardmind.web import app as web
from wardmind.web import formatting

TEMPLATES = Path(web.TEMPLATES_DIR)
APP_CSS = web.STATIC_DIR / "css" / "app.css"
NEW_CLASSES = (
    "cell-forecast",
    "forecast-unknown",
    "golive-line",
    "golive-finish",
    "shelfware-lead",
    "shelfware-list",
    "shelfware-row",
    "shelfware-head",
    "shelfware-figures",
    "shelfware-renews",
    "shelfware-more",
)
ICON_FOR_STATUS = {
    GoLiveStatus.ON_TRACK: "#health-healthy",
    GoLiveStatus.LATE: "#health-at-risk",
    GoLiveStatus.NO_PACE: "#status-upcoming",
    GoLiveStatus.TOO_EARLY: "#status-upcoming",
}
FLAT_PRICE = "Assumes a flat price per seat."
CAPTION = "Assumes the remaining milestones take as long, on average, as the ones done so far."


class _Seats(DemoConnector):
    """The demo book with seat use set for every account: the accounts in `low` are at 40% in use,
    the rest are fully used, so only `low` can have unused seats."""

    def __init__(self, low: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self._low = low

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        signals = super().list_signals(account_id)
        return [
            signal.model_copy(update={"value": 40.0 if signal.account_id in self._low else 100.0})
            if signal.kind is SignalKind.SEAT_UTILISATION
            else signal
            for signal in signals
        ]


def _text(response: Any) -> str:
    """Page text with entities decoded, since Jinja escapes apostrophes in variables."""
    return html.unescape(response.text)


def _plain(fragment: str) -> str:
    """Text of a fragment: tags removed, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _block(markup: str, pattern: str) -> str:
    match = re.search(pattern, markup, re.S)
    assert match is not None, pattern
    return match.group(1)


def _rows(markup: str) -> list[str]:
    """The picker's account cards, as HTML, in order."""
    body = _block(markup, r'<ul class="prep-list">(.*?)</ul>')
    return re.findall(r'<li class="card prep-card">(.*?)</li>', body, re.S)


def _client(monkeypatch: pytest.MonkeyPatch, connector: DemoConnector) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: connector)
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _onboarding_forecasts(
    connector: DemoConnector, today: date
) -> list[tuple[Any, GoLiveForecast | None]]:
    """Each onboarding account in book order with its forecast, as the picker computes them."""
    config = load_scoring_config()
    book = service.get_book(connector, config, today)
    return [
        (item, insights.golive_forecast(item.account, today, config.motion))
        for item in service.onboarding_accounts(book)
    ]


def _shelfware(connector: DemoConnector, today: date) -> tuple[ShelfwareSummary, dict[str, date]]:
    """The summary the brief computes, and each account's renewal date from the same book."""
    config = load_scoring_config()
    book = service.get_book(connector, config, today)
    latest = service.latest_book_signals(connector)
    summary = insights.renewal_shelfware(book, latest, today, config.motion, limit=5)
    renewals = {item.account.id: item.account.renewal_date for item in book}
    return summary, renewals


def _expected_cell(forecast: GoLiveForecast | None) -> str:
    """The Forecast cell's words: the status, then how far the pace puts the finish."""
    if forecast is None:
        return "Not known"
    label = forecast.status.label
    if forecast.status is GoLiveStatus.LATE:
        overrun = forecast.overrun_days
        return f"{label} {overrun} {'day' if overrun == 1 else 'days'} late"
    if forecast.status is GoLiveStatus.ON_TRACK:
        if forecast.overrun_days == 0:
            return f"{label} On time"
        spare = -forecast.overrun_days
        return f"{label} {spare} {'day' if spare == 1 else 'days'} to spare"
    return label


def _forecast(
    status: GoLiveStatus,
    overrun: int | None = None,
    *,
    projected: date | None = None,
    sentence: str = "A sentence about the pace.",
) -> GoLiveForecast:
    return GoLiveForecast(
        status=status,
        day=30,
        window_days=90,
        done=2,
        total=6,
        projected_day=None if overrun is None else 90 + overrun,
        projected_date=projected,
        overrun_days=overrun,
        sentence=sentence,
    )


@pytest.fixture
def connector() -> DemoConnector:
    return DemoConnector()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, connector: DemoConnector) -> TestClient:
    return _client(monkeypatch, connector)


# The picker: the Forecast column


def test_each_card_puts_the_forecast_after_the_plan(client: TestClient) -> None:
    card = _rows(_text(client.get("/onboarding")))[0]
    fields = ["prep-name", 'class="health', "prep-runway", "prep-plan", "prep-forecast"]
    fields.append("prep-action")
    positions = [card.index(field) for field in fields]
    assert positions == sorted(positions)


def test_each_row_shows_its_forecast_status_and_words(
    client: TestClient, connector: DemoConnector
) -> None:
    forecasts = _onboarding_forecasts(connector, date.today())
    rows = _rows(_text(client.get("/onboarding")))
    assert len(rows) == len(forecasts) == 8
    statuses = {forecast.status for _, forecast in forecasts if forecast is not None}
    assert {GoLiveStatus.ON_TRACK, GoLiveStatus.LATE} <= statuses  # both tag kinds in the demo
    for row, (_, forecast) in zip(rows, forecasts, strict=True):
        cell = _block(row, r'<p class="prep-forecast">(.*?)</p>')
        assert _plain(cell) == _expected_cell(forecast)
        assert forecast is not None
        assert ICON_FOR_STATUS[forecast.status] in cell


@pytest.mark.parametrize(
    ("forecast", "expected", "tag"),
    [
        pytest.param(
            _forecast(GoLiveStatus.LATE, 5),
            "Late 5 days late",
            'class="health health--at_risk golive-tag"',
            id="late-plural",
        ),
        pytest.param(
            _forecast(GoLiveStatus.LATE, 1),
            "Late 1 day late",
            'class="health health--at_risk golive-tag"',
            id="late-singular",
        ),
        pytest.param(
            _forecast(GoLiveStatus.ON_TRACK, 0),
            "On track On time",
            'class="health health--healthy golive-tag"',
            id="on-time",
        ),
        pytest.param(
            _forecast(GoLiveStatus.ON_TRACK, -3),
            "On track 3 days to spare",
            'class="health health--healthy golive-tag"',
            id="spare-plural",
        ),
        pytest.param(
            _forecast(GoLiveStatus.ON_TRACK, -1),
            "On track 1 day to spare",
            'class="health health--healthy golive-tag"',
            id="spare-singular",
        ),
        pytest.param(
            _forecast(GoLiveStatus.NO_PACE),
            "No pace yet",
            'class="tag golive-tag"',
            id="no-pace",
        ),
        pytest.param(
            _forecast(GoLiveStatus.TOO_EARLY),
            "Too early",
            'class="tag golive-tag"',
            id="too-early",
        ),
    ],
)
def test_each_status_gets_its_tag_and_short_text(
    monkeypatch: pytest.MonkeyPatch,
    connector: DemoConnector,
    forecast: GoLiveForecast,
    expected: str,
    tag: str,
) -> None:
    monkeypatch.setattr(insights, "golive_forecast", lambda account, as_of, windows: forecast)
    rows = _rows(_text(_client(monkeypatch, connector).get("/onboarding")))
    assert len(rows) == 8
    for row in rows:
        cell = _block(row, r'<p class="prep-forecast">(.*?)</p>')
        assert _plain(cell) == expected
        assert tag in cell
        assert ICON_FOR_STATUS[forecast.status] in cell


def test_an_account_with_no_forecast_shows_not_known_and_the_rest_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, connector: DemoConnector
) -> None:
    forecasts = _onboarding_forecasts(connector, date.today())
    unknown_id = forecasts[0][0].account.id
    real = insights.golive_forecast

    def unknown_for_one(account: Account, as_of: date, windows: MotionWindows) -> Any:
        return None if account.id == unknown_id else real(account, as_of, windows)

    monkeypatch.setattr(insights, "golive_forecast", unknown_for_one)
    rows = _rows(_text(_client(monkeypatch, connector).get("/onboarding")))
    cells = [_plain(_block(row, r'<p class="prep-forecast">(.*?)</p>')) for row in rows]
    assert cells[0] == "Not known"
    assert cells[1:] == [_expected_cell(forecast) for _, forecast in forecasts[1:]]
    markup = _text(_client(monkeypatch, connector).get("/onboarding"))
    assert '<span class="forecast-unknown">Not known</span>' in markup


def test_no_start_date_means_no_forecast() -> None:
    """The premise of "Not known": an account without a start date has no forecast at all."""
    today = date.today()
    account = Account(
        id="acc-x",
        name="No start",
        arr=1_000,
        tier="Growth",
        owner="Owner",
        start_date=None,
        renewal_date=today,
        milestones=Milestones(done=1, total=4),
    )
    assert insights.golive_forecast(account, today, MotionWindows()) is None


# The brief: the go-live block


@pytest.mark.parametrize("status", [GoLiveStatus.ON_TRACK, GoLiveStatus.LATE])
def test_the_brief_shows_the_tag_sentence_and_projected_finish(
    client: TestClient, connector: DemoConnector, status: GoLiveStatus
) -> None:
    today = date.today()
    item, forecast = next(
        (item, forecast)
        for item, forecast in _onboarding_forecasts(connector, today)
        if forecast is not None and forecast.status is status
    )
    assert forecast is not None and forecast.projected_date is not None
    markup = client.get(f"/onboarding/{item.account.id}").text
    section = _block(
        markup,
        r'<section class="onboarding-section golive-section no-print" '
        r'aria-labelledby="golive-heading">'
        r"(.*?)</section>",
    )
    assert _plain(_block(section, r'<h2 class="section-title" id="golive-heading">(.*?)</h2>')) == (
        "Go-live forecast"
    )
    line = _block(section, r'<p class="golive-line">(.*?)</p>')
    assert _plain(line) == f"{status.label} {forecast.sentence}"
    assert ICON_FOR_STATUS[status] in line
    finish = _block(section, r'<p class="golive-finish">(.*?)</p>')
    assert _plain(finish) == (
        f"Projected finish: {formatting.day_label(forecast.projected_date, today)}"
    )
    assert _plain(_block(section, r'<p class="section-note">(.*?)</p>')) == CAPTION


@pytest.mark.parametrize("status", [GoLiveStatus.NO_PACE, GoLiveStatus.TOO_EARLY])
def test_a_brief_with_no_pace_yet_has_no_projected_finish(
    monkeypatch: pytest.MonkeyPatch, connector: DemoConnector, status: GoLiveStatus
) -> None:
    sentence = "Day 5 of onboarding. A forecast appears after 14 days."
    forecast = _forecast(status, sentence=sentence)
    monkeypatch.setattr(insights, "golive_forecast", lambda account, as_of, windows: forecast)
    client = _client(monkeypatch, connector)
    markup = client.get(f"/onboarding/{_first_onboarding_id(connector)}").text
    assert _plain(_block(markup, r'<p class="golive-line">(.*?)</p>')) == (
        f"{status.label} {sentence}"
    )
    assert 'class="golive-finish"' not in markup
    assert CAPTION in markup


def test_a_brief_with_no_forecast_has_no_go_live_block(
    monkeypatch: pytest.MonkeyPatch, connector: DemoConnector
) -> None:
    monkeypatch.setattr(insights, "golive_forecast", lambda account, as_of, windows: None)
    client = _client(monkeypatch, connector)
    markup = client.get(f"/onboarding/{_first_onboarding_id(connector)}").text
    assert "golive" not in markup
    assert "Go-live forecast" not in markup
    assert "The plan" in markup


def _first_onboarding_id(connector: DemoConnector) -> str:
    return _onboarding_forecasts(connector, date.today())[0][0].account.id


# The weekly brief: unused seats on upcoming renewals


def test_the_section_sits_after_the_follow_ups_and_before_the_motions(client: TestClient) -> None:
    markup = client.get("/brief").text
    due = markup.index('id="due-heading"')
    shelfware = markup.index('id="shelfware-heading"')
    motions = markup.index('id="motion-heading"')
    assert due < shelfware < motions
    assert (
        '<section class="brief-section shelfware-section no-print" '
        'aria-labelledby="shelfware-heading">' in markup
    )
    assert (
        '<h2 class="section-title" id="shelfware-heading">'
        "Unused seats on upcoming renewals</h2>" in markup
    )


def test_the_lead_gives_the_count_and_the_total_unused_value(
    client: TestClient, connector: DemoConnector
) -> None:
    summary, _ = _shelfware(connector, date.today())
    assert summary.count > 5  # the demo book has more than the brief lists, so the limit shows
    markup = client.get("/brief").text
    lead = _plain(_block(markup, r'<p class="shelfware-lead">(.*?)</p>'))
    assert lead == (
        f"{summary.count} renewing accounts pay for about "
        f"{web.money(summary.total_unused_arr)} of unused seats."
    )


def test_each_row_links_the_account_and_gives_its_unused_share_and_renewal(
    client: TestClient, connector: DemoConnector
) -> None:
    today = date.today()
    summary, renewals = _shelfware(connector, today)
    markup = client.get("/brief").text
    rows = re.findall(
        r'<li class="shelfware-row">(.*?)</li>',
        _block(markup, r'<ul class="shelfware-list">(.*?)</ul>'),
        re.S,
    )
    assert len(rows) == len(summary.rows) == 5
    for markup_row, row in zip(rows, summary.rows, strict=True):
        link = f'<a class="account-link" href="/accounts/{row.account_id}">'
        assert link in markup_row
        assert _plain(_block(markup_row, r'<p class="shelfware-head">(.*?)</p>')) == row.name
        figures = _block(markup_row, r'<p class="shelfware-figures">(.*?)</p>')
        assert _plain(figures) == (
            f"{row.shelfware.unused_percent}% of seats unused, "
            f"about {web.money(row.shelfware.unused_arr)}"
        )
        renews = _block(markup_row, r'<p class="shelfware-renews">(.*?)</p>')
        assert _plain(renews) == (f"Renews {formatting.day_label(renewals[row.account_id], today)}")


def test_the_rows_are_the_largest_unused_values_first(
    client: TestClient, connector: DemoConnector
) -> None:
    summary, _ = _shelfware(connector, date.today())
    block = _block(client.get("/brief").text, r'<ul class="shelfware-list">(.*?)</ul>')
    assert re.findall(r'href="/accounts/([^"]+)"', block) == [
        row.account_id for row in summary.rows
    ]
    values = [row.shelfware.unused_arr for row in summary.rows]
    assert values == sorted(values, reverse=True)


def test_more_accounts_than_the_limit_are_counted_on_one_line(
    client: TestClient, connector: DemoConnector
) -> None:
    summary, _ = _shelfware(connector, date.today())
    assert summary.count > len(summary.rows)
    markup = client.get("/brief").text
    more = _plain(_block(markup, r'<p class="shelfware-more">(.*?)</p>'))
    assert more == f"and {summary.count - len(summary.rows)} more."


def test_the_section_ends_with_the_flat_price_assumption(client: TestClient) -> None:
    section = _block(
        client.get("/brief").text,
        r'<section class="brief-section shelfware-section no-print" '
        r'aria-labelledby="shelfware-heading">(.*?)</section>',
    )
    assert _plain(_block(section, r'<p class="section-note">(.*?)</p>')) == FLAT_PRICE


def test_one_account_uses_the_singular_and_no_more_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # Greyholt Studio renews in 69 days, inside the renewal window, and 60% of its seats are unused.
    connector = _Seats(frozenset({"acc-014"}))
    today = date.today()
    summary, _ = _shelfware(connector, today)
    assert summary.count == 1
    assert [row.account_id for row in summary.rows] == ["acc-014"]

    markup = _client(monkeypatch, connector).get("/brief").text
    lead = _plain(_block(markup, r'<p class="shelfware-lead">(.*?)</p>'))
    assert lead == (
        f"1 renewing account pays for about {web.money(summary.total_unused_arr)} of unused seats."
    )
    assert 'href="/accounts/acc-014"' in markup
    assert "shelfware-more" not in markup


def test_the_section_is_left_out_when_no_renewing_account_has_unused_seats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _Seats()
    summary, _ = _shelfware(connector, date.today())
    assert summary.count == 0
    markup = _client(monkeypatch, connector).get("/brief").text
    assert "shelfware" not in markup
    assert "Unused seats on upcoming renewals" not in markup
    assert 'id="motion-heading"' in markup


def test_the_lead_pluralises_for_one_and_for_many() -> None:
    assert web._shelfware_sentence(1, 100_200.0) == (
        "1 renewing account pays for about $100,200 of unused seats."
    )
    assert web._shelfware_sentence(2, 337_500.0) == (
        "2 renewing accounts pay for about $337,500 of unused seats."
    )


# Pages, templates and styles


def test_the_pages_keep_the_security_setup(client: TestClient, connector: DemoConnector) -> None:
    first = _first_onboarding_id(connector)
    for path in ["/onboarding", "/onboarding?missing=1", f"/onboarding/{first}", "/brief"]:
        response = client.get(path)
        assert response.headers["content-security-policy"] == web.CSP, path
        markup = response.text
        assert not re.search(r"\sstyle\s*=", markup), path
        assert not re.search(r"\son[a-z]+\s*=", markup), path
        for tag in re.findall(r"<script\b[^>]*>", markup, flags=re.IGNORECASE):
            assert re.search(r"\ssrc=", tag), tag


@pytest.mark.parametrize(
    "template",
    [
        "onboarding.html",
        "onboarding_brief.html",
        "brief.html",
        "partials/golive_tag.html",
        "partials/renewal_shelfware.html",
    ],
)
def test_the_templates_have_no_inline_styles_or_handlers(template: str) -> None:
    source = (TEMPLATES / template).read_text(encoding="utf-8")
    assert not re.search(r"\sstyle\s*=", source), template
    assert not re.search(r"\son[a-z]+\s*=", source), template
    assert "\r" not in source, template


def test_the_new_classes_are_styled_in_the_app_sheet() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert "\r" not in css
    assert "@media print" in css
    for name in NEW_CLASSES:
        assert f".{name}" in css, name


def test_on_paper_unused_seats_are_one_line_in_this_week(client: TestClient) -> None:
    # The section is screen-only, so the printed brief stays on two pages; the lead sentence
    # prints as a This week call-out instead.
    markup = client.get("/brief").text
    week = _block(
        markup, r'<section class="brief-section" aria-labelledby="week-heading">(.*?)</section>'
    )
    line = _plain(_block(week, r'<li class="print-only">(.*?)</li>'))
    lead = _plain(_block(markup, r'<p class="shelfware-lead">(.*?)</p>'))
    assert line + "." == lead


def test_on_paper_the_forecast_is_a_sentence_of_the_summary(client: TestClient) -> None:
    # The onboarding brief prints on one A4 page, so its go-live block is screen-only and the
    # forecast sentence prints at the end of the summary instead.
    item = next(i for i in service.get_book() if i.motion is Motion.ONBOARDING)
    forecast = insights.golive_forecast(item.account, date.today(), load_scoring_config().motion)
    assert forecast is not None
    summary = _block(
        client.get(f"/onboarding/{item.account.id}").text,
        r'<p class="page-lede brief-summary">(.*?)</p>',
    )
    assert _plain(_block(summary, r'<span class="print-only">(.*?)</span>')) == forecast.sentence
