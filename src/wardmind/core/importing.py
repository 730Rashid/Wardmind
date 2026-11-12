"""Bringing a book in from a spreadsheet: CSV/TSV files, Excel files, or pasted cells.

The flow: read the file into a Table, auto_map the headers to import fields (the CSM reviews
and corrects the mapping), detect the date order, then build_import turns rows into Accounts and
Signals with a plain-English issue for every row that can't be used.

Only three fields are required: name, ARR and renewal date. Every signal is optional.

CONTRACT FILE: models, constants, signatures and docstrings are fixed; implementers escalate
before changing them. Nothing here touches the network.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import math
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Any

from openpyxl import load_workbook
from pydantic import BaseModel

from wardmind.core.config import CustomSignal, ScoringConfig, load_scoring_config
from wardmind.core.models import (
    CUSTOM_PREFIX,
    Account,
    CustomKind,
    Milestones,
    Signal,
    SignalKey,
    SignalKind,
)

MAX_BYTES = 5 * 1024 * 1024  # larger files are refused with a plain message
MAX_ROWS = 5000  # data rows; more are refused with a plain message
MAX_COLUMNS = 500  # columns with text; blank trailing columns don't count
MAX_CELL_CHARS = 32_767  # Excel's own cell limit; a longer cell is refused
TOO_MANY_COLUMNS_MESSAGE = (
    f"That has more than {MAX_COLUMNS} columns. Remove the ones Wardmind doesn't need."
)
CELL_TOO_LONG_MESSAGE = (
    f"A cell is longer than a spreadsheet allows ({MAX_CELL_CHARS:,} characters)."
)
# An .xlsx is a zip. Its parts are judged by their declared unpacked sizes before openpyxl opens
# it, so a small file that unpacks to hundreds of megabytes is refused.
MAX_XLSX_UNCOMPRESSED = 200_000_000  # all the parts together, unpacked
MAX_XLSX_MEMBER = 100_000_000  # one part, unpacked


class FieldType(StrEnum):
    TEXT = "text"
    MONEY = "money"  # "$120,000", "120k", "1.2m", "€120.000,50", "(1,000)" is refused
    DATE = "date"
    INTEGER = "integer"
    NUMBER = "number"  # plain number, a trailing "%" is allowed and stripped
    BOOLEAN = "boolean"  # yes/no, true/false, y/n, 1/0, x/blank


class ImportField(BaseModel):
    key: str  # "name", "arr", "signal:usage_trend_30d", …
    label: str  # what the mapping screen shows, e.g. "ARR"
    required: bool
    type: FieldType
    help: str  # one line for the template and mapping screen, e.g. "Annual recurring revenue"
    synonyms: tuple[str, ...]  # header spellings auto_map recognises, already normalized


# The fields, in the order the mapping screen and the template show them. Implementers fill in
# the synonym lists generously (common CRM and spreadsheet header spellings) but keep keys,
# labels, required flags and types exactly as below.
IMPORT_FIELDS: tuple[ImportField, ...] = (
    ImportField(
        key="name",
        label="Account name",
        required=True,
        type=FieldType.TEXT,
        help="The customer's name",
        synonyms=(
            "account name",
            "account",
            "company",
            "company name",
            "customer",
            "customer name",
            "customer account",
            "client",
            "client name",
            "organisation",
            "organization",
            "organisation name",
            "organization name",
            "name",
            "account title",
            "business name",
            "legal name",
        ),
    ),
    ImportField(
        key="arr",
        label="ARR",
        required=True,
        type=FieldType.MONEY,
        help="Annual recurring revenue",
        synonyms=(
            "arr",
            "arr usd",
            "arr gbp",
            "arr eur",
            "current arr",
            "total arr",
            "annual recurring revenue",
            "annual revenue",
            "annual contract value",
            "annual value",
            "acv",
            "contract value",
            "revenue",
            "yearly revenue",
            "recurring revenue",
            "subscription value",
            "subscription revenue",
        ),
    ),
    ImportField(
        key="renewal_date",
        label="Renewal date",
        required=True,
        type=FieldType.DATE,
        help="When the contract renews",
        synonyms=(
            "renewal date",
            "renewal",
            "renewal day",
            "renewal due",
            "renewal due date",
            "next renewal",
            "next renewal date",
            "renews",
            "renews on",
            "contract end",
            "contract end date",
            "contract ends",
            "contract expiry",
            "contract expiry date",
            "end date",
            "term end",
            "term end date",
            "expiry",
            "expiry date",
            "expiration date",
            "expires",
            "expires on",
            "subscription end",
            "subscription end date",
            "license end date",
            "licence end date",
        ),
    ),
    ImportField(
        key="external_id",
        label="ID",
        required=False,
        type=FieldType.TEXT,
        help="Your own ID for the account, so re-imports update the same account",
        synonyms=(
            "account id",
            "customer id",
            "company id",
            "external id",
            "crm id",
            "record id",
            "hubspot id",
            "hubspot record id",
            "salesforce id",
            "account number",
            "customer number",
            "account code",
            "customer code",
            "account reference",
            "customer reference",
        ),
    ),
    ImportField(
        key="domain",
        label="Website",
        required=False,
        type=FieldType.TEXT,
        help="Helps spot the same customer in another connection",
        synonyms=(
            "domain",
            "domain name",
            "primary domain",
            "website",
            "website url",
            "web site",
            "web",
            "web address",
            "web url",
            "url",
            "company website",
            "company domain",
            "company url",
            "company site",
            "homepage",
            "home page",
            "site",
        ),
    ),
    ImportField(
        key="owner",
        label="Owner",
        required=False,
        type=FieldType.TEXT,
        help="The CSM who owns the account",
        synonyms=(
            "owner",
            "owner name",
            "account owner",
            "account owner name",
            "relationship owner",
            "csm",
            "csm name",
            "csm owner",
            "csm assigned",
            "assigned csm",
            "account csm",
            "customer success manager",
            "customer success manager name",
            "customer success owner",
            "cs manager",
            "cs owner",
            "account manager",
            "success manager",
            "assigned to",
        ),
    ),
    ImportField(
        key="tier",
        label="Tier",
        required=False,
        type=FieldType.TEXT,
        help="Plan or segment, e.g. Enterprise",
        synonyms=(
            "tier",
            "customer tier",
            "account tier",
            "service tier",
            "product tier",
            "license tier",
            "plan",
            "plan name",
            "plan tier",
            "subscription plan",
            "subscription tier",
            "segment",
            "customer segment",
            "account segment",
            "market segment",
            "edition",
            "package",
            "product",
            "product name",
        ),
    ),
    ImportField(
        key="start_date",
        label="Customer since",
        required=False,
        type=FieldType.DATE,
        help="When they became a customer",
        synonyms=(
            "customer since",
            "customer start",
            "customer start date",
            "start date",
            "start",
            "started",
            "contract start",
            "contract start date",
            "start of contract",
            "subscription start",
            "subscription start date",
            "go live",
            "go live date",
            "became a customer",
            "onboarded",
            "onboarding start",
            "onboarding start date",
            "first contract date",
            "kickoff date",
        ),
    ),
    ImportField(
        key="milestones_total",
        label="Onboarding milestones",
        required=False,
        type=FieldType.INTEGER,
        help="How many onboarding milestones in total",
        synonyms=(
            "onboarding milestones",
            "onboarding milestones total",
            "total onboarding milestones",
            "milestones total",
            "total milestones",
            "milestones",
            "milestone count",
            "number of milestones",
            "onboarding steps",
            "onboarding steps total",
            "total steps",
            "plan steps",
            "onboarding plan steps",
        ),
    ),
    ImportField(
        key="milestones_done",
        label="Milestones done",
        required=False,
        type=FieldType.INTEGER,
        help="Onboarding milestones completed",
        synonyms=(
            "milestones done",
            "done milestones",
            "milestones completed",
            "completed milestones",
            "milestones complete",
            "milestones achieved",
            "onboarding milestones done",
            "onboarding milestones completed",
            "steps done",
            "steps completed",
            "completed steps",
            "onboarding steps done",
            "onboarding steps completed",
        ),
    ),
    ImportField(
        key="milestones_overdue",
        label="Milestones overdue",
        required=False,
        type=FieldType.INTEGER,
        help="Onboarding milestones past their date",
        synonyms=(
            "milestones overdue",
            "overdue milestones",
            "onboarding milestones overdue",
            "overdue onboarding milestones",
            "milestones late",
            "late milestones",
            "milestones past due",
            "past due milestones",
            "milestones behind",
            "behind milestones",
            "overdue steps",
            "steps overdue",
            "overdue onboarding steps",
        ),
    ),
    ImportField(
        key="signal:usage_trend_30d",
        label="Usage trend",
        required=False,
        type=FieldType.NUMBER,
        help="% change in active users over 30 days, e.g. -25",
        synonyms=(
            "usage trend",
            "usage trend 30d",
            "usage trend 30 days",
            "usage trend percent",
            "usage change",
            "usage change 30d",
            "usage change 30 days",
            "usage change percent",
            "30 day usage change",
            "30d usage change",
            "usage growth",
            "active users change",
            "active users change 30d",
            "active users change 30 days",
            "active users change percent",
            "active users 30 day change",
            "active users trend",
            "active user change",
            "change in active users",
            "user activity change",
            "mau change",
        ),
    ),
    ImportField(
        key="signal:days_since_last_login",
        label="Days since last login",
        required=False,
        type=FieldType.INTEGER,
        help="Days since anyone logged in",
        synonyms=(
            "days since last login",
            "days since login",
            "days since last logon",
            "days since logon",
            "days since last seen",
            "days since last activity",
            "days since active",
            "days since last active",
            "last login days",
            "last active days",
            "days inactive",
            "inactive days",
            "days without login",
        ),
    ),
    ImportField(
        key="signal:open_tickets_sev1",
        label="Open severity-1 tickets",
        required=False,
        type=FieldType.INTEGER,
        help="How many urgent tickets are open",
        synonyms=(
            "open severity 1 tickets",
            "open severity 1",
            "severity 1 tickets",
            "severity 1",
            "open sev1 tickets",
            "open sev 1 tickets",
            "open sev1",
            "open sev 1",
            "sev1 tickets",
            "sev 1 tickets",
            "sev1",
            "sev 1",
            "sev1 open tickets",
            "open tickets sev1",
            "open tickets severity 1",
            "p1 tickets",
            "open p1 tickets",
            "p1",
            "open p1",
            "urgent tickets",
            "open urgent tickets",
            "urgent open tickets",
            "critical tickets",
            "open critical tickets",
        ),
    ),
    ImportField(
        key="signal:nps_latest",
        label="NPS",
        required=False,
        type=FieldType.NUMBER,
        help="Latest NPS response, 0 to 10",
        synonyms=(
            "nps",
            "nps score",
            "nps rating",
            "nps response",
            "nps latest",
            "latest nps",
            "latest nps score",
            "latest nps response",
            "last nps",
            "nps last",
            "net promoter score",
            "promoter score",
        ),
    ),
    ImportField(
        key="signal:champion_left",
        label="Champion left",
        required=False,
        type=FieldType.BOOLEAN,
        help="Yes if your main contact has left",
        synonyms=(
            "champion left",
            "champion left company",
            "champion left the company",
            "champion has left",
            "champion has left the company",
            "champion churned",
            "champion departed",
            "champion gone",
            "champion lost",
            "main contact left",
            "main contact has left",
            "contact left",
        ),
    ),
    ImportField(
        key="signal:seat_utilisation",
        label="Seats in use",
        required=False,
        type=FieldType.NUMBER,
        help="% of purchased seats in use, e.g. 64",
        synonyms=(
            "seat utilisation",
            "seat utilization",
            "seat utilisation percent",
            "seat utilization percent",
            "seat utilisation pct",
            "seat utilization pct",
            "seat usage",
            "seat use",
            "seats in use",
            "seats used",
            "seats used percent",
            "seats used percentage",
            "seats used pct",
            "seat percent used",
            "percent of seats used",
            "license usage",
            "licence usage",
            "license utilisation",
            "licence utilisation",
            "license utilization",
            "licence utilization",
            "licenses used",
            "licences used",
        ),
    ),
    ImportField(
        key="signal:billing_overdue_days",
        label="Days invoice overdue",
        required=False,
        type=FieldType.INTEGER,
        help="Days the oldest invoice is overdue",
        synonyms=(
            "days invoice overdue",
            "invoice overdue days",
            "invoice overdue",
            "invoice days overdue",
            "overdue invoice days",
            "days overdue",
            "overdue days",
            "days overdue invoice",
            "days past due",
            "days late",
            "days in arrears",
            "billing overdue",
            "billing overdue days",
            "payment overdue days",
            "aging days",
            "ageing days",
        ),
    ),
)


# The import field for each signal the book defines (config.CustomSignal) is keyed
# "signal:custom:<id>", the same text as "signal:" followed by the signal's kind.
_CUSTOM_FIELD_PREFIX = "signal:" + CUSTOM_PREFIX


def _custom_field(signal: CustomSignal) -> ImportField:
    """The import field for one of the book's own signals. Its label is the signal's name, so a
    column with that header (any case and spacing) matches it through auto_map's exact match."""
    help_text = "One of your own signals"
    if signal.unit:
        help_text += f", in {signal.unit}"
    return ImportField(
        key=f"signal:{signal.kind.value}",
        label=signal.name,
        required=False,
        type=FieldType.NUMBER,
        help=help_text,
        synonyms=(),
    )


