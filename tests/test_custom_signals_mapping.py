"""Your own signals in the data: a spreadsheet column matched by its name, a SIGNALS feed's column,
a reading typed on an account, a saved mapping whose signal was removed, the matching pages'
"Your own signals" group, and a custom value that isn't a number reported like the others.

Every test runs against the temp config and data folders from conftest.py, so nothing reaches
the real config/ folder or the CSM's data.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from wardmind.core import editing, import_flow, importing, service
from wardmind.core.config import (
    CustomSignal,
    ScoringConfig,
    SignalRule,
    load_scoring_config,
    save_scoring_config,
)
from wardmind.core.feeds import setup as feed_setup
from wardmind.core.feeds import signals
from wardmind.core.feeds.models import FeedRole, MatchBy, Schedule
from wardmind.core.importing import DateOrder, FieldType, Table
from wardmind.core.models import Account, CustomKind, Milestones, SignalKind
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.web import app as web

NOW = datetime(2026, 11, 12, 9, 0)
TODAY = date(2026, 11, 12)
ORIGIN = {"origin": "http://127.0.0.1"}
API_HEADER = "API calls per week"
API_KIND = CustomKind.of("api_calls")
API_FIELD = "signal:custom:api_calls"
API = CustomSignal(
    id="api_calls",
    name=API_HEADER,
    unit="calls",
    rule=SignalRule(weight=20, start=0, full=500),
)
LARKSPUR_NAME = "Larkspur Freight"
LARKSPUR_ID = "sp1-larkspur-freight"  # the id a spreadsheet connection "sheet-1" gives it


def _book(*custom: CustomSignal) -> None:
    """Make the open book's own signals exactly these (none when called with no arguments)."""
    save_scoring_config(ScoringConfig(custom=list(custom)))


def _table(headers: list[str], *rows: list[str]) -> Table:
    return Table(source_name="renewals.csv", headers=headers, rows=[list(row) for row in rows])


def _accounts_table(api_value: str = "1,200") -> Table:
    return _table(
        ["Account name", "ARR", "Renewal date", API_HEADER],
        [LARKSPUR_NAME, "$184,000", "2026-12-01", api_value],
        ["Quillon Health", "$96,500", "2026-11-15", "12%"],
    )


def _import_table(store: Any, table: Table, *, target: str = "new") -> Any:
    """Save a spreadsheet draft and commit it with the suggested mapping, as the import page
    does. Returns the commit's result."""
    draft = import_flow.save_draft(store, table=table, new_name="Renewals", target=target)
    mapping = import_flow.suggested_mapping(draft, store)
    return import_flow.commit(draft, mapping, DateOrder.DMY, store, now=NOW, today=TODAY)


def _account(acc_id: str, name: str) -> Account:
    return Account(
        id=acc_id,
        name=name,
        arr=50_000.0,
        tier="Growth",
        owner="Dana Whitfield",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
    )


def _feed_table(headers: list[str], *rows: list[str]) -> Table:
    return Table(source_name="feed.csv", headers=headers, rows=[list(row) for row in rows])


def _own_readings(readings: list[Any]) -> dict[str, float]:
    """account id -> value, for the readings of the own signal only."""
    return {signal.account_id: signal.value for signal in readings if signal.kind == API_KIND}


# The import fields and the auto-match ----------------------------------------------------------


def test_the_book_own_signal_is_an_import_field_after_the_built_in_ones() -> None:
    fields = importing.import_fields(ScoringConfig(custom=[API]))

    assert fields[: len(importing.IMPORT_FIELDS)] == importing.IMPORT_FIELDS
    own = fields[-1]
    assert own.key == API_FIELD
    assert own.label == API_HEADER
    assert own.help == "One of your own signals, in calls"
    assert own.type is FieldType.NUMBER
    assert own.required is False


def test_a_signal_without_a_unit_says_only_that_it_is_one_of_your_own() -> None:
    plain = CustomSignal(
        id="reports", name="Reports exported", rule=SignalRule(weight=5, start=0, full=3)
    )

    own = importing.import_fields(ScoringConfig(custom=[plain]))[-1]

    assert own.key == "signal:custom:reports"
    assert own.help == "One of your own signals"


def test_without_own_signals_the_fields_are_the_built_in_ones() -> None:
    assert importing.import_fields(ScoringConfig()) == importing.IMPORT_FIELDS


