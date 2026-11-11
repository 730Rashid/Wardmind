"""Settings > Overview: the setup check (each state, and a check that fails showing "Couldn't check
this" rather than a 500) and Find a setting (matching, no match, escaping, the length cap)."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import backup, service, vault
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.settings import Currency, DataSource, DateStyle, Settings, save_settings
from wardmind.web import app as web
from wardmind.web import formatting
from wardmind.web import settings_overview as overview

BASE = "http://127.0.0.1"
TODAY = date(2026, 11, 11)
DEFAULT_DISPLAY = (Currency.USD, DateStyle.DAY_MONTH)
ITEM = re.compile(
    r'<li class="overview-item overview-item--(\w+)">\s*<div class="overview-head">\s*'
    r'<h3 class="overview-title">([^<]*)</h3>\s*<p class="overview-mark">(.*?)</p>\s*</div>\s*'
    r'<p class="overview-state">([^<]*)</p>',
    re.DOTALL,
)
RESULT = re.compile(r'<li class="overview-result"><a href="([^"]+)">([^<]+)</a>')
SETTINGS_KEYS = (
    "profile",
    "appearance",
    "follow-ups",
    "note-templates",
    "scoring",
    "books",
    "connections",
    "mailbox",
    "backup",
    "privacy",
    "activity",
)


@pytest.fixture(autouse=True)
def display_defaults() -> Iterator[None]:
    """The display preferences are one module-level choice: each test starts and ends on the
    default, so no test leaves a currency behind for another."""
    yield
    formatting.use_display(*DEFAULT_DISPLAY)


@pytest.fixture
def config_dir(isolated_config_dir: Path) -> Path:
    """Settings are saved to the test's own config folder (see conftest.py)."""
    return isolated_config_dir


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url=BASE)


@pytest.fixture
def no_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "last_backup_at", lambda book_id: None)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _items(markup: str) -> list[tuple[str, str, str, str]]:
    """(state, title, mark, sentence) for each setup check, in page order."""
    return [
        (
            state,
            html.unescape(title),
            html.unescape(re.sub(r"<[^>]+>", "", mark)).strip(),
            html.unescape(sentence),
        )
        for state, title, mark, sentence in ITEM.findall(markup)
    ]


def _results(markup: str) -> list[tuple[str, str]]:
    """(href, title) for each Find a setting result, in page order."""
    return [(href, html.unescape(title)) for href, title in RESULT.findall(markup)]


def _titles(markup: str) -> list[str]:
    return [title for _, title in _results(markup)]


def _no_match(markup: str, query: str) -> bool:
    return f"No setting matches “{query}”. Try another word." in markup


def _by_key(checks: list[overview.SetupCheck]) -> dict[str, overview.SetupCheck]:
    return {check.key: check for check in checks}


# --- Each setup check's reading -----------------------------------------------------------------


def test_encryption_on_is_done_and_off_is_worth_turning_on() -> None:
    on = overview.encryption_reading(vault.State.ON)
    assert on == overview.Reading(
        overview.CheckState.DONE, "On. Your files are encrypted on this computer."
    )
    off = overview.encryption_reading(vault.State.PLAIN)
    assert off.state is overview.CheckState.TODO
    assert off.sentence.startswith("Off. Turn it on")


@pytest.mark.parametrize(
    ("state", "sentence"),
    [
        (vault.State.ENCRYPTING, "Turning on. Wait for it to finish."),
        (vault.State.DECRYPTING, "Turning off. Wait for it to finish."),
    ],
)
def test_encryption_being_changed_is_in_progress(state: vault.State, sentence: str) -> None:
    assert overview.encryption_reading(state) == overview.Reading(
        overview.CheckState.BUSY, sentence
    )


def test_recovery_key_is_set_or_worth_making() -> None:
    assert overview.recovery_reading(True).state is overview.CheckState.DONE
    missing = overview.recovery_reading(False)
    assert missing.state is overview.CheckState.TODO
    assert missing.sentence.startswith("Not set. Make one")


