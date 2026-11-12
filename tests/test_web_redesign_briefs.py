"""The redesigned Weekly brief and Onboarding prep screens: the two-column brief with its jump-to
index and the logo hook, the account cards with their runway, the runway's geometry, and the
stylesheet's screen and print scoping. Printed page counts are measured by hand in Edge, so the
print layout is checked here only through its CSS rules."""

from __future__ import annotations

import html
import re
from dataclasses import FrozenInstanceError, dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import briefs, insights, service
from wardmind.core.config import load_scoring_config
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.insights import GoLiveForecast, GoLiveStatus
from wardmind.core.models import Account, Milestones
from wardmind.core.onboarding import OnboardingBrief
from wardmind.web import app as web
from wardmind.web import formatting
from wardmind.web import runway as geometry

APP_CSS = web.STATIC_DIR / "css" / "app.css"
TEMPLATES = web.TEMPLATES_DIR


class _AllComplete(DemoConnector):
    """The demo book with every milestone done, so no account is in onboarding."""

    def list_accounts(self) -> list[Account]:
        return [
            account.model_copy(
                update={
                    "milestones": Milestones(
                        done=account.milestones.total, total=account.milestones.total
                    )
                }
            )
            for account in super().list_accounts()
        ]


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _plain(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _block(markup: str, pattern: str) -> str:
    match = re.search(pattern, markup, re.S)
    assert match is not None, pattern
    return match.group(1)


def _cards(markup: str) -> list[str]:
    """The picker's account cards, as HTML, in order."""
    body = _block(markup, r'<ul class="prep-list">(.*?)</ul>')
    return re.findall(r'<li class="card prep-card">(.*?)</li>', body, re.S)


def _top_items(markup: str) -> list[str]:
    """The top accounts' items, as HTML, in order."""
    return re.findall(r"<li>(.*?)</li>", _block(markup, r'<ol class="top-list">(.*?)</ol>'), re.S)


def _rect(svg: str, css_class: str) -> dict[str, str]:
    """The attributes of the one rect with this class in a runway."""
    match = re.search(rf'<rect class="{css_class}"([^/]*)/>', svg)
    assert match is not None, css_class
    return dict(re.findall(r'(\w+)="([^"]*)"', match.group(1)))


def _forecast(status: GoLiveStatus, projected_day: int | None, window: int = 90) -> GoLiveForecast:
    overrun = None if projected_day is None else projected_day - window
    return GoLiveForecast(
        status=status,
        day=34,
        window_days=window,
        done=2,
        total=6,
        projected_day=projected_day,
        projected_date=None,
        overrun_days=overrun,
        sentence="A sentence about the pace.",
    )


@pytest.fixture(autouse=True)
def runway_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead registers runway on the app's environment; the tests register it the same way."""
    monkeypatch.setitem(web._env.globals, "runway", geometry.runway)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def brief() -> briefs.WeeklyBrief:
    return briefs.weekly_brief(service.get_book(), as_of=date.today(), top_n=10)


def _with_past_window(monkeypatch: pytest.MonkeyPatch, day: int, projected: int) -> None:
    """Every onboarding account is on `day`, and its forecast finishes on `projected`."""
    real = service.get_onboarding_brief

    def on_day(account_id: str, *args: Any, **kwargs: Any) -> OnboardingBrief | None:
        brief = real(account_id, *args, **kwargs)
        return None if brief is None else brief.model_copy(update={"day": day})

    forecast = _forecast(GoLiveStatus.LATE, projected)
    monkeypatch.setattr(service, "get_onboarding_brief", on_day)
    monkeypatch.setattr(insights, "golive_forecast", lambda account, as_of, windows: forecast)


def _without_logos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render the pages with no `logos` in their context, which is what the template's guard
    has to cope with. The app passes the book's logos itself, so the test takes them out."""
    original = web.templates.TemplateResponse

    def without_logos(request: Any, name: str, context: Any = None, *args: Any, **kw: Any) -> Any:
        values = {key: value for key, value in (context or {}).items() if key != "logos"}
        return original(request, name, values, *args, **kw)

    monkeypatch.setattr(web.templates, "TemplateResponse", without_logos)


def _with_logos(monkeypatch: pytest.MonkeyPatch, logos: dict[str, object]) -> None:
    """Pass the given `logos` to every page render, and a stand-in monogram global."""
    original = web._render

    def render_with_logos(request: Any, template: str, **context: Any) -> Any:
        return original(request, template, logos=logos, **context)

    def monogram(name: str) -> SimpleNamespace:
        initials = "".join(word[0] for word in name.split()[:2]).upper()
        return SimpleNamespace(initials=initials, tone="blue")

    monkeypatch.setattr(web, "_render", render_with_logos)
    monkeypatch.setitem(web._env.globals, "monogram", monogram)


# The runway's geometry


@dataclass(frozen=True)
class _Case:
    day: int | None
    window: int
    projected: int | None
    scale: int
    elapsed: float
    window_end: float
    projected_pct: float | None
    overrun: float
    late: bool


_RUNWAY_CASES = [
    _Case(None, 90, None, 90, 0.0, 100.0, None, 0.0, False),
    _Case(34, 90, None, 90, 37.8, 100.0, None, 0.0, False),
    _Case(34, 90, 80, 90, 37.8, 100.0, 88.9, 0.0, False),
    _Case(34, 90, 90, 90, 37.8, 100.0, 100.0, 0.0, False),  # finishes on the last day
    _Case(34, 90, 102, 102, 33.3, 88.2, 100.0, 11.8, True),
    _Case(34, 90, 91, 91, 37.4, 98.9, 100.0, 1.1, True),  # one day late
    _Case(95, 90, None, 95, 100.0, 94.7, None, 0.0, False),  # past the window, no forecast
    _Case(95, 90, 102, 102, 93.1, 88.2, 100.0, 11.8, True),
    _Case(5, 0, None, 5, 100.0, 0.0, None, 0.0, False),  # zero-length window
    _Case(10, 0, 20, 20, 50.0, 0.0, 100.0, 100.0, True),
    _Case(None, 0, None, 0, 0.0, 0.0, None, 0.0, False),  # nothing known: no division
    _Case(0, 90, None, 90, 0.0, 100.0, None, 0.0, False),
    _Case(50, -5, None, 50, 100.0, 0.0, None, 0.0, False),  # a negative window counts as zero
]


@pytest.mark.parametrize("case", _RUNWAY_CASES, ids=lambda case: f"day{case.day}-w{case.window}")
def test_the_runway_marks_sit_where_the_days_put_them(case: _Case) -> None:
    rw = geometry.runway(case.day, case.window, case.projected)
    assert rw.scale_max == case.scale
    assert rw.elapsed_pct == case.elapsed
    assert rw.window_end_pct == case.window_end
    assert rw.projected_pct == case.projected_pct
    assert rw.overrun_pct == case.overrun
    assert rw.late is case.late


def test_the_runway_is_a_frozen_value() -> None:
    rw = geometry.runway(34, 90, None)
    with pytest.raises(FrozenInstanceError):
        rw.late = True  # type: ignore[misc]


def test_every_runway_position_is_a_percentage_to_one_decimal() -> None:
    for day in (None, 0, 1, 34, 89, 90, 91, 150):
        for projected in (None, 0, 45, 90, 91, 200):
            for window in (0, 1, 90):
                rw = geometry.runway(day, window, projected)
                values = [rw.elapsed_pct, rw.window_end_pct, rw.overrun_pct]
                if rw.projected_pct is not None:
                    values.append(rw.projected_pct)
                for value in values:
                    assert 0.0 <= value <= 100.0, (day, window, projected)
                    assert round(value, 1) == value
                assert rw.overrun_pct >= 0.0
                assert rw.late == (projected is not None and projected > window)


# The brief: the two columns and the jump-to index


def test_the_accounts_are_the_main_column_and_the_rest_the_side(client: TestClient) -> None:
    markup = client.get("/brief").text
    body = markup.index('<div class="brief-body">')
    main = markup.index('<div class="brief-main">')
    top = markup.index('<ol class="top-list">')
    side = markup.index('<div class="brief-side">')
    assert body < main < top < side
    main_block = _block(markup, r'<div class="brief-main">(.*?)</div>')
    # The side column holds nested blocks (the ARR bar's chart box), so it runs to the footer.
    side_block = _block(markup, r'<div class="brief-side">(.*?)<footer class="brief-foot">')
    assert 'id="top-heading"' in main_block
    assert 'id="bar-heading"' not in main_block
    assert 'id="bar-heading"' in side_block
    assert 'id="motion-heading"' in side_block
    assert 'class="top-list"' not in side_block


def test_the_headings_follow_the_reading_order(client: TestClient) -> None:
    headings = re.findall(r'<h2 class="section-title" id="([^"]+)">', client.get("/brief").text)
    assert headings == [
        "top-heading",
        "bar-heading",
        "week-heading",
        "since-heading",
        "due-heading",
        "shelfware-heading",
        "motion-heading",
    ]


def test_the_jump_index_lists_the_top_ten_by_name_and_links_each_card(
    client: TestClient, brief: briefs.WeeklyBrief
) -> None:
    markup = client.get("/brief").text
    nav = _block(
        markup,
        r'(<nav class="top-find no-print" aria-label="Jump to an account in the top 10">'
        r".*?</nav>)",
    )
    links = re.findall(r'<a href="#(contact-[^"]+)">([^<]+)</a>', nav)
    assert [html.unescape(name) for _, name in links] == sorted(
        (item.account.name for item in brief.top), key=str.casefold
    )
    by_name = {html.unescape(name): anchor for anchor, name in links}
    for item in brief.top:
        anchor = by_name[item.account.name]
        assert anchor == f"contact-{web.idpath(item.account.id)}"
        assert markup.count(f'id="{anchor}"') == 1
        assert f'<p class="top-line" id="{anchor}">' in markup


def test_the_index_is_left_out_of_print(client: TestClient) -> None:
    assert 'class="top-find no-print"' in client.get("/brief").text


def test_each_card_names_the_account_its_health_motion_value_reason_and_action(
    client: TestClient, brief: briefs.WeeklyBrief
) -> None:
    items = _top_items(client.get("/brief").text)
    assert len(items) == len(brief.top) == 10
    for item, scored in zip(items, brief.top, strict=True):
        line = _block(item, r'<p class="top-line"[^>]*>(.*?)</p>')
        assert f'<a class="account-link" href="/accounts/{scored.account.id}">' in line
        assert scored.health.label in _plain(line)
        assert web.money(scored.account.arr) in _plain(line)
        assert f'<p class="top-reason">{html.escape(scored.top_reason)}</p>' in item
        assert html.escape(scored.suggested_action) in item


# The brief: the logo hook


def test_without_logos_the_brief_draws_no_logo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_logos(monkeypatch)
    assert "logo" not in client.get("/brief").text


def test_the_app_draws_a_logo_or_monogram_on_every_top_card(client: TestClient) -> None:
    items = _top_items(client.get("/brief").text)
    assert len(items) == 10
    assert all('class="logo logo--' in item and 'href="/accounts/' in item for item in items)


def test_with_logos_each_card_draws_its_logo_or_monogram(
    monkeypatch: pytest.MonkeyPatch, brief: briefs.WeeklyBrief
) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    first = brief.top[0].account.id
    _with_logos(monkeypatch, {first: object()})
    markup = TestClient(web.create_app(), base_url="http://127.0.0.1").get("/brief").text
    items = _top_items(markup)
    assert f'src="/logos/{first}"' in items[0]
    assert 'class="logo logo--md"' in items[0]
    assert all("logo--mono" in item for item in items[1:])
    assert 'aria-hidden="true">' in items[1]
    assert f'<a class="account-link" href="/accounts/{first}">' in items[0]


# Onboarding prep: the cards


def _onboarding_brief(account_id: str) -> OnboardingBrief:
    brief = service.get_onboarding_brief(account_id)
    assert brief is not None
    return brief


def _onboarding_ids() -> list[str]:
    return [item.account.id for item in service.onboarding_accounts(service.get_book())]


def test_the_picker_is_one_card_per_account_in_book_order(client: TestClient) -> None:
    ids = _onboarding_ids()
    markup = client.get("/onboarding").text
    cards = _cards(markup)
    assert len(cards) == len(ids) == 8
    for card, account_id in zip(cards, ids, strict=True):
        assert f'<a class="account-link" href="/onboarding/{account_id}">' in card
    assert "<table" not in markup


def test_the_headings_go_page_then_list_then_cards_then_the_picker(client: TestClient) -> None:
    markup = client.get("/onboarding").text
    levels = re.findall(r"<h([1-6])\b", markup)
    assert levels == ["1", "2"] + ["3"] * len(_onboarding_ids()) + ["2"]
    assert '<h2 class="visually-hidden">Accounts in onboarding, by priority</h2>' in markup


def test_each_card_has_its_runway_words_and_forecast_and_plan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date.today()
    config = load_scoring_config()
    markup = client.get("/onboarding").text
    for card, account_id in zip(_cards(markup), _onboarding_ids(), strict=True):
        brief = _onboarding_brief(account_id)
        forecast = insights.golive_forecast(brief.account, today, config.motion)
        words = f"Day {brief.day} of {brief.window_days}."
        if forecast is not None and forecast.projected_day is not None:
            words += f" Projected finish: day {forecast.projected_day}."
        assert _plain(_block(card, r'<p class="runway-words">(.*?)</p>')) == words
        assert f'aria-label="{words}"' in card
        assert f'<span class="plan-words">{formatting.plan_summary(brief.milestones)}' in card


def test_each_runway_is_an_image_labelled_with_the_words_under_it(client: TestClient) -> None:
    for card in _cards(client.get("/onboarding").text):
        svg = _block(card, r'(<svg class="runway".*?</svg>)')
        assert 'role="img"' in svg
        label = html.unescape(_block(svg, r'aria-label="([^"]*)"'))
        assert label == _plain(_block(card, r'<p class="runway-words">(.*?)</p>'))


def test_each_runway_draws_the_marks_the_geometry_gives(client: TestClient) -> None:
    today = date.today()
    config = load_scoring_config()
    markup = client.get("/onboarding").text
    for card, account_id in zip(_cards(markup), _onboarding_ids(), strict=True):
        brief = _onboarding_brief(account_id)
        forecast = insights.golive_forecast(brief.account, today, config.motion)
        projected = None if forecast is None else forecast.projected_day
        rw = geometry.runway(brief.day, brief.window_days, projected)
        svg = _block(card, r'(<svg class="runway".*?</svg>)')
        assert float(_rect(svg, "runway-elapsed")["width"]) == rw.elapsed_pct
        assert float(_rect(svg, "runway-window")["x"]) == rw.window_end_pct
        assert ("runway-projected" in svg) is (rw.projected_pct is not None)
        assert ("runway-overrun" in svg) is rw.late


def test_a_finish_past_the_window_is_drawn_in_the_risk_colour_past_the_window_end(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_past_window(monkeypatch, day=95, projected=102)
    card = _cards(client.get("/onboarding").text)[0]
    svg = _block(card, r'(<svg class="runway".*?</svg>)')
    overrun = _rect(svg, "runway-overrun")
    assert (float(overrun["x"]), float(overrun["width"])) == (88.2, 11.8)
    assert float(_rect(svg, "runway-projected")["x"]) == 100.0
    assert float(_rect(svg, "runway-elapsed")["width"]) == 93.1
    assert _plain(_block(card, r'<p class="runway-words">(.*?)</p>')) == (
        "Day 95 of 90. Projected finish: day 102."
    )


def test_an_account_with_no_start_date_has_no_fill_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = service.get_onboarding_brief

    def no_start(account_id: str, *args: Any, **kwargs: Any) -> OnboardingBrief | None:
        brief = real(account_id, *args, **kwargs)
        return None if brief is None else brief.model_copy(update={"day": None})

    monkeypatch.setattr(service, "get_onboarding_brief", no_start)
    monkeypatch.setattr(insights, "golive_forecast", lambda account, as_of, windows: None)
    card = _cards(client.get("/onboarding").text)[0]
    assert _plain(_block(card, r'<p class="runway-words">(.*?)</p>')) == ("Start date not known.")
    svg = _block(card, r'(<svg class="runway".*?</svg>)')
    assert float(_rect(svg, "runway-elapsed")["width"]) == 0.0
    assert "runway-projected" not in svg
    assert "Not known" in card


def test_without_the_runway_global_the_cards_still_show_their_words(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(web._env.globals, "runway", raising=False)
    markup = client.get("/onboarding").text
    assert 'class="runway"' not in markup
    cards = _cards(markup)
    assert len(cards) == 8
    assert all('<p class="runway-words">Day ' in card for card in cards)


def test_the_cards_put_the_forecast_after_the_plan_and_the_action_last(
    client: TestClient,
) -> None:
    card = _cards(client.get("/onboarding").text)[0]
    fields = ["prep-name", 'class="health', "prep-runway", "prep-plan", "prep-forecast"]
    positions = [card.index(field) for field in [*fields, "prep-action"]]
    assert positions == sorted(positions)


def test_the_prepare_link_is_a_button_style_link_naming_its_account(client: TestClient) -> None:
    markup = client.get("/onboarding").text
    for card, account_id in zip(_cards(markup), _onboarding_ids(), strict=True):
        assert '<p class="prep-action no-print">' in card
        assert f'href="/onboarding/{account_id}" aria-label="Prepare brief for ' in card


def test_an_empty_picker_has_no_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: _AllComplete())
    markup = TestClient(web.create_app(), base_url="http://127.0.0.1").get("/onboarding").text
    assert 'class="prep-list"' not in markup
    assert "Accounts in onboarding, by priority" not in markup
    assert 'id="another-heading"' in markup


def test_without_logos_the_picker_draws_no_logo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_logos(monkeypatch)
    assert "logo" not in client.get("/onboarding").text


def test_with_logos_each_card_draws_the_logo_it_is_given(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _onboarding_ids()[0]
    _with_logos(monkeypatch, {first: object()})
    cards = _cards(client.get("/onboarding").text)
    assert f'src="/logos/{first}"' in cards[0]
    assert "logo--mono" in cards[1]
    assert f'<a class="account-link" href="/onboarding/{first}">' in cards[0]


def test_the_pages_keep_the_security_setup(client: TestClient) -> None:
    for path in ("/brief", "/onboarding", f"/onboarding/{_onboarding_ids()[0]}"):
        response = client.get(path)
        assert response.headers["content-security-policy"] == web.CSP, path
        assert not re.search(r"\sstyle\s*=", response.text), path
        assert not re.search(r"\son[a-z]+\s*=", response.text), path


# The templates


def test_the_templates_have_lf_line_endings_and_no_inline_styles() -> None:
    for name in ("brief.html", "onboarding.html", "partials/logo.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "\r" not in source, name
        assert not re.search(r"\sstyle\s*=", source), name
        assert not re.search(r"\son[a-z]+\s*=", source), name