def import_fields(config: ScoringConfig | None = None) -> tuple[ImportField, ...]:
    """The import fields for a book: the built-in IMPORT_FIELDS in their order, then one field per
    signal the book defines (config.custom), in the order they were added. Without a config, the
    open book's own is read (config.load_scoring_config)."""
    scoring = config if config is not None else load_scoring_config()
    return (*IMPORT_FIELDS, *(_custom_field(signal) for signal in scoring.custom))


def is_custom_field(key: str) -> bool:
    """True for the import field of one of the book's own signals ("signal:custom:<id>")."""
    return key.startswith(_CUSTOM_FIELD_PREFIX)


def signal_kind_for_field(key: str) -> SignalKey | None:
    """The signal a "signal:*" import field reads: a SignalKind for a built-in field, a CustomKind
    for one of the book's own, and None for any other field."""
    kind = SIGNAL_FIELD_KINDS.get(key)
    if kind is not None or not is_custom_field(key):
        return kind
    try:
        return CustomKind(key.removeprefix("signal:"))
    except ValueError:
        return None


class DateOrder(StrEnum):
    DMY = "dmy"
    MDY = "mdy"
    YMD = "ymd"


class Table(BaseModel):
    """The rows of a file or paste. Importers refuse a table with more than MAX_COLUMNS columns
    with text, a cell longer than MAX_CELL_CHARS characters, or more than MAX_ROWS data rows."""

    source_name: str  # file name, or "Pasted cells"
    headers: list[str]  # stripped; blank headers become "Column <n>"
    rows: list[list[str]]  # every row padded or cut to len(headers); fully blank rows dropped
    # The spreadsheet row number of each entry in `rows` (header = 1), so issues point at the
    # right row even when blank rows were dropped. Same length as `rows`. When empty (a Table
    # built by hand), rows are numbered 2, 3, 4… in order.
    row_numbers: list[int] = []
    sheet_names: list[str] = []  # Excel only: every sheet, so the CSM can pick another
    sheet: str | None = None  # Excel only: the sheet these rows came from


