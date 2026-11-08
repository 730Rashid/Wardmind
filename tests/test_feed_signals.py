"""SIGNALS feeds: how a row finds its account (by ID, website or name, and AUTO's order), what
happens to rows that match nothing or several accounts, and how readings are parsed and kept.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from cs_brief.core.feeds import signals
from cs_brief.core.feeds.models import MatchBy
from cs_brief.core.importing import DateOrder, Table
from cs_brief.core.models import Account, Milestones, SignalKind

NOW = datetime(2026, 11, 8, 9, 0)


def acct(acc_id: str, name: str, domain: str | None = None) -> Account:
    return Account(
        id=acc_id,
        name=name,
        arr=50000.0,
        tier="Growth",
        owner="Dana Whitfield",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
        domain=domain,
    )


def table(headers: list[str], *rows: list[str], row_numbers: list[int] | None = None) -> Table:
    return Table(
        source_name="feed.csv",
        headers=headers,
        rows=[list(row) for row in rows],
        row_numbers=row_numbers or [],
    )


def readings(
    feed: Table,
    mapping: dict[str, str],
    accounts: list[Account],
    *,
    match_by: MatchBy = MatchBy.AUTO,
    date_order: DateOrder = DateOrder.DMY,
    observed_at: datetime = NOW,
):
    return signals.build_signal_readings(
        feed,
        mapping,
        match_by=match_by,
        accounts=accounts,
        date_order=date_order,
        observed_at=observed_at,
        source="feed:feed-1",
    )


USAGE = {"external_id": "ID", "signal:usage_trend_30d": "Usage trend"}


# account_keys -----------------------------------------------------------------------------------


def test_a_hubspot_id_matches_with_and_without_its_prefix() -> None:
    keys = signals.account_keys(acct("hs-123", "Acme"))
    assert keys[MatchBy.ID] == {"hs-123", "123"}


def test_a_spreadsheet_id_matches_without_its_prefix_too() -> None:
    keys = signals.account_keys(acct("sp2-acme-inc", "Acme Inc"))
    assert keys[MatchBy.ID] == {"sp2-acme-inc", "acme-inc"}
    assert keys[MatchBy.NAME] == {"acme"}


@pytest.mark.parametrize(
    ("account_id", "expected"),
    [
        ("fd1-x", {"fd1-x", "x"}),
        ("m-ab12", {"m-ab12", "ab12"}),
        ("acc-001", {"acc-001"}),
        ("HS-ABC", {"hs-abc", "abc"}),
    ],
)
def test_other_connections_prefixes_and_the_case_of_ids(
    account_id: str, expected: set[str]
) -> None:
    assert signals.account_keys(acct(account_id, "Acme"))[MatchBy.ID] == expected


def test_the_website_key_is_the_normalised_domain_when_there_is_one() -> None:
    keys = signals.account_keys(acct("hs-1", "Acme", domain="https://www.Acme.com/about"))
    assert keys[MatchBy.WEBSITE] == {"acme.com"}
    assert signals.account_keys(acct("hs-2", "Beta"))[MatchBy.WEBSITE] == set()


# Finding the account ----------------------------------------------------------------------------


def test_a_row_matches_by_id() -> None:
    result = readings(
        table(["ID", "Usage trend"], ["123", "-38"]),
        USAGE,
        [acct("hs-123", "Acme")],
        match_by=MatchBy.ID,
    )
    assert result.matched_accounts == 1
    assert len(result.signals) == 1
    signal = result.signals[0]
    assert signal.account_id == "hs-123"
    assert signal.kind is SignalKind.USAGE_TREND_30D
    assert signal.value == -38.0
    assert signal.observed_at == NOW
    assert signal.source == "feed:feed-1"


def test_a_row_matches_by_website_whatever_its_form() -> None:
    result = readings(
        table(["Website", "Usage trend"], ["https://www.acme.com/pricing", "5"]),
        {"domain": "Website", "signal:usage_trend_30d": "Usage trend"},
        [acct("hs-1", "Acme", domain="acme.com"), acct("hs-2", "Beta")],
        match_by=MatchBy.WEBSITE,
    )
    assert [signal.account_id for signal in result.signals] == ["hs-1"]


def test_a_row_matches_by_name_ignoring_case_and_legal_words() -> None:
    result = readings(
        table(["Name", "Usage trend"], ["ACME Inc.", "5"]),
        {"name": "Name", "signal:usage_trend_30d": "Usage trend"},
        [acct("hs-1", "Acme"), acct("hs-2", "Beta")],
        match_by=MatchBy.NAME,
    )
    assert [signal.account_id for signal in result.signals] == ["hs-1"]


def test_auto_tries_the_id_before_the_website_and_the_name() -> None:
    mapping = {
        "external_id": "ID",
        "domain": "Website",
        "name": "Name",
        "signal:usage_trend_30d": "Usage trend",
    }
    accounts = [acct("hs-1", "Alpha", domain="a.com"), acct("hs-2", "Beta", domain="b.com")]
    result = readings(
        table(["ID", "Website", "Name", "Usage trend"], ["1", "b.com", "Beta", "5"]),
        mapping,
        accounts,
    )
    assert [signal.account_id for signal in result.signals] == ["hs-1"]


def test_auto_falls_through_when_the_id_matches_nothing() -> None:
    mapping = {"external_id": "ID", "name": "Name", "signal:usage_trend_30d": "Usage trend"}
    result = readings(
        table(["ID", "Name", "Usage trend"], ["999", "Beta", "5"]),
        mapping,
        [acct("hs-1", "Alpha"), acct("hs-2", "Beta")],
    )
    assert [signal.account_id for signal in result.signals] == ["hs-2"]


def test_auto_falls_through_when_the_id_is_shared_by_two_accounts() -> None:
    mapping = {"external_id": "ID", "name": "Name", "signal:usage_trend_30d": "Usage trend"}
    accounts = [acct("hs-123", "Acme"), acct("sp1-123", "Other")]
    result = readings(
        table(["ID", "Name", "Usage trend"], ["123", "Other", "5"]),
        mapping,
        accounts,
    )
    assert [signal.account_id for signal in result.signals] == ["sp1-123"]
    assert result.unmatched_count == 0


def test_a_name_shared_by_two_accounts_is_unmatched_with_the_count() -> None:
    result = readings(
        table(["Name", "Usage trend"], ["Acme", "5"]),
        {"name": "Name", "signal:usage_trend_30d": "Usage trend"},
        [acct("hs-1", "Acme"), acct("hs-2", "Acme Inc")],
    )
    assert result.signals == []
    assert result.unmatched_count == 1
    assert result.unmatched[0].key == "Acme"
    assert result.unmatched[0].reason == "2 accounts are called Acme."


def test_a_website_no_account_has_is_unmatched_with_the_row_text() -> None:
    result = readings(
        table(["Website", "Usage trend"], ["nowhere.example", "5"]),
        {"domain": "Website", "signal:usage_trend_30d": "Usage trend"},
        [acct("hs-1", "Acme", domain="acme.com")],
        match_by=MatchBy.WEBSITE,
    )
    assert result.unmatched[0].row == 2
    assert result.unmatched[0].key == "nowhere.example"
    assert result.unmatched[0].reason == "No account has the website nowhere.example."


def test_an_id_no_account_has_is_unmatched_and_the_reason_says_so() -> None:
    result = readings(
        table(["ID", "Usage trend"], ["555", "5"]),
        USAGE,
        [acct("hs-1", "Acme")],
        match_by=MatchBy.ID,
    )
    assert result.unmatched[0].reason == "No account has the ID 555."


def test_a_row_with_no_key_at_all_is_unmatched() -> None:
    result = readings(
        table(["ID", "Name", "Usage trend"], ["", "  ", "5"]),
        {"external_id": "ID", "name": "Name", "signal:usage_trend_30d": "Usage trend"},
        [acct("hs-1", "Acme")],
    )
    assert result.unmatched[0].reason == "This row has no ID, website or name."
    assert result.unmatched[0].key == ""


def test_a_chosen_method_with_a_blank_cell_says_which_key_is_missing() -> None:
    result = readings(
        table(["ID", "Usage trend"], ["", "5"]),
        USAGE,
        [acct("hs-1", "Acme")],
        match_by=MatchBy.ID,
    )
    assert result.unmatched[0].reason == "This row has no ID."


def test_a_chosen_method_does_not_fall_through_to_another_column() -> None:
    result = readings(
        table(["ID", "Name", "Usage trend"], ["999", "Acme", "5"]),
        {"external_id": "ID", "name": "Name", "signal:usage_trend_30d": "Usage trend"},
        [acct("hs-1", "Acme")],
        match_by=MatchBy.ID,
    )
    assert result.signals == []
    assert result.unmatched[0].reason == "No account has the ID 999."


def test_unmatched_rows_report_their_spreadsheet_row_numbers() -> None:
    result = readings(
        table(["ID", "Usage trend"], ["555", "5"], ["556", "5"], row_numbers=[5, 9]),
        USAGE,
        [],
        match_by=MatchBy.ID,
    )
    assert [item.row for item in result.unmatched] == [5, 9]


def test_unmatched_rows_are_capped_but_the_count_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signals, "MAX_UNMATCHED", 2)
    rows = [[str(900 + index), "5"] for index in range(5)]
    result = readings(table(["ID", "Usage trend"], *rows), USAGE, [], match_by=MatchBy.ID)
    assert len(result.unmatched) == 2
    assert result.unmatched_count == 5
    assert [item.row for item in result.unmatched] == [2, 3]


# Mapping checks -----------------------------------------------------------------------------------


def test_a_mapping_with_no_key_column_is_refused() -> None:
    with pytest.raises(ValueError, match="Match a column to ID, Website or Account name."):
        readings(table(["Usage trend"], ["5"]), {"signal:usage_trend_30d": "Usage trend"}, [])


def test_a_chosen_method_without_its_own_column_is_refused() -> None:
    with pytest.raises(ValueError, match="Match a column to website."):
        readings(
            table(["Name", "Usage trend"], ["Acme", "5"]),
            {"name": "Name", "signal:usage_trend_30d": "Usage trend"},
            [],
            match_by=MatchBy.WEBSITE,
        )


def test_a_mapping_with_no_signal_column_is_refused() -> None:
    with pytest.raises(ValueError, match="Match at least one signal column"):
        readings(table(["ID"], ["1"]), {"external_id": "ID"}, [])


def test_a_mapping_to_a_missing_column_or_unknown_field_is_refused() -> None:
    with pytest.raises(ValueError, match="isn't a column"):
        readings(table(["ID"], ["1"]), {"external_id": "Nope", "signal:nps_latest": "Nope"}, [])
    with pytest.raises(ValueError, match="isn't an import field"):
        readings(table(["ID"], ["1"]), {"bogus": "ID"}, [])


# Readings -----------------------------------------------------------------------------------------


def test_the_later_row_wins_for_the_same_account_and_kind() -> None:
    result = readings(
        table(["ID", "Usage trend"], ["123", "-10"], ["123", "-20"]),
        USAGE,
        [acct("hs-123", "Acme")],
        match_by=MatchBy.ID,
    )
    assert len(result.signals) == 1
    assert result.signals[0].value == -20.0


def test_values_are_read_with_the_import_rules_and_bad_ones_become_issues() -> None:
    headers = ["ID", "Usage trend", "NPS", "Champion", "Days since login"]
    feed = table(
        headers,
        ["1", "abc", "11", "yes", "12"],
    )
    mapping = {
        "external_id": "ID",
        "signal:usage_trend_30d": "Usage trend",
        "signal:nps_latest": "NPS",
        "signal:champion_left": "Champion",
        "signal:days_since_last_login": "Days since login",
    }
    result = readings(feed, mapping, [acct("hs-1", "Acme")], match_by=MatchBy.ID)
    kinds = {signal.kind: signal.value for signal in result.signals}
    assert kinds == {SignalKind.CHAMPION_LEFT: 1.0, SignalKind.DAYS_SINCE_LAST_LOGIN: 12.0}
    fields = [(issue.row, issue.field) for issue in result.issues]
    assert fields == [(2, "signal:usage_trend_30d"), (2, "signal:nps_latest")]
    assert result.issues[0].message.startswith("Usage trend ")
    assert "between 0 and 10" in result.issues[1].message


def test_observed_time_is_stored_as_naive_utc() -> None:
    moment = datetime(2026, 11, 8, 10, 0, tzinfo=timezone(timedelta(hours=1)))
    result = readings(
        table(["ID", "Usage trend"], ["123", "5"]),
        USAGE,
        [acct("hs-123", "Acme")],
        match_by=MatchBy.ID,
        observed_at=moment,
    )
    assert result.signals[0].observed_at == datetime(2026, 11, 8, 9, 0)
    assert result.signals[0].observed_at.tzinfo is None
    assert moment.astimezone(UTC).replace(tzinfo=None) == result.signals[0].observed_at


def test_matched_accounts_counts_each_account_that_got_a_reading() -> None:
    result = readings(
        table(["ID", "Usage trend"], ["1", "5"], ["1", "6"], ["2", "7"], ["3", ""]),
        USAGE,
        [acct("hs-1", "Alpha"), acct("hs-2", "Beta"), acct("hs-3", "Gamma")],
        match_by=MatchBy.ID,
    )
    assert result.matched_accounts == 2
    assert {signal.account_id for signal in result.signals} == {"hs-1", "hs-2"}


def test_a_matched_row_with_no_readings_is_not_counted_as_an_account_with_readings() -> None:
    result = readings(
        table(["ID", "Usage trend"], ["1", ""]),
        USAGE,
        [acct("hs-1", "Alpha")],
        match_by=MatchBy.ID,
    )
    assert result.matched_accounts == 0
    assert result.signals == []
    assert result.unmatched_count == 0