def test_auto_lock_never_is_worth_a_limit_and_a_limit_is_done() -> None:
    never = overview.auto_lock_reading(0)
    assert never == overview.Reading(
        overview.CheckState.TODO,
        "Never locks. Choose a limit, so Wardmind locks when you step away.",
    )
    assert overview.auto_lock_reading(15) == overview.Reading(
        overview.CheckState.DONE, "Locks after 15 minutes without use."
    )


def test_no_backup_yet_is_worth_making_one() -> None:
    assert overview.backup_reading(None, TODAY) == overview.Reading(
        overview.CheckState.TODO, "No backup yet. Make one before anything goes wrong."
    )


def test_a_backup_within_thirty_days_is_done_and_an_older_one_is_stale() -> None:
    made = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    day = made.astimezone().date()  # the local day, as the page shows it
    within = overview.backup_reading(made, day + timedelta(days=30))
    assert within == overview.Reading(overview.CheckState.DONE, f"Last made on {_long(day)}.")
    stale = overview.backup_reading(made, day + timedelta(days=31))
    assert stale.state is overview.CheckState.TODO
    assert stale.sentence == f"The last backup was made on {_long(day)}. Make a new one."


def test_a_naive_backup_time_is_read_as_utc() -> None:
    naive = datetime(2026, 11, 10, 12, 0)
    assert overview.backup_reading(naive, TODAY).state is overview.CheckState.DONE


def _long(day: date) -> str:
    return formatting.long_date(day)


@pytest.mark.parametrize(
    ("source", "connections", "state", "fragment"),
    [
        (DataSource.DEMO, 0, overview.CheckState.TODO, "Demo data is showing."),
        (DataSource.MINE, 0, overview.CheckState.TODO, "nothing is connected yet."),
        (DataSource.MINE, 1, overview.CheckState.DONE, "from 1 connection."),
        (DataSource.MINE, 3, overview.CheckState.DONE, "from 3 connections."),
    ],
)
def test_your_data_states(
    source: DataSource, connections: int, state: overview.CheckState, fragment: str
) -> None:
    reading = overview.your_data_reading(source, connections)
    assert reading.state is state
    assert fragment in reading.sentence


def test_connection_problems_count_the_failing_ones() -> None:
    assert overview.problems_reading(0) == overview.Reading(
        overview.CheckState.DONE, "Every connection loaded."
    )
    one = overview.problems_reading(1)
    assert one.state is overview.CheckState.TODO
    assert one.sentence.startswith("1 connection couldn't load.")
    assert overview.problems_reading(2).sentence.startswith("2 connections couldn't load.")


def test_mailbox_is_connected_or_worth_connecting() -> None:
    assert overview.mailbox_reading(True).state is overview.CheckState.DONE
    assert overview.mailbox_reading(False).state is overview.CheckState.TODO


def test_automatic_follow_ups_name_the_rules_that_are_on() -> None:
    none = overview.follow_ups_reading(Settings())
    assert none.state is overview.CheckState.TODO
    assert none.sentence.startswith("None are on.")
    renewals = overview.follow_ups_reading(Settings(rule_renewal=True))
    assert renewals == overview.Reading(overview.CheckState.DONE, "On for renewals.")
    every = overview.follow_ups_reading(
        Settings(rule_at_risk=True, rule_renewal=True, rule_champion_left=True)
    )
    assert every.sentence == "On for At risk accounts, renewals and a champion leaving."


def test_your_name_is_set_or_worth_adding() -> None:
    assert overview.name_reading("Ada Lovelace").state is overview.CheckState.DONE
    assert overview.name_reading("").state is overview.CheckState.TODO


# --- The whole check ----------------------------------------------------------------------------


def test_a_fresh_book_lists_what_is_worth_doing(no_backup: None) -> None:
    checks = _by_key(overview.setup_checks(TODAY))
    # Encryption is off, so its follow-on checks don't apply; demo data has no connections.
    assert set(checks) == {
        "encryption",
        "backup",
        "your-data",
        "mailbox",
        "follow-ups",
        "your-name",
    }
    assert all(check.state is overview.CheckState.TODO for check in checks.values())
    assert checks["encryption"].href == "/settings/privacy"
    assert checks["backup"].href == "/settings/backup"
    assert checks["your-data"].href == "/settings/connections"
    assert checks["mailbox"].href == "/settings/mailbox"
    assert checks["follow-ups"].href == "/settings/follow-ups"
    assert checks["your-name"].href == "/settings/profile"


