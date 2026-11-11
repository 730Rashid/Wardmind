"""The growth radar's two service helpers, account_funding and book_funding. They only read the
saved web research (nothing is fetched here), respect the web research setting, and use the same
window as core/growth.py. Sites are fake (research_fake.py); the autouse fixture in conftest fails
any test that reaches for a real connection."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from research_fake import NOW, acme_site
from wardmind.core import editing, growth, research, service
from wardmind.core.models import Account
from wardmind.core.settings import DataSource, load_settings, save_settings

TODAY = date(2026, 10, 22)
TITLE = "Acme raises Series B to hire support"
DEMO_FUNDED_ACCOUNT = "acc-002"  # its demo sample has a funding headline (Tidewell Energy)


def _settings(**changes: object) -> None:
    save_settings(load_settings().model_copy(update=changes))


def _on(source: DataSource = DataSource.DEMO) -> None:
    _settings(web_research=True, source=source)


def _your_account(domain: str | None = "acme.example", name: str = "Acme Corporation") -> str:
    """A hand-added account in Your data. Returns its id."""
    account = editing.create_account(
        service.get_store(),
        {
            "name": name,
            "arr": "$120,000",
            "renewal_date": "2027-01-10",
            "domain": domain or "",
        },
        today=TODAY,
    )
    _settings(source=DataSource.MINE)
    service.reset_connectors()
    return account.id


def _account(account_id: str) -> Account:
    return next(item for item in service.get_connector().list_accounts() if item.id == account_id)


def _article(
    title: str,
    published: date | None,
    topics: tuple[research.Topic, ...] = (research.Topic.FUNDING,),
) -> research.Article:
    return research.Article(
        title=title,
        url="https://acme.example/news/item",
        published=published,
        topics=list(topics),
    )


def _save(account_id: str, website: str, articles: list[research.Article]) -> None:
    """Put a saved lookup on this computer, as a lookup made at NOW would have."""
    service.research_cache().put(
        research.SiteResearch(
            account_id=account_id,
            website=website,
            fetched_at=NOW,
            site_title=None,
            site_description=None,
            feed_url=None,
            articles=articles,
        )
    )


# --- account_funding ---------------------------------------------------------------------------


def test_account_funding_is_none_while_web_research_is_off() -> None:
    account_id = _your_account()
    _save(account_id, "acme.example", [_article(TITLE, date(2026, 10, 5))])
    assert load_settings().web_research is False
    assert service.account_funding(_account(account_id), now=NOW) is None


def test_a_recent_funding_article_in_the_saved_lookup_is_the_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    monkeypatch.setattr(service, "_site_client_factory", acme_site().client)
    service.research_account(account_id, now=NOW)

    news = service.account_funding(_account(account_id), now=NOW)

    assert news == growth.FundingNews(title=TITLE, published=date(2026, 10, 5))


def test_no_saved_lookup_gives_none() -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    assert service.account_funding(_account(account_id), now=NOW) is None


def test_a_saved_lookup_for_another_website_gives_none() -> None:
    account_id = _your_account("acme.example")
    _on(DataSource.MINE)
    _save(account_id, "old-name.example", [_article(TITLE, date(2026, 10, 5))])
    assert service.account_funding(_account(account_id), now=NOW) is None


def test_funding_news_older_than_the_window_gives_none() -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    _save(account_id, "acme.example", [_article(TITLE, date(2026, 4, 1))])
    assert service.account_funding(_account(account_id), now=NOW) is None


def test_the_window_is_measured_from_the_moment_asked_for() -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    _save(account_id, "acme.example", [_article(TITLE, date(2026, 10, 5))])
    account = _account(account_id)
    assert service.account_funding(account, now=NOW) is not None
    later = NOW + timedelta(days=200)
    assert service.account_funding(account, now=later) is None


def test_a_saved_demo_sample_with_funding_news_counts() -> None:
    _on()
    service.research_account(DEMO_FUNDED_ACCOUNT, now=NOW)
    account = _account(DEMO_FUNDED_ACCOUNT)

    news = service.account_funding(account, now=NOW)

    assert news == growth.FundingNews(
        title="Tidewell Energy raises a Series B to hire in support",
        published=date(2026, 8, 30),
    )


# --- book_funding ------------------------------------------------------------------------------


def test_book_funding_is_empty_at_once_while_web_research_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account()
    _save(account_id, "acme.example", [_article(TITLE, date(2026, 10, 5))])

    def refuse() -> None:
        pytest.fail("The book must not be read while web research is off.")

    monkeypatch.setattr(service, "get_connector", refuse)
    assert service.book_funding(now=NOW) == {}


def test_book_funding_has_an_entry_only_for_accounts_with_recent_funding_news() -> None:
    _on(DataSource.MINE)
    funded_id = _your_account("acme.example", "Acme Corporation")
    quiet_id = _your_account("quiet.example", "Quiet Company")
    old_id = _your_account("old.example", "Old News Limited")
    _your_account("empty.example", "Nothing Saved Limited")
    _save(
        funded_id,
        "acme.example",
        [
            _article("Acme names a new CTO", date(2026, 10, 15), (research.Topic.LEADERSHIP,)),
            _article(TITLE, date(2026, 10, 5)),
        ],
    )
    _save(
        quiet_id,
        "quiet.example",
        [_article("Quiet Company opens an office", date(2026, 10, 1), ())],
    )
    _save(old_id, "old.example", [_article("Old News raises a seed round", date(2026, 1, 3))])

    assert service.book_funding(now=NOW) == {
        funded_id: growth.FundingNews(title=TITLE, published=date(2026, 10, 5)),
    }


def test_book_funding_is_empty_when_nothing_is_saved() -> None:
    _on(DataSource.MINE)
    _your_account("acme.example", "Acme Corporation")
    assert service.book_funding(now=NOW) == {}


def test_book_funding_agrees_with_account_funding_for_every_account() -> None:
    _on(DataSource.MINE)
    funded_id = _your_account("acme.example", "Acme Corporation")
    _your_account("quiet.example", "Quiet Company")
    _save(funded_id, "acme.example", [_article(TITLE, date(2026, 10, 5))])

    per_account = {
        item.id: service.account_funding(item, now=NOW)
        for item in service.get_connector().list_accounts()
    }

    assert service.book_funding(now=NOW) == {
        account_id: news for account_id, news in per_account.items() if news is not None
    }


def test_a_hidden_account_is_not_in_the_funding_map() -> None:
    _on(DataSource.MINE)
    account_id = _your_account("acme.example", "Acme Corporation")
    _save(account_id, "acme.example", [_article(TITLE, date(2026, 10, 5))])
    service.get_store().set_hidden(account_id, True)
    service.reset_connectors()
    assert service.book_funding(now=NOW) == {}


def test_book_funding_includes_a_demo_sample_with_funding_news() -> None:
    _on()
    service.research_account(DEMO_FUNDED_ACCOUNT, now=NOW)
    assert service.book_funding(now=NOW) == {
        DEMO_FUNDED_ACCOUNT: growth.FundingNews(
            title="Tidewell Energy raises a Series B to hire in support",
            published=date(2026, 8, 30),
        ),
    }
