"""An account with no start date stays unknown (None) everywhere. cs-brief never gives it today's
date, so it is never mistaken for a new customer.

Covers motion, briefs, onboarding, import, editing, linking, HubSpot, the demo book, the web
pages and the MCP tools. The web checks run through the real routes against the temp data
folder from conftest, as test_editing_pages does.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cs_brief
from cs_brief.core import importing, service
from cs_brief.core.briefs import account_detail
from cs_brief.core.config import MotionWindows, ScoringConfig
from cs_brief.core.connectors.combined import CombinedConnector
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.local import ManualConnector
from cs_brief.core.editing import EditError, create_account, edit_account, parse_edit_value
from cs_brief.core.models import Account, Milestones, Motion, Signal
from cs_brief.core.onboarding import onboarding_brief
from cs_brief.core.scoring import classify_motion, score_book
from cs_brief.core.settings import DataSource, Settings, save_settings
from cs_brief.core.store import ConnectionConfig, ConnectionKind, LinkData, LinkGroup, LocalStore
from cs_brief.mcp import server
from cs_brief.web import app as web
from cs_brief.web import formatting
from test_editing_pages import _import
from test_hubspot_connector import edit_batch, make

AS_OF = date(2026, 10, 19)
TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 9, 0)
WINDOWS = MotionWindows()  # onboarding 90 days, renewal window 120 days
ORIGIN = {"origin": "http://127.0.0.1"}
BASE = ["Account name", "ARR", "Renewal date"]
NEW = {"name": "Larkspur Freight", "arr": "$184,000", "renewal_date": "2026-12-01"}
HUBSPOT = ConnectionConfig(
    id="hubspot", kind=ConnectionKind.HUBSPOT, name="HubSpot", created_at=NOW
)
SHEET = ConnectionConfig(
    id="sheet-1", kind=ConnectionKind.SPREADSHEET, name="Q4 renewals sheet", created_at=NOW
)
MANUAL = ConnectionConfig(
    id="manual", kind=ConnectionKind.MANUAL, name="Added by hand", created_at=NOW
)


def _account(
    account_id: str = "acc-x",
    *,
    start: date | None,
    renewal: date = AS_OF + timedelta(days=400),
    done: int = 2,
    total: int = 4,
) -> Account:
    return Account(
        id=account_id,
        name="Test Co",
        arr=50_000,
        tier="Growth",
        owner="Owner",
        start_date=start,
        renewal_date=renewal,
        milestones=Milestones(done=done, total=total),
    )


class _Member:
    """A member connection that serves the accounts it was given and no signals."""

    def __init__(self, accounts: list[Account]) -> None:
        self._accounts = accounts

    def list_accounts(self) -> list[Account]:
        return list(self._accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        return []


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


# Motion and briefs -------------------------------------------------------------------------


def test_an_account_with_no_start_date_is_never_onboarding():
    renewing = _account(start=None, renewal=AS_OF + timedelta(days=30))
    churning = _account(start=None, renewal=AS_OF + timedelta(days=400))
    assert classify_motion(renewing, AS_OF, WINDOWS) is Motion.RENEWAL
    assert classify_motion(churning, AS_OF, WINDOWS) is Motion.CHURN


def test_an_account_with_a_recent_start_date_is_still_onboarding():
    account = _account(start=AS_OF - timedelta(days=10), renewal=AS_OF + timedelta(days=30))
    assert classify_motion(account, AS_OF, WINDOWS) is Motion.ONBOARDING


def _detail(start: date | None, *, renewal: date = AS_OF + timedelta(days=30)):
    config = ScoringConfig()
    book = score_book([_account(start=start, renewal=renewal)], [], config, AS_OF)
    detail = account_detail(book, "acc-x", [], config, AS_OF)
    assert detail is not None
    return detail


def test_the_account_detail_has_no_days_since_start_when_the_start_date_is_unknown():
    detail = _detail(None)
    assert detail.days_since_start is None
    assert detail.days_to_renewal == 30
    assert formatting.motion_fact(detail) == "Renewal in 30 days"


def test_the_onboarding_brief_has_no_day_and_uses_the_not_in_onboarding_wording():
    brief = onboarding_brief(_detail(None), WINDOWS)
    assert brief.day is None
    assert brief.in_onboarding is False
    assert brief.summary.startswith("2 of 4 onboarding steps done.")
    assert "days left" not in brief.summary


def test_the_plain_text_facts_line_says_the_start_date_is_not_known():
    brief = onboarding_brief(_detail(None), WINDOWS)
    text = formatting.onboarding_plain_text(brief)
    assert text.splitlines()[1].startswith("Start date not known. ")
    assert "Day " not in text
    assert "None" not in text


# Import ------------------------------------------------------------------------------------


def _import_rows(headers: list[str], rows: list[list[str]]) -> importing.ImportResult:
    table = importing.Table(source_name="test.csv", headers=headers, rows=rows)
    return importing.build_import(
        table,
        importing.auto_map(headers),
        connection_id="sheet-1",
        date_order=importing.DateOrder.DMY,
        imported_at=NOW,
        today=TODAY,
    )


def test_an_import_with_no_customer_since_column_leaves_the_start_date_unknown():
    [account] = _import_rows(BASE, [["Acme", "$1", "2026-12-01"]]).accounts
    assert account.start_date is None
    assert account.start_date != TODAY


def test_a_blank_customer_since_cell_leaves_the_start_date_unknown():
    result = _import_rows([*BASE, "Customer since"], [["Acme", "$1", "2026-12-01", ""]])
    [account] = result.accounts
    assert account.start_date is None
    assert account.start_date != TODAY
    assert result.issues == []


# Editing -----------------------------------------------------------------------------------


def test_creating_an_account_without_a_start_date_leaves_it_unknown(store):
    account = create_account(store, dict(NEW), today=TODAY)
    assert account.start_date is None
    assert store.manual().accounts[0].start_date is None


def test_creating_an_account_with_a_start_date_keeps_it(store):
    account = create_account(store, {**NEW, "start_date": "2024-03-01"}, today=TODAY)
    assert account.start_date == date(2024, 3, 1)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_start_date_parses_to_none(blank):
    assert parse_edit_value("start_date", blank) is None


def test_a_blank_renewal_date_is_still_an_error():
    with pytest.raises(EditError) as caught:
        parse_edit_value("renewal_date", "")
    assert caught.value.field == "renewal_date"


def test_a_hand_added_start_date_can_be_cleared_and_set_again_in_place(store):
    created = create_account(store, {**NEW, "start_date": "2024-03-01"}, today=TODAY)
    combined = CombinedConnector([(MANUAL, ManualConnector(store))], store, today=TODAY)

    changed = edit_account(store, combined, created.id, {"start_date": ""}, now=NOW)
    assert changed == ["start_date"]
    assert store.manual().accounts[0].start_date is None
    [account] = combined.list_accounts()
    assert account.start_date is None

    edit_account(store, combined, created.id, {"start_date": "2025-01-05"}, now=NOW)
    [account] = combined.list_accounts()
    assert account.start_date == date(2025, 1, 5)


def test_clearing_a_synced_start_date_is_a_field_edit_that_holds(store):
    sheet = _Member([_account("sp1-acme", start=date(2024, 1, 1))])
    combined = CombinedConnector([(SHEET, sheet)], store, today=TODAY)

    edit_account(store, combined, "sp1-acme", {"start_date": ""}, now=NOW)

    [account] = combined.list_accounts()
    assert account.start_date is None
    assert combined.provenance("sp1-acme").edits["start_date"].value is None
    assert store.edits()["sp1-acme"]["start_date"].base_value == date(2024, 1, 1)


def test_a_synced_account_with_no_start_date_can_be_given_one(store):
    sheet = _Member([_account("sp1-acme", start=None)])
    combined = CombinedConnector([(SHEET, sheet)], store, today=TODAY)

    edit_account(store, combined, "sp1-acme", {"start_date": "2025-02-01"}, now=NOW)

    [account] = combined.list_accounts()
    assert account.start_date == date(2025, 2, 1)


# Linking, HubSpot and the demo book --------------------------------------------------------


def test_a_linked_account_without_a_start_date_takes_the_other_members_date(store):
    hubspot = _Member([_account("hs-1", start=date(2024, 1, 15))])
    sheet = _Member([_account("sp1-acme", start=None)])
    store.save_links(LinkData(groups=[LinkGroup(primary="sp1-acme", members=["sp1-acme", "hs-1"])]))
    combined = CombinedConnector([(HUBSPOT, hubspot), (SHEET, sheet)], store, today=TODAY)

    [merged] = combined.list_accounts()
    assert merged.id == "sp1-acme"
    assert merged.start_date == date(2024, 1, 15)


def test_a_hubspot_company_with_no_start_date_or_createdate_has_none():
    def drop_dates(result: dict) -> None:
        if result["id"] == "1003":
            result["properties"].pop("hs_lifecyclestage_customer_date", None)
            result["properties"].pop("createdate", None)

    connector, _ = make(overrides={("POST", "companies_batch_read"): edit_batch(drop_dates)})
    accounts = {account.id: account for account in connector.list_accounts()}
    assert accounts["hs-1003"].start_date is None
    assert accounts["hs-1001"].start_date == date(2024, 1, 15)


def test_the_demo_shifts_the_start_dates_it_has_and_skips_a_missing_one(tmp_path):
    source = Path(cs_brief.__file__).parent / "data" / "demo"
    for name in ("accounts.json", "signals.json"):
        shutil.copy(source / name, tmp_path / name)
    doc = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    first, second = doc["accounts"][0], doc["accounts"][1]
    first["start_date"] = None
    (tmp_path / "accounts.json").write_text(json.dumps(doc), encoding="utf-8")

    accounts = {a.id: a for a in DemoConnector(data_dir=tmp_path, today=TODAY).list_accounts()}
    delta = TODAY - date.fromisoformat(doc["anchor"])
    assert accounts[first["id"]].start_date is None
    assert accounts[first["id"]].renewal_date == date.fromisoformat(first["renewal_date"]) + delta
    assert accounts[second["id"]].start_date is not None


# The web pages -----------------------------------------------------------------------------


def _text(response) -> str:
    return html.unescape(response.text)


def _facts(page: str) -> str:
    """The account facts aside, where Started is shown."""
    match = re.search(r'<aside class="account-aside".*?</aside>', page, re.S)
    assert match is not None
    return match.group(0)


def _tail(location: str) -> str:
    return location.split("?", 1)[0].rsplit("/", 1)[-1]


def _add_without_start(client: TestClient) -> str:
    """Add an account by hand, with no start date, in Your data. Returns its id."""
    save_settings(Settings(source=DataSource.MINE))
    response = client.post(
        "/accounts/new",
        data={"name": "Brindle Studio", "arr": "$543,000", "renewal_date": "2027-01-10"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, _text(response)
    return _tail(str(response.headers["location"]))


def test_the_account_page_says_not_known_and_offers_to_add_it(client: TestClient) -> None:
    account_id = _add_without_start(client)
    response = client.get(f"/accounts/{account_id}")
    assert response.status_code == 200
    facts = _facts(response.text)
    assert "Not known" in facts
    assert "Add it" in facts
    assert f'href="/accounts/{account_id}/edit#field-start-date"' in response.text
    assert "today" not in facts.lower()


def test_the_onboarding_brief_says_the_start_date_is_not_known(client: TestClient) -> None:
    account_id = _add_without_start(client)
    response = client.get(f"/onboarding/{account_id}")
    assert response.status_code == 200
    text = _text(response)
    assert "Start date not known" in text
    assert "isn't in onboarding right now" in text
    assert "Start date not known." in text  # the copyable brief
    assert "Day None" not in text


def test_the_edit_form_leaves_the_start_date_empty_and_never_prints_none(
    client: TestClient,
) -> None:
    account_id = _add_without_start(client)
    response = client.get(f"/accounts/{account_id}/edit")
    assert response.status_code == 200
    [field] = re.findall(r'<input[^>]*\bname="start_date"[^>]*>', response.text)
    assert 'type="date"' in field
    assert 'value=""' in field
    assert "Leave blank if you don't know it." in _text(response)
    assert "None" not in response.text


def test_the_add_form_offers_the_same_start_date_help(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE))
    response = client.get("/accounts/new")
    assert response.status_code == 200
    assert "Leave blank if you don't know it." in _text(response)
    assert "None" not in response.text


def _csv(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()


def test_a_template_without_a_customer_since_column_shows_no_today(client: TestClient) -> None:
    """The regression: a sheet with no start column must not put today on the account page."""
    rows = list(csv.reader(io.StringIO(importing.template_csv())))
    column = rows[0].index("Customer since")
    without_start = [[cell for index, cell in enumerate(row) if index != column] for row in rows]
    _import(client, _csv(without_start), new_name="Template")

    accounts = service.get_connector().list_accounts()
    assert accounts
    for account in accounts:
        assert account.start_date is None
        response = client.get(f"/accounts/{account.id}")
        assert response.status_code == 200
        assert "today" not in _facts(response.text).lower()


# The MCP tools -----------------------------------------------------------------------------


def test_the_mcp_tools_return_null_for_a_start_date_that_is_not_known() -> None:
    save_settings(Settings(source=DataSource.MINE))
    account = create_account(service.get_store(), dict(NEW), today=TODAY)
    service.reset_connectors()

    assert server.get_account(account.id)["account"]["start_date"] is None
    brief = server.onboarding_brief(account.id)
    assert brief["day"] is None
    assert brief["in_onboarding"] is False
