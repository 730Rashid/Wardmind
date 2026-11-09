"""The Weekly brief at /brief: the page, the book bar, the print setup and the brief's helpers."""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import briefs, service
from wardmind.core.briefs import WeeklyBrief
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Health, Motion
from wardmind.web import app as web
from wardmind.web import formatting

MOTIONS = (Motion.CHURN, Motion.RENEWAL, Motion.ONBOARDING)


class _HubspotConnector(DemoConnector):
    """The demo book under another connector name, to check the footer's source."""

    name = "hubspot"


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


def _summary(markup: str) -> tuple[str, str]:
    first, second = re.findall(r'<span class="summary-line(?: [^"]*)?">([^<]*)</span>', markup)
    return html.unescape(first), html.unescape(second)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def brief() -> WeeklyBrief:
    """The demo brief for today, as the route builds it."""
    return briefs.weekly_brief(service.get_book(), as_of=date.today(), top_n=10)


def _reshape(monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    """Make the brief route see a brief with some fields replaced."""
    original = briefs.weekly_brief

    def reshaped(book: Any, as_of: date, top_n: int = 10) -> WeeklyBrief:
        return original(book, as_of=as_of, top_n=top_n).model_copy(update=changes)

    monkeypatch.setattr(briefs, "weekly_brief", reshaped)


def test_brief_page_is_the_weekly_brief(client: TestClient) -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    markup = response.text
    assert '<a class="nav-link" href="/brief" aria-current="page">Weekly brief</a>' in markup
    assert '<h1 class="page-title">Weekly brief</h1>' in markup
    assert f'<p class="date-line">{formatting.week_of(date.today())}</p>' in markup
    assert "Showing demo data" in _text(response)


def test_summary_sentences_match_my_book(client: TestClient, brief: WeeklyBrief) -> None:
    brief_summary = _summary(client.get("/brief").text)
    book_summary = _summary(client.get("/").text)
    assert brief_summary == book_summary
    assert brief_summary[0] == (
        f"{brief.counts_by_health[Health.AT_RISK]} accounts are at risk, "
        f"carrying {web.money_short(brief.arr_at_risk)} of ARR."
    )


def test_book_bar_has_a_rect_per_band_at_the_helper_geometry(
    client: TestClient, brief: WeeklyBrief
) -> None:
    markup = client.get("/brief").text
    svg = _block(markup, r'(<svg class="book-bar".*?</svg>)')
    rects = re.findall(
        r'<rect x="([^"]+)" width="([^"]+)" height="24" class="seg seg--(\w+)"/>', svg
    )
    expected = formatting.book_bar_segments(brief.arr_by_health, brief.total_arr)
    assert len(rects) == len(expected) == 3
    assert [(health, float(x), float(width)) for x, width, health in rects] == [
        (health.value, x, width) for health, x, width in expected
    ]


def test_book_bar_label_names_every_non_zero_band(client: TestClient) -> None:
    markup = client.get("/brief").text
    label = _block(markup, r'<svg class="book-bar"[^>]*aria-label="([^"]+)"')
    assert label == "ARR by health: $1.2M at risk, $1.2M watch, $2.6M healthy"


def test_legend_has_three_columns_in_bar_order(client: TestClient) -> None:
    markup = client.get("/brief").text
    legend = _block(markup, r'<ul class="legend">(.*?)</ul>')
    items = re.findall(r'<li class="legend-item">(.*?)</li>', legend, re.S)
    assert [_plain(item) for item in items] == [
        "At risk $1.2M across 10 accounts",
        "Watch $1.2M across 12 accounts",
        "Healthy $2.6M across 18 accounts",
    ]
    assert [re.findall(r"health--(\w+)", item) for item in items] == [
        ["at_risk"],
        ["watch"],
        ["healthy"],
    ]


def test_a_band_with_no_arr_still_shows_in_the_legend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reshape(
        monkeypatch,
        total_arr=800.0,
        arr_at_risk=0.0,
        arr_by_health={Health.AT_RISK: 0.0, Health.WATCH: 0.0, Health.HEALTHY: 800.0},
        counts_by_health={Health.AT_RISK: 0, Health.WATCH: 0, Health.HEALTHY: 1},
        notable=[],
    )
    response = client.get("/brief")
    markup = response.text

    legend = _block(markup, r'<ul class="legend">(.*?)</ul>')
    items = re.findall(r'<li class="legend-item">(.*?)</li>', legend, re.S)
    assert [_plain(item) for item in items] == [
        "At risk $0 across 0 accounts",
        "Watch $0 across 0 accounts",
        "Healthy $800 across 1 account",
    ]

    svg = _block(markup, r'(<svg class="book-bar".*?</svg>)')
    assert re.findall(r'<rect x="([^"]+)" width="([^"]+)"', svg) == [("0.0", "100.0")]
    assert 'class="seg seg--healthy"' in svg
    assert 'aria-label="ARR by health: $800 healthy"' in markup
    assert "No accounts are at risk this week." in _text(response)


def test_book_bar_says_so_when_there_is_no_arr(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reshape(
        monkeypatch,
        total_arr=0.0,
        arr_at_risk=0.0,
        arr_by_health={health: 0.0 for health in Health},
    )
    markup = client.get("/brief").text
    assert 'aria-label="ARR by health: no ARR in the book"' in markup
    assert 'class="seg' not in markup


def test_this_week_lists_the_notable_call_outs(client: TestClient, brief: WeeklyBrief) -> None:
    markup = client.get("/brief").text
    notable = _block(markup, r'<ul class="notable">(.*?)</ul>')
    assert [_plain(item) for item in re.findall(r"<li>(.*?)</li>", notable)] == brief.notable
    assert "Nothing unusual this week." not in markup


def test_this_week_says_so_when_nothing_is_notable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reshape(monkeypatch, notable=[])
    markup = client.get("/brief").text
    assert "Nothing unusual this week." in markup
    assert 'class="notable"' not in markup


def test_motion_table_has_three_rows_in_order(client: TestClient, brief: WeeklyBrief) -> None:
    markup = client.get("/brief").text
    table = _block(markup, r'<table class="motions">(.*?)</table>')
    body = _block(table, r"<tbody>(.*?)</tbody>")

    assert re.findall(r'<th scope="row">([^<]+)</th>', body) == [
        "Churn watch",
        "Renewal",
        "Onboarding",
    ]
    counts = [brief.counts_by_motion[motion] for motion in MOTIONS]
    assert [int(count) for count in re.findall(r'<td class="num">(\d+)</td>', body)] == counts
    largest = max(counts)
    widths = re.findall(r'<rect class="count-fill" width="([^"]+)"', body)
    assert [float(width) for width in widths] == [
        formatting.count_bar_width(count, largest) for count in counts
    ]
    assert [float(width) for width in widths] == [100.0, 60.0, 40.0]


def test_top_list_has_ten_accounts_in_book_order(client: TestClient, brief: WeeklyBrief) -> None:
    markup = client.get("/brief").text
    top = _block(markup, r'<ol class="top-list">(.*?)</ol>')
    items = re.findall(r"<li>(.*?)</li>", top, re.S)
    assert len(items) == 10
    assert "·" not in top

    motion_labels = {
        Motion.CHURN: "Churn watch",
        Motion.RENEWAL: "Renewal",
        Motion.ONBOARDING: "Onboarding",
    }
    for item, scored in zip(items, brief.top, strict=True):
        assert f'<a class="account-link" href="/accounts/{scored.account.id}">' in item
        assert html.escape(scored.account.name) in item
        assert f'class="health health--{scored.health.value}"' in item
        assert scored.health.label in _plain(item)
        assert f'<span class="top-motion">{motion_labels[scored.motion]}</span>' in item
        assert web.money(scored.account.arr) in item
        assert f'<p class="top-reason">{html.escape(scored.top_reason)}</p>' in item
        assert '<span class="top-next-label">Next step:</span>' in item
        assert html.escape(scored.suggested_action) in item


def test_print_button_is_js_only_and_app_js_prints(client: TestClient) -> None:
    markup = client.get("/brief").text
    assert (
        '<button type="button" class="button js-only" data-print>Print or save as PDF</button>'
        in markup
    )
    script = (web.STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    assert "window.print" in script
    assert "[data-print]" in script
    assert 'classList.add("js")' in script


def test_brief_and_account_pages_have_no_inline_handlers_or_styles(client: TestClient) -> None:
    for path in ["/brief", "/accounts/acc-001"]:
        markup = client.get(path).text
        assert not re.search(r"\son[a-z]+\s*=", markup), path
        assert not re.search(r"\sstyle\s*=", markup), path


def test_footer_names_the_date_and_the_demo_source(client: TestClient) -> None:
    text = _text(client.get("/brief"))
    assert f"Made by Wardmind on {formatting.long_date(date.today())} from demo data." in text


def test_footer_names_another_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: _HubspotConnector())
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")
    text = _text(client.get("/brief"))
    assert f"Made by Wardmind on {formatting.long_date(date.today())} from hubspot." in text


class _CombinedConnector(DemoConnector):
    """The demo book under the name the combined "Your data" connector has."""

    name = "combined"


def test_footer_names_your_data_for_the_combined_book(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: _CombinedConnector())
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")
    text = _text(client.get("/brief"))
    assert f"Made by Wardmind on {formatting.long_date(date.today())} from your data." in text


def test_print_stylesheet_is_in_app_css() -> None:
    css = (web.STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    for needle in [
        "@media print",
        "@page",
        "size: A4",
        "print-color-adjust: exact",
        "-webkit-print-color-adjust: exact",
        ".js .js-only",
    ]:
        assert needle in css, needle


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 21), "Week of 19 October 2026"),  # a Wednesday
        (date(2026, 10, 19), "Week of 19 October 2026"),  # the Monday itself
        (date(2026, 10, 25), "Week of 19 October 2026"),  # the Sunday
        (date(2027, 1, 1), "Week of 28 December 2026"),  # across the year end
        (datetime(2026, 10, 21, 8, 30), "Week of 19 October 2026"),
    ],
)
def test_week_of_uses_the_monday(day: date | datetime, expected: str) -> None:
    assert formatting.week_of(day) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 21), "21 October 2026"),
        (date(2025, 1, 5), "5 January 2025"),
        (datetime(2026, 3, 2, 9, 0), "2 March 2026"),
    ],
)
def test_long_date(day: date | datetime, expected: str) -> None:
    assert formatting.long_date(day) == expected