class ImportError_(ValueError):
    """A file or paste that can't be read at all. str() is a plain-English message."""


class ImportIssue(BaseModel):
    row: int  # spreadsheet row number: the header is row 1, so the first data row is 2
    field: str | None  # import field key, or None for a whole-row problem
    message: str  # e.g. "Renewal date \"31/31/2026\" isn't a date."


class ImportResult(BaseModel):
    accounts: list[Account]
    signals: list[Signal]
    issues: list[ImportIssue]  # every problem, including ones on rows that were still imported
    skipped_rows: int  # rows left out (missing or invalid required field, duplicate id)
    ready_rows: int  # rows that became accounts


# ---------------------------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------------------------

_QUOTE_LIMIT = 40  # quoted cell values are cut to this many characters in messages
_SLUG_LIMIT = 40  # account id slugs
_ZIP_MAGIC = b"PK\x03\x04"
_Record = tuple[int, list[str]]  # (spreadsheet row number, cells); blank rows included
_EXCEL_EPOCH = date(1899, 12, 30)
_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTHS: dict[str, int] = {
    **{name: number for number, name in enumerate(_MONTH_NAMES, start=1)},
    **{name[:3]: number for number, name in enumerate(_MONTH_NAMES, start=1)},
    "sept": 9,
}

# Every symbol and code Settings > Profile & region can show (settings.Currency), so an amount
# the app wrote ("CA$120,000", "₹4,00,000") reads back. The prefixed dollars go first.
_CURRENCY_SYMBOL = re.compile(r"(?:CA|A|NZ)\$|[$€£¥₹]", re.IGNORECASE)
_CURRENCY_CODE = re.compile(
    r"(?<![A-Za-z])(?:USD|EUR|GBP|CAD|AUD|NZD|INR|JPY)(?![A-Za-z])", re.IGNORECASE
)
_MONEY_SUFFIXES = (  # checked in this order, so "bn" isn't read as "n"
    ("bn", Decimal(1_000_000_000)),
    ("k", Decimal(1_000)),
    ("m", Decimal(1_000_000)),
)
# digits in groups of three, with an optional decimal part of one or two digits
_GROUPED_DECIMAL = re.compile(r"(\d+(?:[.,]\d{3})*)(?:[.,](\d{1,2}))?")
_TIME_TAIL = re.compile(
    r"(.+?)(?:\s+|T)\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*[ap]\.?m\.?)?"
    r"(?:\s*(?:z|utc|[+-]\d{2}:?\d{2}))?",
    re.IGNORECASE,
)
_YEAR_FIRST_DATE = re.compile(r"(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})")
_NUMERIC_DATE = re.compile(r"(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4}|\d{2})")
_DAY_FIRST_NAMED = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?[-/. ]+([a-z]+)\.?[-/. ,]+(\d{4}|\d{2})", re.IGNORECASE
)
_MONTH_FIRST_NAMED = re.compile(
    r"([a-z]+)\.?[-/. ]+(\d{1,2})(?:st|nd|rd|th)?,?[-/. ]+(\d{4}|\d{2})", re.IGNORECASE
)
_COMPACT_DATE = re.compile(r"\d{8}")  # 20261201
_SERIAL_DATE = re.compile(r"(\d+)(?:\.\d+)?")  # Excel serial, e.g. 46357
_CONNECTION = re.compile(r"sheet-(\d+)")
_FEED_CONNECTION = re.compile(r"feed-(\d+)")

# Sanity ranges for optional signal fields. Values outside them are left out, with an issue.
_RANGES: dict[str, tuple[int, int]] = {
    "signal:nps_latest": (0, 10),
    "signal:seat_utilisation": (0, 200),
    "signal:usage_trend_30d": (-100, 1000),
}
_FIELD_INDEX = {spec.key: index for index, spec in enumerate(IMPORT_FIELDS)}  # built-in fields only
# Done and overdue only count when the row has a milestones total (the total comes first in
# IMPORT_FIELDS, so it has been read by the time these are).
_NEEDS_TOTAL = frozenset({"milestones_done", "milestones_overdue"})
# Headers about monthly revenue are not annual recurring revenue, so they never map to ARR.
_MONTHLY_WORDS = ("monthly", "mrr", "per month")
# Account text fields longer than this get an issue: a required one skips the row, an optional
# one is left empty and the row is kept.
_ACCOUNT_TEXT_CHARS = 200
_LIMITED_TEXT = frozenset({"name", "owner", "tier"})
# A value longer than this isn't a date. Normal "date time" values are far shorter, and the time
# pattern is slow on long runs of spaces.
_DATE_TEXT_CHARS = 64
# The line breaks str.splitlines() splits on, so the delimiter sample is cut the same way.
_LINE_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")

_EMPTY_FILE = "That file is empty."
_EMPTY_PASTE = "The pasted text is empty."
_NOT_EXCEL = "That file isn't a readable Excel file. Save it as .xlsx or CSV and try again."
_TOO_LARGE_UNPACKED = "That workbook is too large once unpacked."
_XLS = "Old Excel files (.xls) aren't supported. Save it as .xlsx or CSV and try again."
_TOO_BIG_FILE = (
    f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB. Split it or remove extra columns."
)
_TOO_BIG_PASTE = (
    f"That paste is larger than {MAX_BYTES // (1024 * 1024)} MB. Split it or remove extra columns."
)
_TOO_MANY_FILE = f"That file has more than {MAX_ROWS:,} rows."
_TOO_MANY_PASTE = f"That paste has more than {MAX_ROWS:,} rows."
_UNREADABLE_FILE = "That file couldn't be read as a table. Save it as .xlsx or CSV and try again."
_UNREADABLE_PASTE = "The pasted cells couldn't be read as a table. Copy them again and try again."

