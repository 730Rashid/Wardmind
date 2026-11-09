"""HubSpotConnector on the fake portal: loading, mapping, signals, caching and the checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta

import httpx
import pytest

from hubspot_fake import fake_transport, fixture, route_name, serve
from wardmind.core import service
from wardmind.core.config import ScoringConfig, config_dir
from wardmind.core.connectors import hubspot
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.connectors.hubspot import (
    HubSpotClient,
    HubSpotConnector,
    HubSpotMapping,
    HubSpotUnavailable,
    load_mapping,
)
from wardmind.core.models import Health, Milestones, Motion, SignalKind

TOKEN = "pat-na1-TESTTOKEN-0000-1111-2222-333344445555"
TODAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 12, 0)
AUTH_MESSAGE = (
    "HubSpot didn't accept the access token. It may be mistyped, revoked or expired. "
    "Save a new one on the Connections page."
)
EXPECTED_WARNINGS = [
    "1 company has no ARR, so it counts as $0.",
    "1 company has no renewal date, so it was left out.",
]
EXPECTED = {
    # id: (name, arr, tier, owner, start, renewal, milestones (done, total, overdue))
    "hs-1001": (
        "Brightwater Logistics",
        120000.0,
        "Enterprise",
        "Dana Whitfield",
        date(2024, 1, 15),
        date(2026, 12, 15),
        (4, 4, 0),
    ),
    "hs-1002": (
        "Northgate Health Partners",
        64000.0,
        "Mid-market",
        "Priya Raman",
        date(2026, 9, 19),
        date(2027, 9, 19),
        (2, 6, 1),
    ),
    "hs-1003": (
        "Cobalt Ridge Software",
        48000.0,
        "Growth",
        "Tom Okafor",
        date(2025, 3, 1),
        date(2027, 3, 1),
        (0, 0, 0),
    ),
    "hs-1005": (
        "Lumen Print Co",
        0.0,
        "Unknown",
        "ops@example.com",
        date(2023, 8, 14),
        date(2026, 11, 30),
        (0, 0, 0),
    ),
    "hs-1006": (
        "Pinecrest Clinics",
        210000.0,
        "Enterprise",
        "Unassigned",
        date(2024, 9, 1),
        date(2027, 1, 15),
        (3, 3, 0),
    ),
    "hs-1007": (
        "Old Mill Travel",
        36000.0,
        "Growth",
        "Unassigned",
        date(2025, 11, 3),
        date(2026, 11, 20),
        (0, 0, 0),
    ),
}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """The open-ticket signal is stamped with the load time, so pin that time."""
    monkeypatch.setattr(hubspot, "_now_utc", lambda: NOW)


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(
    *,
    overrides=None,
    dated: bool = True,
    mapping: HubSpotMapping | None = None,
    clock: FakeClock | None = None,
    cache_seconds: float = 900.0,
):
    transport, seen = fake_transport(overrides=overrides, dated=dated)
    client = HubSpotClient(TOKEN, transport=transport, sleep=lambda seconds: None)
    kwargs = {"today": TODAY, "cache_seconds": cache_seconds}
    if clock is not None:
        kwargs["clock"] = clock
    return HubSpotConnector(client, mapping, **kwargs), seen


def edit_batch(edit: Callable[[dict], None]) -> Callable[[httpx.Request], httpx.Response]:
    """A batch-read override: the fake's answer with each company's result edited in place."""

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(serve(request).content)
        for result in body.get("results", []):
            edit(result)
        return httpx.Response(200, json=body)

    return answer


def edit_search(edit: Callable[[dict], None]) -> Callable[[httpx.Request], httpx.Response]:
    """A search override: the fake's answer with the whole response edited in place."""

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(serve(request).content)
        edit(body)
        return httpx.Response(200, json=body)

    return answer


def signals_of(connector: HubSpotConnector, account_id: str, kind: SignalKind):
    return sorted(
        (s for s in connector.list_signals(account_id) if s.kind is kind),
        key=lambda s: s.observed_at,
    )


# Accounts


