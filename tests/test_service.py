from __future__ import annotations

from datetime import date

import pytest

from wardmind.core import service
from wardmind.core.briefs import suggest_action
from wardmind.core.config import ScoringConfig
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Motion
from wardmind.core.scoring import score_book

AS_OF = date(2026, 10, 19)


@pytest.fixture(scope="module")
def book():
    return service.get_book(
        connector=DemoConnector(today=AS_OF), config=ScoringConfig(), as_of=AS_OF
    )


def test_get_connector_is_the_demo_connector():
    assert isinstance(service.get_connector(), DemoConnector)


def test_get_book_returns_all_forty_accounts(book):
    assert len(book) == 40


def test_every_item_has_a_suggested_action(book):
    assert all(item.suggested_action for item in book)
    assert all(item.suggested_action == suggest_action(item.motion, item.reasons) for item in book)


def test_order_and_scores_equal_score_book(book):
    connector = DemoConnector(today=AS_OF)
    expected = score_book(
        connector.list_accounts(), connector.list_signals(), ScoringConfig(), AS_OF
    )
    # Only the suggested action differs, so blank it before comparing the whole item.
    assert [item.model_copy(update={"suggested_action": ""}) for item in book] == expected


@pytest.mark.parametrize("motion", list(Motion))
def test_filter_by_motion_keeps_book_order(book, motion):
    kept = service.filter_book(book, motion=motion)
    assert kept == [item for item in book if item.motion is motion]
    assert kept


@pytest.mark.parametrize("query", ["hessler", "HESSLER", "  Hessler  ", "\tHessler\n"])
def test_search_ignores_case_and_surrounding_whitespace(book, query):
    kept = service.filter_book(book, search=query)
    assert sorted(item.account.name for item in kept) == [
        "Hessler Fabrication",
        "Hessler Fitness",
        "Hessler Legal",
    ]
    assert kept == [item for item in book if "Hessler" in item.account.name]


@pytest.mark.parametrize("query", [None, "", "   ", "\n\t"])
def test_empty_search_keeps_the_whole_book(book, query):
    assert service.filter_book(book, search=query) == book


def test_search_with_no_match_is_empty(book):
    assert service.filter_book(book, search="zzz-no-such-account") == []


def test_motion_and_search_combine(book):
    kept = service.filter_book(book, motion=Motion.CHURN, search="hessler")
    assert all(item.motion is Motion.CHURN for item in kept)
    assert all("Hessler" in item.account.name for item in kept)
    assert kept == [
        item for item in book if item.motion is Motion.CHURN and "Hessler" in item.account.name
    ]


def test_find_account_returns_the_match(book):
    found = service.find_account(book, "acc-001")
    assert found is not None
    assert found.account.name == "Hessler Fitness"


def test_find_account_returns_none_for_an_unknown_id(book):
    assert service.find_account(book, "acc-999") is None