_TEMPLATE_EXAMPLES: tuple[dict[str, str], ...] = (
    {
        "name": "Larkspur Freight",
        "arr": "$184,000",
        "renewal_date": "2026-12-01",
        "external_id": "LF-1001",
        "domain": "larkspur.example",
        "owner": "Dana Whitfield",
        "tier": "Enterprise",
        "start_date": "2023-03-01",
        "milestones_total": "6",
        "milestones_done": "4",
        "milestones_overdue": "1",
        "signal:usage_trend_30d": "-38",
        "signal:days_since_last_login": "44",
        "signal:open_tickets_sev1": "1",
        "signal:nps_latest": "8",
        "signal:champion_left": "yes",
        "signal:seat_utilisation": "64",
        "signal:billing_overdue_days": "0",
    },
    {
        "name": "Quillon Health",
        "arr": "$96,500",
        "renewal_date": "2026-11-15",
        "external_id": "QH-2040",
        "domain": "quillon.example",
        "owner": "Sam Okafor",
        "tier": "Mid-market",
        "start_date": "2024-07-15",
        "milestones_total": "5",
        "milestones_done": "5",
        "milestones_overdue": "0",
        "signal:usage_trend_30d": "6",
        "signal:days_since_last_login": "3",
        "signal:open_tickets_sev1": "0",
        "signal:nps_latest": "9",
        "signal:champion_left": "no",
        "signal:seat_utilisation": "81",
        "signal:billing_overdue_days": "0",
    },
)


def _quote(text: str) -> str:
    """The offending value in double quotes, cut to _QUOTE_LIMIT characters."""
    shown = " ".join(text.split())
    if len(shown) > _QUOTE_LIMIT:
        shown = shown[: _QUOTE_LIMIT - 1] + "…"
    return f'"{shown}"'


def _normalise_header(text: str) -> str:
    """Casefold, punctuation to spaces, spaces collapsed. The form auto_map compares."""
    return " ".join(re.sub(r"[\W_]+", " ", text.casefold()).split())


def _may_map(key: str, text: str) -> bool:
    """False when a normalised header must not map to this field."""
    if key != "arr":
        return True
    padded = f" {text} "
    return not any(f" {word} " in padded for word in _MONTHLY_WORDS)


def _parse_decimal(body: str) -> Decimal | None:
    """Digits with optional thousands groups and a decimal part, no sign or spaces. The last
    "," or "." followed by 1-2 digits is the decimal separator; other separators must group
    digits in threes."""
    match = _GROUPED_DECIMAL.fullmatch(body)
    if match is None:
        return None
    whole = match.group(1).replace(".", "").replace(",", "")
    fraction = match.group(2)
    return Decimal(f"{whole}.{fraction}") if fraction else Decimal(whole)


def _without_time(text: str) -> str:
    """Drop a trailing time such as " 00:00:00" or "T10:30Z", so the date part can be read."""
    if len(text) > _DATE_TEXT_CHARS:
        return text  # too long to hold a date; the time pattern is slow on it
    match = _TIME_TAIL.fullmatch(text)
    return match.group(1).strip() if match else text


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _two_digit_year(text: str) -> int:
    return int(text) + 2000 if len(text) == 2 else int(text)


def _connection_prefix(connection_id: str) -> str:
    """ "sheet-<n>" gives "sp<n>-" and a feed's "feed-<n>" gives "fd<n>-"."""
    match = _CONNECTION.fullmatch(connection_id)
    if match is not None:
        return f"sp{match.group(1)}-"
    match = _FEED_CONNECTION.fullmatch(connection_id)
    if match is not None:
        return f"fd{match.group(1)}-"
    raise ValueError(
        f"Connection id {_quote(connection_id)} isn't in the form sheet-<n> or feed-<n>."
    )


def _slug(text: str) -> str:
    """Lowercase ASCII, other characters to "-", collapsed, trimmed, at most _SLUG_LIMIT."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-")
    return slug[:_SLUG_LIMIT].rstrip("-")


def _fallback_slug(text: str) -> str:
    """For names with no ASCII letters or digits: a stable short hash, so ids are still unique."""
    return "account-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _normalise_domain(text: str) -> str | None:
    """A website as a bare domain ("https://www.Acme.com/about" gives "acme.com"), or None when
    what's left isn't a domain. A private copy of the rule; importing never depends on matching."""
    value = text.strip().casefold()
    value = re.sub(r"^(?:[a-z][a-z0-9+.-]*:)?//", "", value)  # scheme
    value = re.split(r"[/?#]", value, maxsplit=1)[0]  # path, query, fragment
    value = value.rsplit("@", 1)[-1]  # user:password@
    value = re.sub(r":\d+$", "", value)  # port
    value = value.rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    if "." not in value:
        return None
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    label = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    if re.fullmatch(rf"(?:{label}\.)+{label}", ascii_value) is None:
        return None
    return ascii_value


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _clamp_milestones(total: int, done: int, overdue: int) -> tuple[Milestones, bool]:
    """Keep done within total and overdue within what's left. Also reports whether it changed."""
    fixed_done = min(done, total)
    fixed_overdue = min(overdue, total - fixed_done)
    changed = (fixed_done, fixed_overdue) != (done, overdue)
    return Milestones(done=fixed_done, total=total, overdue=fixed_overdue), changed


def _parse_count(text: str) -> int:
    """A whole number of zero or more (milestones, days, counts)."""
    raw = text.strip()
    try:
        number = parse_number(raw)
    except ValueError:
        raise ValueError(f"{_quote(raw)} isn't a whole number.") from None
    if number < 0:
        raise ValueError(f"{_quote(raw)} can't be negative.")
    if not number.is_integer():
        raise ValueError(f"{_quote(raw)} isn't a whole number.")
    return int(number)


def _in_range(key: str, text: str, value: float) -> float:
    bounds = _RANGES.get(key)
    if bounds is None:
        # A number with no range of its own (one of the book's signals) only has to be finite:
        # a run of digits too long for a float reads as infinity.
        if not math.isfinite(value):
            raise ValueError(f"{_quote(text)} isn't a number.")
        return value
    if not bounds[0] <= value <= bounds[1]:
        raise ValueError(
            f"{_quote(text)} should be between {bounds[0]} and {bounds[1]}, so it was left out."
        )
    return value


def _convert(spec: ImportField, text: str, date_order: DateOrder) -> Any:
    """One non-blank cell as its field's value. Raises ValueError with the message that follows
    the field label in an issue."""
    if spec.key == "domain":
        domain = _normalise_domain(text)
        if domain is None:
            raise ValueError(f"{_quote(text)} isn't a web address.")
        return domain
    match spec.type:
        case FieldType.TEXT:
            return text
        case FieldType.MONEY:
            return parse_money(text)
        case FieldType.DATE:
            return parse_date(text, date_order)
        case FieldType.INTEGER:
            return _parse_count(text)
        case FieldType.NUMBER:
            return _in_range(spec.key, text, parse_number(text))
        case FieldType.BOOLEAN:
            return 1.0 if parse_bool(text) else 0.0


def _mapped_columns(
    table: Table, mapping: dict[str, str], fields: tuple[ImportField, ...] = IMPORT_FIELDS
) -> dict[str, int]:
    """Field key to the column index of the header it's mapped to. A key for one of the book's own
    signals that the book no longer has is left out: a saved mapping gone out of date, not wrong."""
    known = {spec.key for spec in fields}
    columns: dict[str, int] = {}
    for key, header in mapping.items():
        if key not in known:
            if is_custom_field(key):
                continue
            raise ValueError(f"{_quote(key)} isn't an import field.")
        if header not in table.headers:
            raise ValueError(f"{_quote(header)} isn't a column in this file.")
        columns[key] = table.headers.index(header)
    return columns


