"""Bringing a book in from a spreadsheet: CSV/TSV files, Excel files, or pasted cells.

The flow: read the file into a Table, auto_map the headers to import fields (the CSM reviews
and corrects the mapping), detect the date order, then build_import turns rows into Accounts and
Signals with a plain-English issue for every row that can't be used.

Only three fields are required: name, ARR and renewal date. Every signal is optional.

CONTRACT FILE: models, constants, signatures and docstrings are fixed; implementers escalate
before changing them. Nothing here touches the network.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel

from cs_brief.core.models import Account, Signal, SignalKind

MAX_BYTES = 5 * 1024 * 1024  # larger files are refused with a plain message
MAX_ROWS = 5000  # data rows; more are refused with a plain message


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
        synonyms=(),
    ),
    ImportField(
        key="arr",
        label="ARR",
        required=True,
        type=FieldType.MONEY,
        help="Annual recurring revenue",
        synonyms=(),
    ),
    ImportField(
        key="renewal_date",
        label="Renewal date",
        required=True,
        type=FieldType.DATE,
        help="When the contract renews",
        synonyms=(),
    ),
    ImportField(
        key="external_id",
        label="ID",
        required=False,
        type=FieldType.TEXT,
        help="Your own ID for the account, so re-imports update the same account",
        synonyms=(),
    ),
    ImportField(
        key="domain",
        label="Website",
        required=False,
        type=FieldType.TEXT,
        help="Helps spot the same customer in another connection",
        synonyms=(),
    ),
    ImportField(
        key="owner",
        label="Owner",
        required=False,
        type=FieldType.TEXT,
        help="The CSM who owns the account",
        synonyms=(),
    ),
    ImportField(
        key="tier",
        label="Tier",
        required=False,
        type=FieldType.TEXT,
        help="Plan or segment, e.g. Enterprise",
        synonyms=(),
    ),
    ImportField(
        key="start_date",
        label="Customer since",
        required=False,
        type=FieldType.DATE,
        help="When they became a customer",
        synonyms=(),
    ),
    ImportField(
        key="milestones_total",
        label="Onboarding milestones",
        required=False,
        type=FieldType.INTEGER,
        help="How many onboarding milestones in total",
        synonyms=(),
    ),
    ImportField(
        key="milestones_done",
        label="Milestones done",
        required=False,
        type=FieldType.INTEGER,
        help="Onboarding milestones completed",
        synonyms=(),
    ),
    ImportField(
        key="milestones_overdue",
        label="Milestones overdue",
        required=False,
        type=FieldType.INTEGER,
        help="Onboarding milestones past their date",
        synonyms=(),
    ),
    ImportField(
        key="signal:usage_trend_30d",
        label="Usage trend",
        required=False,
        type=FieldType.NUMBER,
        help="% change in active users over 30 days, e.g. -25",
        synonyms=(),
    ),
    ImportField(
        key="signal:days_since_last_login",
        label="Days since last login",
        required=False,
        type=FieldType.INTEGER,
        help="Days since anyone logged in",
        synonyms=(),
    ),
    ImportField(
        key="signal:open_tickets_sev1",
        label="Open severity-1 tickets",
        required=False,
        type=FieldType.INTEGER,
        help="How many urgent tickets are open",
        synonyms=(),
    ),
    ImportField(
        key="signal:nps_latest",
        label="NPS",
        required=False,
        type=FieldType.NUMBER,
        help="Latest NPS response, 0 to 10",
        synonyms=(),
    ),
    ImportField(
        key="signal:champion_left",
        label="Champion left",
        required=False,
        type=FieldType.BOOLEAN,
        help="Yes if your main contact has left",
        synonyms=(),
    ),
    ImportField(
        key="signal:seat_utilisation",
        label="Seats in use",
        required=False,
        type=FieldType.NUMBER,
        help="% of purchased seats in use, e.g. 64",
        synonyms=(),
    ),
    ImportField(
        key="signal:billing_overdue_days",
        label="Days invoice overdue",
        required=False,
        type=FieldType.INTEGER,
        help="Days the oldest invoice is overdue",
        synonyms=(),
    ),
)


class DateOrder(StrEnum):
    DMY = "dmy"
    MDY = "mdy"
    YMD = "ymd"


class Table(BaseModel):
    source_name: str  # file name, or "Pasted cells"
    headers: list[str]  # stripped; blank headers become "Column <n>"
    rows: list[list[str]]  # every row padded or cut to len(headers); fully blank rows dropped
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


def read_csv(data: bytes, source_name: str) -> Table:
    """Decode (UTF-8 with or without BOM, then cp1252), sniff the delimiter (comma,
    semicolon, tab, pipe) and read with the csv module. Raises ImportError_ for empty input,
    no header row, more than MAX_ROWS rows or more than MAX_BYTES."""
    raise NotImplementedError


def read_pasted(text: str) -> Table:
    """Cells copied from Excel or Google Sheets arrive tab-separated; fall back to the CSV
    sniffer otherwise. source_name is "Pasted cells". Same limits as read_csv."""
    raise NotImplementedError


def read_xlsx(data: bytes, source_name: str, sheet: str | None = None) -> Table:
    """Read the named sheet (default: the first one with any data) with openpyxl in read-only,
    data-only mode. Dates become ISO strings, whole floats lose ".0". Raises ImportError_ for a
    file that isn't a valid .xlsx, a missing sheet, or the limits above."""
    raise NotImplementedError


