"""Settings > Profile & region: the form, saving (and the 422 that writes nothing), escaping, and
the choices every screen, the brief, the renewal sheet and the CLI then show: the currency
symbol, the date styles and "Prepared by"."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from wardmind import cli
from wardmind.core import service
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.settings import (
    Accent,
    Currency,
    DateStyle,
    Settings,
    Theme,
    load_settings,
    save_settings,
    settings_path,
)
from wardmind.web import app as web
from wardmind.web import formatting, journal_pages

BASE = "http://127.0.0.1"
ORIGIN = {"origin": BASE}
DEFAULT_DISPLAY = (Currency.USD, DateStyle.DAY_MONTH)
DEMO_ACCOUNT = "acc-014"  # Greyholt Studio: a busy renewal in the demo book
runner = CliRunner()
MONEY_WITH_DOLLAR = re.compile(r"\$\d")


@pytest.fixture(autouse=True)
def display_defaults() -> Iterator[None]:
    """The display preferences are one module-level choice: each test starts and ends on the
    default, so no test leaves a currency or date style behind for another."""
    yield
    formatting.use_display(*DEFAULT_DISPLAY)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url=BASE)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _saved(**changes: Any) -> Settings:
    """Save the given settings and return what was saved."""
    settings = load_settings().model_copy(update=changes)
    save_settings(settings)
    return settings


def _post(client: TestClient, **fields: str) -> Any:
    data = {"your_name": "", "currency": "usd", "date_style": "day_month", **fields}
    return client.post("/settings/profile", data=data, headers=ORIGIN, follow_redirects=False)


def _checked(markup: str, name: str) -> str | None:
    """The value of the checked radio in the group called `name`."""
    for tag in re.findall(r"<input[^>]*>", markup):
        if f'name="{name}"' in tag and "checked" in tag:
            value = re.search(r'value="([^"]*)"', tag)
            return value.group(1) if value else None
    return None


def _error_text(markup: str, field_id: str) -> str:
    match = re.search(rf'<p class="field-error"[^>]*id="{field_id}"[^>]*>([^<]*)</p>', markup)
    assert match is not None, field_id
    return html.unescape(match.group(1))


# --- The page -------------------------------------------------------------------------------------


def test_the_profile_page_shows_the_three_fields_with_their_help(client: TestClient) -> None:
    markup = client.get("/settings/profile").text
    assert '<input type="text" name="your_name"' in markup
    assert 'maxlength="60"' in markup
    assert "Shown as “Prepared by” on renewal sheets and the weekly brief." in markup
    assert "Changes the symbol only. Wardmind never converts amounts." in markup
    assert markup.count('name="currency"') == len(Currency)
    assert markup.count('name="date_style"') == len(DateStyle)


def test_each_currency_is_labelled_with_its_symbol(client: TestClient) -> None:
    text = _text(client.get("/settings/profile"))
    for choice in Currency:
        assert f"{choice.label} ({choice.symbol})" in text


def test_each_date_style_shows_todays_date_in_that_style(client: TestClient) -> None:
    text = _text(client.get("/settings/profile"))
    today = date.today()
    for style in DateStyle:
        assert formatting.long_date_in(today, style) in text


def test_the_saved_choices_are_checked(client: TestClient) -> None:
    _saved(currency=Currency.GBP, date_style=DateStyle.ISO, your_name="Ada")
    markup = client.get("/settings/profile").text
    assert _checked(markup, "currency") == "gbp"
    assert _checked(markup, "date_style") == "iso"
    assert 'value="Ada"' in markup


def test_the_defaults_are_checked_when_nothing_is_saved(client: TestClient) -> None:
    markup = client.get("/settings/profile").text
    assert _checked(markup, "currency") == "usd"
    assert _checked(markup, "date_style") == "day_month"


def test_a_saved_notice_comes_from_the_server_not_the_url(client: TestClient) -> None:
    assert "Profile saved." in client.get("/settings/profile?saved=1").text
    assert "Profile saved." not in client.get("/settings/profile?saved=Profile+saved").text


# --- Saving ---------------------------------------------------------------------------------------


def test_a_valid_save_writes_the_three_fields_and_redirects(client: TestClient) -> None:
    response = _post(client, your_name="  Ada   Lovelace ", currency="gbp", date_style="iso")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/profile?saved=1"
    saved = load_settings()
    assert saved.your_name == "Ada Lovelace"  # spaces are collapsed
    assert saved.currency is Currency.GBP
    assert saved.date_style is DateStyle.ISO


def test_saving_keeps_every_other_setting(client: TestClient) -> None:
    before = _saved(
        theme=Theme.DARK,
        accent=Accent.PETROL,
        auto_lock_minutes=30,
        rule_renewal=True,
        follow_up_days=10,
    )
    _post(client, your_name="Ada", currency="eur", date_style="month_day")
    after = load_settings()
    for field in (
        "theme",
        "accent",
        "auto_lock_minutes",
        "rule_renewal",
        "follow_up_days",
        "source",
    ):
        assert getattr(after, field) == getattr(before, field), field


def test_a_save_takes_effect_on_the_display_straight_away(client: TestClient) -> None:
    _post(client, currency="gbp", date_style="iso")
    assert formatting.currency_symbol() == "£"
    assert formatting.date_style() is DateStyle.ISO


def test_a_name_can_be_left_empty(client: TestClient) -> None:
    _saved(your_name="Ada")
    assert _post(client, your_name="").status_code == 303
    assert load_settings().your_name == ""


@pytest.mark.parametrize(
    ("fields", "field_id", "message"),
    [
        ({"your_name": "A" * 61}, "your-name-error", "Keep your name to 60 characters."),
        (
            {"your_name": "Ada\x07"},
            "your-name-error",
            "Your name can't contain control characters.",
        ),
        ({"currency": "xyz"}, "currency-error", "Pick a currency from the list."),
        ({"date_style": "mdy"}, "date-style-error", "Pick a date style from the list."),
    ],
)
def test_an_invalid_value_is_refused_with_its_message_and_writes_nothing(
    client: TestClient, fields: dict[str, str], field_id: str, message: str
) -> None:
    _saved(your_name="Ada", currency=Currency.EUR, date_style=DateStyle.ISO)
    before = load_settings().model_dump()
    response = _post(client, **fields)
    assert response.status_code == 422
    assert _error_text(response.text, field_id) == message
    assert "Nothing was saved." in response.text
    assert load_settings().model_dump() == before


def test_a_missing_currency_is_refused(client: TestClient) -> None:
    response = client.post(
        "/settings/profile",
        data={"your_name": "Ada", "date_style": "day_month"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Pick a currency from the list." in response.text


def test_a_refused_save_keeps_the_typed_name_and_the_saved_currency(
    client: TestClient,
) -> None:
    _saved(currency=Currency.EUR)
    response = _post(client, your_name="Ada Lovelace", currency="xyz", date_style="day_month")
    assert response.status_code == 422
    assert 'value="Ada Lovelace"' in response.text
    assert _checked(response.text, "currency") == "eur"


def test_a_refused_save_never_creates_the_settings_file(client: TestClient) -> None:
    assert not settings_path().exists()
    assert _post(client, currency="xyz").status_code == 422
    assert not settings_path().exists()


def test_a_cross_site_save_is_refused_and_writes_nothing(client: TestClient) -> None:
    before = load_settings().model_dump()
    response = client.post(
        "/settings/profile",
        data={"your_name": "Mallory", "currency": "gbp", "date_style": "iso"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert load_settings().model_dump() == before


# --- Escaping -------------------------------------------------------------------------------------


HOSTILE = "<img src=x onerror=alert(1)>"


def test_a_hostile_name_is_escaped_on_the_profile_page(client: TestClient) -> None:
    assert _post(client, your_name=HOSTILE).status_code == 303
    markup = client.get("/settings/profile").text
    assert "<img src=x" not in markup
    assert 'value="&lt;img src=x onerror=alert(1)&gt;"' in markup


def test_a_hostile_name_is_escaped_on_the_brief_and_the_renewal_sheet(client: TestClient) -> None:
    _saved(your_name=HOSTILE)
    for path in ("/brief", f"/accounts/{DEMO_ACCOUNT}/renewal-sheet"):
        markup = client.get(path).text
        assert "<img src=x" not in markup, path
        assert "Prepared by &lt;img src=x onerror=alert(1)&gt;" in markup, path


# --- Currency: every screen, the brief and the CLI ----------------------------------------------


@pytest.mark.parametrize("choice", list(Currency))
def test_money_is_written_with_the_symbol_before_the_number(choice: Currency) -> None:
    formatting.use_display(choice, DateStyle.DAY_MONTH)
    assert web.money(184_000) == f"{choice.symbol}184,000"
    assert web.money_short(1_400_000) == f"{choice.symbol}1.4M"
    assert web.money_short(950_000) == f"{choice.symbol}950K"
    assert web.money_short(800) == f"{choice.symbol}800"
    assert journal_pages.money(120_000) == f"{choice.symbol}120,000"


def test_the_default_is_still_dollars() -> None:
    assert web.money(184_000) == "$184,000"
    assert web.money_short(1_400_000) == "$1.4M"
    assert journal_pages.money(120_000) == "$120,000"


def test_my_book_shows_the_chosen_currency(client: TestClient) -> None:
    _saved(currency=Currency.GBP)
    text = _text(client.get("/"))
    assert "£" in text
    assert MONEY_WITH_DOLLAR.search(text) is None


def test_the_brief_shows_the_chosen_currency(client: TestClient) -> None:
    _saved(currency=Currency.GBP)
    markup = client.get("/brief").text
    assert "£" in markup
    assert MONEY_WITH_DOLLAR.search(markup) is None


def test_the_renewal_sheet_shows_the_chosen_currency(client: TestClient) -> None:
    _saved(currency=Currency.GBP)
    markup = client.get(f"/accounts/{DEMO_ACCOUNT}/renewal-sheet").text
    assert "£" in markup
    assert MONEY_WITH_DOLLAR.search(markup) is None


def test_the_brief_and_book_keep_dollars_by_default(client: TestClient) -> None:
    assert "$" in client.get("/brief").text


def test_the_cli_score_shows_the_chosen_currency() -> None:
    _saved(currency=Currency.GBP)
    result = runner.invoke(cli.app, ["score", "--top", "3"])
    assert result.exit_code == 0, result.output
    assert "£" in result.output
    assert "$" not in result.output
    assert re.search(r"£[\d,]+ ARR at risk", result.output)


def test_the_cli_score_keeps_dollars_by_default() -> None:
    result = runner.invoke(cli.app, ["score", "--top", "3"])
    assert result.exit_code == 0, result.output
    assert re.search(r"\$[\d,]+ ARR at risk", result.output)


# --- Dates: each style through the formatters -----------------------------------------------------

DAY = date(2026, 10, 21)  # a Wednesday
SAME_YEAR = date(2026, 10, 1)


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (DateStyle.DAY_MONTH, "21 October 2026"),
        (DateStyle.MONTH_DAY, "October 21, 2026"),
        (DateStyle.ISO, "2026-10-21"),
    ],
)
def test_long_date_in_each_style(style: DateStyle, expected: str) -> None:
    formatting.use_display(Currency.USD, style)
    assert formatting.long_date(DAY) == expected
    assert formatting.long_date_in(DAY, style) == expected


@pytest.mark.parametrize(
    ("style", "same_year", "other_year"),
    [
        (DateStyle.DAY_MONTH, "14 October", "14 October 2025"),
        (DateStyle.MONTH_DAY, "October 14", "October 14, 2025"),
        (DateStyle.ISO, "2026-10-14", "2025-10-14"),
    ],
)
def test_day_label_in_each_style(style: DateStyle, same_year: str, other_year: str) -> None:
    formatting.use_display(Currency.USD, style)
    assert formatting.day_label(date(2026, 10, 14), SAME_YEAR) == same_year
    assert formatting.day_label(date(2025, 10, 14), SAME_YEAR) == other_year


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (DateStyle.DAY_MONTH, "Week of 19 October 2026"),
        (DateStyle.MONTH_DAY, "Week of October 19, 2026"),
        (DateStyle.ISO, "Week of 2026-10-19"),
    ],
)
def test_week_of_in_each_style(style: DateStyle, expected: str) -> None:
    formatting.use_display(Currency.USD, style)
    assert formatting.week_of(DAY) == expected  # the Monday of that week


@pytest.mark.parametrize(
    ("style", "same_year", "other_year"),
    [
        (DateStyle.DAY_MONTH, "Mon 16 Nov", "Mon 4 Jan 2027"),
        (DateStyle.MONTH_DAY, "Mon Nov 16", "Mon Jan 4, 2027"),
        (DateStyle.ISO, "Mon 2026-11-16", "Mon 2027-01-04"),
    ],
)
def test_short_day_in_each_style(style: DateStyle, same_year: str, other_year: str) -> None:
    formatting.use_display(Currency.USD, style)
    today = date(2026, 11, 10)
    assert journal_pages.short_day(date(2026, 11, 16), today) == same_year
    assert journal_pages.short_day(date(2027, 1, 4), today) == other_year


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (DateStyle.DAY_MONTH, "Wednesday 21 October"),
        (DateStyle.MONTH_DAY, "Wednesday, October 21"),
        (DateStyle.ISO, "Wednesday 2026-10-21"),
    ],
)
def test_the_date_line_in_each_style(style: DateStyle, expected: str) -> None:
    formatting.use_display(Currency.USD, style)
    assert web.format_today(DAY) == expected


def test_the_profile_date_styles_are_the_ones_the_formatters_write(client: TestClient) -> None:
    _saved(date_style=DateStyle.ISO)
    markup = client.get("/brief").text
    assert re.search(r'<p class="date-line">Week of \d{4}-\d{2}-\d{2}</p>', markup)


# --- Prepared by ----------------------------------------------------------------------------------


def test_prepared_by_shows_on_the_brief_and_the_renewal_sheet_when_a_name_is_set(
    client: TestClient,
) -> None:
    _saved(your_name="Ada Lovelace")
    for path in ("/brief", f"/accounts/{DEMO_ACCOUNT}/renewal-sheet"):
        assert "Prepared by Ada Lovelace" in client.get(path).text, path


def test_prepared_by_is_left_out_when_no_name_is_set(client: TestClient) -> None:
    for path in ("/brief", f"/accounts/{DEMO_ACCOUNT}/renewal-sheet"):
        assert "Prepared by" not in client.get(path).text, path


def test_prepared_by_sits_in_the_brief_header(client: TestClient) -> None:
    _saved(your_name="Ada Lovelace")
    markup = client.get("/brief").text
    line = markup.index("Prepared by Ada Lovelace")
    assert (
        markup.index('<div class="brief-title">') < line < markup.index('<div class="brief-body">')
    )
