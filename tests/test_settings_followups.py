"""Settings > Follow-ups: the page, saving (303 on success; 422 with a message per field and nothing
written on a bad value), each bound and checkbox, escaping of what is typed back, the form without
JavaScript, and the preview line (computed now, nothing saved)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, service
from wardmind.core.connectors.combined import CombinedConnector
from wardmind.core.models import SignalKind
from wardmind.core.settings import (
    Accent,
    DataSource,
    Settings,
    Theme,
    load_settings,
    save_settings,
)
from wardmind.core.store import JOURNAL_FILE
from wardmind.web import app as web
from wardmind.web.settings_followups import check_form

ORIGIN = {"origin": "http://127.0.0.1"}
PAGE = "/settings/follow-ups"
TODAY = date.today()
NOW = datetime.now().replace(microsecond=0)
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)

# Every posted value that makes a valid form, so a test changes only the field it is about.
VALID: dict[str, str] = {
    "follow_up_days": "7",
    "quiet_days": "14",
    "reply_wait_days": "5",
    "answer_wait_days": "2",
    "rule_at_risk_days": "2",
    "rule_renewal_days": "60",
}


class _Controls(HTMLParser):
    """Collects every input, select and option start tag, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("input", "select", "option"):
            self.tags.append((tag, dict(attrs)))


def _controls(markup: str) -> dict[str, dict[str, str | None]]:
    """The input and select tags by name (the attributes of the first one of each name)."""
    parser = _Controls()
    parser.feed(markup)
    found: dict[str, dict[str, str | None]] = {}
    for _tag, attrs in parser.tags:
        name = attrs.get("name")
        if name is not None and name not in found:
            found[name] = attrs
    return found


def _checked(markup: str) -> set[str]:
    return {
        name
        for name, attrs in _controls(markup).items()
        if attrs.get("type") == "checkbox" and "checked" in attrs
    }


def _selected(markup: str, name: str) -> str | None:
    parser = _Controls()
    parser.feed(markup)
    chosen: str | None = None
    inside = False
    for tag, attrs in parser.tags:
        if tag == "select" and attrs.get("name") == name:
            inside = True
        elif tag == "select":
            inside = False
        elif tag == "option" and inside and "selected" in attrs:
            chosen = attrs.get("value")
    return chosen


def _saved_settings() -> Settings:
    return load_settings()