def test_loads_six_accounts_and_skips_the_company_without_a_renewal_date():
    connector, _ = make()
    accounts = {account.id: account for account in connector.list_accounts()}
    assert set(accounts) == set(EXPECTED)
    for account_id, (name, arr, tier, owner, start, renewal, done_total) in EXPECTED.items():
        account = accounts[account_id]
        done, total, overdue = done_total
        assert account.name == name
        assert account.arr == arr
        assert account.tier == tier
        assert account.owner == owner
        assert account.start_date == start
        assert account.renewal_date == renewal
        assert account.milestones == Milestones(done=done, total=total, overdue=overdue)


def test_owners_are_read_across_both_pages_with_the_fallbacks():
    connector, seen = make()
    connector.list_accounts()
    owner_pages = [r for r in seen if r.method == "GET" and "/owners/" in r.url.path]
    assert [r.url.params.get("after") for r in owner_pages] == [None, "p2"]
    assert all(r.url.params["limit"] == "100" for r in owner_pages)


def test_default_book_warnings_are_exact_and_in_order():
    connector, _ = make()
    connector.list_accounts()
    assert connector.warnings == EXPECTED_WARNINGS


def test_warnings_are_empty_before_the_first_load():
    connector, _ = make()
    assert connector.warnings == []
    assert connector.loaded_at is None


def test_warning_counts_use_plural_wording():
    def fix(result: dict) -> None:
        if result["id"] == "1006":
            result["properties"].pop("cs_arr")
        if result["id"] == "1007":
            result["properties"].pop("cs_renewal_date")

    connector, _ = make(overrides={("POST", "companies_batch_read"): edit_batch(fix)})
    connector.list_accounts()
    assert connector.warnings == [
        "2 companies have no ARR, so they count as $0.",
        "2 companies have no renewal date, so they were left out.",
    ]


def test_milestones_that_do_not_add_up_are_adjusted_with_a_warning():
    def fix(result: dict) -> None:
        if result["id"] == "1002":
            result["properties"]["cs_onboarding_milestones_done"] = "9"

    connector, _ = make(overrides={("POST", "companies_batch_read"): edit_batch(fix)})
    accounts = {account.id: account for account in connector.list_accounts()}
    assert accounts["hs-1002"].milestones == Milestones(done=6, total=6, overdue=0)
    assert connector.warnings == [
        *EXPECTED_WARNINGS,
        "1 company has onboarding milestone numbers that don't add up, so it was adjusted.",
    ]


def test_several_milestone_adjustments_use_plural_wording():
    def fix(result: dict) -> None:
        if result["id"] in ("1002", "1006"):
            result["properties"]["cs_onboarding_milestones_done"] = "9"

    connector, _ = make(overrides={("POST", "companies_batch_read"): edit_batch(fix)})
    connector.list_accounts()
    assert connector.warnings[-1] == (
        "2 companies have onboarding milestone numbers that don't add up, so they were adjusted."
    )


def test_search_total_over_10000_warns_about_the_cap():
    connector, _ = make(
        overrides={("POST", "companies_search"): edit_search(lambda b: b.update(total=10001))}
    )
    connector.list_accounts()
    assert connector.warnings[0] == (
        "HubSpot's search returns at most 10,000 companies, so only the first 10,000 are included."
    )


@pytest.mark.parametrize(
    ("raw", "value"),
    [("true", 1.0), ("Yes", 1.0), ("1", 1.0), ("false", 0.0), ("no", 0.0), ("0", 0.0)],
)
def test_champion_left_reads_true_false_yes_no_and_numbers(raw, value):
    def fix(result: dict) -> None:
        if result["id"] == "1003":
            result["properties"]["cs_champion_left"] = raw

    connector, _ = make(overrides={("POST", "companies_batch_read"): edit_batch(fix)})
    [signal] = signals_of(connector, "hs-1003", SignalKind.CHAMPION_LEFT)
    assert signal.value == value
    assert signal.observed_at == datetime(2026, 10, 14, 8, 0)  # the company's updatedAt


# Signals


def test_usage_history_becomes_signals_with_naive_utc_timestamps_oldest_first():
    connector, _ = make()
    usage = signals_of(connector, "hs-1001", SignalKind.USAGE_TREND_30D)
    assert [s.value for s in usage] == [-4.0, -10.0, -22.0, -38.0]
    assert [s.observed_at for s in usage] == [
        datetime(2026, 7, 25, 9, 0),
        datetime(2026, 8, 20, 9, 0),
        datetime(2026, 9, 19, 13, 0),
        datetime(2026, 10, 17, 13, 0),
    ]
    for signal in connector.list_signals():
        assert signal.observed_at.tzinfo is None
        assert signal.source == "hubspot"


