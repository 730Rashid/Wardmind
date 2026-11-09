"""Name and website normalising, and the duplicate suggestions across connections."""

from __future__ import annotations

from datetime import date

import pytest

from wardmind.core.matching import normalize_domain, normalize_name, suggest_duplicates
from wardmind.core.models import Account, Milestones
from wardmind.core.store import LinkData, LinkGroup


def account(acc_id: str, name: str, domain: str | None = None) -> Account:
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme, Inc.", "acme"),
        ("ACME inc", "acme"),
        ("acme", "acme"),
        ("Smith & Sons Ltd", "smith sons"),
        ("  Larkspur   Freight  ", "larkspur freight"),
        ("O'Brien-Lee Co.", "o brien lee"),
        ("Café Nord GmbH", "café nord"),
        ("Holdings Group", "holdings group"),  # only suffixes: the words are kept
        ("The Company", "the"),
        ("Inc.", "inc"),
        ("", ""),
    ],
)
def test_names_normalise_by_casefolding_punctuation_and_suffixes(name, expected):
    assert normalize_name(name) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.Acme.com/about", "acme.com"),
        ("http://acme.com:8080/x?y=1#z", "acme.com"),
        ("https://user:secret@www.acme.com", "acme.com"),
        ("ACME.COM", "acme.com"),
        ("  www.acme.com  ", "acme.com"),
        ("acme.com.", "acme.com"),
        ("acme.com/path", "acme.com"),
        ("sub.acme.com", "sub.acme.com"),
        ("", None),
        ("   ", None),
        (None, None),
        ("localhost", None),
        ("https://", None),
        ("https://[bad", None),
    ],
)
def test_domains_drop_scheme_www_port_path_and_case(value, expected):
    assert normalize_domain(value) == expected


def test_a_plain_name_is_not_read_as_a_website():
    assert normalize_domain("Acme Ltd") is None


def test_a_website_match_is_reported_as_the_website():
    accounts = [
        account("hs-1", "Acme", "acme.com"),
        account("sp1-acme", "Something else entirely", "https://www.ACME.com/"),
    ]
    found = suggest_duplicates(accounts, {"hs-1": "hubspot", "sp1-acme": "sheet-1"}, LinkData())
    assert len(found) == 1
    assert found[0].reason == "Same website: acme.com"
    assert found[0].account_ids == ("hs-1", "sp1-acme")
    assert found[0].names == ("Acme", "Something else entirely")


def test_a_domain_match_beats_a_name_match_and_gives_the_website_reason():
    accounts = [
        account("hs-1", "Acme", "acme.com"),
        account("sp1-a", "Acme", "acme.com"),
    ]
    found = suggest_duplicates(accounts, {"hs-1": "hubspot", "sp1-a": "sheet-1"}, LinkData())
    assert [s.reason for s in found] == ["Same website: acme.com"]


def test_a_name_match_uses_the_first_accounts_display_name():
    accounts = [
        account("sp1-acme", "ACME inc", None),
        account("hs-1", "Acme, Inc.", None),
    ]
    found = suggest_duplicates(accounts, {"hs-1": "hubspot", "sp1-acme": "sheet-1"}, LinkData())
    assert len(found) == 1
    assert found[0].reason == "Same name: Acme, Inc."  # hs-1 sorts first
    assert found[0].account_ids == ("hs-1", "sp1-acme")
    assert found[0].names == ("Acme, Inc.", "ACME inc")


def test_suffix_only_names_still_match_each_other():
    accounts = [account("hs-1", "Inc.", None), account("sp1-x", "inc", None)]
    found = suggest_duplicates(accounts, {"hs-1": "hubspot", "sp1-x": "sheet-1"}, LinkData())
    assert [s.reason for s in found] == ["Same name: Inc."]


