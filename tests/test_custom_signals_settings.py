"""Settings > Scoring > Your own signals: the list, add, edit, remove with its confirm page, the
validation messages, the id rules, the 12 limit, and that the built-in sliders, the reset and the
cross-site checks leave the book's own signals alone.

Every test runs against the temp config folder from conftest.py, so saves never reach config/.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import audit, service
from wardmind.core.config import (
    CustomSignal,
    ScoringConfig,
    SignalRule,
    load_scoring_config,
    save_scoring_config,
)
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import CUSTOM_ID
from wardmind.web import app as web
from wardmind.web import custom_signals_form as form
from wardmind.web.scoring_form import form_values, parse_scoring_form

ORIGIN = {"origin": "http://127.0.0.1"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "use", "wbr"}
SECTION_ID = "custom-signals"
ADD_FORM_ID = "custom-signal-add"
DEFAULT_CHANGES: dict[str, str] = {
    "custom.name": "API calls per week",
    "custom.unit": "calls",
    "custom.direction": "lower",
    "custom.start": "500",
    "custom.full": "50",
    "custom.weight": "30",
    "custom.question": "",
    "custom.action": "",
    "custom.stale_days": "30",
}
API_CALLS_ID = "api_calls_per_week"


class _Tree(HTMLParser):
    """Every start tag with its attributes and the tags open around it."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.found: list[tuple[str, dict[str, str | None], list[tuple[str, dict[str, Any]]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.found.append((tag, attributes, list(self.stack)))
        if tag not in VOID:
            self.stack.append((tag, attributes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.append((tag, dict(attrs), list(self.stack)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def _walk(markup: str) -> list[Any]:
    parser = _Tree()
    parser.feed(markup)
    return parser.found


def _by_id(markup: str, element_id: str) -> dict[str, str | None]:
    matches = [attrs for _, attrs, _ in _walk(markup) if attrs.get("id") == element_id]
    assert len(matches) == 1, f"expected one element with id {element_id}"
    return matches[0]


def _radios(markup: str, name: str) -> list[dict[str, str | None]]:
    return [
        attrs
        for tag, attrs, _ in _walk(markup)
        if tag == "input" and attrs.get("name") == name and attrs.get("type") == "radio"
    ]


def _described_by(markup: str, error_id: str) -> dict[str, str | None] | None:
    for _, attrs, _ in _walk(markup):
        if error_id in str(attrs.get("aria-describedby") or "").split():
            return attrs
    return None


def _signal_form(changes: dict[str, str]) -> dict[str, str]:
    return {**DEFAULT_CHANGES, **changes}


def _custom(name: str, signal_id: str, *, start: float = 0, full: float = 10) -> CustomSignal:
    return CustomSignal(id=signal_id, name=name, rule=SignalRule(weight=10, start=start, full=full))


def _saved_custom() -> list[CustomSignal]:
    return load_scoring_config().custom


def _add(client: TestClient, changes: dict[str, str] | None = None, **kwargs: Any) -> Any:
    return client.post(
        "/settings/scoring/custom",
        data=_signal_form(changes or {}),
        headers=kwargs.pop("headers", ORIGIN),
        follow_redirects=False,
        **kwargs,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def config_dir(isolated_config_dir: Path) -> Path:
    return isolated_config_dir


@pytest.fixture
def one_signal(client: TestClient, config_dir: Path) -> str:
    """A book with one signal of its own, added through the page: its id."""
    response = _add(client)
    assert response.status_code == 303
    return API_CALLS_ID


# The section on the page


def test_the_section_sits_under_the_built_in_form_and_outside_it(client: TestClient) -> None:
    text = client.get("/settings/scoring").text
    assert text.count("<h1") == 1
    section = _by_id(text, SECTION_ID)
    assert section["class"] == "settings-card custom-signals"
    assert section["aria-labelledby"] == "custom-signals-title"
    assert "Your own signals" in text
    assert text.index('id="scoring-form"') < text.index(f'id="{SECTION_ID}"')
    assert text.index(f'id="{SECTION_ID}"') < text.index('id="scoring-preview"')
    # Not nested in the built-in form: the add form is a form of its own.
    (add_form,) = [
        (attrs, ancestors)
        for tag, attrs, ancestors in _walk(text)
        if tag == "form" and attrs.get("id") == ADD_FORM_ID
    ]
    attrs, ancestors = add_form
    assert attrs["method"] == "post"
    assert attrs["action"] == "/settings/scoring/custom"
    assert not any(found.get("id") == "scoring-form" for _, found in ancestors)


def test_an_empty_list_says_what_custom_signals_are_for(client: TestClient) -> None:
    text = client.get("/settings/scoring").text
    assert "You haven't added any signals yet." in text
    assert "Custom signals are numbers you track for this book." in text
    assert (
        "Then map a column to it when you import a spreadsheet or add a connection, "
        "or type a reading on an account."
    ) in text
    assert 'class="rows custom-signal-list"' not in text


def test_each_signal_shows_its_name_its_range_and_edit_and_remove(
    client: TestClient, one_signal: str
) -> None:
    text = client.get("/settings/scoring").text
    assert 'class="rows custom-signal-list"' in text
    assert f'id="custom-signal-{one_signal}"' in text
    assert '<p class="custom-signal-name">API calls per week</p>' in text
    assert "Risk starts at 500 calls, maxed at 50 calls, up to 30 points." in text
    assert "Lower numbers are worse. A reading is out of date after 30 days." in text
    assert f'href="/settings/scoring/custom/{one_signal}">Edit' in text
    assert f'href="/settings/scoring/custom/{one_signal}/remove">Remove' in text


def test_a_full_list_hides_the_add_form_and_says_so(client: TestClient, config_dir: Path) -> None:
    save_scoring_config(
        ScoringConfig(custom=[_custom(f"Signal {n}", f"signal_{n}") for n in range(12)])
    )
    text = client.get("/settings/scoring").text
    assert f'id="{ADD_FORM_ID}"' not in text
    assert "You have 12 of your own signals, the most Wardmind keeps." in text
    assert "Remove one to add another." in text


def test_the_section_is_below_the_slider_form_in_the_left_column(client: TestClient) -> None:
    text = client.get("/settings/scoring").text
    assert text.index('class="scoring-main"') < text.index('id="scoring-form"')


# Add


def test_adding_a_signal_saves_it_and_shows_it_with_a_notice(
    client: TestClient, config_dir: Path
) -> None:
    response = _add(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/scoring?custom=added#custom-signals"
    (signal,) = _saved_custom()
    assert signal.id == API_CALLS_ID
    assert signal.name == "API calls per week"
    assert signal.unit == "calls"
    assert signal.rule == SignalRule(weight=30, start=500, full=50)
    assert signal.stale_days == 30
    assert (signal.question, signal.action) == ("", "")
    page = client.get("/settings/scoring?custom=added").text
    assert '<p class="settings-status" role="status">Signal added.</p>' in page
    assert "Risk starts at 500 calls, maxed at 50 calls, up to 30 points." in page


def test_adding_writes_one_activity_entry_for_your_own_signals(client: TestClient) -> None:
    _add(client)
    newest = audit.entries(1)[0]
    assert newest.event is audit.Event.SETTINGS_SAVED
    assert newest.detail == "Your own signals"


def test_higher_numbers_are_worse_when_the_maximum_is_above_the_start(
    client: TestClient, config_dir: Path
) -> None:
    changes = {
        "custom.name": "Support tickets",
        "custom.unit": "",
        "custom.direction": "higher",
        "custom.start": "2",
        "custom.full": "10",
        "custom.weight": "20",
    }
    assert _add(client, changes).status_code == 303
    (signal,) = _saved_custom()
    assert signal.rule == SignalRule(weight=20, start=2, full=10)
    page = client.get("/settings/scoring").text
    assert "Risk starts at 2, maxed at 10, up to 20 points." in page
    assert "Higher numbers are worse. A reading is out of date after 30 days." in page


def test_the_numbers_are_kept_as_typed_and_never_swapped(
    client: TestClient, config_dir: Path
) -> None:
    _add(client, {"custom.start": "50", "custom.full": "500"})  # lower is worse: refused
    assert _saved_custom() == []
    _add(client, {"custom.direction": "higher", "custom.start": "50", "custom.full": "500"})
    (signal,) = _saved_custom()
    assert (signal.rule.start, signal.rule.full) == (50, 500)


def test_the_name_and_text_are_saved_on_one_line_with_single_spaces(
    client: TestClient, config_dir: Path
) -> None:
    _add(
        client,
        {
            "custom.name": "  Reports\n   exported  ",
            "custom.question": "  What is\nbehind it?  ",
            "custom.action": "Agree a fix.",
            "custom.stale_days": "90",
            "custom.weight": "12.5",
        },
    )
    (signal,) = _saved_custom()
    assert signal.name == "Reports exported"
    assert signal.question == "What is behind it?"
    assert signal.action == "Agree a fix."
    assert signal.stale_days == 90
    assert signal.rule.weight == 12.5


def test_the_slider_and_range_show_the_signal_values_on_the_edit_page(
    client: TestClient, one_signal: str
) -> None:
    text = client.get(f"/settings/scoring/custom/{one_signal}").text
    assert _by_id(text, "custom-start")["value"] == "500"
    assert _by_id(text, "custom-full")["value"] == "50"
    assert _by_id(text, "custom-weight")["value"] == "30"
    assert _by_id(text, "custom-stale_days")["value"] == "30"  # the number field's id: its name
    assert _by_id(text, "custom-unit")["value"] == "calls"
    radios = {attrs["value"]: "checked" in attrs for attrs in _radios(text, "custom.direction")}
    assert radios == {"lower": True, "higher": False}


# Edit


def test_editing_renames_the_signal_and_keeps_its_id(client: TestClient, one_signal: str) -> None:
    response = client.post(
        f"/settings/scoring/custom/{one_signal}",
        data=_signal_form({"custom.name": "Calls a week"}),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/scoring?custom=saved#custom-signals"
    (signal,) = _saved_custom()
    assert signal.id == one_signal
    assert signal.name == "Calls a week"
    page = client.get("/settings/scoring?custom=saved").text
    assert '<p class="settings-status" role="status">Signal saved.</p>' in page
    assert f'id="custom-signal-{one_signal}"' in page


def test_editing_changes_the_range_and_the_text(client: TestClient, one_signal: str) -> None:
    response = client.post(
        f"/settings/scoring/custom/{one_signal}",
        data=_signal_form(
            {
                "custom.direction": "higher",
                "custom.start": "40",
                "custom.full": "90",
                "custom.question": "Who is using it less?",
                "custom.action": "Book a training call.",
            }
        ),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    (signal,) = _saved_custom()
    assert signal.id == one_signal
    assert signal.rule == SignalRule(weight=30, start=40, full=90)
    assert (signal.question, signal.action) == ("Who is using it less?", "Book a training call.")


def test_the_edit_page_is_a_form_with_cancel_and_save(client: TestClient, one_signal: str) -> None:
    text = client.get(f"/settings/scoring/custom/{one_signal}").text
    assert text.count("<h1") == 1
    assert "Edit API calls per week" in text
    # The header's book switcher has forms of its own; only this page's forms are counted here.
    form_attrs = [
        attrs
        for tag, attrs, _ in _walk(text)
        if tag == "form" and attrs.get("action") != "/books/switch"
    ]
    assert [(attrs["method"], attrs["action"]) for attrs in form_attrs] == [
        ("post", f"/settings/scoring/custom/{one_signal}")
    ]
    assert 'href="/settings/scoring#custom-signals">Cancel</a>' in text
    assert 'class="button-primary">Save changes</button>' in text


def test_a_refused_edit_shows_the_messages_and_saves_nothing(
    client: TestClient, one_signal: str, config_dir: Path
) -> None:
    before = (config_dir / "scoring.yaml").read_bytes()
    response = client.post(
        f"/settings/scoring/custom/{one_signal}",
        data=_signal_form({"custom.direction": "lower", "custom.start": "10", "custom.full": "50"}),
        headers=ORIGIN,
    )
    assert response.status_code == 422
    assert "Nothing was saved." in response.text
    assert "Lower numbers are worse, so the maximum has to be below where risk starts." in (
        response.text
    )
    assert _by_id(response.text, "custom-full")["value"] == "50"  # what was typed stays
    assert (config_dir / "scoring.yaml").read_bytes() == before


# Remove


def test_the_remove_page_asks_first_and_says_what_stays(
    client: TestClient, one_signal: str
) -> None:
    response = client.get(f"/settings/scoring/custom/{one_signal}/remove")
    assert response.status_code == 200
    assert "Remove API calls per week?" in response.text
    assert "Its readings stay on accounts but stop counting toward their risk." in response.text
    assert "Adding a signal with the same name later brings them back." in response.text
    assert f'action="/settings/scoring/custom/{one_signal}/remove"' in response.text
    assert [s.id for s in _saved_custom()] == [one_signal]  # the GET removed nothing


def test_removing_takes_the_signal_off_the_list(client: TestClient, one_signal: str) -> None:
    response = client.post(
        f"/settings/scoring/custom/{one_signal}/remove", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/scoring?custom=removed#custom-signals"
    assert _saved_custom() == []
    page = client.get("/settings/scoring?custom=removed").text
    assert '<p class="settings-status" role="status">Signal removed.</p>' in page
    assert "You haven't added any signals yet." in page


def test_a_signal_added_again_with_the_same_name_gets_the_same_id(
    client: TestClient, one_signal: str
) -> None:
    client.post(
        f"/settings/scoring/custom/{one_signal}/remove", headers=ORIGIN, follow_redirects=False
    )
    assert _add(client).status_code == 303
    assert [s.id for s in _saved_custom()] == [one_signal]


# Unknown ids are the app's 404 page


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("GET", ""), ("POST", ""), ("GET", "/remove"), ("POST", "/remove")],
)
def test_an_unknown_id_is_the_normal_not_found_page(
    client: TestClient, one_signal: str, config_dir: Path, method: str, suffix: str
) -> None:
    before = (config_dir / "scoring.yaml").read_bytes()
    response = client.request(
        method,
        f"/settings/scoring/custom/no_such_signal{suffix}",
        data=_signal_form({}) if method == "POST" else None,
        headers=ORIGIN if method == "POST" else None,
    )
    assert response.status_code == 404
    assert "That signal isn't in your list." in html.unescape(response.text)
    assert "Back to Scoring" in response.text
    assert (config_dir / "scoring.yaml").read_bytes() == before
    assert [s.id for s in _saved_custom()] == [one_signal]


# Validation: each message is a 422 on its field, with nothing saved


def _with_one_signal(client: TestClient) -> None:
    assert _add(client).status_code == 303


VALIDATION_CASES = [
    pytest.param(
        {"custom.name": "   "}, form.NAME, "Enter a name for the signal.", id="blank-name"
    ),
    pytest.param(
        {"custom.name": "A" * 41}, form.NAME, "Use 40 characters or fewer.", id="long-name"
    ),
    pytest.param(
        {"custom.name": "%%% ###"},
        form.NAME,
        "The name needs at least one letter or number (A to Z, 0 to 9).",
        id="symbols-only-name",
    ),
    pytest.param(
        {"custom.name": "api CALLS per week"},
        form.NAME,
        "You already have a signal with this name. Names have to be different.",
        id="duplicate-name-any-case",
    ),
    pytest.param(
        {"custom.unit": "x" * 13}, form.UNIT, "Use 12 characters or fewer.", id="long-unit"
    ),
    pytest.param(
        {"custom.direction": ""}, form.DIRECTION, "Choose which way is worse.", id="no-way"
    ),
    pytest.param(
        {"custom.direction": "lower", "custom.start": "10", "custom.full": "50"},
        form.FULL,
        "Lower numbers are worse, so the maximum has to be below where risk starts.",
        id="lower-but-max-above-start",
    ),
    pytest.param(
        {"custom.direction": "higher", "custom.start": "50", "custom.full": "10"},
        form.FULL,
        "Higher numbers are worse, so the maximum has to be above where risk starts.",
        id="higher-but-max-below-start",
    ),
    pytest.param(
        {"custom.direction": "higher", "custom.start": "50", "custom.full": "50"},
        form.FULL,
        "Higher numbers are worse, so the maximum has to be above where risk starts.",
        id="same-start-and-max",
    ),
    pytest.param({"custom.start": "many"}, form.START, "Enter a number.", id="start-not-a-number"),
    pytest.param({"custom.full": ""}, form.FULL, "Enter a number.", id="max-blank"),
    pytest.param(
        {"custom.weight": "150"}, form.WEIGHT, "Use a value from 0 to 100.", id="weight-over-100"
    ),
    pytest.param(
        {"custom.question": "q" * 161},
        form.QUESTION,
        "Use 160 characters or fewer.",
        id="long-question",
    ),
    pytest.param(
        {"custom.action": "a" * 161}, form.ACTION, "Use 160 characters or fewer.", id="long-action"
    ),
    pytest.param(
        {"custom.stale_days": "0"},
        form.STALE_DAYS,
        "Use from 1 to 365 days.",
        id="stale-days-zero",
    ),
    pytest.param(
        {"custom.stale_days": "366"},
        form.STALE_DAYS,
        "Use from 1 to 365 days.",
        id="stale-days-too-many",
    ),
    pytest.param(
        {"custom.stale_days": "2.5"},
        form.STALE_DAYS,
        "Enter a whole number of days.",
        id="stale-days-not-whole",
    ),
]


@pytest.mark.parametrize(("changes", "field", "message"), VALIDATION_CASES)
def test_a_refused_add_is_422_marks_the_field_and_saves_nothing(
    client: TestClient,
    config_dir: Path,
    changes: dict[str, str],
    field: str,
    message: str,
) -> None:
    _with_one_signal(client)
    before = (config_dir / "scoring.yaml").read_bytes()
    response = _add(client, changes)
    assert response.status_code == 422
    assert "Nothing was saved." in response.text
    assert message in response.text
    error_id = f"{field.replace('.', '-')}-error"
    assert f'id="{error_id}"' in response.text
    if field == form.DIRECTION:
        radios = _radios(response.text, form.DIRECTION)
        assert len(radios) == 2
        assert all(radio.get("aria-invalid") == "true" for radio in radios)
        assert _described_by(response.text, error_id) is not None
    else:
        field_attrs = _by_id(response.text, field.replace(".", "-"))
        assert field_attrs["aria-invalid"] == "true"
        assert error_id in str(field_attrs["aria-describedby"]).split()
    assert len(_saved_custom()) == 1  # only the signal already there
    assert (config_dir / "scoring.yaml").read_bytes() == before


def test_a_refused_add_keeps_what_was_typed(client: TestClient, config_dir: Path) -> None:
    response = _add(
        client,
        {"custom.name": "Admins active", "custom.start": "many", "custom.unit": "people"},
    )
    assert response.status_code == 422
    assert _by_id(response.text, "custom-name")["value"] == "Admins active"
    assert _by_id(response.text, "custom-unit")["value"] == "people"
    assert _by_id(response.text, "custom-start").get("value") == "many"


def test_every_message_shows_at_once(client: TestClient, config_dir: Path) -> None:
    response = _add(client, {"custom.name": "", "custom.unit": "x" * 13, "custom.weight": "999"})
    assert response.status_code == 422
    for field in (form.NAME, form.UNIT, form.WEIGHT):
        assert f'id="{field.replace(".", "-")}-error"' in response.text
    assert "Use a value from 0 to 100." in response.text
    assert _saved_custom() == []


def test_a_refused_add_on_a_full_list_is_422_and_saves_nothing(
    client: TestClient, config_dir: Path
) -> None:
    save_scoring_config(
        ScoringConfig(custom=[_custom(f"Signal {n}", f"signal_{n}") for n in range(12)])
    )
    before = (config_dir / "scoring.yaml").read_bytes()
    response = _add(client, {"custom.name": "One more"})
    assert response.status_code == 422
    assert 'role="alert">You have 12 of your own signals' in response.text
    assert (config_dir / "scoring.yaml").read_bytes() == before


def test_the_twelfth_signal_is_allowed_and_the_thirteenth_is_not(
    client: TestClient, config_dir: Path
) -> None:
    save_scoring_config(
        ScoringConfig(custom=[_custom(f"Signal {n}", f"signal_{n}") for n in range(11)])
    )
    assert _add(client, {"custom.name": "Twelfth"}).status_code == 303
    assert len(_saved_custom()) == 12
    assert _add(client, {"custom.name": "Thirteenth"}).status_code == 422
    assert len(_saved_custom()) == 12


# The id


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("API calls per week", "api_calls_per_week"),
        ("Admins active", "admins_active"),
        ("  Reports   exported!  ", "reports_exported"),
        ("% of seats used", "of_seats_used"),
        ("2FA logins", "s_2fa_logins"),
        ("Café visits", "caf_visits"),
        ("API calls (per week)", "api_calls_per_week"),
        ("x" * 60, "x" * 40),
    ],
)
def test_the_id_is_made_from_the_name(name: str, expected: str) -> None:
    assert form.signal_id(name) == expected
    assert CUSTOM_ID.fullmatch(expected)


def test_a_taken_id_gets_a_number_then_another() -> None:
    assert form.signal_id("Admins active", ["admins_active"]) == "admins_active_2"
    assert (
        form.signal_id("Admins active", ["admins_active", "admins_active_2"]) == "admins_active_3"
    )


def test_a_long_id_stays_inside_the_limit_when_numbered() -> None:
    numbered = form.signal_id("x" * 60, ["x" * 40])
    assert numbered == "x" * 38 + "_2"
    assert CUSTOM_ID.fullmatch(numbered)


def test_two_names_with_the_same_letters_get_different_ids(
    client: TestClient, config_dir: Path
) -> None:
    assert _add(client, {"custom.name": "Admins active"}).status_code == 303
    assert _add(client, {"custom.name": "admins-active"}).status_code == 303
    assert [s.id for s in _saved_custom()] == ["admins_active", "admins_active_2"]


def test_the_id_never_changes_on_rename(client: TestClient, one_signal: str) -> None:
    for name in ("Calls per week", "Weekly API calls", "Something else"):
        client.post(
            f"/settings/scoring/custom/{one_signal}",
            data=_signal_form({"custom.name": name}),
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert [s.id for s in _saved_custom()] == [one_signal]
    assert _saved_custom()[0].name == "Something else"


def test_the_name_must_be_unique_when_renaming_to_another_signals_name(
    client: TestClient, config_dir: Path
) -> None:
    _add(client, {"custom.name": "Admins active"})
    _add(client, {"custom.name": "Reports exported"})
    response = client.post(
        "/settings/scoring/custom/reports_exported",
        data=_signal_form({"custom.name": "ADMINS ACTIVE"}),
        headers=ORIGIN,
    )
    assert response.status_code == 422
    assert "You already have a signal with this name." in response.text
    assert [s.name for s in _saved_custom()] == ["Admins active", "Reports exported"]


def test_a_signal_may_keep_its_own_name_when_edited(client: TestClient, one_signal: str) -> None:
    response = client.post(
        f"/settings/scoring/custom/{one_signal}",
        data=_signal_form({"custom.name": "api calls per week", "custom.weight": "5"}),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _saved_custom()[0].rule.weight == 5


# Cross-site writes are refused, and nothing changes


@pytest.mark.parametrize(
    "path",
    [
        "/settings/scoring/custom",
        "/settings/scoring/custom/api_calls_per_week",
        "/settings/scoring/custom/api_calls_per_week/remove",
    ],
)
@pytest.mark.parametrize(
    "headers", [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}]
)
def test_a_cross_site_post_is_refused_and_writes_nothing(
    client: TestClient,
    one_signal: str,
    config_dir: Path,
    path: str,
    headers: dict[str, str],
) -> None:
    before = (config_dir / "scoring.yaml").read_bytes()
    response = client.post(path, data=DEFAULT_CHANGES, headers=headers)
    assert response.status_code == 403
    assert (config_dir / "scoring.yaml").read_bytes() == before


# The built-in sliders and the reset keep the book's own signals


def test_saving_the_built_in_sliders_keeps_your_own_signals(
    client: TestClient, one_signal: str, config_dir: Path
) -> None:
    before = _saved_custom()
    data = {**form_values(load_scoring_config()), "thresholds.at_risk": "30"}
    response = client.post("/settings/scoring", data=data, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    saved = load_scoring_config()
    assert saved.custom == before
    assert saved.thresholds.at_risk == 30


def test_a_refused_slider_save_still_lists_your_own_signals(
    client: TestClient, one_signal: str, config_dir: Path
) -> None:
    before = (config_dir / "scoring.yaml").read_bytes()
    data = {**form_values(load_scoring_config()), "thresholds.watch": "99"}
    response = client.post("/settings/scoring", data=data, headers=ORIGIN)
    assert response.status_code == 422
    assert f'id="custom-signal-{one_signal}"' in response.text
    assert (config_dir / "scoring.yaml").read_bytes() == before


def test_the_built_in_form_round_trip_carries_the_custom_list() -> None:
    base = ScoringConfig(custom=[_custom("Admins active", "admins_active", start=0, full=5)])
    parsed, errors = parse_scoring_form(form_values(base), base)
    assert errors == {}
    assert parsed is not None
    assert parsed.custom == base.custom


def test_reset_to_defaults_keeps_your_own_signals_and_says_so(
    client: TestClient, one_signal: str, config_dir: Path
) -> None:
    before = _saved_custom()
    text = client.get("/settings/scoring?defaults=1").text
    assert "Showing the defaults. Your own signals are kept. Save changes to use them." in text
    assert f'id="custom-signal-{one_signal}"' in text
    assert _saved_custom() == before  # showing the defaults saves nothing

    response = client.post(
        "/settings/scoring",
        data=form_values(ScoringConfig()),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    saved = load_scoring_config()
    assert saved.custom == before
    assert saved.thresholds.at_risk == ScoringConfig().thresholds.at_risk


# The rule sentence


@pytest.mark.parametrize(
    ("unit", "weight", "expected"),
    [
        ("%", 20, "Risk starts at 40%, maxed at 90%, up to 20 points."),
        ("calls", 1, "Risk starts at 40 calls, maxed at 90 calls, up to 1 point."),
        ("", 2.5, "Risk starts at 40, maxed at 90, up to 2.5 points."),
        ("calls", 30, "Risk starts at 40 calls, maxed at 90 calls, up to 30 points."),
    ],
)
def test_the_rule_sentence_reads_in_the_units_the_csm_chose(
    unit: str, weight: float, expected: str
) -> None:
    signal = CustomSignal(
        id="seats",
        name="Seats",
        unit=unit,
        rule=SignalRule(weight=weight, start=40, full=90),
    )
    assert form.rule_sentence(signal) == expected


def test_a_signal_with_a_thousands_value_gets_separators() -> None:
    signal = CustomSignal(
        id="calls", name="Calls", unit="calls", rule=SignalRule(weight=5, start=1200, full=9000)
    )
    assert form.rule_sentence(signal) == (
        "Risk starts at 1,200 calls, maxed at 9,000 calls, up to 5 points."
    )


# The form's own helpers


def test_a_new_signals_form_starts_blank_with_its_defaults() -> None:
    values = form.form_values(None)
    assert values[form.DIRECTION] == ""
    assert values[form.WEIGHT] == "10"
    assert values[form.STALE_DAYS] == "30"
    assert values[form.NAME] == ""


def test_index_of_finds_a_signal_by_its_id_only() -> None:
    listed = [_custom("A", "a_one"), _custom("B", "b_two")]
    assert form.index_of(listed, "b_two") == 1
    assert form.index_of(listed, "missing") is None
    assert form.index_of(listed, "B") is None


def test_listing_keeps_the_order_the_signals_were_added() -> None:
    listed = [_custom("Zebra", "zebra"), _custom("Apple", "apple")]
    assert [line.name for line in form.listing(listed)] == ["Zebra", "Apple"]


def test_the_escaped_name_appears_as_text(client: TestClient, config_dir: Path) -> None:
    _add(client, {"custom.name": "Logins <script>"})
    text = client.get("/settings/scoring").text
    assert "<script>" not in text
    assert html.escape("Logins <script>") in text