def test_readings_older_than_history_days_and_unparseable_values_are_dropped():
    connector, _ = make()
    usage = signals_of(connector, "hs-1007", SignalKind.USAGE_TREND_30D)
    assert [s.value for s in usage] == [-15.0]


def test_a_timestamp_with_an_offset_is_converted_to_naive_utc():
    connector, _ = make()
    [nps] = signals_of(connector, "hs-1001", SignalKind.NPS_LATEST)
    assert nps.value == 3.0
    assert nps.observed_at == datetime(2026, 9, 30, 9, 0)  # 10:00 at +01:00


def test_an_epoch_millisecond_timestamp_becomes_a_naive_utc_datetime():
    connector, _ = make()
    readings = signals_of(connector, "hs-1003", SignalKind.NPS_LATEST)
    assert [s.value for s in readings] == [9.0]
    assert readings[0].observed_at == datetime(1970, 1, 1) + timedelta(milliseconds=1790758800000)


def test_champion_history_keeps_both_readings():
    connector, _ = make()
    champion = signals_of(connector, "hs-1001", SignalKind.CHAMPION_LEFT)
    assert [s.value for s in champion] == [0.0, 1.0]


def test_an_empty_history_falls_back_to_the_current_value_at_updated_at():
    connector, _ = make()
    [billing] = signals_of(connector, "hs-1001", SignalKind.BILLING_OVERDUE_DAYS)
    assert billing.value == 0.0
    assert billing.observed_at == datetime(2026, 10, 17, 13, 0)


def test_overdue_milestone_history_becomes_onboarding_signals():
    connector, _ = make()
    overdue = signals_of(connector, "hs-1002", SignalKind.ONBOARDING_OVERDUE_MILESTONES)
    assert [s.value for s in overdue] == [0.0, 1.0]


def test_open_sev1_tickets_count_distinct_tickets_per_company_at_load_time():
    connector, _ = make()
    counts = {
        s.account_id: s for s in connector.list_signals() if s.kind is SignalKind.OPEN_TICKETS_SEV1
    }
    assert {account_id: s.value for account_id, s in counts.items()} == {
        "hs-1001": 2.0,  # two HIGH tickets, one linked with the {id} shape
        "hs-1002": 0.0,  # its only ticket is LOW
        "hs-1003": 0.0,  # its only URGENT ticket is closed
        "hs-1005": 1.0,  # shares one ticket with 1006
        "hs-1006": 1.0,
        "hs-1007": 0.0,
    }
    assert all(s.observed_at == NOW and s.source == "hubspot" for s in counts.values())


def test_the_company_without_a_renewal_date_has_no_account_and_no_signals():
    connector, _ = make()
    assert "hs-1004" not in {account.id for account in connector.list_accounts()}
    assert not [s for s in connector.list_signals() if s.account_id == "hs-1004"]


def test_non_customers_are_not_loaded():
    connector, _ = make()
    assert "hs-1008" not in {account.id for account in connector.list_accounts()}


def test_list_signals_filters_to_one_account():
    connector, _ = make()
    only = connector.list_signals("hs-1001")
    assert only and {s.account_id for s in only} == {"hs-1001"}


def test_company_lookups_are_chunked_at_50():
    def many(request: httpx.Request) -> httpx.Response:
        body = json.loads(serve(request).content)
        body["total"] = 120
        body["results"] = [
            {"id": str(2000 + i), "createdAt": "x", "updatedAt": "x"} for i in range(120)
        ]
        body.pop("paging", None)
        return httpx.Response(200, json=body)

    connector, seen = make(overrides={("POST", "companies_search"): many})
    connector.list_accounts()
    bodies = [json.loads(r.content) for r in seen if r.content]
    batches = [body for body in bodies if "propertiesWithHistory" in body]
    assert [len(batch["inputs"]) for batch in batches] == [50, 50, 20]


