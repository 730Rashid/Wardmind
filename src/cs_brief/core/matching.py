"""Spotting the same customer arriving from two connections.

cs-brief only ever *suggests* a duplicate; the CSM confirms ("Link these") or dismisses ("Not
the same"). Nothing is merged automatically.

CONTRACT FILE: models, signatures and docstrings are fixed; implementers escalate before
changing them.
"""

from __future__ import annotations

from pydantic import BaseModel

from cs_brief.core.models import Account
from cs_brief.core.store import LinkData

# Legal-entity words dropped before comparing names, matched as whole words after casefolding
# and removing punctuation.
LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "plc",
        "gmbh",
        "ag",
        "sa",
        "sas",
        "bv",
        "nv",
        "pty",
        "co",
        "company",
        "corp",
        "corporation",
        "group",
        "holdings",
    }
)


class DuplicateSuggestion(BaseModel):
    account_ids: tuple[str, str]  # sorted
    names: tuple[str, str]  # in the same order as account_ids
    reason: str  # "Same website: acme.com" or "Same name: Acme"


def normalize_name(name: str) -> str:
    """Casefold, turn punctuation and "&" into spaces, drop LEGAL_SUFFIXES words, collapse
    spaces. "Acme, Inc." and "ACME inc" both give "acme". A name that is only suffixes keeps
    its words (so "The Company" doesn't become "")."""
    raise NotImplementedError


def normalize_domain(value: str | None) -> str | None:
    """ "https://www.Acme.com/about" gives "acme.com". Strips scheme, credentials, "www.",
    port, path, query and a trailing dot; lowercases. Returns None for blank or for something
    with no dot in it."""
    raise NotImplementedError


def suggest_duplicates(
    accounts: list[Account],
    connection_of: dict[str, str],
    links: LinkData,
) -> list[DuplicateSuggestion]:
    """Pairs of accounts from *different* connections that look like the same customer.

    A pair matches on the same normalized domain (reason "Same website: …"), otherwise on the
    same normalized name (reason "Same name: …", using the first account's display name).
    Pairs already in one LinkGroup, or listed in links.dismissed, are left out. When three or
    more accounts match each other, every cross-connection pair is listed. Sorted by reason
    (website matches first), then by the sorted ids.
    """
    raise NotImplementedError