def test_no_config_reads_the_open_books_own_signals() -> None:
    _book(API)

    assert importing.import_fields()[-1].key == API_FIELD
    assert load_scoring_config().custom == [API]


def test_a_column_with_the_signals_name_matches_it_whatever_the_case_and_spacing() -> None:
    _book(API)

    exact = importing.auto_map(["Account name", "ARR", "Renewal date", API_HEADER])
    spelled = importing.auto_map(["Account name", "ARR", "Renewal date", "api  calls   PER week"])

    assert exact[API_FIELD] == API_HEADER
    assert spelled[API_FIELD] == "api  calls   PER week"


def test_a_header_that_only_contains_the_name_does_not_match_it() -> None:
    _book(API)

    mapping = importing.auto_map(["Account name", "ARR", "Renewal date", "API calls"])

    assert API_FIELD not in mapping


def test_signal_kind_for_field_reads_built_in_and_own_fields() -> None:
    assert importing.signal_kind_for_field("signal:nps_latest") is SignalKind.NPS_LATEST
    assert importing.signal_kind_for_field(API_FIELD) == API_KIND
    assert importing.signal_kind_for_field("name") is None
    assert importing.signal_kind_for_field("signal:custom:Not Valid") is None


# Spreadsheet import -----------------------------------------------------------------------------


def test_a_spreadsheet_api_calls_column_imports_as_custom_readings() -> None:
    _book(API)
    table = _accounts_table()

    result = importing.build_import(
        table,
        importing.auto_map(table.headers),
        connection_id="sheet-1",
        date_order=DateOrder.DMY,
        imported_at=NOW,
        today=TODAY,
    )

    assert result.ready_rows == 2
    assert result.issues == []
    own = [signal for signal in result.signals if signal.kind == API_KIND]
    assert {signal.account_id: signal.value for signal in own} == {
        LARKSPUR_ID: 1200.0,
        "sp1-quillon-health": 12.0,
    }
    assert {signal.source for signal in own} == {"spreadsheet"}


def test_a_committed_import_keeps_the_custom_readings_in_the_snapshot() -> None:
    _book(API)
    store = service.get_store()

    result = _import_table(store, _accounts_table())

    snapshot = store.snapshot(result.connection.id)
    assert snapshot is not None
    assert sorted(_own_readings(snapshot.signals).values()) == [12.0, 1200.0]


def test_the_suggested_mapping_includes_the_own_signal_column() -> None:
    _book(API)
    store = service.get_store()
    draft = import_flow.save_draft(store, table=_accounts_table(), new_name="Renewals")

    assert import_flow.suggested_mapping(draft, store)[API_FIELD] == API_HEADER


def test_numbers_with_separators_and_a_percent_read_like_a_built_in_number() -> None:
    _book(API)
    table = _table(
        ["Account name", "ARR", "Renewal date", API_HEADER],
        ["Acme", "$1", "2026-12-01", "1,200"],
        ["Brindle", "$1", "2026-12-01", "12%"],
    )

    result = importing.build_import(
        table,
        importing.auto_map(table.headers),
        connection_id="sheet-1",
        date_order=DateOrder.DMY,
        imported_at=NOW,
        today=TODAY,
    )

    assert _own_readings(result.signals) == {"sp1-acme": 1200.0, "sp1-brindle": 12.0}


def test_a_custom_value_that_is_not_a_number_is_an_issue_like_the_others() -> None:
    _book(API)
    table = _table(
        ["Account name", "ARR", "Renewal date", "Usage trend", API_HEADER],
        ["Acme", "$1", "2026-12-01", "-5", "lots"],
    )

    result = importing.build_import(
        table,
        importing.auto_map(table.headers),
        connection_id="sheet-1",
        date_order=DateOrder.DMY,
        imported_at=NOW,
        today=TODAY,
    )

    assert result.ready_rows == 1
    assert result.skipped_rows == 0
    assert [(issue.row, issue.field, issue.message) for issue in result.issues] == [
        (2, API_FIELD, 'API calls per week "lots" isn\'t a number.')
    ]
    assert [signal.kind.value for signal in result.signals] == ["usage_trend_30d"]