def test_book_bar_segments_places_the_bands_in_order() -> None:
    arr = {Health.AT_RISK: 1_191_000.0, Health.WATCH: 1_204_000.0, Health.HEALTHY: 2_618_500.0}
    segments = formatting.book_bar_segments(arr, sum(arr.values()))
    assert segments == [
        (Health.AT_RISK, 0.0, 23.76),
        (Health.WATCH, 23.76, 24.01),
        (Health.HEALTHY, 47.77, 52.23),
    ]


def test_book_bar_segments_leave_out_zero_bands() -> None:
    segments = formatting.book_bar_segments(
        {Health.AT_RISK: 0.0, Health.WATCH: 5.0, Health.HEALTHY: 5.0}, 10.0
    )
    assert segments == [(Health.WATCH, 0.0, 50.0), (Health.HEALTHY, 50.0, 50.0)]


def test_book_bar_segments_treat_a_missing_band_as_zero() -> None:
    assert formatting.book_bar_segments({Health.HEALTHY: 10.0}, 10.0) == [
        (Health.HEALTHY, 0.0, 100.0)
    ]


def test_book_bar_segments_give_all_the_arr_to_one_band() -> None:
    arr = {Health.AT_RISK: 0.0, Health.WATCH: 0.0, Health.HEALTHY: 1_000.0}
    assert formatting.book_bar_segments(arr, 1_000.0) == [(Health.HEALTHY, 0.0, 100.0)]


