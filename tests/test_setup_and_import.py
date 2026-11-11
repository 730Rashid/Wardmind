"""Set up your book, the book's connection notices, and the import flow through the web.

Everything runs through the real routes against the temp data folder from conftest. No test here
reaches HubSpot: the one page that could (Connections, with HubSpot connected) runs in Demo mode,
and the failing connection is built over fakes.
"""

from __future__ import annotations

import html
import io
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from wardmind.core import importing, secrets, service
from wardmind.core.connectors.combined import CombinedConnector
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.connectors.errors import ConnectionNotReady
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.core.store import ConnectionConfig, ConnectionKind, LocalStore
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
HX = {"origin": "http://127.0.0.1", "HX-Request": "true"}
TOKEN = "pat-na1-SECRETTEST-0000-1111-2222-333344445555"
CROSS_SITE = [
    {"origin": "https://evil.example"},
    {"origin": "null", "sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "cross-site"},
]
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)

RENEWALS = (
    "Company,ARR,Renewal date,Website,Usage trend\n"
    '"Larkspur Freight","$184,000",2026-12-01,larkspur.example,-38\n'
    '"Quillon Health","$96,500",2026-11-15,quillon.example,6\n'
)
RENEWALS_CHANGED = (
    "Company,ARR,Renewal date,Website,Usage trend\n"
    '"Larkspur Freight","$190,000",2026-12-01,larkspur.example,-38\n'
    '"Brindle Studio","$543,000",2027-01-10,brindle.example,-5\n'
)
NO_ARR = 'Company,Renewal date\n"Larkspur Freight",2026-12-01\n'
BAD_ROW = (
    "Company,ARR,Renewal date\n"
    '"Larkspur Freight","$184,000",2026-12-01\n'
    '"Quillon Health","TBC",2026-11-15\n'
)