def read_upload(data: bytes, filename: str, sheet: str | None = None) -> Table:
    """Pick the reader by content (the ZIP signature means .xlsx) and extension (.csv, .tsv,
    .txt, .xlsx). .xls (old Excel) raises ImportError_ asking to save as .xlsx or CSV."""
    raise NotImplementedError


def auto_map(headers: list[str]) -> dict[str, str]:
    """Import field key -> header. Normalize headers (casefold, punctuation to spaces, collapse
    spaces) and match exact synonyms first, then a header that contains a synonym as whole
    words. Each header maps to at most one field; required fields get first pick."""
    raise NotImplementedError


def detect_date_order(values: list[str]) -> DateOrder | None:
    """Look at every non-blank value in the date columns. ISO (2026-03-04) means YMD. A first
    number above 12 means DMY; a second number above 12 means MDY. None when nothing decides
    it (every value like 03/04/2026), so the screen asks the CSM."""
    raise NotImplementedError


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
    - start_date defaults to today when absent; tier to "Unknown"; owner to "Unassigned".
    - Milestones are clamped to a valid Milestones with an issue when they don't add up.
    - Each mapped signal with a value becomes Signal(source="spreadsheet",
      observed_at=imported_at as naive UTC). The import field "signal:open_tickets_sev1"
      becomes SignalKind.OPEN_TICKETS_SEV1, and so on (see SIGNAL_FIELD_KINDS).
    - A mapped milestones_overdue value also becomes an ONBOARDING_OVERDUE_MILESTONES reading,
      as with HubSpot.
    """
    raise NotImplementedError


def merge_into_snapshot_signals(
    previous: list[Signal], new: list[Signal], kept_ids: set[str]
) -> list[Signal]:
    """Signal history across re-imports: keep previous readings for accounts still present,
    and append a new reading only when its value differs from that account's latest reading of
    the same kind (so re-importing an unchanged file adds nothing)."""
    raise NotImplementedError


def template_csv() -> str:
    """A CSV with every IMPORT_FIELDS label as the header row and two example rows, ready to
    fill in. Example names are clearly invented."""
    raise NotImplementedError


def issues_csv(table: Table, issues: list[ImportIssue]) -> str:
    """The rows that have issues, with their original cells plus a "Problems" column, so the
    CSM can fix them and import again."""
    raise NotImplementedError


SIGNAL_FIELD_KINDS: dict[str, SignalKind] = {
    f"signal:{kind.value}": kind
    for kind in SignalKind
    if kind is not SignalKind.ONBOARDING_OVERDUE_MILESTONES  # comes from milestones_overdue
}