def test_ticket_lookups_are_chunked_at_100():
    def many(request: httpx.Request) -> httpx.Response:
        body = json.loads(serve(request).content)
        body["total"] = 150
        body["results"] = [{"id": str(9000 + i), "properties": {}} for i in range(150)]
        body.pop("paging", None)
        return httpx.Response(200, json=body)

    connector, seen = make(overrides={("POST", "tickets_search"): many})
    connector.list_accounts()
    sizes = [len(json.loads(r.content)["inputs"]) for r in seen if "/associations/" in r.url.path]
    assert sizes == [100, 50]


def test_requests_carry_the_customer_and_sev1_filters():
    connector, seen = make()
    connector.list_accounts()
    search = next(r for r in seen if r.url.path.endswith("/companies/search"))
    [group] = json.loads(search.content)["filterGroups"]
    assert group["filters"] == [
        {"propertyName": "lifecyclestage", "operator": "EQ", "value": "customer"}
    ]
    tickets = next(r for r in seen if r.url.path.endswith("/tickets/search"))
    [group] = json.loads(tickets.content)["filterGroups"]
    assert group["filters"] == [
        {"propertyName": "hs_ticket_priority", "operator": "IN", "values": ["HIGH", "URGENT"]},
        {"propertyName": "closed_date", "operator": "NOT_HAS_PROPERTY"},
    ]


# Caching


def test_a_second_call_is_served_from_memory():
    connector, seen = make()
    connector.list_accounts()
    loaded = len(seen)
    assert loaded > 0
    connector.list_accounts()
    connector.list_signals()
    assert len(seen) == loaded


def test_refresh_fetches_again():
    connector, seen = make()
    connector.list_accounts()
    loaded = len(seen)
    connector.refresh()
    connector.list_accounts()
    assert len(seen) == 2 * loaded


def test_the_cache_expires_after_cache_seconds_on_the_injected_clock():
    clock = FakeClock(100.0)
    connector, seen = make(clock=clock, cache_seconds=900.0)
    connector.list_accounts()
    loaded = len(seen)
    assert connector.loaded_at == 100.0
    clock.now = 999.5
    connector.list_accounts()
    assert len(seen) == loaded
    clock.now = 1000.0
    connector.list_accounts()
    assert len(seen) == 2 * loaded
    assert connector.loaded_at == 1000.0


def test_a_failed_refresh_keeps_the_last_good_load():
    state = {"down": False}

    def flaky(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            return httpx.Response(500, json={"status": "error", "message": "boom"})
        return serve(request)

    clock = FakeClock(0.0)
    connector, _ = make(overrides={("POST", "companies_search"): flaky}, clock=clock)
    before = connector.list_accounts()
    warnings_before = connector.warnings
    loaded_at = connector.loaded_at

    state["down"] = True
    connector.refresh()
    with pytest.raises(HubSpotUnavailable):
        connector.list_accounts()
    assert connector.warnings == warnings_before
    assert connector.loaded_at == loaded_at

    state["down"] = False
    assert connector.list_accounts() == before


def test_legacy_paths_load_the_same_book():
    dated, _ = make()
    legacy, seen = make(dated=False)
    assert legacy.list_accounts() == dated.list_accounts()
    assert legacy.list_signals() == dated.list_signals()
    assert legacy.warnings == dated.warnings
    assert any("/crm/v3/" in r.url.path for r in seen)


# Mapping file


def test_missing_mapping_file_gives_the_defaults(tmp_path):
    assert load_mapping(tmp_path / "hubspot.yaml") == HubSpotMapping()


def test_mapping_is_read_from_the_config_folder_by_default():
    (config_dir() / "hubspot.yaml").write_text("arr: annual_revenue\n", encoding="utf-8")
    assert load_mapping().arr == "annual_revenue"


def test_partial_mapping_overrides_only_the_given_fields(tmp_path):
    path = tmp_path / "hubspot.yaml"
    path.write_text(
        "arr: annual_recurring_revenue\n"
        "history_days: 30\n"
        "signals:\n"
        "  usage_trend_30d: product_usage_change_30d\n",
        encoding="utf-8",
    )
    mapping = load_mapping(path)
    defaults = HubSpotMapping()
    assert mapping.arr == "annual_recurring_revenue"
    assert mapping.history_days == 30
    assert mapping.renewal_date == defaults.renewal_date
    assert mapping.signals[SignalKind.USAGE_TREND_30D] == "product_usage_change_30d"
    assert mapping.signals[SignalKind.NPS_LATEST] == defaults.signals[SignalKind.NPS_LATEST]
    assert set(mapping.signals) == set(defaults.signals)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("signals: [unclosed", "it isn't valid YAML"),
        ("history_days: -3", "history_days"),
        ("arr_name: cs_arr", "arr_name isn't a setting Wardmind knows"),
        ("signals:\n  nope_kind: cs_x\n", "signals.nope_kind isn't a signal Wardmind knows"),
        ("signals:\n  open_tickets_sev1: x\n", "signals.open_tickets_sev1 comes from tickets"),
        ("arr: '   '\n", "arr is blank"),
        ("- just\n- a list\n", "it should be a set of name: value settings"),
    ],
)
def test_an_invalid_mapping_file_raises_a_plain_error(tmp_path, text, reason):
    path = tmp_path / "hubspot.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConnectorError) as info:
        load_mapping(path)
    message = info.value.message
    assert message.startswith("config/hubspot.yaml has a problem: ")
    assert message.endswith(". Fix it or delete the file to use the defaults.")
    assert reason in message


