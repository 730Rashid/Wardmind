"""Spotting the same customer arriving from two connections.

cs-brief only ever *suggests* a duplicate; the CSM confirms ("Link these") or dismisses ("Not
the same"). Nothing is merged automatically.

CONTRACT FILE: models, signatures and docstrings are fixed; implementers escalate before
changing them.
"""

from __future__ import annotations

import re
from itertools import combinations
from urllib.parse import urlsplit

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

# Anything that is not a letter, digit or space (including "&" and "_") becomes a space.
_NON_WORD = re.compile(r"[^\w\s]|_")
_WWW = "www."


class DuplicateSuggestion(BaseModel):
    account_ids: tuple[str, str]  # sorted
    names: tuple[str, str]  # in the same order as account_ids
    reason: str  # "Same website: acme.com" or "Same name: Acme"


def normalize_name(name: str) -> str:
    """Casefold, turn punctuation and "&" into spaces, drop LEGAL_SUFFIXES words, collapse
    spaces. "Acme, Inc." and "ACME inc" both give "acme". A name that is only suffixes keeps
    its words (so "The Company" doesn't become "")."""
    words = _NON_WORD.sub(" ", name.casefold()).split()
    kept = [word for word in words if word not in LEGAL_SUFFIXES]
    return " ".join(kept or words)


def normalize_domain(value: str | None) -> str | None:
    """ "https://www.Acme.com/about" gives "acme.com". Strips scheme, credentials, "www.",
    port, path, query and a trailing dot; lowercases. Returns None for blank or for something
    with no dot in it."""
    text = (value or "").strip()
    if not text:
        return None
    # urlsplit only finds the host after "//", so a bare "acme.com/about" gets one in front.
    rest = text.split("://", 1)[1] if "://" in text else text
    try:
        host = urlsplit("//" + rest).hostname
    except ValueError:  # for example an unclosed "[" in an address
        return None
    if not host:
        return None
    host = host.rstrip(".")
    if host.startswith(_WWW):
        host = host[len(_WWW) :]
    if "." not in host:
        return None
    return host


def _pair(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first < second else (second, first)


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
    by_id: dict[str, Account] = {}
    for account in accounts:
        if account.id in connection_of and account.id not in by_id:
            by_id[account.id] = account

    by_domain: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for acc_id, account in by_id.items():
        domain = normalize_domain(account.domain)
        if domain is not None:
            by_domain.setdefault(domain, []).append(acc_id)
        name = normalize_name(account.name)
        if name:
            by_name.setdefault(name, []).append(acc_id)

    # Website matches are recorded first, so a pair that matches on both gives the website reason.
    reasons: dict[tuple[str, str], str] = {}
    for domain, ids in by_domain.items():
        for first, second in combinations(ids, 2):
            reasons.setdefault(_pair(first, second), f"Same website: {domain}")
    for ids in by_name.values():
        for first, second in combinations(ids, 2):
            pair = _pair(first, second)
            reasons.setdefault(pair, f"Same name: {by_id[pair[0]].name}")

    linked = {
        _pair(first, second)
        for group in links.groups
        for first, second in combinations(group.members, 2)
    }
    dismissed = {_pair(first, second) for first, second in links.dismissed}

    suggestions: list[DuplicateSuggestion] = []
    for pair, reason in reasons.items():
        first, second = pair
        if connection_of[first] == connection_of[second]:
            continue
        if pair in linked or pair in dismissed:
            continue
        suggestions.append(
            DuplicateSuggestion(
                account_ids=pair,
                names=(by_id[first].name, by_id[second].name),
                reason=reason,
            )
        )
    suggestions.sort(
        key=lambda s: (0 if s.reason.startswith("Same website") else 1, s.reason, s.account_ids)
    )
    return suggestions