def test_a_number_too_long_for_a_float_is_refused_as_not_a_number() -> None:
    _book(API)
    table = _table(
        ["Account name", "ARR", "Renewal date", API_HEADER],
        ["Acme", "$1", "2026-12-01", "1" + "0" * 400],
    )

    result = importing.build_import(
        table,
        importing.auto_map(table.headers),
        connection_id="sheet-1",
        date_order=DateOrder.DMY,
        imported_at=NOW,
        today=TODAY,
    )

    assert [issue.field for issue in result.issues] == [API_FIELD]
    assert "isn't a number." in result.issues[0].message
    assert _own_readings(result.signals) == {}


def test_a_blank_custom_cell_gives_no_reading_and_no_issue() -> None:
    _book(API)
    table = _table(
        ["Account name", "ARR", "Renewal date", API_HEADER], ["Acme", "$1", "2026-12-01", ""]
    )

    result = importing.build_import(
        table,
        importing.auto_map(table.headers),
        connection_id="sheet-1",
        date_order=DateOrder.DMY,
        imported_at=NOW,
        today=TODAY,
    )

    assert result.issues == []
    assert _own_readings(result.signals) == {}


# SIGNALS feeds ----------------------------------------------------------------------------------


def test_a_signals_feed_row_maps_to_the_custom_signal() -> None:
    _book(API)
    feed = _feed_table(["Name", API_HEADER], [LARKSPUR_NAME, "1,200"])

    readings = signals.build_signal_readings(
        feed,
        {"name": "Name", API_FIELD: API_HEADER},
        match_by=MatchBy.NAME,
        accounts=[_account(LARKSPUR_ID, LARKSPUR_NAME)],
        date_order=DateOrder.DMY,
        observed_at=NOW,
        source="feed:feed-1",
    )

    assert readings.issues == []
    assert readings.matched_accounts == 1
    assert [(s.account_id, s.kind, s.value) for s in readings.signals] == [
        (LARKSPUR_ID, API_KIND, 1200.0)
    ]


def test_a_signals_feed_reports_a_custom_value_that_is_not_a_number() -> None:
    _book(API)
    feed = _feed_table(["Name", API_HEADER], [LARKSPUR_NAME, "lots"])

    readings = signals.build_signal_readings(
        feed,
        {"name": "Name", API_FIELD: API_HEADER},
        match_by=MatchBy.NAME,
        accounts=[_account(LARKSPUR_ID, LARKSPUR_NAME)],
        date_order=DateOrder.DMY,
        observed_at=NOW,
        source="feed:feed-1",
    )

    assert readings.signals == []
    assert [(issue.row, issue.field, issue.message) for issue in readings.issues] == [
        (2, API_FIELD, 'API calls per week "lots" isn\'t a number.')
    ]


def test_a_signals_feed_setup_keeps_and_previews_the_own_signal() -> None:
    _book(API)
    feed = _feed_table(["Name", API_HEADER], [LARKSPUR_NAME, "1,200"])
    draft = feed_setup.FeedDraft(
        id="0123456789abcdef",
        created_at=NOW,
        provider="csv",
        name="Usage feed",
        role=FeedRole.SIGNALS,
        settings={},
        schedule=Schedule.MANUAL,
        match_by=MatchBy.NAME,
        table=feed,
    )
    accounts = [_account(LARKSPUR_ID, LARKSPUR_NAME)]

    suggested = feed_setup.suggested_mapping(draft)
    preview = feed_setup.preview(
        draft, suggested, None, book_accounts=lambda _id: accounts, now=NOW, today=TODAY
    )

    assert suggested == {"name": "Name", API_FIELD: API_HEADER}
    assert preview.blocker is None
    assert preview.accounts == 1


# A saved mapping whose signal was removed -------------------------------------------------------


def test_a_saved_mapping_to_a_removed_signal_is_skipped_without_error() -> None:
    _book()  # the book no longer has the API calls signal
    table = _table(
        ["Account name", "ARR", "Renewal date", "Usage trend", API_HEADER],
        ["Acme", "$1", "2026-12-01", "-5", "1,200"],
    )
    saved = {
        "name": "Account name",
        "arr": "ARR",
        "renewal_date": "Renewal date",
        "signal:usage_trend_30d": "Usage trend",
        API_FIELD: API_HEADER,
    }

    result = importing.build_import(
        table,
        saved,
        connection_id="sheet-1",
        date_order=DateOrder.DMY,
        imported_at=NOW,
        today=TODAY,
    )

    assert result.ready_rows == 1
    assert result.issues == []
    assert [signal.kind.value for signal in result.signals] == ["usage_trend_30d"]
    assert API_FIELD not in import_flow.clean_mapping(table, saved)


