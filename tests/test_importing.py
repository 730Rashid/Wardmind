"""Parsers, mapping, building, merging, the template and the issues download for spreadsheet
imports. File reading lives in test_importing_readers.py."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cs_brief.core import importing
from cs_brief.core.importing import (
    IMPORT_FIELDS,
    SIGNAL_FIELD_KINDS,
    DateOrder,
    FieldType,
    ImportIssue,
    Table,
    auto_map,
    build_import,
    detect_date_order,
    issues_csv,
    merge_into_snapshot_signals,
    parse_bool,
    parse_date,
    parse_money,
    parse_number,
    read_csv,
    template_csv,
)
from cs_brief.core.models import Milestones, Signal, SignalKind

FIXTURES = Path(__file__).parent / "fixtures" / "import"
NOW = datetime(2026, 10, 22, 9, 0)
TODAY = date(2026, 10, 22)
DMY = DateOrder.DMY
MDY = DateOrder.MDY
YMD = DateOrder.YMD


def _table(headers: list[str], rows: list[list[str]]) -> Table:
    return Table(source_name="test.csv", headers=headers, rows=rows)


def _run(
    headers: list[str],
    rows: list[list[str]],
    *,
    mapping: dict[str, str] | None = None,
    order: DateOrder = DMY,
    connection_id: str = "sheet-1",
    imported_at: datetime = NOW,
):
    table = _table(headers, rows)
    return build_import(
        table,
        auto_map(headers) if mapping is None else mapping,
        connection_id=connection_id,
        date_order=order,
        imported_at=imported_at,
        today=TODAY,
    )


BASE = ["Account name", "ARR", "Renewal date"]


def _messages(result) -> list[str]:
    return [issue.message for issue in result.issues]


def _kinds(signals: list[Signal]) -> dict[str, float]:
    return {signal.kind.value: signal.value for signal in signals}


# ------------------------------------------------------------------------------------ money


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120,000", 120000.0),
        ("120000", 120000.0),
        ("120k", 120000.0),
        ("120K", 120000.0),
        ("1.2m", 1_200_000.0),
        ("2.5M", 2_500_000.0),
        ("1.5bn", 1_500_000_000.0),
        ("€120.000,50", 120000.5),
        ("120 000", 120000.0),
        ("120 000", 120000.0),
        ("£96.5k", 96500.0),
        ("GBP 1,200.75", 1200.75),
        ("USD120k", 120000.0),
        ("120,000 EUR", 120000.0),
        ("¥1,000", 1000.0),
        ("1,000", 1000.0),
        ("1.000", 1000.0),
        ("12.50", 12.5),
        ("1,5", 1.5),
        ("1,5k", 1500.0),
        ("0", 0.0),
        ("  $ 5 ,000  ", 5000.0),
    ],
)
def test_parse_money_accepts(text, expected):
    assert parse_money(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "is empty."),
        ("   ", "is empty."),
        ("-5", '"-5" can\'t be negative.'),
        ("−5", '"−5" can\'t be negative.'),
        ("$-5", '"$-5" can\'t be negative.'),
        ("(1,000)", '"(1,000)" isn\'t an amount.'),
        ("TBC", '"TBC" isn\'t an amount.'),
        ("k", '"k" isn\'t an amount.'),
        ("1.2.3", '"1.2.3" isn\'t an amount.'),
        ("12,50,000", '"12,50,000" isn\'t an amount.'),
        ("1e6", '"1e6" isn\'t an amount.'),
        ("12a", '"12a" isn\'t an amount.'),
    ],
)
def test_parse_money_refuses(text, message):
    with pytest.raises(ValueError) as caught:
        parse_money(text)
    assert str(caught.value) == message


# ------------------------------------------------------------------------------------ dates


@pytest.mark.parametrize(
    ("text", "order", "expected"),
    [
        ("2026-12-01", DMY, date(2026, 12, 1)),
        ("2026-12-01", MDY, date(2026, 12, 1)),
        ("2026-12-01", YMD, date(2026, 12, 1)),
        ("2026/12/01", DMY, date(2026, 12, 1)),
        ("2026-12-01 00:00:00", DMY, date(2026, 12, 1)),
        ("2026-12-01T00:00:00.000Z", MDY, date(2026, 12, 1)),
        ("2026-12-01 10:30 PM", DMY, date(2026, 12, 1)),
        ("31/12/2026", DMY, date(2026, 12, 31)),
        ("12/31/2026", MDY, date(2026, 12, 31)),
        ("03/04/2026", DMY, date(2026, 4, 3)),
        ("03/04/2026", MDY, date(2026, 3, 4)),
        ("03-04-2026", DMY, date(2026, 4, 3)),
        ("03.04.2026", DMY, date(2026, 4, 3)),
        ("03 04 2026", MDY, date(2026, 3, 4)),
        ("31/12/26", DMY, date(2026, 12, 31)),
        ("01/12/26", MDY, date(2026, 1, 12)),
        ("1 Dec 2026", DMY, date(2026, 12, 1)),
        ("1 Dec 2026", MDY, date(2026, 12, 1)),
        ("Dec 1, 2026", DMY, date(2026, 12, 1)),
        ("Dec 1, 2026", MDY, date(2026, 12, 1)),
        ("1 December 2026", MDY, date(2026, 12, 1)),
        ("December 1st, 2026", DMY, date(2026, 12, 1)),
        ("1st December 2026", DMY, date(2026, 12, 1)),
        ("1-Dec-26", DMY, date(2026, 12, 1)),
        ("Sept 5 2026", DMY, date(2026, 9, 5)),
        ("5 Sep 2026", MDY, date(2026, 9, 5)),
        ("2026-02-28", DMY, date(2026, 2, 28)),
        ("29/02/2028", DMY, date(2028, 2, 29)),
        ("46357", DMY, date(2026, 12, 1)),
        ("46357", MDY, date(2026, 12, 1)),
        ("46357.5", DMY, date(2026, 12, 1)),
        ("20000", DMY, date(1899, 12, 30) + timedelta(days=20000)),
        ("79999", MDY, date(1899, 12, 30) + timedelta(days=79999)),
        ("80000", DMY, date(1899, 12, 30) + timedelta(days=80000)),
        ("20261201", DMY, date(2026, 12, 1)),
        ("  2026-12-01  ", DMY, date(2026, 12, 1)),
    ],
)
def test_parse_date_accepts(text, order, expected):
    assert parse_date(text, order) == expected


@pytest.mark.parametrize(
    ("text", "order", "message"),
    [
        ("31/31/2026", DMY, '"31/31/2026" isn\'t a date.'),
        ("31/12/2026", MDY, '"31/12/2026" isn\'t a date.'),
        ("12/31/2026", DMY, '"12/31/2026" isn\'t a date.'),
        ("2026-02-30", DMY, '"2026-02-30" isn\'t a date.'),
        ("29/02/2027", DMY, '"29/02/2027" isn\'t a date.'),
        ("03/04/2026", YMD, '"03/04/2026" isn\'t a date.'),
        ("12345", DMY, '"12345" isn\'t a date.'),
        ("19999", DMY, '"19999" isn\'t a date.'),
        ("80001", DMY, '"80001" isn\'t a date.'),
        ("Foo 1 2026", DMY, '"Foo 1 2026" isn\'t a date.'),
        ("1 Dec", DMY, '"1 Dec" isn\'t a date.'),
        ("Dec 2026", DMY, '"Dec 2026" isn\'t a date.'),
        ("TBC", DMY, '"TBC" isn\'t a date.'),
        ("", DMY, "is empty."),
    ],
)
def test_parse_date_refuses(text, order, message):
    with pytest.raises(ValueError) as caught:
        parse_date(text, order)
    assert str(caught.value) == message


# ------------------------------------------------------------------------- bools and numbers


@pytest.mark.parametrize("text", ["yes", "Y", "TRUE", "1", "x", "X", " Yes "])
def test_parse_bool_true_words(text):
    assert parse_bool(text) is True


@pytest.mark.parametrize("text", ["no", "N", "false", "0", "", "   "])
def test_parse_bool_false_words_including_blank(text):
    assert parse_bool(text) is False


def test_parse_bool_refuses_other_words():
    with pytest.raises(ValueError) as caught:
        parse_bool("maybe")
    assert str(caught.value) == '"maybe" isn\'t yes or no.'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("64", 64.0),
        ("64%", 64.0),
        (" 64 % ", 64.0),
        ("-25", -25.0),
        ("−25%", -25.0),
        ("+5", 5.0),
        ("1,234.5", 1234.5),
        ("1 234", 1234.0),
        ("12,5", 12.5),
        ("0.0", 0.0),
    ],
)
def test_parse_number_accepts(text, expected):
    assert parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("abc", '"abc" isn\'t a number.'),
        ("%", '"%" isn\'t a number.'),
        ("1e3", '"1e3" isn\'t a number.'),
        ("NaN", '"NaN" isn\'t a number.'),
        ("inf", '"inf" isn\'t a number.'),
        ("", "is empty."),
    ],
)
def test_parse_number_refuses(text, message):
    with pytest.raises(ValueError) as caught:
        parse_number(text)
    assert str(caught.value) == message


# ------------------------------------------------------------------------------------ mapping


def test_auto_map_matches_every_label_exactly():
    labels = [spec.label for spec in IMPORT_FIELDS]
    assert auto_map(labels) == {spec.key: spec.label for spec in IMPORT_FIELDS}


def test_synonyms_are_normalised_unique_and_belong_to_one_field():
    seen: dict[str, str] = {}
    for spec in IMPORT_FIELDS:
        for synonym in spec.synonyms:
            assert synonym == importing._normalise_header(synonym), synonym
            assert synonym not in seen, f"{synonym} is in {seen.get(synonym)} and {spec.key}"
            seen[synonym] = spec.key


def test_every_signal_field_has_a_kind_and_back():
    for spec in IMPORT_FIELDS:
        if spec.key.startswith("signal:"):
            assert SIGNAL_FIELD_KINDS[spec.key] is not None
    assert SignalKind.ONBOARDING_OVERDUE_MILESTONES not in SIGNAL_FIELD_KINDS.values()


@pytest.mark.parametrize(
    ("header", "key"),
    [
        ("Company Name", "name"),
        ("Client", "name"),
        ("Annual Recurring Revenue", "arr"),
        ("ARR (USD)", "arr"),
        ("Contract end date", "renewal_date"),
        ("Renews on", "renewal_date"),
        ("Customer success manager", "owner"),
        ("CSM", "owner"),
        ("Website URL", "domain"),
        ("Company website", "domain"),
        ("Segment", "tier"),
        ("Go live", "start_date"),
        ("Account ID", "external_id"),
        ("ID", "external_id"),
        ("Usage change (%)", "signal:usage_trend_30d"),
        ("Active users change 30d", "signal:usage_trend_30d"),
        ("Days since login", "signal:days_since_last_login"),
        ("sev1", "signal:open_tickets_sev1"),
        ("P1 tickets", "signal:open_tickets_sev1"),
        ("NPS score", "signal:nps_latest"),
        ("Champion churned", "signal:champion_left"),
        ("Seats used %", "signal:seat_utilisation"),
        ("License usage", "signal:seat_utilisation"),
        ("Invoice overdue days", "signal:billing_overdue_days"),
        ("Onboarding milestones", "milestones_total"),
        ("Milestones done", "milestones_done"),
        ("Overdue milestones", "milestones_overdue"),
    ],
)
def test_auto_map_recognises_common_spellings(header, key):
    assert auto_map([header]).get(key) == header


def test_auto_map_picks_the_longest_phrase_so_a_company_column_is_not_the_name():
    assert auto_map(["Company tier"]) == {"tier": "Company tier"}
    assert auto_map(["Company website"]) == {"domain": "Company website"}
    assert auto_map(["Renewal ARR"]) == {"arr": "Renewal ARR"}
    assert auto_map(["Name of CSM"]) == {"owner": "Name of CSM"}


def test_auto_map_gives_a_header_to_one_field_and_a_field_to_one_header():
    mapping = auto_map(["Account name", "Name", "Owner", "Owner", "ARR", "Renewal"])
    assert len(set(mapping.values())) == len(mapping)
    assert mapping["name"] == "Account name"
    assert mapping["owner"] == "Owner"


def test_auto_map_is_deterministic():
    headers = ["Company", "CSM", "ARR (USD)", "Renewal Date", "Company website", "Seats used"]
    assert auto_map(headers) == auto_map(list(headers))


def test_account_and_account_owner_split_cleanly():
    mapping = auto_map(["Account", "Account owner"])
    assert mapping["name"] == "Account"
    assert mapping["owner"] == "Account owner"


def test_auto_map_skips_blank_headers():
    assert auto_map(["", "   ", "Column 3"]) == {}


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        (
            "hubspot_companies_export.csv",
            {
                "name": "Company name",
                "external_id": "Record ID",
                "owner": "Company owner",
                "arr": "ARR",
                "renewal_date": "Renewal date",
                "start_date": "Customer since",
                "tier": "Plan tier",
                "domain": "Website URL",
            },
        ),
        (
            "salesforce_account_report.csv",
            {
                "name": "Account Name",
                "external_id": "Account ID",
                "owner": "Account Owner",
                "arr": "Annual Revenue",
                "renewal_date": "Contract End Date",
                "domain": "Website",
            },
        ),
        (
            "gainsight_style_export.csv",
            {
                "name": "Company",
                "external_id": "Company ID",
                "owner": "CSM",
                "arr": "ARR (USD)",
                "renewal_date": "Renewal Date",
                "tier": "Segment",
                "signal:days_since_last_login": "Last Login (days)",
                "signal:open_tickets_sev1": "Tickets Sev1 Open",
                "signal:nps_latest": "NPS Score",
                "signal:champion_left": "Champion Left?",
                "signal:seat_utilisation": "Seat Utilization %",
                "signal:usage_trend_30d": "Usage Change 30d %",
                "signal:billing_overdue_days": "Days Overdue",
            },
        ),
        (
            "messy_hand_made_sheet.csv",
            {
                "name": "Client name",
                "arr": "Annual value",
                "renewal_date": "Renewal",
                "owner": "owner",
                "start_date": "Start",
                "tier": "Plan",
                "domain": "Url",
                "milestones_done": "Milestones done",
                "signal:champion_left": "Champion has left",
            },
        ),
    ],
)
def test_auto_map_on_realistic_exports(file, expected):
    headers = read_csv((FIXTURES / file).read_bytes(), file).headers
    mapping = auto_map(headers)
    assert mapping == expected
    for required in ("name", "arr", "renewal_date"):
        assert required in mapping
    assert len(set(mapping.values())) == len(mapping)


# ------------------------------------------------------------------------------ date order


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["2026-03-04"], YMD),
        (["2026-03-04 00:00:00", "2026-11-01"], YMD),
        (["", "   ", "2026-01-02"], YMD),
        (["31/12/2026", "01/02/2026"], DMY),
        (["13/01/2026"], DMY),
        (["12/31/2026", "01/12/2026"], MDY),
        (["01/13/2026"], MDY),
        (["31/12/2026", "12/31/2026"], None),
        (["03/04/2026"], None),
        (["03/04/2026", "05/06/2026"], None),
        (["2026-01-02", "03/04/2026"], None),
        (["1 Dec 2026", "Dec 1, 2026"], None),
        (["46357"], None),
        ([], None),
        (["", "  "], None),
    ],
)
def test_detect_date_order(values, expected):
    assert detect_date_order(values) is expected


# ----------------------------------------------------------------------------- build: basics


def test_happy_path_builds_accounts_and_signals():
    headers = [
        "Account name",
        "ID",
        "ARR",
        "Renewal date",
        "Website",
        "Owner",
        "Tier",
        "Customer since",
        "Onboarding milestones",
        "Milestones done",
        "Milestones overdue",
        "Usage trend",
        "Days since last login",
        "Open severity-1 tickets",
        "NPS",
        "Champion left",
        "Seats in use",
        "Days invoice overdue",
    ]
    row = [
        "Larkspur Freight",
        "LF-1001",
        "$184,000",
        "01/12/2026",
        "https://www.Larkspur.example/about",
        "Dana Whitfield",
        "Enterprise",
        "2023-03-01",
        "6",
        "4",
        "1",
        "-38",
        "44",
        "1",
        "8",
        "yes",
        "64%",
        "0",
    ]
    result = _run(headers, [row], order=DMY)
    assert result.issues == []
    assert (result.ready_rows, result.skipped_rows) == (1, 0)
    account = result.accounts[0]
    assert account.id == "sp1-lf-1001"
    assert account.name == "Larkspur Freight"
    assert account.arr == 184000.0
    assert account.renewal_date == date(2026, 12, 1)
    assert account.start_date == date(2023, 3, 1)
    assert account.owner == "Dana Whitfield"
    assert account.tier == "Enterprise"
    assert account.domain == "larkspur.example"
    assert account.milestones == Milestones(done=4, total=6, overdue=1)
    assert _kinds(result.signals) == {
        "usage_trend_30d": -38.0,
        "days_since_last_login": 44.0,
        "open_tickets_sev1": 1.0,
        "nps_latest": 8.0,
        "champion_left": 1.0,
        "seat_utilisation": 64.0,
        "billing_overdue_days": 0.0,
        "onboarding_overdue_milestones": 1.0,
    }
    for signal in result.signals:
        assert signal.account_id == "sp1-lf-1001"
        assert signal.source == "spreadsheet"
        assert signal.observed_at == NOW
        assert signal.observed_at.tzinfo is None


def test_optional_fields_take_defaults_when_blank():
    result = _run(
        [*BASE, "Tier", "Owner", "Customer since"],
        [["Acme", "$1", "2026-12-01", "", "", ""]],
    )
    account = result.accounts[0]
    assert account.tier == "Unknown"
    assert account.owner == "Unassigned"
    assert account.start_date is None  # a blank cell is not known, so never today
    assert account.domain is None
    assert account.milestones == Milestones(done=0, total=0, overdue=0)
    assert result.signals == []
    assert result.issues == []


def test_no_optional_columns_means_no_signals_and_default_milestones():
    result = _run(BASE, [["Acme", "$1", "2026-12-01"]])
    assert result.accounts[0].milestones == Milestones(done=0, total=0, overdue=0)
    assert result.signals == []


# -------------------------------------------------------------------------- build: skipping


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (["", "$1", "2026-12-01"], "Account name is empty."),
        (["Acme", "", "2026-12-01"], "ARR is empty."),
        (["Acme", "TBC", "2026-12-01"], 'ARR "TBC" isn\'t an amount.'),
        (["Acme", "-5", "2026-12-01"], 'ARR "-5" can\'t be negative.'),
        (["Acme", "$1", ""], "Renewal date is empty."),
        (["Acme", "$1", "31/31/2026"], 'Renewal date "31/31/2026" isn\'t a date.'),
    ],
)
def test_missing_or_invalid_required_field_skips_the_row(row, message):
    result = _run(BASE, [row, ["Good", "$2", "2026-12-01"]])
    assert result.ready_rows == 1
    assert result.skipped_rows == 1
    assert [account.name for account in result.accounts] == ["Good"]
    assert _messages(result) == [message]
    assert result.issues[0].row == 2
    assert result.issues[0].field in {"name", "arr", "renewal_date"}


def test_each_problem_on_a_skipped_row_is_reported_in_field_order():
    result = _run(BASE, [["", "TBC", ""]])
    assert _messages(result) == [
        "Account name is empty.",
        'ARR "TBC" isn\'t an amount.',
        "Renewal date is empty.",
    ]
    assert [issue.field for issue in result.issues] == ["name", "arr", "renewal_date"]
    assert result.skipped_rows == 1


def test_unmapped_required_field_skips_every_row_with_one_issue_each():
    headers = ["Company", "ARR", "Renewal date"]
    mapping = {"arr": "ARR", "renewal_date": "Renewal date"}
    result = _run(headers, [["A", "$1", "2026-12-01"], ["B", "$2", "2026-12-01"]], mapping=mapping)
    assert result.ready_rows == 0
    assert result.skipped_rows == 2
    assert _messages(result) == ["Account name isn't matched to a column."] * 2
    assert [issue.row for issue in result.issues] == [2, 3]


def test_a_row_with_only_optional_problems_is_still_imported():
    headers = [*BASE, "Website", "Customer since"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "N/A", "31/31/2026"]])
    assert result.ready_rows == 1
    assert result.skipped_rows == 0
    account = result.accounts[0]
    assert account.domain is None
    assert account.start_date is None  # the invalid date is dropped, not replaced by today
    assert _messages(result) == [
        'Website "N/A" isn\'t a web address.',
        'Customer since "31/31/2026" isn\'t a date.',
    ]


def test_skipped_row_still_reports_its_optional_problems():
    headers = [*BASE, "Website"]
    result = _run(headers, [["", "$1", "2026-12-01", "not a site"]])
    assert result.skipped_rows == 1
    assert _messages(result) == [
        "Account name is empty.",
        'Website "not a site" isn\'t a web address.',
    ]


# ------------------------------------------------------------------- build: optional issues


@pytest.mark.parametrize(
    ("header", "value", "message"),
    [
        ("NPS", "11", 'NPS "11" should be between 0 and 10, so it was left out.'),
        ("NPS", "abc", 'NPS "abc" isn\'t a number.'),
        (
            "Seats in use",
            "250",
            'Seats in use "250" should be between 0 and 200, so it was left out.',
        ),
        (
            "Usage trend",
            "-150",
            'Usage trend "-150" should be between -100 and 1000, so it was left out.',
        ),
        ("Days since last login", "-3", 'Days since last login "-3" can\'t be negative.'),
        ("Days since last login", "2.5", 'Days since last login "2.5" isn\'t a whole number.'),
        ("Open severity-1 tickets", "2.5", 'Open severity-1 tickets "2.5" isn\'t a whole number.'),
        ("Open severity-1 tickets", "-1", 'Open severity-1 tickets "-1" can\'t be negative.'),
        ("Champion left", "maybe", 'Champion left "maybe" isn\'t yes or no.'),
        ("Days invoice overdue", "-2", 'Days invoice overdue "-2" can\'t be negative.'),
    ],
)
def test_invalid_optional_value_is_left_out_and_the_row_is_kept(header, value, message):
    result = _run([*BASE, header], [["Acme", "$1", "2026-12-01", value]])
    assert result.ready_rows == 1
    assert result.skipped_rows == 0
    assert _messages(result) == [message]
    # No reading comes from the rejected cell.
    field_key = next(spec.key for spec in IMPORT_FIELDS if spec.label == header)
    assert SIGNAL_FIELD_KINDS.get(field_key) not in {s.kind for s in result.signals}


@pytest.mark.parametrize(
    ("header", "value"),
    [("NPS", "0"), ("NPS", "10"), ("Usage trend", "-100"), ("Usage trend", "1000")],
)
def test_sanity_range_edges_are_kept(header, value):
    result = _run([*BASE, header], [["Acme", "$1", "2026-12-01", value]])
    assert result.issues == []
    assert len(result.signals) == 1
    assert result.signals[0].value == float(value)


def test_milestones_that_do_not_add_up_are_clamped_with_one_issue():
    headers = [*BASE, "Onboarding milestones", "Milestones done", "Milestones overdue"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "3", "5", "2"]])
    assert result.accounts[0].milestones == Milestones(done=3, total=3, overdue=0)
    assert _messages(result) == ["Onboarding milestones didn't add up, so they were adjusted."]
    assert result.issues[0].field == "milestones_total"
    assert _kinds(result.signals) == {"onboarding_overdue_milestones": 0.0}


def test_overdue_clamped_to_remaining_milestones():
    headers = [*BASE, "Onboarding milestones", "Milestones done", "Milestones overdue"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "5", "4", "3"]])
    assert result.accounts[0].milestones == Milestones(done=4, total=5, overdue=1)
    assert _kinds(result.signals) == {"onboarding_overdue_milestones": 1.0}
    assert len(result.issues) == 1


def test_milestones_consistent_produce_no_issue():
    headers = [*BASE, "Onboarding milestones", "Milestones done"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "6", "6"]])
    assert result.issues == []
    assert result.accounts[0].milestones == Milestones(done=6, total=6, overdue=0)


def test_negative_milestone_is_left_empty_with_an_issue():
    headers = [*BASE, "Onboarding milestones"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "-2"]])
    assert _messages(result) == ['Onboarding milestones "-2" can\'t be negative.']
    assert result.accounts[0].milestones == Milestones(done=0, total=0, overdue=0)


def test_champion_yes_and_no_become_one_and_zero_and_blank_is_no_reading():
    headers = [*BASE, "Champion left"]
    rows = [
        ["Yes row", "$1", "2026-12-01", "yes"],
        ["No row", "$1", "2026-12-01", "no"],
        ["Blank row", "$1", "2026-12-01", ""],
    ]
    result = _run(headers, rows)
    by_account = {}
    for signal in result.signals:
        by_account.setdefault(signal.account_id, []).append(signal)
    assert [s.value for s in by_account["sp1-yes-row"]] == [1.0]
    assert [s.value for s in by_account["sp1-no-row"]] == [0.0]
    assert "sp1-blank-row" not in by_account


def test_x_in_a_yes_no_column_means_yes():
    result = _run([*BASE, "Champion left"], [["Acme", "$1", "2026-12-01", "x"]])
    assert [signal.value for signal in result.signals] == [1.0]


def test_overdue_reading_needs_a_milestones_total():
    headers = [*BASE, "Onboarding milestones", "Milestones overdue"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "6", "2"]])
    assert [signal.kind for signal in result.signals] == [SignalKind.ONBOARDING_OVERDUE_MILESTONES]
    assert [signal.value for signal in result.signals] == [2.0]


def test_done_and_overdue_are_ignored_without_a_total_and_raise_no_issue():
    # Done and overdue have values, but with no total column they don't count for the row.
    result = _run([*BASE, "Milestones overdue"], [["Acme", "$1", "2026-12-01", "2"]])
    assert result.accounts[0].milestones == Milestones(done=0, total=0, overdue=0)
    assert result.signals == []
    assert result.issues == []
    result = _run([*BASE, "Milestones done"], [["Acme", "$1", "2026-12-01", "2"]])
    assert result.accounts[0].milestones == Milestones(done=0, total=0, overdue=0)
    assert result.issues == []


def test_done_and_overdue_are_ignored_when_the_total_is_blank():
    headers = [*BASE, "Onboarding milestones", "Milestones done", "Milestones overdue"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "", "5", "9"]])
    assert result.accounts[0].milestones == Milestones(done=0, total=0, overdue=0)
    assert result.issues == []
    assert result.signals == []


def test_an_invalid_total_gets_its_own_issue_and_no_clamp_issue():
    headers = [*BASE, "Onboarding milestones", "Milestones done"]
    result = _run(headers, [["Acme", "$1", "2026-12-01", "abc", "3"]])
    assert result.accounts[0].milestones == Milestones(done=0, total=0, overdue=0)
    assert _messages(result) == ['Onboarding milestones "abc" isn\'t a whole number.']
    assert result.signals == []


def test_domain_is_normalised_to_a_bare_host():
    headers = [*BASE, "Website"]
    cases = {
        "https://www.Acme.example/about?x=1": "acme.example",
        "WWW.acme.example": "acme.example",
        "user:pw@acme.example:8443/": "acme.example",
        "acme.example.": "acme.example",
        "//acme.example": "acme.example",
        "Bücher.example": "xn--bcher-kva.example",
    }
    rows = [[f"Company {n}", "$1", "2026-12-01", website] for n, website in enumerate(cases)]
    result = _run(headers, rows)
    assert [account.domain for account in result.accounts] == list(cases.values())
    assert result.issues == []


@pytest.mark.parametrize("website", ["localhost", "a b.example", "...", "https://"])
def test_domain_without_a_real_host_is_an_issue(website):
    result = _run([*BASE, "Website"], [["Acme", "$1", "2026-12-01", website]])
    assert result.accounts[0].domain is None
    assert _messages(result) == [f'Website "{website}" isn\'t a web address.']


# --------------------------------------------------------------------------- build: duplicates


def test_repeated_external_id_skips_the_later_row():
    headers = [*BASE, "ID"]
    result = _run(
        headers, [["Acme", "$1", "2026-12-01", "A-1"], ["Other", "$2", "2026-12-01", "a 1"]]
    )
    assert [account.name for account in result.accounts] == ["Acme"]
    assert result.skipped_rows == 1
    assert _messages(result) == ['ID "a 1" is also on row 2, so this row was skipped.']
    assert result.issues[0].row == 3
    assert result.issues[0].field == "external_id"


def test_repeated_name_without_an_id_skips_the_later_row():
    result = _run(BASE, [["Acme", "$1", "2026-12-01"], ["ACME!", "$2", "2026-12-01"]])
    assert [account.id for account in result.accounts] == ["sp1-acme"]
    assert _messages(result) == ['Account name "ACME!" is also on row 2, so this row was skipped.']


def test_a_skipped_row_does_not_take_its_id():
    headers = [*BASE, "ID"]
    rows = [["", "$1", "2026-12-01", "A-1"], ["Acme", "$2", "2026-12-01", "A-1"]]
    result = _run(headers, rows)
    assert [account.id for account in result.accounts] == ["sp1-a-1"]
    assert result.skipped_rows == 1
    assert _messages(result) == ["Account name is empty."]


# ------------------------------------------------------------------------------- build: ids


@pytest.mark.parametrize(
    ("connection_id", "prefix"),
    [("sheet-1", "sp1-"), ("sheet-12", "sp12-"), ("sheet-007", "sp007-")],
)
def test_connection_number_becomes_the_id_prefix(connection_id, prefix):
    result = _run(BASE, [["Acme", "$1", "2026-12-01"]], connection_id=connection_id)
    assert result.accounts[0].id == f"{prefix}acme"


@pytest.mark.parametrize("connection_id", ["sheet", "sp1", "csv-1", "sheet-", "sheet-1x", ""])
def test_bad_connection_id_raises(connection_id):
    with pytest.raises(ValueError) as caught:
        _run(BASE, [["Acme", "$1", "2026-12-01"]], connection_id=connection_id)
    assert "sheet-<n>" in str(caught.value)


@pytest.mark.parametrize(
    ("name", "external", "slug"),
    [
        ("Acme", "ACME 001!", "acme-001"),
        ("Café Müller GmbH", None, "cafe-muller-gmbh"),
        ("Acme Ltd.", None, "acme-ltd"),
        ("  Spaced   Out  ", None, "spaced-out"),
        ("A" * 60, None, "a" * 40),
        ("Name", "  --X--  ", "x"),
    ],
)
def test_id_slug(name, external, slug):
    headers = [*BASE, "ID"]
    result = _run(headers, [[name, "$1", "2026-12-01", external or ""]])
    assert result.accounts[0].id == f"sp1-{slug}"


def test_slug_cut_at_forty_characters_never_ends_in_a_hyphen():
    # "a"*39 then a hyphen falls exactly at the 40-character cut.
    result = _run(BASE, [["a" * 39 + " bc", "$1", "2026-12-01"]])
    assert result.accounts[0].id == "sp1-" + "a" * 39


def test_name_with_no_ascii_letters_gets_a_stable_hashed_id():
    first = _run(BASE, [["東京株式会社", "$1", "2026-12-01"]]).accounts[0].id
    second = _run(BASE, [["東京株式会社", "$1", "2026-12-01"]]).accounts[0].id
    other = _run(BASE, [["大阪株式会社", "$1", "2026-12-01"]]).accounts[0].id
    assert first == second
    assert first.startswith("sp1-account-")
    assert first != other


# -------------------------------------------------------------------------- build: timestamps


def test_imported_at_with_an_offset_becomes_naive_utc():
    stamp = datetime(2026, 10, 22, 9, 0, tzinfo=timezone(timedelta(hours=2)))
    result = _run([*BASE, "NPS"], [["Acme", "$1", "2026-12-01", "7"]], imported_at=stamp)
    assert result.signals[0].observed_at == datetime(2026, 10, 22, 7, 0)
    assert result.signals[0].observed_at.tzinfo is None


def test_naive_imported_at_is_used_as_given():
    result = _run([*BASE, "NPS"], [["Acme", "$1", "2026-12-01", "7"]], imported_at=NOW)
    assert result.signals[0].observed_at == NOW


def test_utc_imported_at_is_naive_utc():
    stamp = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    result = _run([*BASE, "NPS"], [["Acme", "$1", "2026-12-01", "7"]], imported_at=stamp)
    assert result.signals[0].observed_at == NOW


# --------------------------------------------------------------------------- build: messages


def test_issues_are_ordered_by_row_then_field_order():
    headers = [*BASE, "NPS", "Website"]
    rows = [
        ["Acme", "$1", "2026-12-01", "11", "bad site"],
        ["", "TBC", "2026-12-01", "abc", "x"],
    ]
    result = _run(headers, rows)
    assert [(issue.row, issue.field) for issue in result.issues] == [
        (2, "domain"),
        (2, "signal:nps_latest"),
        (3, "name"),
        (3, "arr"),
        (3, "domain"),
        (3, "signal:nps_latest"),
    ]


def test_issue_order_follows_import_fields_not_column_order():
    headers = ["Website", "NPS", "Account name", "ARR", "Renewal date"]
    result = _run(headers, [["bad site", "11", "", "TBC", ""]])
    fields = [issue.field for issue in result.issues]
    expected_order = [spec.key for spec in IMPORT_FIELDS]
    assert fields == sorted(fields, key=expected_order.index)


def test_long_quoted_values_are_cut_to_forty_characters():
    long_value = "x" * 60
    result = _run(BASE, [["Acme", "$1", long_value]])
    message = result.issues[0].message
    quoted = message[message.index('"') + 1 : message.rindex('"')]
    assert len(quoted) == 40
    assert quoted.endswith("…")
    assert message == f'Renewal date "{"x" * 39}…" isn\'t a date.'


def test_messages_use_full_stops_and_no_em_dashes():
    headers = [*BASE, "NPS", "Website", "Customer since", "Champion left"]
    rows = [
        ["", "TBC", "31/31/2026", "11", "not a site", "31/31/2026", "maybe"],
    ]
    result = _run(headers, rows)
    assert len(result.issues) >= 5
    for issue in result.issues:
        assert issue.message.endswith(".")
        assert "—" not in issue.message
        assert "–" not in issue.message


def test_row_numbers_start_at_two_for_the_first_data_row():
    result = _run(BASE, [["", "$1", "2026-12-01"], ["", "$1", "2026-12-01"]])
    assert [issue.row for issue in result.issues] == [2, 3]


def test_mapping_errors_are_plain_value_errors():
    with pytest.raises(ValueError, match="isn't an import field"):
        _run(BASE, [["Acme", "$1", "2026-12-01"]], mapping={"nope": "Account name"})
    with pytest.raises(ValueError, match="isn't a column in this file"):
        _run(BASE, [["Acme", "$1", "2026-12-01"]], mapping={"name": "Missing"})


# ------------------------------------------------------------------------------ merge


def _signal(account: str, kind: SignalKind, value: float, day: int = 1) -> Signal:
    return Signal(
        account_id=account,
        kind=kind,
        value=value,
        observed_at=datetime(2026, 10, day, 9, 0),
        source="spreadsheet",
    )


def test_unchanged_reimport_adds_nothing():
    previous = [_signal("a", SignalKind.NPS_LATEST, 8), _signal("b", SignalKind.CHAMPION_LEFT, 1)]
    new = [
        _signal("a", SignalKind.NPS_LATEST, 8, day=2),
        _signal("b", SignalKind.CHAMPION_LEFT, 1, 2),
    ]
    merged = merge_into_snapshot_signals(previous, new, {"a", "b"})
    assert merged == previous


def test_changed_value_appends_a_new_reading_and_keeps_history():
    previous = [_signal("a", SignalKind.NPS_LATEST, 8, day=1)]
    new = [_signal("a", SignalKind.NPS_LATEST, 6, day=2)]
    merged = merge_into_snapshot_signals(previous, new, {"a"})
    assert [s.value for s in merged] == [8, 6]
    assert merged[-1].observed_at == datetime(2026, 10, 2, 9, 0)


def test_change_is_measured_against_the_latest_reading_not_the_first():
    previous = [
        _signal("a", SignalKind.NPS_LATEST, 8, day=1),
        _signal("a", SignalKind.NPS_LATEST, 6, day=2),
    ]
    back_to_first = merge_into_snapshot_signals(
        previous, [_signal("a", SignalKind.NPS_LATEST, 8, 3)], {"a"}
    )
    assert [s.value for s in back_to_first] == [8, 6, 8]
    unchanged = merge_into_snapshot_signals(
        previous, [_signal("a", SignalKind.NPS_LATEST, 6, 3)], {"a"}
    )
    assert unchanged == previous


def test_removed_accounts_are_dropped_with_their_history():
    previous = [_signal("a", SignalKind.NPS_LATEST, 8), _signal("gone", SignalKind.NPS_LATEST, 3)]
    merged = merge_into_snapshot_signals(previous, [], {"a"})
    assert [s.account_id for s in merged] == ["a"]


def test_new_account_readings_are_appended_and_other_kinds_are_kept():
    previous = [_signal("a", SignalKind.NPS_LATEST, 8), _signal("a", SignalKind.CHAMPION_LEFT, 0)]
    new = [_signal("a", SignalKind.NPS_LATEST, 8, 2), _signal("c", SignalKind.NPS_LATEST, 5, 2)]
    merged = merge_into_snapshot_signals(previous, new, {"a", "c"})
    assert [(s.account_id, s.kind.value, s.value) for s in merged] == [
        ("a", "nps_latest", 8),
        ("a", "champion_left", 0),
        ("c", "nps_latest", 5),
    ]


def test_readings_for_accounts_not_kept_are_never_appended():
    merged = merge_into_snapshot_signals([], [_signal("x", SignalKind.NPS_LATEST, 5)], {"a"})
    assert merged == []


# ------------------------------------------------------------------------------ template


def test_template_has_every_label_then_two_example_rows():
    lines = template_csv().splitlines()
    assert lines[0].split(",")[:3] == ["Account name", "ARR", "Renewal date"]
    assert len(lines) == 3
    assert "Larkspur Freight" in lines[1]
    assert "Quillon Health" in lines[2]
    assert "larkspur.example" in lines[1]
    assert "quillon.example" in lines[2]


def test_template_round_trips_with_no_issues():
    table = read_csv(template_csv().encode("utf-8"), "template.csv")
    mapping = auto_map(table.headers)
    assert set(mapping) == {spec.key for spec in IMPORT_FIELDS}
    columns = [table.headers.index(mapping[key]) for key in ("renewal_date", "start_date")]
    order = detect_date_order([row[column] for row in table.rows for column in columns])
    assert order is YMD
    result = build_import(
        table,
        mapping,
        connection_id="sheet-1",
        date_order=order,
        imported_at=NOW,
        today=TODAY,
    )
    assert result.issues == []
    assert result.skipped_rows == 0
    assert [account.name for account in result.accounts] == ["Larkspur Freight", "Quillon Health"]
    assert result.accounts[0].milestones == Milestones(done=4, total=6, overdue=1)
    assert len(result.signals) == 16


def test_template_names_are_clearly_invented_and_use_example_domains():
    for line in template_csv().splitlines()[1:]:
        assert ".example" in line


# ---------------------------------------------------------------------------- issues csv


def test_issues_csv_keeps_only_rows_with_issues_and_adds_problems():
    rows = [["Acme", "$1", "2026-12-01"], ["", "TBC", "31/31/2026"], ["Ok", "$2", "2026-12-01"]]
    table = _table(BASE, rows)
    issues = [
        ImportIssue(row=3, field="name", message="Account name is empty."),
        ImportIssue(row=3, field="arr", message='ARR "TBC" isn\'t an amount.'),
        ImportIssue(
            row=3, field="renewal_date", message='Renewal date "31/31/2026" isn\'t a date.'
        ),
    ]
    rows_out = list(csv.reader(io.StringIO(issues_csv(table, issues))))
    assert rows_out[0] == ["Account name", "ARR", "Renewal date", "Problems"]
    assert len(rows_out) == 2
    assert rows_out[1] == [
        "",
        "TBC",
        "31/31/2026",
        'Account name is empty. ARR "TBC" isn\'t an amount. '
        'Renewal date "31/31/2026" isn\'t a date.',
    ]


def test_issues_csv_round_trips_through_read_csv_and_quotes_commas():
    table = _table(BASE, [["Acme, Inc", "$1,000", ""]])
    issues = [ImportIssue(row=2, field="renewal_date", message="Renewal date is empty.")]
    text = issues_csv(table, issues)
    reread = read_csv(text.encode("utf-8"), "issues.csv")
    assert reread.headers == [*BASE, "Problems"]
    assert reread.rows == [["Acme, Inc", "$1,000", "", "Renewal date is empty."]]


def test_issues_csv_is_just_the_header_when_nothing_failed():
    table = _table(BASE, [["Acme", "$1", "2026-12-01"]])
    assert issues_csv(table, []).strip() == "Account name,ARR,Renewal date,Problems"


def test_issues_csv_for_a_real_import_contains_each_bad_row_once():
    headers = [*BASE, "NPS"]
    rows = [
        ["Acme", "$1", "2026-12-01", "7"],
        ["", "$2", "2026-12-01", "11"],
        ["Ok", "$3", "x", ""],
    ]
    table = _table(headers, rows)
    result = build_import(
        table,
        auto_map(headers),
        connection_id="sheet-1",
        date_order=DMY,
        imported_at=NOW,
        today=TODAY,
    )
    lines = issues_csv(table, result.issues).splitlines()
    assert len(lines) == 1 + 2
    assert lines[1].startswith(",$2,")
    assert lines[2].startswith("Ok,$3,x,")


# ----------------------------------------------------------------------- field type coverage


def test_every_field_type_is_used_by_a_field():
    used = {spec.type for spec in IMPORT_FIELDS}
    assert used == set(FieldType)


def test_import_fields_match_the_documented_keys():
    keys = [spec.key for spec in IMPORT_FIELDS]
    assert keys[:3] == ["name", "arr", "renewal_date"]
    assert [spec.key for spec in IMPORT_FIELDS if spec.required] == ["name", "arr", "renewal_date"]
    assert len(keys) == len(set(keys)) == 18


def test_header_only_table_builds_nothing():
    table = _table(["Account name"], [])
    result = build_import(
        table,
        {"name": "Account name"},
        connection_id="sheet-1",
        date_order=DMY,
        imported_at=datetime(2026, 10, 22, tzinfo=UTC),
        today=TODAY,
    )
    assert result.accounts == []
    assert result.signals == []
    assert result.issues == []
    assert (result.skipped_rows, result.ready_rows) == (0, 0)


def test_signal_value_types_are_floats():
    result = _run([*BASE, "NPS", "Champion left"], [["Acme", "$1", "2026-12-01", "7", "yes"]])
    assert all(isinstance(signal.value, float) for signal in result.signals)


def test_dates_with_day_first_and_month_first_follow_the_chosen_order():
    headers = BASE
    rows = [["Acme", "$1", "03/04/2026"]]
    dmy = _run(headers, rows, order=DMY).accounts[0].renewal_date
    mdy = _run(headers, rows, order=MDY).accounts[0].renewal_date
    assert dmy == date(2026, 4, 3)
    assert mdy == date(2026, 3, 4)


def test_renewal_in_iso_with_ymd_order_builds():
    result = _run(BASE, [["Acme", "$1", "2026-12-01"]], order=YMD)
    assert result.accounts[0].renewal_date == date(2026, 12, 1)
    assert result.issues == []


def test_slash_date_under_ymd_order_is_an_issue_not_a_guess():
    result = _run(BASE, [["Acme", "$1", "03/04/2026"]], order=YMD)
    assert result.skipped_rows == 1
    assert _messages(result) == ['Renewal date "03/04/2026" isn\'t a date.']


def test_optional_signal_fields_keep_their_declared_type():
    for spec in IMPORT_FIELDS:
        if spec.key in SIGNAL_FIELD_KINDS:
            assert spec.type in {FieldType.NUMBER, FieldType.INTEGER, FieldType.BOOLEAN}


def test_domain_normaliser_rejects_only_non_hosts():
    assert importing._normalise_domain("acme.example") == "acme.example"
    assert importing._normalise_domain("ftp://x.example/a") == "x.example"
    assert importing._normalise_domain("nodots") is None
    assert importing._normalise_domain("bad..dots.example") is None
    assert importing._normalise_domain("www.") is None


def test_slug_and_fallback_helpers_are_stable():
    assert importing._slug("  Acme   Co  ") == "acme-co"
    assert importing._slug("") == ""
    assert importing._fallback_slug("東京").startswith("account-")
    assert importing._fallback_slug("東京") == importing._fallback_slug("東京")


def test_clamp_helper_reports_change():
    milestones, changed = importing._clamp_milestones(3, 3, 0)
    assert changed is False
    assert milestones == Milestones(done=3, total=3, overdue=0)
    milestones, changed = importing._clamp_milestones(2, 5, 4)
    assert changed is True
    assert milestones == Milestones(done=2, total=2, overdue=0)


def test_money_result_is_a_float_and_never_negative():
    for text in ["0", "5", "5k"]:
        value = parse_money(text)
        assert isinstance(value, float)
        assert value >= 0


def test_date_type_is_python_date():
    assert isinstance(parse_date("2026-12-01", DMY), date)
    assert not isinstance(parse_date("2026-12-01", DMY), datetime)


# ------------------------------------------------------------------ monthly figures are not ARR


def test_monthly_revenue_never_maps_to_arr():
    assert auto_map(["Monthly revenue", "ARR"]) == {"arr": "ARR"}
    assert auto_map(["Monthly revenue"]) == {}
    assert auto_map(["Revenue per month", "ARR"]) == {"arr": "ARR"}
    assert auto_map(["MRR", "Renewal date"]) == {"renewal_date": "Renewal date"}


def test_annual_revenue_still_maps_to_arr_next_to_monthly_revenue():
    assert auto_map(["Monthly revenue", "Annual revenue"]) == {"arr": "Annual revenue"}


# ------------------------------------------------------------------------- row numbers


def test_a_hand_built_table_numbers_its_rows_from_two():
    rows = [["", "$1", "2026-12-01"], ["", "$1", "2026-12-01"], ["", "$1", "2026-12-01"]]
    result = _run(BASE, rows)
    assert [issue.row for issue in result.issues] == [2, 3, 4]


def test_row_numbers_on_a_table_are_used_for_issues():
    table = Table(
        source_name="hand.csv",
        headers=BASE,
        rows=[["", "$1", "2026-12-01"]],
        row_numbers=[7],
    )
    result = build_import(
        table,
        auto_map(BASE),
        connection_id="sheet-1",
        date_order=DMY,
        imported_at=NOW,
        today=TODAY,
    )
    assert [issue.row for issue in result.issues] == [7]


def test_row_numbers_that_do_not_match_the_rows_are_refused():
    table = Table(
        source_name="hand.csv",
        headers=BASE,
        rows=[["Acme", "$1", "2026-12-01"]],
        row_numbers=[5, 6],
    )
    with pytest.raises(ValueError, match="row numbers"):
        build_import(
            table,
            auto_map(BASE),
            connection_id="sheet-1",
            date_order=DMY,
            imported_at=NOW,
            today=TODAY,
        )


def test_issues_csv_writes_the_row_an_issue_points_at_after_a_blank_line():
    table = read_csv(
        b"Account name,ARR,Renewal date\n\nGood,$1,2026-12-01\nBad,TBC,2026-12-01\n", "x.csv"
    )
    issues = [ImportIssue(row=4, field="arr", message='ARR "TBC" isn\'t an amount.')]
    rows_out = list(csv.reader(io.StringIO(issues_csv(table, issues))))
    assert rows_out == [
        ["Account name", "ARR", "Renewal date", "Problems"],
        ["Bad", "TBC", "2026-12-01", 'ARR "TBC" isn\'t an amount.'],
    ]


def test_issues_csv_uses_row_numbers_for_a_hand_built_table():
    table = _table(BASE, [["Good", "$1", "2026-12-01"], ["Bad", "TBC", "2026-12-01"]])
    issues = [ImportIssue(row=3, field="arr", message='ARR "TBC" isn\'t an amount.')]
    rows_out = list(csv.reader(io.StringIO(issues_csv(table, issues))))
    assert rows_out[1][:2] == ["Bad", "TBC"]
    assert len(rows_out) == 2


def test_issues_csv_skips_an_issue_with_no_matching_row():
    table = _table(BASE, [["Acme", "$1", "2026-12-01"]])
    issues = [ImportIssue(row=3, field="name", message="Account name is empty.")]
    rows_out = list(csv.reader(io.StringIO(issues_csv(table, issues))))
    assert rows_out == [["Account name", "ARR", "Renewal date", "Problems"]]


@pytest.mark.parametrize(
    ("cell", "written"),
    [
        ('=HYPERLINK("http://evil.example","x")', '\'=HYPERLINK("http://evil.example","x")'),
        ("+1+2", "'+1+2"),
        ("@SUM(A1:A2)", "'@SUM(A1:A2)"),
        ("-2+3", "'-2+3"),
        ("-25", "-25"),
        ("-1,200.5", "-1,200.5"),
        ("Larkspur Freight", "Larkspur Freight"),
    ],
)
def test_issues_csv_neutralises_cells_a_spreadsheet_would_run_as_formulas(cell, written):
    table = _table(BASE, [[cell, "TBC", "2026-12-01"]])
    issues = [ImportIssue(row=2, field="arr", message='ARR "TBC" isn\'t an amount.')]
    rows_out = list(csv.reader(io.StringIO(issues_csv(table, issues))))
    assert rows_out[1][0] == written