# check_connection


def test_check_connection_passes_on_the_fixture_portal():
    connector, _ = make()
    result = connector.check_connection()
    assert result.ok is True
    assert [(c.name, c.ok, c.message) for c in result.checks] == [
        ("Token accepted", True, "HubSpot accepted the access token."),
        ("Companies", True, "7 customer companies found."),
        ("Owners", True, "Owners can be read."),
        ("Tickets", True, "High-priority tickets can be read."),
    ]
    assert result.missing_properties == []


def test_a_single_customer_company_is_singular():
    connector, _ = make(
        overrides={("POST", "companies_search"): edit_search(lambda b: b.update(total=1))}
    )
    companies = connector.check_connection().checks[1]
    assert companies.message == "1 customer company found."


def test_401_on_the_first_call_fails_the_token_and_skips_the_rest():
    connector, _ = make(
        overrides={
            ("POST", "companies_search"): httpx.Response(401, json=fixture("error_401.json"))
        }
    )
    result = connector.check_connection()
    assert result.ok is False
    assert [(c.name, c.ok, c.message) for c in result.checks] == [
        ("Token accepted", False, AUTH_MESSAGE),
        ("Companies", False, "Skipped until the token works."),
        ("Owners", False, "Skipped until the token works."),
        ("Tickets", False, "Skipped until the token works."),
    ]
    assert result.missing_properties == []


def test_403_on_tickets_fails_only_the_tickets_check():
    connector, _ = make(
        overrides={
            ("POST", "tickets_search"): httpx.Response(403, json=fixture("error_403_tickets.json"))
        }
    )
    result = connector.check_connection()
    by_name = {c.name: c for c in result.checks}
    assert result.ok is False
    assert by_name["Token accepted"].ok
    assert by_name["Companies"].ok
    assert by_name["Owners"].ok
    assert by_name["Tickets"].ok is False
    assert by_name["Tickets"].message == (
        'This key is missing the "tickets" scope. Add it in HubSpot and test again.'
    )


def test_403_on_companies_still_accepts_the_token_and_runs_the_other_checks():
    connector, _ = make(
        overrides={
            ("POST", "companies_search"): httpx.Response(
                403, json=fixture("error_403_two_scopes.json")
            )
        }
    )
    result = connector.check_connection()
    by_name = {c.name: c for c in result.checks}
    assert by_name["Token accepted"].ok
    assert by_name["Companies"].ok is False
    assert by_name["Companies"].message == (
        'This key is missing the "tickets" and "crm.objects.owners.read" scopes. '
        "Add them in HubSpot and test again."
    )
    assert by_name["Owners"].ok and by_name["Tickets"].ok


def test_zero_customer_companies_is_still_ok_and_says_where_to_look():
    empty = httpx.Response(200, json={"total": 0, "results": []})
    connector, _ = make(overrides={("POST", "companies_search"): empty})
    result = connector.check_connection()
    companies = result.checks[1]
    assert companies.ok is True
    assert companies.message == (
        "No companies have lifecyclestage set to customer. "
        "Check customer_property in config/hubspot.yaml."
    )
    assert result.missing_properties == []
    assert result.ok is True


