"""The account page at /accounts/{id}, and the formatting helpers behind it."""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import service
from wardmind.core.briefs import AccountDetail, BreakdownRow, TimelineEntry
from wardmind.core.config import Thresholds, load_scoring_config
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Account, Health, Milestones, Motion, Signal, SignalKind
from wardmind.web import app as web
from wardmind.web import formatting

TODAY = date(2026, 10, 21)


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


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _plain(fragment: str) -> str:
    """Text of a fragment: tags removed, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _block(markup: str, pattern: str) -> str:
    match = re.search(pattern, markup, re.S)
    assert match is not None, pattern
    return match.group(1)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def detail() -> AccountDetail:
    """The rank-1 account of the demo book."""
    account_id = service.get_book()[0].account.id
    found = service.get_account_detail(account_id)
    assert found is not None
    return found


def test_rank_one_page_has_the_name_nav_and_summary(client: TestClient, detail: AccountDetail):
    response = client.get(f"/accounts/{detail.scored.account.id}")
    assert response.status_code == 200
    markup = response.text
    text = _text(response)

    name = detail.scored.account.name
    assert f"<title>{html.escape(name)} - Wardmind</title>" in markup
    heading = _block(markup, r'<h1 class="account-name">(.*?)</h1>')
    assert html.unescape(heading) == name
    assert '<a class="nav-link" href="/" aria-current="page">My book</a>' in markup
    assert "Back to My book" in text
    assert detail.explanation.summary in text
    assert (
        detail.explanation.summary
        == service.get_account_detail(detail.scored.account.id).explanation.summary
    )


def test_rank_one_page_has_the_facts_row(client: TestClient, detail: AccountDetail):
    markup = client.get(f"/accounts/{detail.scored.account.id}").text
    items = re.findall(r"<li>(.*?)</li>", _block(markup, r'<ul class="facts">(.*?)</ul>'), re.S)
    account = detail.scored.account
    assert [_plain(item) for item in items] == [
        detail.scored.health.label,
        formatting.motion_fact(detail),
        f"{web.money(account.arr)} ARR",
        account.owner,
        "Not contacted yet",  # the last-contact fact, from the journal
    ]
    assert "health--" in items[0]


def test_next_step_and_risk_sentence_are_shown(client: TestClient, detail: AccountDetail):
    response = client.get(f"/accounts/{detail.scored.account.id}")
    text = _text(response)
    assert '<h2 class="section-title" id="next-step-heading">Next step</h2>' in response.text
    assert detail.scored.suggested_action in text
    assert f"Why it's ranked 1 of {detail.book_size}" in text
    assert detail.book_size == 40
    sentence = formatting.risk_sentence(
        detail.scored.risk,
        detail.scored.priority,
        detail.scored.health,
        load_scoring_config().thresholds,
    )
    assert f'<p class="risk-sentence">{sentence}</p>' in response.text


def test_breakdown_rows_follow_the_breakdown_with_bar_geometry(
    client: TestClient, detail: AccountDetail
):
    markup = client.get(f"/accounts/{detail.scored.account.id}").text
    table = _block(markup, r'<table class="breakdown">(.*?)</table>')

    rows = re.findall(
        r'<tr( class="breakdown-row--muted")?>\s*<th scope="row" class="cell-signal">([^<]+)</th>',
        table,
    )
    assert len(rows) == 8
    assert [label for _, label in rows] == [row.label for row in detail.breakdown]
    assert [bool(muted) for muted, _ in rows] == [row.contribution == 0 for row in detail.breakdown]

    bars = re.findall(
        r'<svg class="pbar" width="(\d+)" height="8" role="img" aria-label="([^"]+)">'
        r'<rect class="pbar-track" width="(\d+)" height="8" rx="4"/>'
        r'<rect class="pbar-fill" width="(\d+)" height="8" rx="4"/></svg>',
        table,
    )
    longest = max(row.max_points for row in detail.breakdown)
    assert len(bars) == 8
    for (svg_width, label, track, fill), row in zip(bars, detail.breakdown, strict=True):
        expected_track, expected_fill = formatting.bar_geometry(row, longest)
        assert (int(svg_width), int(track), int(fill)) == (
            expected_track,
            expected_track,
            expected_fill,
        )
        assert label == (
            f"{formatting.number(row.contribution)} of {formatting.number(row.max_points)} points"
        )

    points = re.findall(r'<span class="points-text">([^<]+)</span>', table)
    assert points == [
        f"{formatting.number(row.contribution)} of {formatting.number(row.max_points)}"
        for row in detail.breakdown
    ]


def test_timeline_has_an_entry_per_reading_newest_first(client: TestClient, detail: AccountDetail):
    markup = client.get(f"/accounts/{detail.scored.account.id}").text
    timeline = _block(markup, r'<dl class="timeline">(.*?)</dl>')

    assert len(re.findall(r"<dd\b", timeline)) == len(detail.timeline)
    assert timeline.count('class="timeline-current"') == sum(
        entry.is_latest for entry in detail.timeline
    )
    assert re.findall(r'<span class="timeline-label">([^<]+)</span>', timeline) == [
        entry.label for entry in detail.timeline
    ]

    dates = re.findall(r'<time datetime="([^"]+)">', timeline)
    expected_days = [day.isoformat() for day, _ in formatting.group_by_day(detail.timeline)]
    assert dates == expected_days
    assert dates == sorted(dates, reverse=True)


def test_account_facts_aside_lists_the_facts(client: TestClient, detail: AccountDetail):
    markup = client.get(f"/accounts/{detail.scored.account.id}").text
    aside = _block(markup, r'<aside class="account-aside"[^>]*>(.*?)</aside>')
    account = detail.scored.account
    today = date.today()  # the page renders against the real today, as the route does

    keys = [_plain(key) for key in re.findall(r"<dt>(.*?)</dt>", aside)]
    # The owner is in the header only; the aside no longer repeats it.
    assert keys == ["Tier", "Started", "Renews", "Onboarding milestones", "Data source"]

    values = [_plain(value) for value in re.findall(r"<dd>(.*?)</dd>", aside)]
    milestones = account.milestones
    milestone_text = f"{milestones.done} of {milestones.total} done"
    if milestones.overdue > 0:
        milestone_text += f", {milestones.overdue} overdue"
    assert values == [
        account.tier,
        f"{formatting.day_label(account.start_date, today)}, "
        f"{formatting.ago(detail.days_since_start)}",
        formatting.day_label(account.renewal_date, today),
        milestone_text,
        "Demo data",
    ]


def test_overdue_milestones_are_named_in_the_facts(monkeypatch: pytest.MonkeyPatch):
    connector = DemoConnector()
    accounts = connector.list_accounts()
    target = accounts[0].model_copy(update={"milestones": Milestones(done=2, total=6, overdue=1)})
    accounts[0] = target
    monkeypatch.setattr(
        service, "get_connector", lambda: _StubConnector(accounts, connector.list_signals())
    )
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")

    markup = client.get(f"/accounts/{target.id}").text
    aside = _block(markup, r'<aside class="account-aside"[^>]*>(.*?)</aside>')
    values = [_plain(value) for value in re.findall(r"<dd>(.*?)</dd>", aside)]
    assert values[3] == "2 of 6 done, 1 overdue"


def test_account_with_no_readings_says_so(monkeypatch: pytest.MonkeyPatch):
    accounts = DemoConnector().list_accounts()
    monkeypatch.setattr(service, "get_connector", lambda: _StubConnector(accounts))
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")

    response = client.get(f"/accounts/{accounts[0].id}")
    assert response.status_code == 200
    assert "No signal readings yet." in response.text
    assert '<dl class="timeline">' not in response.text


def test_unknown_account_id_is_a_404_with_the_existing_copy(client: TestClient):
    response = client.get("/accounts/acc-999")
    assert response.status_code == 404
    assert "<title>Account not found - Wardmind</title>" in response.text
    assert "No account with id acc-999." in _text(response)
    assert "Back to My book" in _text(response)


def test_account_page_keeps_the_security_setup(client: TestClient, detail: AccountDetail):
    response = client.get(f"/accounts/{detail.scored.account.id}")
    assert response.headers["content-security-policy"] == web.CSP
    assert response.headers["x-frame-options"] == "DENY"
    markup = response.text
    assert not re.search(r"\sstyle\s*=", markup)
    assert not re.search(r"\son[a-z]+\s*=", markup)
    for tag in re.findall(r"<script\b[^>]*>", markup, flags=re.IGNORECASE):
        assert re.search(r"\ssrc=", tag), tag


# Formatting helpers


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 14), "14 October"),
        (date(2026, 1, 3), "3 January"),
        (date(2025, 10, 14), "14 October 2025"),
        (date(2027, 1, 1), "1 January 2027"),
        (datetime(2026, 10, 14, 9, 30), "14 October"),
        (datetime(2025, 3, 2, 23, 0), "2 March 2025"),
    ],
)
def test_day_label(day: date | datetime, expected: str):
    assert formatting.day_label(day, TODAY) == expected


def test_day_label_accepts_a_datetime_for_today():
    today = datetime(2026, 10, 21, 8, 0)
    assert formatting.day_label(date(2025, 10, 14), today) == "14 October 2025"
    assert formatting.day_label(date(2026, 10, 14), today) == "14 October"


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (-3, "today"),
        (0, "today"),
        (1, "1 day ago"),
        (2, "2 days ago"),
        (59, "59 days ago"),
        (60, "2 months ago"),
        (89, "2 months ago"),
        (90, "3 months ago"),
        (729, "24 months ago"),
        (730, "2 years ago"),
        (1_100, "3 years ago"),
    ],
)
def test_ago(days: int, expected: str):
    assert formatting.ago(days) == expected


def test_ago_says_one_month_and_one_year_in_the_singular():
    # ago() cannot reach these with its cut-offs (60 days is already 2 months, 730 is 2 years),
    # so the singular is checked on the phrase helper it uses.
    assert formatting._ago_phrase(1, "month") == "1 month ago"
    assert formatting._ago_phrase(1, "year") == "1 year ago"
    assert formatting._ago_phrase(2, "year") == "2 years ago"


def _renewal(detail: AccountDetail, days: int, motion: Motion = Motion.RENEWAL) -> AccountDetail:
    scored = detail.scored.model_copy(update={"motion": motion})
    return detail.model_copy(update={"scored": scored, "days_to_renewal": days})


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (41, "Renewal in 41 days"),
        (2, "Renewal in 2 days"),
        (1, "Renewal tomorrow"),
        (0, "Renews today"),
        (-1, "Renewal was yesterday"),
        (-12, "Renewal was 12 days ago"),
    ],
)
def test_motion_fact_for_a_renewal(detail: AccountDetail, days: int, expected: str):
    assert formatting.motion_fact(_renewal(detail, days)) == expected


def test_motion_fact_for_a_churn_account_still_counts_the_renewal(detail: AccountDetail):
    assert formatting.motion_fact(_renewal(detail, 41, Motion.CHURN)) == "Renewal in 41 days"


def test_motion_fact_for_onboarding_counts_the_first_day_as_day_one(detail: AccountDetail):
    scored = detail.scored.model_copy(update={"motion": Motion.ONBOARDING})
    onboarding = detail.model_copy(update={"scored": scored, "days_since_start": 33})
    assert formatting.motion_fact(onboarding) == "Day 34 of onboarding"


def test_risk_sentence_for_an_at_risk_account():
    assert formatting.risk_sentence(82, 79, Health.AT_RISK, Thresholds()) == (
        "Risk 82 of 100, which is At risk (45 or more). Priority 79 after weighting by ARR."
    )


def test_risk_sentence_for_a_watch_account_names_both_thresholds():
    assert formatting.risk_sentence(30, 28, Health.WATCH, Thresholds()) == (
        "Risk 30 of 100, which is Watch (20 or more, under 45). Priority 28 after weighting by ARR."
    )


def test_risk_sentence_for_a_healthy_account():
    assert formatting.risk_sentence(5, 4, Health.HEALTHY, Thresholds()) == (
        "Risk 5 of 100, which is Healthy (under 20). Priority 4 after weighting by ARR."
    )


def test_risk_sentence_uses_the_configured_thresholds():
    thresholds = Thresholds(at_risk=60, watch=30)
    assert "(60 or more)" in formatting.risk_sentence(70, 70, Health.AT_RISK, thresholds)
    assert "(30 or more, under 60)" in formatting.risk_sentence(40, 40, Health.WATCH, thresholds)
    assert "(under 30)" in formatting.risk_sentence(10, 10, Health.HEALTHY, thresholds)


def test_risk_sentence_keeps_one_decimal_only_when_not_whole():
    assert formatting.risk_sentence(41.3, 8.6, Health.WATCH, Thresholds()) == (
        "Risk 41.3 of 100, which is Watch (20 or more, under 45). Priority 8.6 after weighting "
        "by ARR."
    )
    assert formatting.risk_sentence(82.04, 79.0, Health.AT_RISK, Thresholds()).startswith(
        "Risk 82 of 100"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (19.0, "19"), (8.6, "8.6"), (41.26, "41.3"), (82.04, "82"), (100, "100")],
)
def test_number_drops_a_whole_decimal(value: float, expected: str):
    assert formatting.number(value) == expected


def _row(max_points: float, contribution: float) -> BreakdownRow:
    return BreakdownRow(
        kind=SignalKind.USAGE_TREND_30D,
        label="Usage trend",
        value_text="",
        contribution=contribution,
        max_points=max_points,
        observed_at=None,
    )


@pytest.mark.parametrize(
    ("max_points", "contribution", "longest", "expected"),
    [
        (25, 19, 25, (160, 122)),  # track 160; fill round(121.6)
        (10, 6, 25, (64, 38)),  # track round(64.0); fill round(38.4)
        (15, 0, 25, (96, 0)),  # a zero contribution has no fill
        (25, 25, 25, (160, 160)),  # the longest signal, fully triggered
        (20, 7.5, 40, (80, 30)),  # the longest is not the one being drawn
        (0, 0, 25, (0, 0)),  # a zero weight has no track
        (10, 5, 0, (0, 0)),  # no longest weight, so nothing to scale against
    ],
)
def test_bar_geometry(max_points: float, contribution: float, longest: float, expected: Any):
    assert formatting.bar_geometry(_row(max_points, contribution), longest) == expected


def _entry(observed_at: datetime, label: str) -> TimelineEntry:
    return TimelineEntry(
        observed_at=observed_at,
        kind=SignalKind.NPS_LATEST,
        label=label,
        value=3,
        plain_english="Latest NPS response was 3 out of 10",
        contribution=8.6,
        is_latest=False,
    )


def test_group_by_day_keeps_the_order_and_groups_one_day_together():
    entries = [
        _entry(datetime(2026, 10, 14, 9), "first"),
        _entry(datetime(2026, 10, 14, 8), "second"),
        _entry(datetime(2026, 9, 30, 9), "third"),
    ]
    groups = formatting.group_by_day(entries)
    assert [(day, [entry.label for entry in items]) for day, items in groups] == [
        (date(2026, 10, 14), ["first", "second"]),
        (date(2026, 9, 30), ["third"]),
    ]
    assert formatting.group_by_day([]) == []