def _cell(row: list[str], columns: dict[str, int], key: str) -> str | None:
    """The stripped cell for a field, None when the field isn't mapped."""
    index = columns.get(key)
    if index is None:
        return None
    return row[index].strip() if index < len(row) else ""


def _row_numbers(table: Table) -> list[int]:
    """The spreadsheet row number of each entry in table.rows: the table's own when it has
    them, otherwise 2, 3, 4… (a Table built by hand)."""
    if not table.row_numbers:
        return [index + 2 for index in range(len(table.rows))]
    if len(table.row_numbers) != len(table.rows):
        raise ValueError("The row numbers don't match the rows in this table.")
    return list(table.row_numbers)


@dataclass
class _ParsedRow:
    fields: tuple[ImportField, ...] = IMPORT_FIELDS  # the fields the index in a problem refers to
    values: dict[str, Any] = dc_field(default_factory=dict)  # usable values, by field key
    problems: list[tuple[int, ImportIssue]] = dc_field(default_factory=list)  # (field index, issue)
    blocked: bool = False  # a required field is missing or invalid, or the id is taken
    milestones: Milestones = dc_field(default_factory=lambda: Milestones(done=0, total=0))

    def problem(self, number: int, index: int, message: str, *, blocks: bool) -> None:
        issue = ImportIssue(row=number, field=self.fields[index].key, message=message)
        self.problems.append((index, issue))
        self.blocked = self.blocked or blocks


def _parse_row(
    row: list[str],
    columns: dict[str, int],
    number: int,
    date_order: DateOrder,
    fields: tuple[ImportField, ...] = IMPORT_FIELDS,
) -> _ParsedRow:
    parsed = _ParsedRow(fields=fields)
    for index, spec in enumerate(fields):
        cell = _cell(row, columns, spec.key)
        if spec.key in _NEEDS_TOTAL and "milestones_total" not in parsed.values:
            continue  # no total for this row: done and overdue are ignored, with no issue
        if not cell:
            if spec.required:
                missing = (
                    f"{spec.label} isn't matched to a column."
                    if cell is None
                    else f"{spec.label} is empty."
                )
                parsed.problem(number, index, missing, blocks=True)
            continue
        if spec.key in _LIMITED_TEXT and len(cell) > _ACCOUNT_TEXT_CHARS:
            # A required field skips the row; an optional one is left empty and the row is kept.
            parsed.problem(
                number,
                index,
                f"{spec.label} is longer than {_ACCOUNT_TEXT_CHARS} characters.",
                blocks=spec.required,
            )
            continue
        try:
            parsed.values[spec.key] = _convert(spec, cell, date_order)
        except ValueError as exc:
            parsed.problem(number, index, f"{spec.label} {exc}", blocks=spec.required)
    return parsed


def _apply_milestones(parsed: _ParsedRow, number: int) -> None:
    """Without a total the row has no milestones (0 of 0): done and overdue were ignored when the
    row was read, with no issue. With a total, done and overdue are clamped to fit it."""
    if "milestones_total" not in parsed.values:
        parsed.milestones = Milestones(done=0, total=0)
        return
    milestones, changed = _clamp_milestones(
        parsed.values["milestones_total"],
        parsed.values.get("milestones_done", 0),
        parsed.values.get("milestones_overdue", 0),
    )
    if changed:
        parsed.problem(
            number,
            _FIELD_INDEX["milestones_total"],
            "Onboarding milestones didn't add up, so they were adjusted.",
            blocks=False,
        )
    parsed.milestones = milestones


def _ordered(problems: list[tuple[int, ImportIssue]]) -> list[ImportIssue]:
    """Issues in IMPORT_FIELDS order; the sort is stable, so each field keeps its own order."""
    return [issue for _, issue in sorted(problems, key=lambda item: item[0])]


def _sniff_delimiter(text: str) -> str:
    """The delimiter csv.Sniffer finds in the first 50 lines. Only those lines are split off, so
    a file of millions of blank lines isn't split whole."""
    lines: list[str] = []
    start = 0
    for found in _LINE_BREAK.finditer(text):
        lines.append(text[start : found.start()])
        start = found.end()
        if len(lines) == 50:
            break
    if len(lines) < 50 and start < len(text):
        lines.append(text[start:])
    sample = "\n".join(lines)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _decode(data: bytes) -> str:
    """UTF-16 (with BOM), UTF-8 with or without BOM, then cp1252; latin-1 as a last resort."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8) :]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _unique_headers(cells: list[str]) -> list[str]:
    """Blank headers become "Column <n>"; repeats get " (2)", " (3)", and so on. Linear: the
    next copy number to try is remembered for each name, and a number below it is always taken."""
    used: set[str] = set()
    next_copy: dict[str, int] = {}
    headers: list[str] = []
    for position, cell in enumerate(cells, start=1):
        base = cell or f"Column {position}"
        name = base
        if name in used:
            copy = next_copy.get(base, 2)
            while f"{base} ({copy})" in used:
                copy += 1
            name = f"{base} ({copy})"
            next_copy[base] = copy + 1
        used.add(name)
        headers.append(name)
    return headers


def _strip_cells(cells: list[str]) -> list[str]:
    """The cells, stripped. A cell longer than MAX_CELL_CHARS is refused."""
    stripped = [cell.strip() for cell in cells]
    if any(len(cell) > MAX_CELL_CHARS for cell in stripped):
        raise ImportError_(CELL_TOO_LONG_MESSAGE)
    return stripped


def _build_table(
    records: Iterable[_Record],
    source_name: str,
    *,
    what: str,
    empty: str,
    sheet_names: list[str] | None = None,
    sheet: str | None = None,
) -> Table:
    """Strip cells, drop blank rows, take the first kept row as headers and fit every row to them.
    The records are read one at a time and reading stops at the first limit broken, so a huge
    file is refused without being read whole. The kept data rows keep their spreadsheet row
    numbers, so issues point at the right row."""
    too_many = _TOO_MANY_PASTE if what == "That paste" else _TOO_MANY_FILE
    header: list[str] | None = None  # every stripped cell of the header row, untrimmed
    numbers: list[int] = []
    body: list[list[str]] = []  # each row cut to the header's width, trailing blanks dropped
    for number, cells in records:
        if not any(cells):  # the usual blank line, skipped before any other work
            continue
        row = _strip_cells(cells)
        if not any(row):
            continue
        if header is None:
            if any(row[MAX_COLUMNS:]):
                raise ImportError_(TOO_MANY_COLUMNS_MESSAGE)
            header = row
            continue
        width = len(header)
        if width > MAX_COLUMNS and any(row[MAX_COLUMNS:width]):
            raise ImportError_(TOO_MANY_COLUMNS_MESSAGE)
        if len(body) == MAX_ROWS:
            raise ImportError_(too_many)
        fitted = row[:width]
        while fitted and not fitted[-1]:
            fitted.pop()
        body.append(fitted)
        numbers.append(number)
    if header is None:
        raise ImportError_(empty)
    # Trailing columns that are blank in the header and in every data row aren't columns.
    head = list(header)
    while head and not head[-1]:
        head.pop()
    width = max([len(head), *(len(row) for row in body)])
    fitted_rows = [row + [""] * (width - len(row)) for row in body]
    return Table(
        source_name=source_name,
        headers=_unique_headers(header[:width]),
        rows=fitted_rows,
        row_numbers=numbers,
        sheet_names=list(sheet_names or []),
        sheet=sheet,
    )


def _table_from_text(text: str, source_name: str, *, delimiter: str | None, pasted: bool) -> Table:
    """Read delimited text. A delimiter of None means sniff it."""
    what = "That paste" if pasted else "That file"
    if not text.strip():
        raise ImportError_(_EMPTY_PASTE if pasted else _EMPTY_FILE)
    if delimiter is None:
        delimiter = _sniff_delimiter(text)
    # One record per csv row, read lazily: a quoted cell that spans lines is still one record,
    # and a blank line is a record of its own, so the numbers match the sheet.
    records = enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), start=1)
    try:
        return _build_table(
            records,
            source_name,
            what=what,
            empty=_EMPTY_PASTE if pasted else _EMPTY_FILE,
        )
    except csv.Error:
        raise ImportError_(_UNREADABLE_PASTE if pasted else _UNREADABLE_FILE) from None


def _read_text_bytes(data: bytes, source_name: str, *, delimiter: str | None) -> Table:
    if len(data) > MAX_BYTES:
        raise ImportError_(_TOO_BIG_FILE)
    text = _decode(data).lstrip("﻿")
    return _table_from_text(text, source_name, delimiter=delimiter, pasted=False)


def _xlsx_text(value: Any) -> str:
    """One Excel cell as the text the importer reads."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _sheet_records(workbook: Any, name: str) -> Iterator[_Record]:
    """The non-blank rows of a sheet, numbered as Excel numbers them (from 1), read lazily.
    openpyxl pads a read-only sheet's missing cells out to its dimension, so only the first
    MAX_COLUMNS + 1 columns are read; a text cell beyond MAX_COLUMNS is refused by _build_table.
    A padded row is skipped without converting its cells."""
    sheet_obj = workbook[name]
    if not hasattr(sheet_obj, "iter_rows"):  # chart sheets have no cells
        return
    rows = sheet_obj.iter_rows(min_row=1, max_col=MAX_COLUMNS + 1, values_only=True)
    blank: tuple[object, ...] | None = None  # the last empty row seen
    for number, row in enumerate(rows, start=1):
        if row is blank:  # openpyxl hands out one shared object for every missing row
            continue
        if row.count(None) == len(row):
            blank = row
            continue
        yield number, [_xlsx_text(value) for value in row]