def test_a_removed_signal_in_a_saved_feed_mapping_is_skipped_without_error() -> None:
    _book()
    feed = _feed_table(["Name", "Usage trend", API_HEADER], [LARKSPUR_NAME, "-5", "1,200"])

    readings = signals.build_signal_readings(
        feed,
        {"name": "Name", "signal:usage_trend_30d": "Usage trend", API_FIELD: API_HEADER},
        match_by=MatchBy.NAME,
        accounts=[_account(LARKSPUR_ID, LARKSPUR_NAME)],
        date_order=DateOrder.DMY,
        observed_at=NOW,
        source="feed:feed-1",
    )

    assert readings.issues == []
    assert [signal.kind.value for signal in readings.signals] == ["usage_trend_30d"]


def test_a_re_import_after_the_signal_is_removed_still_commits() -> None:
    _book(API)
    store = service.get_store()
    first = _import_table(store, _accounts_table())
    _book()  # the CSM removes the signal, then imports the same sheet again

    draft = import_flow.save_draft(
        store, table=_accounts_table(), new_name="", target=first.connection.id
    )
    mapping = import_flow.suggested_mapping(draft, store)
    again = import_flow.commit(draft, mapping, DateOrder.DMY, store, now=NOW, today=TODAY)

    assert API_FIELD not in mapping
    assert again.ready_rows == 2
    assert again.removed == 0


# Typed readings ---------------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def your_data() -> str:
    """Your data, with one imported account (Larkspur Freight). Returns its id."""
    save_settings(Settings(source=DataSource.MINE))
    store = service.get_store()
    result = _import_table(
        store,
        _table(["Account name", "ARR", "Renewal date"], [LARKSPUR_NAME, "$184,000", "2026-12-01"]),
    )
    snapshot = store.snapshot(result.connection.id)
    assert snapshot is not None
    return snapshot.accounts[0].id


def test_the_reading_form_lists_the_own_signal_after_the_built_in_ones(
    client: TestClient, your_data: str
) -> None:
    _book(API)

    page = html.unescape(client.get(f"/accounts/{your_data}/edit").text)

    select = re.search(r'<select id="reading-kind".*?</select>', page, re.DOTALL)
    assert select is not None
    options = re.findall(
        r'<option value="([^"]+)"[^>]*data-reading-unit="([^"]*)"[^>]*>([^<]*)</option>',
        select.group(0),
    )
    values = [value for value, _unit, _label in options]
    assert values[:-1] == [kind.value for kind in SignalKind]
    assert values[-1] == "custom:api_calls"
    assert options[-1] == ("custom:api_calls", "calls", API_HEADER)
    assert ("usage_trend_30d", "", "Usage trend") in options


def test_a_typed_reading_for_the_own_signal_saves_and_shows_on_the_account(
    client: TestClient, your_data: str
) -> None:
    _book(API)

    posted = client.post(
        f"/accounts/{your_data}/readings",
        data={"kind": "custom:api_calls", "value": "1,200", "yes_no": "yes"},
        headers=ORIGIN,
        follow_redirects=False,
    )

    assert posted.status_code == 303
    saved = [signal for signal in service.get_store().manual().signals if signal.kind == API_KIND]
    assert [(signal.value, signal.source) for signal in saved] == [(1200.0, "manual")]
    page = html.unescape(client.get(f"/accounts/{your_data}?reading=1").text)
    assert "Reading added." in page
    assert API_HEADER in page


def test_a_typed_reading_is_a_plain_finite_number(your_data: str) -> None:
    _book(API)
    store = service.get_store()
    connector = service.get_connector()

    with pytest.raises(editing.EditError, match='API calls per week "lots" isn\'t a number.'):
        editing.add_reading(store, connector, your_data, API_KIND, "lots", now=NOW)
    with pytest.raises(editing.EditError, match="can't be empty"):
        editing.add_reading(store, connector, your_data, API_KIND, "  ", now=NOW)
    with pytest.raises(editing.EditError, match="isn't a number"):
        editing.add_reading(store, connector, your_data, API_KIND, "1" * 400, now=NOW)
    assert [signal for signal in store.manual().signals if signal.kind == API_KIND] == []

    editing.add_reading(store, connector, your_data, "custom:api_calls", "-3.5", now=NOW)
    assert [s.value for s in store.manual().signals if s.kind == API_KIND] == [-3.5]