def test_a_key_absent_from_every_sampled_company_is_missing():
    def no_seats(result: dict) -> None:
        result["properties"].pop("cs_seat_utilisation", None)

    connector, _ = make(overrides={("POST", "companies_batch_read"): edit_batch(no_seats)})
    result = connector.check_connection()
    assert result.missing_properties == ["cs_seat_utilisation"]
    assert result.ok is True


def test_a_key_missing_from_the_first_company_but_present_later_is_not_missing():
    def first_company_without_seats(result: dict) -> None:
        if result["id"] == "1001":
            result["properties"].pop("cs_seat_utilisation", None)

    connector, _ = make(
        overrides={("POST", "companies_batch_read"): edit_batch(first_company_without_seats)}
    )
    result = connector.check_connection()
    assert result.missing_properties == []
    assert result.ok is True


def test_a_key_returned_as_null_counts_as_present():
    def null_seats_on_first_company(result: dict) -> None:
        result["properties"].pop("cs_seat_utilisation", None)
        if result["id"] == "1001":
            result["properties"]["cs_seat_utilisation"] = None

    connector, _ = make(
        overrides={("POST", "companies_batch_read"): edit_batch(null_seats_on_first_company)}
    )
    result = connector.check_connection()
    assert result.missing_properties == []
    assert result.ok is True


def test_the_property_check_samples_at_most_50_companies_in_one_batch_read():
    def many(request: httpx.Request) -> httpx.Response:
        body = json.loads(serve(request).content)
        body["total"] = 60
        body["results"] = [
            {"id": str(3000 + i), "properties": {}, "createdAt": "x", "updatedAt": "x"}
            for i in range(60)
        ]
        body.pop("paging", None)
        return httpx.Response(200, json=body)

    connector, seen = make(overrides={("POST", "companies_search"): many})
    connector.check_connection()
    reads = [
        json.loads(r.content)
        for r in seen
        if route_name(r.method, r.url.path) == "companies_batch_read"
    ]
    assert len(reads) == 1
    assert "propertiesWithHistory" not in reads[0]
    assert [item["id"] for item in reads[0]["inputs"]] == [str(3000 + i) for i in range(50)]


def test_check_connection_never_raises_for_a_hubspot_error():
    connector, _ = make(
        overrides={("POST", "companies_search"): httpx.Response(500, json={"message": "x"})}
    )
    result = connector.check_connection()
    assert result.checks[0].name == "Token accepted" and result.checks[0].ok is False
    assert all(not c.ok for c in result.checks)


# Website (the company domain property)


def test_the_domain_is_read_normalised_and_missing_gives_none():
    connector, _ = make()
    accounts = {account.id: account for account in connector.list_accounts()}
    assert accounts["hs-1001"].domain == "brightwater-logistics.example"  # a URL, www and a path
    assert accounts["hs-1002"].domain == "northgate-health.example"  # upper case
    assert accounts["hs-1003"].domain == "cobaltridge.example"
    assert accounts["hs-1006"].domain == "pinecrest.example"  # "www." removed
    assert accounts["hs-1005"].domain is None  # no domain property
    assert accounts["hs-1007"].domain is None


def test_the_domain_property_is_requested_with_the_other_company_properties():
    connector, seen = make()
    connector.list_accounts()
    company_requests = [r for r in seen if "/companies/" in r.url.path and r.content]
    bodies = [json.loads(request.content) for request in company_requests]
    requests_with_properties = [body for body in bodies if "properties" in body]
    assert requests_with_properties
    assert all("domain" in body["properties"] for body in requests_with_properties)


def test_a_mapping_can_name_a_different_domain_property(tmp_path):
    path = tmp_path / "hubspot.yaml"
    path.write_text("domain: website_url\n", encoding="utf-8")
    assert load_mapping(path).domain == "website_url"
    assert HubSpotMapping().domain == "domain"


# End to end


def test_get_book_scores_the_hubspot_book():
    connector, _ = make()
    book = service.get_book(connector=connector, config=ScoringConfig(), as_of=TODAY)
    by_id = {item.account.id: item for item in book}
    assert set(by_id) == set(EXPECTED)
    brightwater = by_id["hs-1001"]
    assert brightwater.health is Health.AT_RISK
    assert brightwater.motion is Motion.RENEWAL
    assert by_id["hs-1002"].motion is Motion.ONBOARDING
    assert by_id["hs-1003"].motion is Motion.CHURN