def _has_data(records: Iterable[_Record]) -> bool:
    return any(any(cells) for _, cells in records)


def _match_sheet(names: list[str], wanted: str) -> str:
    if wanted in names:
        return wanted
    folded = wanted.casefold()
    for name in names:
        if name.casefold() == folded:
            return name
    raise ImportError_(
        f"There's no sheet called {_quote(wanted)}. Pick one of the sheets in this file."
    )


def _pick_sheet(
    workbook: Any, names: list[str], wanted: str | None
) -> tuple[str | None, Iterable[_Record]]:
    """The named sheet, or else the first sheet with any data. The rows are read lazily, so a
    sheet is read once to check it has data and again, row by row, to build the table."""
    if wanted:
        chosen = _match_sheet(names, wanted)
        if not _has_data(_sheet_records(workbook, chosen)):
            raise ImportError_(f"The sheet {_quote(chosen)} is empty.")
        return chosen, _sheet_records(workbook, chosen)
    for name in names:
        if _has_data(_sheet_records(workbook, name)):
            return name, _sheet_records(workbook, name)
    return (names[0] if names else None), []


# ---------------------------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------------------------


def parse_money(text: str) -> float:
    """ "$120,000", "120000", "120k", "1.2m", "€120.000,50", "120 000" -> a float >= 0.
    The last "," or "." followed by 1-2 digits is the decimal separator; other separators are
    thousands. Raises ValueError (plain English) for blanks, negatives and anything else."""
    raw = text.strip()
    if not raw:
        raise ValueError("is empty.")
    stripped = _CURRENCY_CODE.sub(" ", _CURRENCY_SYMBOL.sub(" ", raw)).strip()
    if re.search(r"[-−]", stripped):
        raise ValueError(f"{_quote(raw)} can't be negative.")
    multiplier = Decimal(1)
    for suffix, factor in _MONEY_SUFFIXES:
        if stripped.casefold().endswith(suffix):
            multiplier = factor
            stripped = stripped[: -len(suffix)]
            break
    number = _parse_decimal(re.sub(r"\s+", "", stripped))
    if number is None:
        raise ValueError(f"{_quote(raw)} isn't an amount.")
    return float(number * multiplier)


def parse_date(text: str, order: DateOrder) -> date:
    """ISO "2026-12-01" always works. Otherwise d/m/y or m/d/y per `order`, with "/", "-", "."
    or spaces; 2-digit years are 20xx; month names ("1 Dec 2026", "Dec 1, 2026") work in
    either order; Excel serial numbers (e.g. "46357") are accepted. Raises ValueError."""
    raw = text.strip()
    if not raw:
        raise ValueError("is empty.")
    if len(raw) > _DATE_TEXT_CHARS:
        raise ValueError(f"{_quote(raw)} isn't a date.")
    value = _without_time(raw)
    found: date | None = None
    if _COMPACT_DATE.fullmatch(value):
        found = _make_date(int(value[:4]), int(value[4:6]), int(value[6:]))
    elif (serial := _SERIAL_DATE.fullmatch(value)) and 20000 <= int(serial.group(1)) <= 80000:
        found = _EXCEL_EPOCH + timedelta(days=int(serial.group(1)))
    elif year_first := _YEAR_FIRST_DATE.fullmatch(value):
        found = _make_date(int(year_first[1]), int(year_first[2]), int(year_first[3]))
    elif numeric := _NUMERIC_DATE.fullmatch(value):
        # An all-ISO column is YMD, so a slash date in it can't be read.
        if order is not DateOrder.YMD:
            first, second = int(numeric[1]), int(numeric[2])
            if order is DateOrder.MDY:
                month, day = first, second
            else:
                day, month = first, second
            found = _make_date(_two_digit_year(numeric[3]), month, day)
    elif (named := _DAY_FIRST_NAMED.fullmatch(value)) and (
        month := _MONTHS.get(named[2].casefold())
    ):
        found = _make_date(_two_digit_year(named[3]), month, int(named[1]))
    elif (named := _MONTH_FIRST_NAMED.fullmatch(value)) and (
        month := _MONTHS.get(named[1].casefold())
    ):
        found = _make_date(_two_digit_year(named[3]), month, int(named[2]))
    if found is None:
        raise ValueError(f"{_quote(raw)} isn't a date.")
    return found


def parse_bool(text: str) -> bool:
    """yes/no, y/n, true/false, 1/0, x/blank (blank is False). Raises ValueError otherwise."""
    word = text.strip().casefold()
    if word in {"yes", "y", "true", "1", "x"}:
        return True
    if word in {"no", "n", "false", "0", ""}:
        return False
    raise ValueError(f"{_quote(text.strip())} isn't yes or no.")