def _post(client: TestClient, data: dict[str, Any]) -> Any:
    return client.post(PAGE, data=data, headers=ORIGIN, follow_redirects=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


# --- The page ---------------------------------------------------------------------------------


def test_the_page_shows_the_saved_values_and_the_defaults(client: TestClient) -> None:
    response = client.get(PAGE)
    assert response.status_code == 200
    assert "<title>Follow-ups - Wardmind</title>" in response.text
    controls = _controls(response.text)
    assert controls["follow_up_days"]["value"] == "7"
    assert controls["quiet_days"]["value"] == "14"
    assert controls["reply_wait_days"]["value"] == "5"
    assert controls["answer_wait_days"]["value"] == "2"
    assert controls["rule_at_risk_days"]["value"] == "2"
    assert _selected(response.text, "rule_renewal_days") == "60"
    assert _checked(response.text) == set()


def test_the_page_lists_the_renewal_choices_in_order(client: TestClient) -> None:
    response = client.get(PAGE)
    values = re.findall(r'<option value="(\d+)"', response.text)
    assert values == ["30", "60", "90", "120"]


def test_the_sidebar_marks_follow_ups_as_the_current_section(client: TestClient) -> None:
    text = client.get(PAGE).text
    assert re.search(r'href="/settings/follow-ups"[^>]*aria-current="page"', text)


def test_the_checkboxes_and_numbers_show_what_is_saved(client: TestClient) -> None:
    save_settings(
        Settings(
            source=DataSource.DEMO,
            skip_weekends=True,
            follow_up_days=30,
            rule_champion_left=True,
            rule_renewal=True,
            rule_renewal_days=120,
        )
    )
    text = client.get(PAGE).text
    assert _checked(text) == {"skip_weekends", "rule_champion_left", "rule_renewal"}
    assert _controls(text)["follow_up_days"]["value"] == "30"
    assert _selected(text, "rule_renewal_days") == "120"


def test_the_help_text_says_where_each_setting_shows(client: TestClient) -> None:
    text = client.get(PAGE).text
    assert "A due date Wardmind picks that lands on a weekend moves to Monday." in text
    assert "Shows on Today." in text
    assert "account page" in text


def test_the_rules_are_described_as_adding_only(client: TestClient) -> None:
    text = client.get(PAGE).text
    assert "These rules only add follow-ups. They never change or delete anything." in text
    assert "only for your own data, not demo data" in text
    assert "“Check in: health dropped to At risk”" in text
    assert "“Prepare the renewal”" in text
    assert "“Find a new champion”, due in 3 days" in text


def test_a_saved_notice_shows_only_for_the_saved_code(client: TestClient) -> None:
    assert "Follow-ups saved." in client.get(f"{PAGE}?saved=1").text
    other = client.get(f"{PAGE}?saved=<script>alert(1)</script>").text
    assert "Follow-ups saved." not in other
    assert "<script>alert(1)" not in other


def test_the_form_works_without_javascript(client: TestClient) -> None:
    text = client.get(PAGE).text
    assert 'method="post" action="/settings/follow-ups"' in text
    assert not INLINE.search(text)  # the page's own script tag, if any, is a src include


# --- Saving -----------------------------------------------------------------------------------


def test_a_valid_save_writes_the_values_and_redirects_with_the_saved_code(
    client: TestClient,
) -> None:
    response = _post(
        client,
        {**VALID, "follow_up_days": "10", "quiet_days": "21", "skip_weekends": "1"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?saved=1"
    saved = _saved_settings()
    assert saved.follow_up_days == 10
    assert saved.quiet_days == 21
    assert saved.skip_weekends is True


def test_a_save_leaves_every_other_setting_as_it_was(client: TestClient) -> None:
    save_settings(
        Settings(
            theme=Theme.DARK,
            accent=Accent.BLUE,
            your_name="Priya Shah",
            auto_lock_minutes=30,
            source=DataSource.DEMO,
        )
    )
    _post(client, {**VALID, "follow_up_days": "3", "rule_champion_left": "1"})
    saved = _saved_settings()
    assert (saved.theme, saved.accent, saved.your_name) == (Theme.DARK, Accent.BLUE, "Priya Shah")
    assert saved.auto_lock_minutes == 30
    assert saved.source is DataSource.DEMO
    assert saved.follow_up_days == 3
    assert saved.rule_champion_left is True


def test_a_checkbox_that_is_not_posted_is_off(client: TestClient) -> None:
    save_settings(Settings(skip_weekends=True, rule_at_risk=True, rule_renewal=True))
    _post(client, dict(VALID))  # no checkbox fields at all
    saved = _saved_settings()
    assert saved.skip_weekends is False
    assert saved.rule_at_risk is False
    assert saved.rule_renewal is False
    assert saved.rule_champion_left is False


def test_a_posted_checkbox_is_on(client: TestClient) -> None:
    _post(client, {**VALID, "rule_at_risk": "1", "rule_renewal": "on", "rule_champion_left": "1"})
    saved = _saved_settings()
    assert (saved.rule_at_risk, saved.rule_renewal, saved.rule_champion_left) == (True, True, True)


def test_the_renewal_choice_and_the_at_risk_days_are_saved(client: TestClient) -> None:
    _post(client, {**VALID, "rule_renewal_days": "90", "rule_at_risk_days": "0"})
    saved = _saved_settings()
    assert saved.rule_renewal_days == 90
    assert saved.rule_at_risk_days == 0


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [
        ("follow_up_days", 1, 90),
        ("quiet_days", 3, 90),
        ("reply_wait_days", 1, 30),
        ("answer_wait_days", 1, 14),
        ("rule_at_risk_days", 0, 30),
    ],
)
def test_the_edges_of_each_range_are_accepted(
    client: TestClient, field: str, low: int, high: int
) -> None:
    assert _post(client, {**VALID, field: str(low)}).status_code == 303
    assert _post(client, {**VALID, field: str(high)}).status_code == 303
    assert getattr(_saved_settings(), field) == high


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("follow_up_days", "0", "Pick between 1 and 90 days."),
        ("follow_up_days", "91", "Pick between 1 and 90 days."),
        ("quiet_days", "2", "Pick between 3 and 90 days."),
        ("quiet_days", "91", "Pick between 3 and 90 days."),
        ("reply_wait_days", "0", "Pick between 1 and 30 days."),
        ("reply_wait_days", "31", "Pick between 1 and 30 days."),
        ("answer_wait_days", "0", "Pick between 1 and 14 days."),
        ("answer_wait_days", "15", "Pick between 1 and 14 days."),
        ("rule_at_risk_days", "-1", "Pick between 0 and 30 days."),
        ("rule_at_risk_days", "31", "Pick between 0 and 30 days."),
        ("rule_renewal_days", "45", "Pick 30, 60, 90 or 120 days."),
        ("rule_renewal_days", "0", "Pick 30, 60, 90 or 120 days."),
    ],
)
def test_a_value_out_of_range_is_refused_with_its_message_and_nothing_is_saved(
    client: TestClient, field: str, value: str, message: str
) -> None:
    before = _saved_settings().model_dump()
    response = _post(client, {**VALID, field: value})
    assert response.status_code == 422
    assert message in response.text
    assert f'id="{field.replace("_", "-")}-error"' in response.text
    assert _saved_settings().model_dump() == before