def test_a_reading_for_a_signal_the_book_does_not_have_is_refused(
    client: TestClient, your_data: str
) -> None:
    _book()  # no own signals, so the kind is unknown

    refused = client.post(
        f"/accounts/{your_data}/readings",
        data={"kind": "custom:api_calls", "value": "5", "yes_no": "yes"},
        headers=ORIGIN,
        follow_redirects=False,
    )

    assert refused.status_code == 422
    assert "That signal isn't one Wardmind knows." in html.unescape(refused.text)
    assert [s for s in service.get_store().manual().signals if s.kind == API_KIND] == []


# The matching pages -----------------------------------------------------------------------------


def _render(template: str, **context: Any) -> str:
    """Render one of the matching templates on its own. The page layout is replaced by an empty
    block; every other template is the app's own."""
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(web.TEMPLATES_DIR),
            ]
        ),
        autoescape=True,
    )
    env.filters["idpath"] = web.idpath
    return env.get_template(template).render(**context)


def _import_page(rows: list[dict[str, Any]]) -> str:
    return _render(
        "import/map.html",
        draft_id="0123456789abcdef",
        draft=SimpleNamespace(source_name="renewals.csv", table=SimpleNamespace(sheet=None)),
        sheet_names=None,
        error="",
        row_count=2,
        rows=rows,
        headers=["Account name", API_HEADER],
        example_row=2,
        show_date_order=False,
        date_choice="dmy",
        accounts="0 accounts",
        preview=SimpleNamespace(
            ready_rows=0, skipped_rows=0, issues=[], removed_ids=[], blocker="Match ARR."
        ),
    )


def test_the_import_match_page_lists_the_own_signals_in_their_own_group() -> None:
    _book(API)
    rows = import_flow.match_rows({}, {})

    markup = _import_page(rows)

    built_in = markup.index(">Days invoice overdue</label>")
    heading = markup.index("Your own signals")
    own = markup.index(f">{API_HEADER}</label>")
    assert markup.count("Your own signals") == 1
    assert built_in < heading < own
    assert [row["custom"] for row in rows].count(True) == 1


def test_the_connection_match_page_lists_the_own_signals_in_their_own_group() -> None:
    markup = _render(
        "connections/match.html",
        name="Usage feed",
        provider_name="Usage feed",
        draft_id="0123456789abcdef",
        error="",
        row_count=1,
        match_label="",
        headers=["Name", API_HEADER],
        example_row=2,
        show_date_order=False,
        date_choice="dmy",
        happens=SimpleNamespace(
            headline="", skipped="", unmatched_heading="", unmatched=(), issues=()
        ),
        blocker="",
        oob=False,
        rows=[
            {
                "index": 0,
                "key": "name",
                "label": "Account name",
                "help": "",
                "required": True,
                "custom": False,
                "selected": "Name",
                "example": LARKSPUR_NAME,
            },
            {
                "index": 1,
                "key": API_FIELD,
                "label": API_HEADER,
                "help": "",
                "required": False,
                "custom": True,
                "selected": API_HEADER,
                "example": "1,200",
            },
        ],
    )

    assert markup.count("Your own signals") == 1
    assert markup.index(">Account name</label>") < markup.index("Your own signals")
    assert markup.index("Your own signals") < markup.index(f">{API_HEADER}</label>")


def test_without_own_signals_no_group_heading_is_shown() -> None:
    _book()
    rows = import_flow.match_rows({}, {})

    markup = _import_page(rows)

    assert "Your own signals" not in markup
    assert [row["custom"] for row in rows].count(True) == 0


def test_match_rows_puts_the_own_signal_last_with_its_column_and_example() -> None:
    _book(API)
    mapping = {"name": "Account name", API_FIELD: API_HEADER}

    rows = import_flow.match_rows(mapping, {"name": LARKSPUR_NAME, API_FIELD: "1,200"})

    assert [row["key"] for row in rows[: len(importing.IMPORT_FIELDS)]] == [
        spec.key for spec in importing.IMPORT_FIELDS
    ]
    own = rows[-1]
    assert own["index"] == len(importing.IMPORT_FIELDS)
    assert own["custom"] is True
    assert own["label"] == API_HEADER
    assert own["selected"] == API_HEADER
    assert own["example"] == "1,200"