def parse_number(text: str) -> float:
    """A plain number; a trailing "%" and thousands separators are allowed. Raises ValueError."""
    raw = text.strip()
    if not raw:
        raise ValueError("is empty.")
    body = raw[:-1].strip() if raw.endswith("%") else raw
    sign = 1
    if body[:1] in ("-", "−"):
        sign, body = -1, body[1:]
    elif body[:1] == "+":
        body = body[1:]
    number = _parse_decimal(re.sub(r"\s+", "", body))
    if number is None:
        raise ValueError(f"{_quote(raw)} isn't a number.")
    return float(sign * number)


# ---------------------------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------------------------


def read_csv(data: bytes, source_name: str) -> Table:
    """Decode (UTF-8 with or without BOM, then cp1252), sniff the delimiter (comma,
    semicolon, tab, pipe) and read with the csv module. Raises ImportError_ for empty input,
    no header row, more than MAX_ROWS rows, more than MAX_COLUMNS columns with text, a cell
    longer than MAX_CELL_CHARS, or more than MAX_BYTES."""
    return _read_text_bytes(data, source_name, delimiter=None)


def read_pasted(text: str) -> Table:
    """Cells copied from Excel or Google Sheets arrive tab-separated; fall back to the CSV
    sniffer otherwise. source_name is "Pasted cells". Same limits as read_csv."""
    body = text.lstrip("﻿")
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise ImportError_(_TOO_BIG_PASTE)
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    delimiter = "\t" if "\t" in first_line else None
    return _table_from_text(body, "Pasted cells", delimiter=delimiter, pasted=True)