@pytest.mark.parametrize("total", [0.0, -1.0])
def test_book_bar_segments_are_empty_without_a_total(total: float) -> None:
    assert formatting.book_bar_segments({Health.AT_RISK: 0.0, Health.WATCH: 0.0}, total) == []
    assert formatting.book_bar_segments({Health.AT_RISK: 5.0}, total) == []


def test_book_bar_segments_round_so_the_last_band_ends_at_100() -> None:
    thirds = formatting.book_bar_segments(
        {Health.AT_RISK: 1.0, Health.WATCH: 1.0, Health.HEALTHY: 1.0}, 3.0
    )
    assert thirds == [
        (Health.AT_RISK, 0.0, 33.33),
        (Health.WATCH, 33.33, 33.34),
        (Health.HEALTHY, 66.67, 33.33),
    ]
    _, start, width = thirds[-1]
    assert round(start + width, 2) == 100.0

    sevenths = formatting.book_bar_segments(
        {Health.AT_RISK: 1.0, Health.WATCH: 2.0, Health.HEALTHY: 4.0}, 7.0
    )
    assert sevenths[-1] == (Health.HEALTHY, 42.86, 57.14)
    assert round(sum(width for _, _, width in sevenths), 2) == 100.0


@pytest.mark.parametrize(
    ("count", "largest", "expected"),
    [
        (20, 20, 100.0),
        (12, 20, 60.0),
        (8, 20, 40.0),
        (0, 20, 0.0),
        (1, 3, 33.3),
        (2, 3, 66.7),
        (25, 20, 100.0),  # clamped: a count above the largest still fits the track
        (5, 0, 0.0),
        (0, 0, 0.0),
    ],
)
def test_count_bar_width(count: int, largest: int, expected: float) -> None:
    assert formatting.count_bar_width(count, largest) == expected