def test_encryption_on_adds_recovery_and_auto_lock(
    monkeypatch: pytest.MonkeyPatch, config_dir: Path, no_backup: None
) -> None:
    monkeypatch.setattr(vault, "state", lambda: vault.State.ON)
    monkeypatch.setattr(
        vault, "load_keys", lambda: vault.KeyFile(vault.State.ON, 1, (), {"default": (b"", b"")})
    )
    save_settings(Settings(auto_lock_minutes=0))
    checks = _by_key(overview.setup_checks(TODAY))
    assert checks["encryption"].state is overview.CheckState.DONE
    assert checks["recovery"].state is overview.CheckState.TODO  # no recovery slot
    assert checks["auto-lock"].state is overview.CheckState.TODO  # never locks


def test_a_recovery_slot_and_a_limit_are_done(
    monkeypatch: pytest.MonkeyPatch, config_dir: Path, no_backup: None
) -> None:
    monkeypatch.setattr(vault, "state", lambda: vault.State.ON)
    slot = vault.Slot(vault.SlotKind.RECOVERY, {})
    keys = vault.KeyFile(vault.State.ON, 1, (slot,), {"default": (b"", b"")})
    monkeypatch.setattr(vault, "load_keys", lambda: keys)
    save_settings(Settings(auto_lock_minutes=30))
    checks = _by_key(overview.setup_checks(TODAY))
    assert checks["recovery"].state is overview.CheckState.DONE
    assert checks["auto-lock"].state is overview.CheckState.DONE


def test_encryption_off_leaves_out_its_follow_on_checks(no_backup: None) -> None:
    keys = set(_by_key(overview.setup_checks(TODAY)))
    assert "recovery" not in keys
    assert "auto-lock" not in keys


def test_a_recent_backup_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = datetime.now(UTC) - timedelta(days=2)
    monkeypatch.setattr(backup, "last_backup_at", lambda book_id: recent)
    check = _by_key(overview.setup_checks())["backup"]
    assert check.state is overview.CheckState.DONE
    assert check.mark == "Done"


def test_your_data_with_nothing_connected_is_worth_a_look(
    config_dir: Path, no_backup: None
) -> None:
    save_settings(Settings(source=DataSource.MINE))
    checks = _by_key(overview.setup_checks(TODAY))
    assert checks["your-data"].state is overview.CheckState.TODO
    assert "nothing is connected yet" in checks["your-data"].sentence
    # With no connection there is nothing that can fail, so the connections check is done.
    assert checks["connections"].state is overview.CheckState.DONE


def test_failing_connections_are_counted_when_your_data_shows(
    monkeypatch: pytest.MonkeyPatch, config_dir: Path, no_backup: None
) -> None:
    save_settings(Settings(source=DataSource.MINE))

    class _Combined:
        problems = [object(), object()]

    monkeypatch.setattr(service, "get_combined", lambda: _Combined())
    checks = _by_key(overview.setup_checks(TODAY))
    assert checks["connections"].state is overview.CheckState.TODO
    assert checks["connections"].sentence.startswith("2 connections couldn't load.")


def test_a_connected_mailbox_is_done(monkeypatch: pytest.MonkeyPatch, no_backup: None) -> None:
    monkeypatch.setattr(service, "mailbox_settings", lambda: object())
    assert _by_key(overview.setup_checks(TODAY))["mailbox"].state is overview.CheckState.DONE


def test_a_failing_check_is_contained_and_says_so(
    monkeypatch: pytest.MonkeyPatch, no_backup: None
) -> None:
    def broken(book_id: str) -> None:
        raise RuntimeError("the activity log is damaged")

    monkeypatch.setattr(backup, "last_backup_at", broken)
    checks = _by_key(overview.setup_checks(TODAY))
    assert checks["backup"].state is overview.CheckState.UNKNOWN
    assert checks["backup"].sentence == "Couldn't check this."
    assert checks["your-name"].state is overview.CheckState.TODO  # the others still run