def _check_unpacked_size(data: bytes) -> None:
    """Refuse an .xlsx whose parts would unpack to more than MAX_XLSX_UNCOMPRESSED together or
    MAX_XLSX_MEMBER for one part, judged from the zip's own headers before openpyxl opens it. A
    file that isn't a zip is the same error as a damaged workbook."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            sizes = [info.file_size for info in archive.infolist()]
    except Exception:  # zipfile raises several types for a damaged archive
        raise ImportError_(_NOT_EXCEL) from None
    if max(sizes, default=0) > MAX_XLSX_MEMBER or sum(sizes) > MAX_XLSX_UNCOMPRESSED:
        raise ImportError_(_TOO_LARGE_UNPACKED)


def read_xlsx(data: bytes, source_name: str, sheet: str | None = None) -> Table:
    """Read the named sheet (default: the first one with any data) with openpyxl in read-only,
    data-only mode. Dates become ISO strings, whole floats lose ".0". Raises ImportError_ for a
    file that isn't a valid .xlsx, a missing sheet, the column and cell limits above, or a
    workbook that unpacks to more than MAX_XLSX_UNCOMPRESSED (or a part over MAX_XLSX_MEMBER)."""
    if len(data) > MAX_BYTES:
        raise ImportError_(_TOO_BIG_FILE)
    _check_unpacked_size(data)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # openpyxl raises many types for damaged files
        raise ImportError_(_NOT_EXCEL) from None
    try:
        names = [str(name) for name in workbook.sheetnames]
        chosen, records = _pick_sheet(workbook, names, sheet)
        return _build_table(
            records,
            source_name,
            what="That file",
            empty=_EMPTY_FILE,
            sheet_names=names,
            sheet=chosen,
        )
    except ImportError_:
        raise
    except Exception:
        raise ImportError_(_NOT_EXCEL) from None
    finally:
        workbook.close()


def read_upload(data: bytes, filename: str, sheet: str | None = None) -> Table:
    """Pick the reader by content (the ZIP signature means .xlsx) and extension (.csv, .tsv,
    .txt, .xlsx). .xls (old Excel) raises ImportError_ asking to save as .xlsx or CSV."""
    name = PureWindowsPath(filename).name or filename
    suffix = PureWindowsPath(filename).suffix.lower()
    if suffix == ".xls":
        raise ImportError_(_XLS)
    if data[:4] == _ZIP_MAGIC:
        return read_xlsx(data, name, sheet)
    if suffix in (".csv", ".txt"):
        return _read_text_bytes(data, name, delimiter=None)
    if suffix == ".tsv":
        return _read_text_bytes(data, name, delimiter="\t")
    if suffix == ".xlsx":
        return read_xlsx(data, name, sheet)
    raise ImportError_(
        f"{_quote(name)} isn't a file type Wardmind can read. "
        "Save it as .xlsx or CSV and try again."
    )


# ---------------------------------------------------------------------------------------------
# Mapping and building
# ---------------------------------------------------------------------------------------------


def auto_map(headers: list[str]) -> dict[str, str]:
    """Import field key -> header. Normalize headers (casefold, punctuation to spaces, collapse
    spaces) and match exact synonyms first, then a header that contains a synonym as whole
    words. Each header maps to at most one field; required fields get first pick. The fields are
    import_fields() for the open book, so one of its own signals matches a header of its name."""
    fields = import_fields()
    normalised = [_normalise_header(header) for header in headers]
    # Required fields first. A tie between two fields goes to the earlier one in this order.
    ranked = sorted(fields, key=lambda spec: not spec.required)
    claimed: dict[str, str] = {}  # field key -> header
    taken: set[int] = set()  # header positions already used

    # Exact matches: the normalised label or any synonym, equal to the whole header.
    for spec in ranked:
        names = {*spec.synonyms, _normalise_header(spec.label)}
        for position, text in enumerate(normalised):
            if position not in taken and text and text in names and _may_map(spec.key, text):
                claimed[spec.key] = headers[position]
                taken.add(position)
                break

    # Contains: a synonym as whole words. The longest phrase wins, then one that ends the
    # header ("Company website" is a website), then the required-first order.
    candidates: list[tuple[int, int, int, int, str]] = []
    for rank, spec in enumerate(ranked):
        if spec.key in claimed:
            continue
        for position, text in enumerate(normalised):
            if position in taken or not text or not _may_map(spec.key, text):
                continue
            padded = f" {text} "
            best: tuple[int, int] | None = None
            for synonym in spec.synonyms:
                if f" {synonym} " in padded:
                    score = (len(synonym.split()), int(padded.endswith(f" {synonym} ")))
                    best = score if best is None or score > best else best
            if best is not None:
                candidates.append((-best[0], -best[1], rank, position, spec.key))
    for _, _, _, position, key in sorted(candidates):
        if key in claimed or position in taken:
            continue
        claimed[key] = headers[position]
        taken.add(position)

    return {spec.key: claimed[spec.key] for spec in fields if spec.key in claimed}


def detect_date_order(values: list[str]) -> DateOrder | None:
    """Look at every non-blank value in the date columns. ISO (2026-03-04) means YMD. A first
    number above 12 means DMY; a second number above 12 means MDY. None when nothing decides
    it (every value like 03/04/2026), so the screen asks the CSM."""
    year_first = False
    ambiguous = False
    dmy = False
    mdy = False
    for value in values:
        text = _without_time(value.strip())
        if not text:
            continue
        if _YEAR_FIRST_DATE.fullmatch(text):
            year_first = True
            continue
        numeric = _NUMERIC_DATE.fullmatch(text)
        if numeric is None:
            continue  # month names and serials read the same in either order
        first, second = int(numeric[1]), int(numeric[2])
        if first > 12:
            dmy = True
        elif second > 12:
            mdy = True
        else:
            ambiguous = True
    if dmy and mdy:
        return None
    if dmy:
        return DateOrder.DMY
    if mdy:
        return DateOrder.MDY
    if year_first and not ambiguous:
        return DateOrder.YMD
    return None


def build_import(
    table: Table,
    mapping: dict[str, str],
    *,
    connection_id: str,
    date_order: DateOrder,
    imported_at: datetime,
    today: date,
) -> ImportResult:
    """Turn rows into accounts and signal readings.

    - Account id: "sp<n>-" + slug of the external id if mapped and present, else of the name
      (connection "sheet-<n>"). A repeated id within the file skips the later row.
    - Missing or invalid required field: the row is skipped with an issue per problem.
    - Invalid optional field: the row is kept, that field is left empty, an issue is recorded.
    - start_date stays None when the column is unmapped or the cell is blank (never today);
      tier defaults to "Unknown"; owner to "Unassigned".
    - Milestones are clamped to a valid Milestones with an issue when they don't add up.
    - Each mapped signal with a value becomes Signal(source="spreadsheet",
      observed_at=imported_at as naive UTC). The import field "signal:open_tickets_sev1"
      becomes SignalKind.OPEN_TICKETS_SEV1, and so on (see SIGNAL_FIELD_KINDS); one of the
      book's own, "signal:custom:<id>", becomes its CustomKind (see signal_kind_for_field).
    - A mapped milestones_overdue value also becomes an ONBOARDING_OVERDUE_MILESTONES reading,
      as with HubSpot.
    """
    fields = import_fields()
    prefix = _connection_prefix(connection_id)
    columns = _mapped_columns(table, mapping, fields)
    observed_at = _naive_utc(imported_at)
    accounts: list[Account] = []
    signals: list[Signal] = []
    issues: list[ImportIssue] = []
    seen: dict[str, int] = {}  # account id -> the row that took it
    skipped = 0

    for number, row in zip(_row_numbers(table), table.rows, strict=True):
        parsed = _parse_row(row, columns, number, date_order, fields)
        _apply_milestones(parsed, number)
        if parsed.blocked:
            skipped += 1
            issues.extend(_ordered(parsed.problems))
            continue

        name = parsed.values["name"]
        external = parsed.values.get("external_id")
        key_text = external or name
        account_id = prefix + (_slug(key_text) or _fallback_slug(key_text))
        if account_id in seen:
            skipped += 1
            first = seen[account_id]
            if external:
                message = f"ID {_quote(external)} is also on row {first}, so this row was skipped."
                index_for_issue = _FIELD_INDEX["external_id"]
            else:
                message = (
                    f"Account name {_quote(name)} is also on row {first}, so this row was skipped."
                )
                index_for_issue = _FIELD_INDEX["name"]
            parsed.problem(number, index_for_issue, message, blocks=True)
            issues.extend(_ordered(parsed.problems))
            continue
        seen[account_id] = number

        milestones = parsed.milestones
        start = parsed.values.get("start_date")
        account = Account(
            id=account_id,
            name=name,
            arr=parsed.values["arr"],
            tier=parsed.values.get("tier") or "Unknown",
            owner=parsed.values.get("owner") or "Unassigned",
            start_date=start,
            renewal_date=parsed.values["renewal_date"],
            milestones=milestones,
            domain=parsed.values.get("domain"),
        )
        accounts.append(account)

        for spec in fields:
            kind = signal_kind_for_field(spec.key)
            if kind is not None and spec.key in parsed.values:
                signals.append(
                    Signal(
                        account_id=account_id,
                        kind=kind,
                        value=float(parsed.values[spec.key]),
                        observed_at=observed_at,
                        source="spreadsheet",
                    )
                )
        if "milestones_overdue" in parsed.values:
            signals.append(
                Signal(
                    account_id=account_id,
                    kind=SignalKind.ONBOARDING_OVERDUE_MILESTONES,
                    value=float(milestones.overdue),
                    observed_at=observed_at,
                    source="spreadsheet",
                )
            )
        issues.extend(_ordered(parsed.problems))

    return ImportResult(
        accounts=accounts,
        signals=signals,
        issues=issues,
        skipped_rows=skipped,
        ready_rows=len(accounts),
    )


def merge_into_snapshot_signals(
    previous: list[Signal], new: list[Signal], kept_ids: set[str]
) -> list[Signal]:
    """Signal history across re-imports: keep previous readings for accounts still present,
    and append a new reading only when its value differs from that account's latest reading of
    the same kind (so re-importing an unchanged file adds nothing)."""
    kept = [signal for signal in previous if signal.account_id in kept_ids]
    latest: dict[tuple[str, SignalKey], Signal] = {}
    for signal in kept:
        key = (signal.account_id, signal.kind)
        current = latest.get(key)
        if current is None or signal.observed_at >= current.observed_at:
            latest[key] = signal
    merged = list(kept)
    for signal in new:
        if signal.account_id not in kept_ids:
            continue
        key = (signal.account_id, signal.kind)
        current = latest.get(key)
        if current is not None and current.value == signal.value:
            continue
        merged.append(signal)
        latest[key] = signal
    return merged


def template_csv(config: ScoringConfig | None = None) -> str:
    """A CSV with every import field's label as the header row (the open book's own signals last)
    and two example rows, ready to fill in. Example names are clearly invented; an own signal's
    column is left blank. A label the CSM typed is made formula-safe, as in issues_csv."""
    fields = import_fields(config)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([_formula_safe(spec.label) for spec in fields])
    for example in _TEMPLATE_EXAMPLES:
        writer.writerow([example.get(spec.key, "") for spec in fields])
    return buffer.getvalue()


def issues_csv(table: Table, issues: list[ImportIssue]) -> str:
    """The rows that have issues, with their original cells plus a "Problems" column, so the
    CSM can fix them and import again."""
    messages: dict[int, list[str]] = {}
    for issue in issues:
        messages.setdefault(issue.row, []).append(issue.message)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([*(_formula_safe(header) for header in table.headers), "Problems"])
    for number, row in zip(_row_numbers(table), table.rows, strict=True):
        if number in messages:
            writer.writerow([_formula_safe(cell) for cell in [*row, " ".join(messages[number])]])
    return buffer.getvalue()


_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"-?\d[\d,]*(\.\d+)?%?")


def _formula_safe(cell: str) -> str:
    """Neutralise a cell that a spreadsheet would run as a formula ("=HYPERLINK(...)", "+1",
    "@SUM(...)") by prefixing an apostrophe. A plain negative number like "-25" or "-1,200.5"
    is left alone, so the downloaded rows still import cleanly after they're fixed."""
    if cell.startswith(_FORMULA_STARTS) and not _PLAIN_NUMBER.fullmatch(cell):
        return "'" + cell
    return cell


SIGNAL_FIELD_KINDS: dict[str, SignalKind] = {
    f"signal:{kind.value}": kind
    for kind in SignalKind
    if kind is not SignalKind.ONBOARDING_OVERDUE_MILESTONES  # comes from milestones_overdue
}
