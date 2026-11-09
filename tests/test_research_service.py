"""Web research in the service: the setting, demo samples, lookups for your own accounts, the saved
lookups, and Clear local cache. Sites are fake (research_fake.py); the autouse fixture in conftest
fails any test that reaches for a real connection."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from research_fake import NOW, acme_site, counted, reply
from wardmind.core import editing, import_flow, importing, research, service
from wardmind.core.settings import DataSource, load_settings, save_settings
from wardmind.core.store import ConnectionKind

TODAY = date(2026, 10, 22)
DEMO_ACCOUNT = "acc-001"


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


def _research_files() -> list[Path]:
    root = service.get_store().root / research.RESEARCH_DIR
    return sorted(root.glob("*.json")) if root.is_dir() else []


def _draft(text: str = 'Company,ARR,Renewal date\nAcme,"$1",2027-01-10\n') -> None:
    store = service.get_store()
    table = importing.read_csv(text.encode("utf-8"), "renewals.csv")
    import_flow.save_draft(store, table=table, file_bytes=text.encode("utf-8"), now=NOW)


def test_with_web_research_off_nothing_is_looked_up_or_read() -> None:
    assert load_settings().web_research is False
    with pytest.raises(research.ResearchOff):
        service.research_account(DEMO_ACCOUNT, now=NOW)
    assert service.cached_research(DEMO_ACCOUNT, now=NOW) is None


def test_with_web_research_off_a_saved_lookup_is_not_shown() -> None:
    _on()
    service.research_account(DEMO_ACCOUNT, now=NOW)
    _settings(web_research=False)
    assert service.cached_research(DEMO_ACCOUNT, now=NOW) is None


def test_a_demo_lookup_is_an_invented_sample_saved_on_this_computer() -> None:
    _on()
    result = service.research_account(DEMO_ACCOUNT, now=NOW)
    assert result.sample is True
    assert result.account_id == DEMO_ACCOUNT
    assert result.fetched_at == NOW
    assert service.cached_research(DEMO_ACCOUNT, now=NOW) == result
    assert len(_research_files()) == 1


def test_a_saved_demo_sample_is_reused_until_refresh_asks_again() -> None:
    _on()
    first = service.research_account(DEMO_ACCOUNT, now=NOW)
    later = NOW + timedelta(hours=1)
    assert service.research_account(DEMO_ACCOUNT, now=later) == first
    again = service.research_account(DEMO_ACCOUNT, refresh=True, now=NOW + timedelta(hours=2))
    assert again.fetched_at == NOW + timedelta(hours=2)
    assert again.articles == first.articles
    assert len(_research_files()) == 1


def test_a_demo_sample_goes_stale_after_a_day() -> None:
    _on()
    service.research_account(DEMO_ACCOUNT, now=NOW)
    assert service.cached_research(DEMO_ACCOUNT, now=NOW + timedelta(hours=23)) is not None
    assert service.cached_research(DEMO_ACCOUNT, now=NOW + timedelta(hours=25)) is None


def test_a_demo_account_with_no_domain_still_gets_a_sample() -> None:
    _on()
    result = service.research_account(DEMO_ACCOUNT, now=NOW)
    assert result.sample is True
    assert result.website.endswith(".example")


def test_an_account_the_book_does_not_show_is_a_lookup_error() -> None:
    _on()
    with pytest.raises(LookupError):
        service.research_account("acc-missing", now=NOW)
    assert service.cached_research("acc-missing", now=NOW) is None


def test_a_hidden_account_is_not_in_the_book_so_it_cannot_be_looked_up() -> None:
    account_id = _your_account()
    _on()
    service.get_store().set_hidden(account_id, True)
    service.reset_connectors()
    with pytest.raises(LookupError):
        service.research_account(account_id, now=NOW)


def test_your_account_is_looked_up_once_then_read_from_the_saved_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    site = acme_site()
    calls, factory = counted(site.client)
    monkeypatch.setattr(service, "_site_client_factory", factory)

    result = service.research_account(account_id, now=NOW)
    assert result.website == "acme.example"
    assert result.sample is False
    assert result.articles
    assert site.seen[0] == "https://acme.example/"
    assert len(calls) == 1
    requests_so_far = len(site.seen)

    assert service.cached_research(account_id, now=NOW) == result
    assert service.research_account(account_id, now=NOW + timedelta(hours=1)) == result
    assert len(site.seen) == requests_so_far
    assert len(calls) == 1

    service.research_account(account_id, refresh=True, now=NOW + timedelta(hours=2))
    assert len(site.seen) > requests_so_far
    assert len(calls) == 2


def test_the_client_is_closed_after_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    closed: list[bool] = []
    site = acme_site()

    class Recording(research.SiteClient):
        def close(self) -> None:
            closed.append(True)
            super().close()

    monkeypatch.setattr(
        service, "_site_client_factory", lambda: Recording(httpx.MockTransport(site.handle))
    )
    service.research_account(account_id, now=NOW)
    assert closed == [True]


def test_an_account_with_no_website_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account(domain=None)
    _on(DataSource.MINE)
    calls, factory = counted(acme_site().client)
    monkeypatch.setattr(service, "_site_client_factory", factory)
    with pytest.raises(research.NoWebsite):
        service.research_account(account_id, now=NOW)
    assert calls == []
    assert service.cached_research(account_id, now=NOW) is None


def test_a_private_website_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account(domain="10.0.0.5")
    _on(DataSource.MINE)
    calls, factory = counted(acme_site().client)
    monkeypatch.setattr(service, "_site_client_factory", factory)
    with pytest.raises(research.BlockedWebsite):
        service.research_account(account_id, now=NOW)
    assert calls == []


def test_a_failed_lookup_raises_and_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    site = acme_site(**{"https://acme.example/": reply(b"", status=500)})
    monkeypatch.setattr(service, "_site_client_factory", site.client)
    with pytest.raises(research.SiteUnreachable):
        service.research_account(account_id, now=NOW)
    assert service.cached_research(account_id, now=NOW) is None
    assert _research_files() == []


def test_turning_web_research_off_removes_saved_lookups_and_samples() -> None:
    _on()
    service.research_account(DEMO_ACCOUNT, now=NOW)
    assert len(_research_files()) == 1
    service.set_web_research(False)
    assert load_settings().web_research is False
    assert _research_files() == []
    assert service.cached_research(DEMO_ACCOUNT, now=NOW) is None


def test_turning_web_research_on_keeps_what_is_saved() -> None:
    _on()
    service.research_account(DEMO_ACCOUNT, now=NOW)
    service.set_web_research(True)
    assert load_settings().web_research is True
    assert len(_research_files()) == 1


def test_clear_local_cache_counts_saved_lookups_and_unfinished_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    monkeypatch.setattr(service, "_site_client_factory", acme_site().client)
    service.research_account(account_id, now=NOW)
    _draft()
    _draft('Company,ARR,Renewal date\nBrindle,"$2",2027-02-10\n')

    cleared = service.clear_local_cache()
    assert cleared == service.CacheCleared(research=1, drafts=2)
    assert _research_files() == []
    assert not any((service.get_store().root / "drafts").glob("*.json"))


def test_clear_local_cache_leaves_connections_accounts_and_settings_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account()
    _on(DataSource.MINE)
    monkeypatch.setattr(service, "_site_client_factory", acme_site().client)
    service.research_account(account_id, now=NOW)
    _draft()
    store = service.get_store()
    connections_before = store.connections()
    settings_before = load_settings()
    manual_before = store.manual().accounts

    service.clear_local_cache()

    assert store.connections() == connections_before
    assert [c.kind for c in store.connections()] == [ConnectionKind.MANUAL]
    assert store.manual().accounts == manual_before
    assert load_settings() == settings_before
    assert [item.account.id for item in service.get_book()] == [account_id]


def test_clear_local_cache_forgets_the_shared_connectors() -> None:
    service._connectors[("token", "mapping", "base")] = object()  # type: ignore[assignment]
    service.clear_local_cache()
    assert service._connectors == {}


def test_clear_local_cache_with_nothing_saved_counts_zero() -> None:
    assert service.clear_local_cache() == service.CacheCleared(research=0, drafts=0)


def test_clear_local_cache_removes_a_demo_sample() -> None:
    _on()
    service.research_account(DEMO_ACCOUNT, now=NOW)
    assert service.clear_local_cache().research == 1
    assert service.cached_research(DEMO_ACCOUNT, now=NOW) is None
    assert load_settings().web_research is True