def test_view_order_puts_things_worth_a_look_first_and_done_last() -> None:
    def check(key: str, state: overview.CheckState) -> overview.SetupCheck:
        return overview.SetupCheck(key, key, state, "m", "s", "/settings", "Settings")

    ordered = overview.view_order(
        [
            check("a", overview.CheckState.DONE),
            check("b", overview.CheckState.TODO),
            check("c", overview.CheckState.UNKNOWN),
            check("d", overview.CheckState.TODO),
            check("e", overview.CheckState.DONE),
        ]
    )
    assert [item.key for item in ordered] == ["b", "d", "c", "a", "e"]


@pytest.mark.parametrize(
    ("states", "sentence"),
    [
        ([overview.CheckState.TODO], "1 thing is worth a look."),
        (
            [overview.CheckState.TODO] * 2 + [overview.CheckState.DONE] * 3,
            "2 things are worth a look. 3 are already in place.",
        ),
        ([overview.CheckState.DONE], "Nothing needs a look. 1 is already in place."),
        (
            [overview.CheckState.DONE, overview.CheckState.UNKNOWN],
            "1 is already in place. 1 check couldn't run.",
        ),
    ],
)
def test_summary_says_how_many_things_are_worth_a_look(
    states: list[overview.CheckState], sentence: str
) -> None:
    checks = [
        overview.SetupCheck(str(index), "t", state, "m", "s", "/settings", "Settings")
        for index, state in enumerate(states)
    ]
    assert overview.summary_sentence(checks) == sentence


# --- The overview page --------------------------------------------------------------------------


def test_settings_opens_on_overview(client: TestClient) -> None:
    response = client.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/overview"


def test_the_overview_lists_worth_a_look_before_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    monkeypatch.setattr(backup, "last_backup_at", lambda book_id: datetime.now(UTC))
    save_settings(Settings(your_name="Ada Lovelace", rule_renewal=True))
    response = client.get("/settings/overview")
    assert response.status_code == 200
    items = _items(_text(response))
    states = [state for state, *_ in items]
    worth = [index for index, state in enumerate(states) if state == "todo"]
    done = [index for index, state in enumerate(states) if state == "done"]
    assert worth and done
    assert max(worth) < min(done)
    marks = {mark for _, _, mark, _ in items}
    assert {"Worth a look", "Done"} <= marks