def test_accounts_of_the_same_connection_are_never_suggested():
    accounts = [
        account("sp1-a", "Acme", "acme.com"),
        account("sp1-b", "Acme", "acme.com"),
    ]
    assert suggest_duplicates(accounts, {"sp1-a": "sheet-1", "sp1-b": "sheet-1"}, LinkData()) == []


def test_same_connection_pairs_are_left_out_but_cross_pairs_are_kept():
    accounts = [
        account("sp1-a", "Acme", "acme.com"),
        account("sp1-b", "Acme", "acme.com"),
        account("hs-1", "Acme", "acme.com"),
    ]
    connections = {"sp1-a": "sheet-1", "sp1-b": "sheet-1", "hs-1": "hubspot"}
    found = suggest_duplicates(accounts, connections, LinkData())
    assert [s.account_ids for s in found] == [("hs-1", "sp1-a"), ("hs-1", "sp1-b")]


def test_linked_pairs_are_left_out():
    accounts = [account("hs-1", "Acme", "acme.com"), account("sp1-a", "Acme", "acme.com")]
    links = LinkData(groups=[LinkGroup(primary="hs-1", members=["hs-1", "sp1-a"])])
    assert suggest_duplicates(accounts, {"hs-1": "hubspot", "sp1-a": "sheet-1"}, links) == []


def test_dismissed_pairs_are_left_out_in_either_order():
    accounts = [account("hs-1", "Acme", "acme.com"), account("sp1-a", "Acme", "acme.com")]
    links = LinkData(dismissed=[("hs-1", "sp1-a")])
    assert suggest_duplicates(accounts, {"hs-1": "hubspot", "sp1-a": "sheet-1"}, links) == []


def test_a_three_way_match_lists_every_cross_connection_pair():
    accounts = [
        account("hs-1", "Acme", "acme.com"),
        account("sp1-a", "Acme", "acme.com"),
        account("sp2-a", "Acme", "acme.com"),
    ]
    connections = {"hs-1": "hubspot", "sp1-a": "sheet-1", "sp2-a": "sheet-2"}
    found = suggest_duplicates(accounts, connections, LinkData())
    assert [s.account_ids for s in found] == [
        ("hs-1", "sp1-a"),
        ("hs-1", "sp2-a"),
        ("sp1-a", "sp2-a"),
    ]


def test_website_matches_come_first_then_by_reason_and_ids():
    accounts = [
        account("hs-9", "Zeta Labs", None),
        account("sp1-z", "Zeta Labs", None),  # a name match
        account("hs-2", "Beta", "beta.example"),
        account("sp1-b", "Other", "www.beta.example"),  # a website match
        account("hs-1", "Alpha", "alpha.example"),
        account("sp1-a", "Different", "alpha.example"),  # a website match
    ]
    connections = {
        "hs-9": "hubspot",
        "sp1-z": "sheet-1",
        "hs-2": "hubspot",
        "sp1-b": "sheet-1",
        "hs-1": "hubspot",
        "sp1-a": "sheet-1",
    }
    found = suggest_duplicates(accounts, connections, LinkData())
    assert [s.reason for s in found] == [
        "Same website: alpha.example",
        "Same website: beta.example",
        "Same name: Zeta Labs",
    ]
    assert [s.account_ids for s in found] == [
        ("hs-1", "sp1-a"),
        ("hs-2", "sp1-b"),
        ("hs-9", "sp1-z"),
    ]


def test_accounts_without_a_connection_are_ignored():
    accounts = [account("hs-1", "Acme", "acme.com"), account("loose", "Acme", "acme.com")]
    assert suggest_duplicates(accounts, {"hs-1": "hubspot"}, LinkData()) == []


def test_nothing_in_common_gives_no_suggestions():
    accounts = [account("hs-1", "Acme", "acme.com"), account("sp1-a", "Globex", "globex.example")]
    assert suggest_duplicates(accounts, {"hs-1": "hubspot", "sp1-a": "sheet-1"}, LinkData()) == []


def test_empty_book_gives_no_suggestions():
    assert suggest_duplicates([], {}, LinkData()) == []