@pytest.mark.parametrize(
    "field",
    [
        "follow_up_days",
        "quiet_days",
        "reply_wait_days",
        "answer_wait_days",
        "rule_at_risk_days",
    ],
)
@pytest.mark.parametrize("value", ["", "abc", "7.5", "000000005", "1 2"])
def test_text_that_is_not_a_whole_number_is_refused(
    client: TestClient, field: str, value: str
) -> None:
    before = _saved_settings().model_dump()
    response = _post(client, {**VALID, field: value})
    assert response.status_code == 422
    assert "Type a whole number of days." in response.text
    assert _saved_settings().model_dump() == before


def test_a_missing_number_is_refused(client: TestClient) -> None:
    data = {key: value for key, value in VALID.items() if key != "quiet_days"}
    response = _post(client, data)
    assert response.status_code == 422
    assert "Type a whole number of days." in response.text


def test_every_bad_field_gets_its_message_at_once_and_the_good_ones_are_kept(
    client: TestClient,
) -> None:
    response = _post(
        client,
        {**VALID, "follow_up_days": "0", "answer_wait_days": "abc", "quiet_days": "14"},
    )
    assert response.status_code == 422
    assert "Pick between 1 and 90 days." in response.text
    assert "Type a whole number of days." in response.text
    controls = _controls(response.text)
    assert controls["follow_up_days"]["value"] == "0"
    assert controls["answer_wait_days"]["value"] == "abc"
    assert controls["quiet_days"]["value"] == "14"
    assert "Nothing was saved. Fix the marked settings, then save again." in response.text


def test_a_refused_form_marks_the_field_and_links_its_message(client: TestClient) -> None:
    response = _post(client, {**VALID, "reply_wait_days": "99"})
    controls = _controls(response.text)
    assert controls["reply_wait_days"]["aria-invalid"] == "true"
    assert controls["reply_wait_days"]["aria-describedby"] == (
        "reply-wait-days-help reply-wait-days-error"
    )
    assert 'id="reply-wait-days-error"' in response.text


def test_a_refused_form_keeps_the_checkboxes_as_they_were_ticked(client: TestClient) -> None:
    response = _post(
        client, {**VALID, "follow_up_days": "0", "skip_weekends": "1", "rule_champion_left": "1"}
    )
    assert response.status_code == 422
    assert _checked(response.text) == {"skip_weekends", "rule_champion_left"}


def test_a_refused_form_does_not_show_a_preview(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE, rule_champion_left=True))
    response = _post(client, {**VALID, "follow_up_days": "0", "rule_champion_left": "1"})
    assert response.status_code == 422
    assert "With these rules on" not in response.text


def test_hostile_text_typed_into_a_number_box_is_escaped_when_shown_again(
    client: TestClient,
) -> None:
    response = _post(client, {**VALID, "quiet_days": '<script>alert("x")</script>'})
    assert response.status_code == 422
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text
    assert not INLINE.search(response.text)