def _tags(markup: str, name: str) -> list[dict[str, str | None]]:
    """The attributes of every tag with this name, in document order."""

    class Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.found: list[dict[str, str | None]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == name:
                self.found.append(dict(attrs))

    collector = Collector()
    collector.feed(markup)
    return collector.found


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _assert_clean(response: Any) -> None:
    """No inline script, style or event handler on a page this work adds."""
    assert response.status_code in (200, 303, 422), response.url
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


def _upload(
    client: TestClient,
    text: str,
    *,
    filename: str = "renewals.csv",
    target: str = "new",
    new_name: str = "",
    back: str = "",
) -> Any:
    data = {"target": target, "new_name": new_name, "back": back}
    files = {"file": (filename, text.encode("utf-8"), "text/csv")}
    return client.post("/import", data=data, files=files, headers=ORIGIN, follow_redirects=False)


def _paste(client: TestClient, text: str, *, new_name: str = "") -> Any:
    """Paste as the browser sends it: a multipart form, as the import form declares."""
    files = {
        "pasted": (None, text),
        "target": (None, "new"),
        "new_name": (None, new_name),
    }
    return client.post("/import", files=files, headers=ORIGIN, follow_redirects=False)


def _draft_id(response: Any) -> str:
    assert response.status_code == 303, _text(response)
    return str(response.headers["location"]).rsplit("/", 1)[-1]


def _form(mapping: dict[str, str], date_order: str = "") -> dict[str, str]:
    """The mapping page's fields, for a mapping keyed by import field."""
    fields = {
        f"column-{index}": mapping.get(spec.key, "")
        for index, spec in enumerate(importing.IMPORT_FIELDS)
    }
    if date_order:
        fields["date_order"] = date_order
    return fields


def _auto(text: str) -> dict[str, str]:
    return importing.auto_map(importing.read_csv(text.encode("utf-8"), "x.csv").headers)


def _commit(client: TestClient, draft_id: str, mapping: dict[str, str]) -> Any:
    return client.post(
        f"/import/{draft_id}/commit",
        data=_form(mapping),
        headers=ORIGIN,
        follow_redirects=False,
    )


def _import(client: TestClient, text: str, **kwargs: str) -> Any:
    """Upload a CSV, then commit it with the automatic mapping. Returns the commit response."""
    draft_id = _draft_id(_upload(client, text, **kwargs))
    return _commit(client, draft_id, _auto(text))


def _draft_files(store: LocalStore) -> list[Path]:
    folder = store.root / "drafts"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


# Set up your book ------------------------------------------------------------------------------


def test_my_book_is_the_setup_page_when_your_data_is_empty(client: TestClient) -> None:
    _use(DataSource.MINE)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert "Set up your book" in _text(client.get("/setup"))


def test_demo_data_never_redirects_to_setup(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Set up your book" not in response.text


def test_setup_offers_the_four_ways_in_the_template_and_the_demo_form(client: TestClient) -> None:
    _use(DataSource.MINE)
    response = client.get("/setup")
    text = _text(response)
    for title in (
        "Import a spreadsheet",
        "Paste from a spreadsheet",
        "Connect HubSpot",
        "Add accounts by hand",
    ):
        assert title in text
    assert "CSV or Excel. We match the columns for you." in text
    assert 'href="/import/template.csv"' in response.text
    forms = [(tag.get("method"), tag.get("action")) for tag in _tags(response.text, "form")]
    assert ("post", "/settings/connections/use-demo") in forms
    assert "Or keep exploring with the demo data." in text
    assert 'accept=".csv,.tsv,.txt,.xlsx"' in response.text
    _assert_clean(response)


def test_setup_has_one_row_per_option(client: TestClient) -> None:
    _use(DataSource.MINE)
    assert client.get("/setup").text.count('class="setup-option"') == 4


# Problems on the book pages -----------------------------------------------------------------------


@pytest.fixture
def failing_book(monkeypatch: pytest.MonkeyPatch, store: LocalStore) -> CombinedConnector:
    """Your data, where one connection works and HubSpot can't load."""
    _use(DataSource.MINE)

    class Working:
        name = "spreadsheet"

        def list_accounts(self) -> list[Any]:
            return DemoConnector().list_accounts()[:5]

        def list_signals(self, account_id: str | None = None) -> list[Any]:
            return DemoConnector().list_signals(account_id)

    class Broken:
        name = "hubspot"

        def list_accounts(self) -> list[Any]:
            raise ConnectionNotReady("HubSpot didn't accept the access token.")

        def list_signals(self, account_id: str | None = None) -> list[Any]:
            raise ConnectionNotReady("HubSpot didn't accept the access token.")

    now = datetime.now(UTC).replace(tzinfo=None)
    working = ConnectionConfig(
        id="sheet-1", kind=ConnectionKind.SPREADSHEET, name="Q4 renewals", created_at=now
    )
    broken = ConnectionConfig(
        id="hubspot", kind=ConnectionKind.HUBSPOT, name="HubSpot", created_at=now
    )
    combined = CombinedConnector([(working, Working()), (broken, Broken())], store)
    monkeypatch.setattr(service, "get_connector", lambda: combined)
    return combined


@pytest.mark.parametrize("path", ["/", "/brief", "/onboarding", "/accounts/acc-001"])
def test_a_failing_connection_gets_a_banner_on_every_book_page(
    client: TestClient, failing_book: CombinedConnector, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    banners = re.findall(r'<div class="banner" role="alert">(.*?)</div>', response.text, re.S)
    assert len(banners) == 1
    banner = html.unescape(banners[0])
    assert (
        "HubSpot couldn't load: HubSpot didn't accept the access token. "
        "Your other connections are still shown."
    ) in banner
    assert 'href="/settings/connections">Check Connections</a>' in banner
    _assert_clean(response)


def test_the_banner_is_not_on_settings_pages(
    client: TestClient, failing_book: CombinedConnector
) -> None:
    assert 'role="alert"' not in client.get("/settings/appearance").text


def test_the_working_connections_still_show_under_the_banner(
    client: TestClient, failing_book: CombinedConnector
) -> None:
    response = client.get("/")
    assert '<summary class="book-switcher-summary">Book 1</summary>' in response.text
    assert 'class="card-grid' in response.text


# Duplicates line -------------------------------------------------------------------------------


def test_my_book_names_the_accounts_that_might_be_duplicates(client: TestClient) -> None:
    _use(DataSource.MINE)
    _import(client, RENEWALS, new_name="Renewals")
    _import(
        client,
        "Company,ARR,Renewal date,Website\n"
        '"Acme Corporation","$2,000",2026-12-02,larkspur.example\n',
        filename="other.csv",
    )
    response = client.get("/")
    line = re.search(r'<p class="duplicates" role="status">(.*?)</p>', response.text, re.S)
    assert line is not None
    assert "accounts might be duplicates." in _text(response)
    assert 'href="/duplicates"' in line.group(1)


# The import flow -------------------------------------------------------------------------------


def test_the_template_round_trips_with_one_row_edited(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.MINE)
    template = client.get("/import/template.csv")
    assert template.status_code == 200
    assert template.headers["content-type"].startswith("text/csv")
    assert template.headers["content-disposition"] == (
        'attachment; filename="wardmind-template.csv"'
    )
    assert template.text.startswith("Account name,ARR,Renewal date,ID,Website,Owner,Tier,")
    edited = template.text.replace("$184,000", "$190,000")

    draft_id = _draft_id(_upload(client, edited, filename="wardmind-template.csv"))
    mapping_page = client.get(f"/import/{draft_id}")
    assert mapping_page.status_code == 200
    assert "Match your columns" in _text(mapping_page)
    assert '<option value="Account name" selected>' in mapping_page.text
    assert "Import 2 accounts" in _text(mapping_page)
    _assert_clean(mapping_page)

    commit = _commit(client, draft_id, _auto(edited))
    assert commit.status_code == 303
    assert commit.headers["location"].startswith("/?imported=sheet-1")
    assert "removed=0" in commit.headers["location"]

    book = client.get(commit.headers["location"])
    assert book.status_code == 200
    assert "Imported 2 accounts from wardmind-template.csv." in _text(book)
    assert 'role="status"' in book.text
    assert "Larkspur Freight" in book.text and "Quillon Health" in book.text
    assert "were skipped" not in book.text
    assert _draft_files(store) == []  # the draft is gone once it is committed


def test_the_commit_switches_demo_data_to_your_data(client: TestClient) -> None:
    _use(DataSource.DEMO)
    _import(client, RENEWALS, new_name="Renewals")
    assert load_settings().source is DataSource.MINE


def test_skipped_rows_are_reported_after_the_import(client: TestClient) -> None:
    _use(DataSource.MINE)
    response = _import(client, BAD_ROW, new_name="Renewals")
    book = _text(client.get(response.headers["location"]))
    assert "Imported 1 account from renewals.csv." in book
    assert "1 row was skipped." in book


def test_a_reimport_appends_history_and_reports_accounts_that_left(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.MINE)
    _import(client, RENEWALS, new_name="Renewals")

    draft_id = _draft_id(_upload(client, RENEWALS_CHANGED, target="sheet-1"))
    page = _text(client.get(f"/import/{draft_id}"))
    assert "Re-import only: 1 account from last time isn't in this file" in page
    assert "Update Renewals" not in page  # the choice is on the form, not the mapping page

    commit = _commit(client, draft_id, _auto(RENEWALS_CHANGED))
    assert commit.status_code == 303
    assert "removed=1" in commit.headers["location"]
    book = _text(client.get(commit.headers["location"]))
    assert "Imported 2 accounts from renewals.csv." in book
    assert "1 account from last time wasn't in the file, so it left your book." in book
    assert "Quillon Health" not in book
    snapshot = store.snapshot("sheet-1")
    assert snapshot is not None
    assert len(snapshot.history) == 2
    assert all(signal.account_id.startswith("sp1-") for signal in snapshot.signals)


def test_a_reimport_starts_from_the_remembered_mapping(client: TestClient) -> None:
    _use(DataSource.MINE)
    _import(client, RENEWALS, new_name="Renewals")
    # The date column is renamed, so the remembered choice for it is gone; the rest stays.
    renamed = RENEWALS_CHANGED.replace("Renewal date", "Contract ends", 1)
    draft_id = _draft_id(_upload(client, renamed, target="sheet-1"))
    text = client.get(f"/import/{draft_id}").text
    assert '<option value="ARR" selected>' in text  # remembered, and still in the file
    assert '<option value="Renewal date"' not in text


def test_a_paste_imports_the_cells(client: TestClient) -> None:
    _use(DataSource.MINE)
    pasted = "Company\tARR\tRenewal date\nAcme Freight\t$12,000\t2027-03-01\n"
    draft_id = _draft_id(_paste(client, pasted, new_name="Pasted renewals"))
    assert "Pasted cells" in _text(client.get(f"/import/{draft_id}"))
    commit = _commit(client, draft_id, _auto("Company,ARR,Renewal date\n"))
    assert commit.status_code == 303
    assert "Imported 1 account from Pasted cells." in _text(client.get(commit.headers["location"]))


def test_the_paste_section_comes_first_with_paste_one(client: TestClient) -> None:
    response = client.get("/import?paste=1")
    text = response.text
    assert text.index('id="paste-title"') < text.index('id="file-title"')
    assert "autofocus" in text
    plain = client.get("/import").text
    assert plain.index('id="file-title"') < plain.index('id="paste-title"')
    _assert_clean(response)


def test_excel_switches_sheets_from_the_stored_file(client: TestClient) -> None:
    _use(DataSource.MINE)
    book = Workbook()
    accounts = book.active
    assert accounts is not None
    accounts.title = "Accounts"
    accounts.append(["Company", "ARR", "Renewal date"])
    accounts.append(["Larkspur Freight", 184000, "2026-12-01"])
    notes = book.create_sheet("Notes")
    notes.append(["Topic", "Detail"])
    notes.append(["Renewal", "Check the contract"])
    buffer = io.BytesIO()
    book.save(buffer)

    response = client.post(
        "/import",
        data={"target": "new", "new_name": "", "back": ""},
        files={"file": ("renewals.xlsx", buffer.getvalue(), "application/octet-stream")},
        headers=ORIGIN,
        follow_redirects=False,
    )
    draft_id = _draft_id(response)
    page = client.get(f"/import/{draft_id}").text
    assert '<option value="Accounts" selected>' in page
    assert '<option value="Notes">' in page
    assert 'value="Company" selected' in page

    switched = client.post(
        f"/import/{draft_id}/sheet", data={"sheet": "Notes"}, headers=ORIGIN, follow_redirects=False
    )
    assert switched.status_code == 303
    notes_page = client.get(f"/import/{draft_id}").text
    assert '<option value="Notes" selected>' in notes_page
    assert '<option value="Topic">' in notes_page

    missing = client.post(
        f"/import/{draft_id}/sheet", data={"sheet": "Nope"}, headers=ORIGIN, follow_redirects=False
    )
    assert missing.status_code == 422
    assert "no sheet called" in _text(missing).lower()


def test_the_preview_partial_refreshes_the_examples_and_the_outcome(client: TestClient) -> None:
    _use(DataSource.MINE)
    draft_id = _draft_id(_upload(client, RENEWALS))
    mapping = _auto(RENEWALS)
    mapping.pop("arr")
    response = client.post(f"/import/{draft_id}/preview", data=_form(mapping), headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="what-happens"' in response.text and 'hx-swap-oob="true"' in response.text
    assert 'id="example-0" hx-swap-oob="true"' in response.text
    assert "Match ARR to import." in _text(response)
    assert "disabled" in response.text
    _assert_clean(response)


def test_the_mapping_page_without_javascript_saves_and_shows_the_preview(
    client: TestClient,
) -> None:
    _use(DataSource.MINE)
    draft_id = _draft_id(_upload(client, RENEWALS))
    response = client.post(f"/import/{draft_id}", data=_form(_auto(RENEWALS)), headers=ORIGIN)
    assert response.status_code == 200
    assert "Import 2 accounts" in _text(response)


def test_the_import_is_disabled_and_refused_without_a_required_field(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.MINE)
    draft_id = _draft_id(_upload(client, NO_ARR))
    page = client.get(f"/import/{draft_id}")
    assert "Match ARR to import." in _text(page)
    assert re.search(r"<button[^>]*disabled[^>]*>Import 0 accounts</button>", page.text)

    refused = _commit(client, draft_id, {"name": "Company", "renewal_date": "Renewal date"})
    assert refused.status_code == 422
    assert "Match ARR to import." in _text(refused)
    assert len(_draft_files(store)) == 1  # the refusal keeps the draft


def test_a_file_with_no_ready_rows_is_refused_at_commit(client: TestClient) -> None:
    _use(DataSource.MINE)
    text = "Company,ARR,Renewal date\nAcme,TBC,2026-01-01\n"
    draft_id = _draft_id(_upload(client, text))
    refused = _commit(client, draft_id, _auto(text))
    assert refused.status_code == 422
    assert "No accounts are ready to import." in _text(refused)


def test_the_problems_download_lists_the_rows_with_a_problems_column(client: TestClient) -> None:
    _use(DataSource.MINE)
    draft_id = _draft_id(_upload(client, BAD_ROW))
    client.post(f"/import/{draft_id}/preview", data=_form(_auto(BAD_ROW)), headers=HX)
    response = client.get(f"/import/{draft_id}/problems.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="rows-with-problems.csv"'
    )
    lines = response.text.strip().splitlines()
    assert lines[0] == "Company,ARR,Renewal date,Problems"
    assert len(lines) == 2
    assert lines[1].startswith("Quillon Health,TBC,2026-11-15,")


def test_cancel_deletes_the_draft_and_goes_back_where_the_import_began(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.MINE)
    from_setup = _draft_id(_upload(client, RENEWALS))
    cancelled = client.post(f"/import/{from_setup}/cancel", headers=ORIGIN, follow_redirects=False)
    assert cancelled.status_code == 303
    assert cancelled.headers["location"] == "/setup"
    assert client.get(f"/import/{from_setup}").status_code == 404

    from_connections = _draft_id(_upload(client, RENEWALS, back="connections"))
    cancelled = client.post(
        f"/import/{from_connections}/cancel", headers=ORIGIN, follow_redirects=False
    )
    assert cancelled.headers["location"] == "/settings/connections"
    assert _draft_files(store) == []


def test_the_connections_link_preselects_the_connection_to_update(client: TestClient) -> None:
    _use(DataSource.MINE)
    _import(client, RENEWALS, new_name="Renewals")
    page = client.get("/import?target=sheet-1&back=connections")
    assert page.status_code == 200
    assert re.search(r'value="sheet-1" checked', page.text)
    assert "Update Renewals" in page.text
    unknown = client.get("/import?target=manual")
    assert 'value="new" checked' in unknown.text


def test_a_name_longer_than_sixty_characters_is_refused_on_upload(client: TestClient) -> None:
    _use(DataSource.MINE)
    response = _upload(client, RENEWALS, new_name="x" * 61)
    assert response.status_code == 422
    assert "A name can be at most 60 characters." in _text(response)


def test_the_upload_needs_exactly_one_of_file_or_paste(client: TestClient) -> None:
    neither = client.post(
        "/import", data={"target": "new", "pasted": "  "}, headers=ORIGIN, follow_redirects=False
    )
    assert neither.status_code == 422
    assert "Choose a file or paste some cells." in _text(neither)
    both = client.post(
        "/import",
        data={"target": "new", "pasted": RENEWALS},
        files={"file": ("renewals.csv", RENEWALS.encode(), "text/csv")},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert both.status_code == 422
    assert "not both" in _text(both)


def test_an_unreadable_file_is_refused_and_stores_no_draft(
    client: TestClient, store: LocalStore
) -> None:
    response = _upload(client, "a,b\n", filename="notes.pdf")
    assert response.status_code == 422
    assert "isn't a file type Wardmind can read" in _text(response)
    assert _draft_files(store) == []


# Upload limits ---------------------------------------------------------------------------------


def test_a_file_over_the_limit_is_refused_with_the_importing_message(
    client: TestClient, store: LocalStore
) -> None:
    body = "Company,ARR,Renewal date\n" + "Acme," * (importing.MAX_BYTES // 5 + 10)
    data = body.encode("utf-8")
    assert len(data) > importing.MAX_BYTES
    response = client.post(
        "/import",
        data={"target": "new", "new_name": ""},
        files={"file": ("big.csv", data, "text/csv")},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "That file is larger than 5 MB." in _text(response)
    assert _draft_files(store) == []


def test_a_paste_over_the_limit_is_refused_and_stores_no_draft(
    client: TestClient, store: LocalStore
) -> None:
    huge = "Company\tARR\n" + "x" * (importing.MAX_BYTES + 10)
    response = _paste(client, huge)
    assert response.status_code == 422
    assert "That paste is larger than 5 MB." in _text(response)
    assert _draft_files(store) == []


# Bad draft ids ---------------------------------------------------------------------------------

BAD_IDS = ["ABCDEF0123456789", "0123", "0123456789abcdef0", "0123456789abcdeg", "0123456789abcdef"]


@pytest.mark.parametrize("draft_id", BAD_IDS)
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("post", ""),
        ("post", "/preview"),
        ("post", "/sheet"),
        ("post", "/commit"),
        ("post", "/cancel"),
        ("get", "/problems.csv"),
    ],
)
def test_an_unknown_or_malformed_draft_id_is_a_404(
    client: TestClient, method: str, suffix: str, draft_id: str
) -> None:
    path = f"/import/{draft_id}{suffix}"
    if method == "get":
        response = client.get(path)
    else:
        headers = HX if suffix == "/preview" else ORIGIN
        response = client.post(path, data={}, headers=headers)
    assert response.status_code == 404, path


@pytest.mark.parametrize("path", ["/import/%2E%2E%5Cx", "/import/%2E%2E%2Fx"])
def test_a_path_traversal_id_is_a_404(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404
    assert client.post(f"{path}/commit", headers=ORIGIN).status_code == 404


def test_the_template_route_is_not_mistaken_for_a_draft(client: TestClient) -> None:
    assert client.get("/import/template.csv").status_code == 200


# The cross-site sweep over every new POST -----------------------------------------------------

NEW_POSTS = [
    ("/import", {"target": "new"}),
    ("/import/0123456789abcdef", {}),
    ("/import/0123456789abcdef/preview", {}),
    ("/import/0123456789abcdef/sheet", {"sheet": "A"}),
    ("/import/0123456789abcdef/commit", {}),
    ("/import/0123456789abcdef/cancel", {}),
    ("/settings/connections/sheet-1/enabled", {"enabled": "1"}),
    ("/settings/connections/sheet-1/rename", {"name": "New"}),
    ("/settings/connections/sheet-1/remove", {}),
    ("/settings/connections/hidden/acc-001/show", {}),
]


@pytest.mark.parametrize(("path", "data"), NEW_POSTS)
@pytest.mark.parametrize("headers", CROSS_SITE)
def test_a_cross_site_post_is_refused_and_changes_nothing(
    client: TestClient,
    store: LocalStore,
    path: str,
    data: dict[str, str],
    headers: dict[str, str],
) -> None:
    _use(DataSource.MINE)
    store.add_connection(ConnectionKind.SPREADSHEET, "Renewals")
    before = sorted(p.relative_to(store.root) for p in store.root.rglob("*") if p.is_file())
    response = client.post(path, data=data, headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert response.text == web.CROSS_SITE_MESSAGE
    after = sorted(p.relative_to(store.root) for p in store.root.rglob("*") if p.is_file())
    assert after == before
    connection = store.get_connection("sheet-1")
    assert connection is not None and connection.name == "Renewals"


# No inline markup, and no token in the new pages ----------------------------------------------


def test_the_new_pages_have_no_inline_markup(client: TestClient, store: LocalStore) -> None:
    _use(DataSource.MINE)
    draft_id = _draft_id(_upload(client, RENEWALS))
    store.add_connection(ConnectionKind.SPREADSHEET, "Renewals")
    for path in (
        "/setup",
        "/import",
        "/import?paste=1",
        f"/import/{draft_id}",
        "/settings/connections",
        "/settings/connections/sheet-1/remove",
    ):
        _assert_clean(client.get(path))


def test_the_token_is_in_no_new_page(client: TestClient, store: LocalStore) -> None:
    # Demo data keeps the Connections page from reaching HubSpot; the pages still list the
    # HubSpot connection, so the token could show up if any of them leaked it.
    _use(DataSource.DEMO)
    secrets.save_hubspot_token(TOKEN)
    store.add_connection(ConnectionKind.HUBSPOT, "HubSpot")
    draft_id = _draft_id(_upload(client, RENEWALS))
    for path in (
        "/setup",
        "/import",
        f"/import/{draft_id}",
        "/settings/connections",
        "/settings/connections/hubspot/remove",
        "/import/template.csv",
    ):
        response = client.get(path)
        assert "SECRETTEST" not in response.text, path
        assert TOKEN not in response.text, path
        assert TOKEN.encode() not in response.content, path


def test_the_import_button_posts_to_the_drafts_own_commit_address(client: TestClient) -> None:
    # Follow the form exactly as a browser would. The page once rendered action="/import//commit"
    # because the template asked for a variable the route didn't pass, and every test posted to
    # a hand-built URL instead of the form's real one.
    _use(DataSource.MINE)
    pasted = (
        "Company\tARR\tRenewal date\n\n"
        "Acme Freight\t$12,000\t2027-03-01\n"
        "Bolt Co\tTBC\t2027-04-01\n"
    )
    draft_id = _draft_id(_paste(client, pasted, new_name="Pasted renewals"))
    page = client.get(f"/import/{draft_id}").text

    actions = re.findall(r'<form id="import-mapping"[^>]*action="([^"]+)"', page)
    assert actions == [f"/import/{draft_id}/commit"]
    addresses = re.findall(r'(?:action|href|hx-post|formaction)="(/[^"]*)"', page)
    assert addresses and not [address for address in addresses if "//" in address]
    # The examples come from the first data row, which is row 3 here because of the blank line.
    assert "Example from row 3" in page
    assert "Download the row with a problem (CSV)" in page
