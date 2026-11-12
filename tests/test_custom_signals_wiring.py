"""Your own signals through the real routes: the spreadsheet match page shows them in their own
group, a column posted for one is imported, the CSV template has a column for each, and the
account and onboarding pages count them.

Every test runs against the temp config and data folders from conftest.py, so nothing reaches
the real config/ folder or the CSM's data.
"""

from __future__ import annotations

import csv
import html
import io
import re

import pytest
from fastapi.testclient import TestClient

from wardmind.core import import_flow, importing, service, signal_info
from wardmind.core.config import CustomSignal, ScoringConfig, SignalRule, save_scoring_config
from wardmind.core.importing import Table
from wardmind.core.models import CustomKind
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
API_HEADER = "API calls per week"
API_KIND = CustomKind.of("api_calls")
API = CustomSignal(
    id="api_calls",
    name=API_HEADER,
    unit="calls",
    rule=SignalRule(weight=20, start=0, full=500),
)


def _book(*custom: CustomSignal) -> None:
    save_scoring_config(ScoringConfig(custom=list(custom)))


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _draft() -> import_flow.Draft:
    table = Table(
        source_name="renewals.csv",
        headers=["Account name", "ARR", "Renewal date", "Weekly calls"],
        rows=[["Larkspur Freight", "$184,000", "2026-12-01", "1,200"]],
    )
    return import_flow.save_draft(
        service.get_store(), table=table, new_name="Renewals", target="new"
    )


def test_the_import_match_page_shows_the_own_signal_group(client: TestClient) -> None:
    _book(API)
    draft = _draft()

    page = html.unescape(client.get(f"/import/{draft.id}").text)

    assert page.count("Your own signals") == 1
    assert page.index("Your own signals") < page.index(f">{API_HEADER}</label>")


def test_a_column_posted_for_the_own_signal_is_imported(client: TestClient) -> None:
    _book(API)
    draft = _draft()
    fields = importing.import_fields()
    chosen = {
        "name": "Account name",
        "arr": "ARR",
        "renewal_date": "Renewal date",
        "signal:custom:api_calls": "Weekly calls",  # no exact match: chosen by hand
    }
    form = {f"column-{index}": chosen.get(spec.key, "") for index, spec in enumerate(fields)}
    form["date_order"] = "dmy"

    response = client.post(
        f"/import/{draft.id}/commit", data=form, headers=ORIGIN, follow_redirects=False
    )

    assert response.status_code == 303, response.text[:400]
    connection = re.search(r"imported=([^&]+)", response.headers["location"])
    assert connection is not None
    snapshot = service.get_store().snapshot(connection.group(1))
    assert snapshot is not None
    own = [signal.value for signal in snapshot.signals if signal.kind == API_KIND]
    assert own == [1200.0]


def test_the_csv_template_has_a_column_for_each_own_signal(client: TestClient) -> None:
    _book(API)

    rows = list(csv.reader(io.StringIO(client.get("/import/template.csv").text)))

    assert rows[0][-1] == API_HEADER
    assert all(row[-1] == "" for row in rows[1:])
    assert rows[0][:-1] == [spec.label for spec in importing.IMPORT_FIELDS]


def test_an_own_signal_name_that_looks_like_a_formula_is_made_safe_in_the_template() -> None:
    risky = API.model_copy(update={"id": "risky", "name": "=SUM(1)"})

    header = next(csv.reader(io.StringIO(importing.template_csv(ScoringConfig(custom=[risky])))))

    assert header[-1] == "'=SUM(1)"


def test_without_own_signals_the_template_is_the_built_in_fields() -> None:
    header = next(csv.reader(io.StringIO(importing.template_csv(ScoringConfig()))))

    assert header == [spec.label for spec in importing.IMPORT_FIELDS]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Reports exported", "reports exported"),
        ("API calls per week", "API calls per week"),
        ("NPS from admins", "NPS from admins"),
        ("iOS installs", "iOS installs"),
        ("X", "X"),
    ],
)
def test_a_signal_name_mid_sentence_keeps_an_acronym(name: str, expected: str) -> None:
    assert signal_info.in_sentence(name) == expected


def test_the_made_up_next_step_keeps_the_acronym_in_the_name() -> None:
    config = ScoringConfig(custom=[API])

    assert signal_info.custom_action(API_KIND, config) == (
        "Find out what is behind the API calls per week reading and agree a fix."
    )
    reports = API.model_copy(update={"id": "reports", "name": "Reports exported"})
    assert signal_info.custom_question(reports.kind, ScoringConfig(custom=[reports])) == (
        "What is behind the change in reports exported?"
    )