def test_text_longer_than_the_box_limit_is_cut_when_shown_again_and_refused(
    client: TestClient,
) -> None:
    long_text = "9" * 500
    response = _post(client, {**VALID, "follow_up_days": long_text})
    assert response.status_code == 422
    assert long_text not in response.text
    assert "9" * 8 in response.text


def test_a_missing_renewal_choice_is_refused_with_the_renewal_message(client: TestClient) -> None:
    data = {key: value for key, value in VALID.items() if key != "rule_renewal_days"}
    response = _post(client, data)
    assert response.status_code == 422
    assert "Pick 30, 60, 90 or 120 days." in response.text


def test_a_save_writes_nothing_to_the_journal(client: TestClient) -> None:
    _post(client, {**VALID, "rule_at_risk": "1"})
    assert not (service.get_store().root / JOURNAL_FILE).exists()


# --- The preview line -------------------------------------------------------------------------


def _at_risk_account(name: str) -> str:
    """A hand-added account whose readings put it over the At risk line (see test_rules.py)."""
    account = editing.create_account(
        service.get_store(),
        {
            "name": name,
            "arr": "$100,000",
            "renewal_date": (TODAY + timedelta(days=300)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()
    store = service.get_store()
    connector = service.get_connector()
    assert isinstance(connector, CombinedConnector)
    for kind, raw in (
        (SignalKind.CHAMPION_LEFT, "yes"),
        (SignalKind.OPEN_TICKETS_SEV1, "3"),
        (SignalKind.BILLING_OVERDUE_DAYS, "60"),
    ):
        editing.add_reading(store, connector, account.id, kind, raw, now=NOW)
    service.reset_connectors()
    return account.id


def test_the_preview_says_how_many_follow_ups_the_rules_would_add(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE, rule_at_risk=True))
    service.reset_connectors()
    _at_risk_account("Acme Robotics")
    _at_risk_account("Globex")
    text = client.get(PAGE).text
    assert "With these rules on, Wardmind would add 2 follow-ups today." in text


def test_the_preview_uses_the_singular_for_one(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE, rule_at_risk=True))
    service.reset_connectors()
    _at_risk_account("Acme Robotics")
    assert "would add 1 follow-up today." in client.get(PAGE).text


def test_the_preview_is_missing_when_no_rule_is_on(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()
    _at_risk_account("Acme Robotics")
    assert "With these rules on" not in client.get(PAGE).text


def test_the_preview_is_missing_for_demo_data_even_with_a_rule_on(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.DEMO, rule_at_risk=True, rule_champion_left=True))
    service.reset_connectors()
    assert "With these rules on" not in client.get(PAGE).text


def test_the_preview_counts_zero_when_nothing_matches(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE, rule_at_risk=True))
    service.reset_connectors()
    account = editing.create_account(
        service.get_store(),
        {
            "name": "Quiet Co",
            "arr": "$50,000",
            "renewal_date": (TODAY + timedelta(days=300)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()
    assert account.name == "Quiet Co"
    assert "With these rules on, Wardmind would add 0 follow-ups today." in client.get(PAGE).text


def test_the_preview_saves_nothing(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE, rule_at_risk=True))
    service.reset_connectors()
    _at_risk_account("Acme Robotics")
    client.get(PAGE)
    assert not (service.get_store().root / JOURNAL_FILE).exists()
    assert service.get_journal().all() == {}


def test_an_invalid_value_the_form_does_not_set_is_refused_as_a_whole_form() -> None:
    """A stored value the form doesn't show can't name a field, so the form says so as a whole
    (and nothing is saved). Built with model_construct, which skips the check the file load does."""
    current = Settings.model_construct(ai_model="not a model id")
    values = {name: False for name in ("skip_weekends", "rule_at_risk", "rule_renewal")}
    values |= {name: "7" for name in ("follow_up_days", "quiet_days", "reply_wait_days")}
    values |= {"answer_wait_days": "2", "rule_at_risk_days": "2", "rule_renewal_days": "60"}
    values["rule_champion_left"] = False
    checked, errors = check_form(values, current)
    assert checked is None
    assert errors == {"form": "These settings can't be saved. Check them and try again."}