def test_done_items_carry_a_check_icon_as_well_as_the_word(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    monkeypatch.setattr(backup, "last_backup_at", lambda book_id: datetime.now(UTC))
    save_settings(Settings(your_name="Ada"))
    marks = re.findall(
        r'<p class="overview-mark">(.*?)</p>', client.get("/settings/overview").text, re.DOTALL
    )
    done = [mark for mark in marks if "Done" in mark]
    assert done
    assert all('href="/static/icons.svg#check"' in mark for mark in done)


def test_each_item_links_to_the_page_that_changes_it(client: TestClient) -> None:
    markup = client.get("/settings/overview").text
    links = re.findall(r'<a href="([^"]+)">Go to ([^<]+)</a>', markup)
    assert ("/settings/privacy", "Data &amp; privacy") in links
    assert ("/settings/backup", "Backup &amp; export") in links
    assert ("/settings/profile", "Profile &amp; region") in links


def test_a_failing_check_shows_couldnt_check_this_not_a_server_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(book_id: str) -> None:
        raise RuntimeError("the activity log is damaged")

    monkeypatch.setattr(backup, "last_backup_at", broken)
    response = client.get("/settings/overview")
    assert response.status_code == 200
    assert ("unknown", "Backup", "Not checked", "Couldn't check this.") in _items(_text(response))


def test_the_find_box_is_a_plain_get_form_with_a_capped_input(client: TestClient) -> None:
    markup = client.get("/settings/overview").text
    assert (
        '<form class="overview-find" method="get" action="/settings/overview" role="search">'
        in markup
    )
    assert 'name="q"' in markup
    assert 'maxlength="100"' in markup
    assert 'for="field-find-setting">Find a setting</label>' in markup


def test_a_blank_search_lists_nothing(client: TestClient) -> None:
    markup = client.get("/settings/overview?q=").text
    assert _results(markup) == []
    assert "No setting matches" not in markup


def test_several_words_must_all_match(client: TestClient) -> None:
    markup = client.get("/settings/overview", params={"q": "dark mode"}).text
    assert _results(markup) == [("/settings/appearance", "Theme")]


def test_a_word_finds_every_setting_that_has_it(client: TestClient) -> None:
    markup = client.get("/settings/overview", params={"q": "automatic"}).text
    assert _titles(markup) == [
        "Automatic follow-ups for at risk accounts",
        "Automatic follow-ups before renewals",
        "Automatic follow-ups when a champion leaves",
    ]


def test_a_typed_word_matches_the_start_of_a_word(client: TestClient) -> None:
    assert "Password" in _titles(client.get("/settings/overview", params={"q": "pass"}).text)


def test_the_search_ignores_case(client: TestClient) -> None:
    upper = client.get("/settings/overview", params={"q": "PASSWORD"}).text
    lower = client.get("/settings/overview", params={"q": "password"}).text
    assert _results(upper) == _results(lower)
    assert _results(upper)


@pytest.mark.parametrize("query", ["pounds", "currency", "£", "dollars"])
def test_money_words_and_symbols_find_the_currency_setting(client: TestClient, query: str) -> None:
    assert "Currency" in _titles(client.get("/settings/overview", params={"q": query}).text)


def test_a_search_with_no_match_says_so(client: TestClient) -> None:
    markup = client.get("/settings/overview", params={"q": "xylophone"}).text
    assert _results(markup) == []
    assert _no_match(markup, "xylophone")


def test_a_search_with_no_words_in_it_says_so(client: TestClient) -> None:
    markup = client.get("/settings/overview", params={"q": "!!!"}).text
    assert _no_match(markup, "!!!")


def test_hostile_text_in_the_search_is_escaped(client: TestClient) -> None:
    hostile = "<script>alert(1)</script>"
    response = client.get("/settings/overview", params={"q": hostile})
    assert "<script>alert(1)" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert _no_match(_text(response), hostile)


def test_an_overlong_search_is_cut_to_one_hundred_characters(client: TestClient) -> None:
    markup = client.get("/settings/overview", params={"q": "a" * 150}).text
    assert f'value="{"a" * 100}"' in markup
    assert "a" * 101 not in markup
    assert _no_match(markup, "a" * 100)


def test_the_index_points_only_at_real_pages(client: TestClient) -> None:
    paths = {getattr(route, "path", "") for route in client.app.routes}  # type: ignore[attr-defined]
    for entry in overview.SETTINGS_INDEX:
        assert entry.href in paths, entry.title


def test_every_settings_page_has_something_to_find() -> None:
    hrefs = {entry.href for entry in overview.SETTINGS_INDEX}
    for key in SETTINGS_KEYS:
        assert f"/settings/{key}" in hrefs, key


def test_find_settings_matches_titles_sections_and_words() -> None:
    assert overview.find_settings("") == []
    assert overview.find_settings("   ") == []
    assert overview.find_settings("!!!") == []
    assert "Theme" in [entry.title for entry in overview.find_settings("appearance")]
    assert "Auto-lock" in [entry.title for entry in overview.find_settings("lock")]
    assert "Export accounts as CSV" in [entry.title for entry in overview.find_settings("csv")]


def test_find_settings_reads_only_the_first_hundred_characters() -> None:
    assert overview.find_settings("dark " + "x" * 200) == []
    assert [entry.title for entry in overview.find_settings("dark " + " " * 200)] == ["Theme"]


def test_find_settings_reads_at_most_eight_words() -> None:
    # The ninth word would match nothing; it must be ignored, not searched.
    query = " ".join(["dark"] * 8 + ["xylophone"])
    assert [entry.title for entry in overview.find_settings(query)] == ["Theme"]
