"""Shared test setup: config and secrets are read and written in temp folders, never the real
config/ or the user's profile, and no HubSpot token from the real environment can reach a test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from wardmind.core import bulk, research, service, summaries
from wardmind.core.feeds import http as feed_http
from wardmind.core.secrets import ENV_OPENROUTER_KEY, ENV_SECRETS_DIR, ENV_TOKEN
from wardmind.core.store import ENV_DATA_DIR

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
ENV_API_BASE = "WARDMIND_HUBSPOT_API_BASE"


def _refuse_feed_request(request: httpx.Request) -> httpx.Response:
    pytest.fail("A test tried to reach a data feed's address. Give it an httpx.MockTransport.")


@pytest.fixture(autouse=True)
def no_feed_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No feed can reach a web address by accident. Tests that fetch give feeds.http an
    httpx.MockTransport (feed_http.transport_factory) that answers from a table."""
    monkeypatch.setattr(
        feed_http, "transport_factory", lambda: httpx.MockTransport(_refuse_feed_request)
    )


@pytest.fixture(autouse=True)
def no_guarded_feed_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect to another origin from a public feed URL goes through feeds.http's guarded
    transport, which would resolve the host's name. By default that transport answers from the
    same table as feed_http.transport_factory, so no test resolves DNS. Tests of the guard
    itself replace guarded_transport_factory."""

    def answer_from_the_feed_table() -> httpx.BaseTransport:
        factory = feed_http.transport_factory
        return factory() if factory is not None else httpx.MockTransport(_refuse_feed_request)

    monkeypatch.setattr(feed_http, "guarded_transport_factory", answer_from_the_feed_table)


@pytest.fixture(autouse=True)
def no_feed_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test never starts the background feed scheduler (the web app starts it on startup).
    Tests of the scheduler itself build their own FeedScheduler with a fake tick."""
    monkeypatch.setattr(service, "start_feed_scheduler", lambda: None)


@pytest.fixture(autouse=True)
def isolated_config_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point WARDMIND_CONFIG_DIR at a temp folder holding only a copy of scoring.yaml, and the
    secrets and local data folders at other temp folders. A stray config/settings.yaml on disk
    can't change what a test sees, saves never reach config/, the CSM's local store is never
    written, and a real token or test base URL is never read."""
    folder = tmp_path_factory.mktemp("config")
    source = REPO_CONFIG / "scoring.yaml"
    if source.exists():
        shutil.copy(source, folder / "scoring.yaml")
    monkeypatch.setenv("WARDMIND_CONFIG_DIR", str(folder))
    monkeypatch.setenv(ENV_SECRETS_DIR, str(tmp_path_factory.mktemp("secrets")))
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path_factory.mktemp("data")))
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.delenv(ENV_API_BASE, raising=False)
    monkeypatch.delenv(ENV_OPENROUTER_KEY, raising=False)
    monkeypatch.delenv(summaries.ENV_API_BASE, raising=False)
    service.reset_connectors()
    yield folder
    service.reset_connectors()


@pytest.fixture(autouse=True)
def no_website_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test can reach a customer's website by accident. Tests that look sites up give
    service a SiteClient on an httpx.MockTransport (see tests/research_fake.py) instead."""

    def refuse() -> research.SiteClient:
        pytest.fail("A test tried to open a website connection. Give it a MockTransport client.")

    monkeypatch.setattr(service, "_site_client_factory", refuse)


@pytest.fixture(autouse=True)
def no_openrouter_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test can reach OpenRouter by accident. Tests that summarise give service an
    OpenRouterClient on an httpx.MockTransport (through service._openrouter_factory)."""

    def refuse() -> summaries.OpenRouterClient:
        pytest.fail("A test tried to call OpenRouter. Give it a MockTransport client.")

    monkeypatch.setattr(service, "_openrouter_factory", refuse)


@pytest.fixture(autouse=True)
def fresh_job_runner(monkeypatch: pytest.MonkeyPatch) -> Iterator[bulk.JobRunner]:
    """Every test gets its own runner for "Look up all websites", and waits for any job thread it
    started, so no background thread outlives its test."""
    runner = bulk.JobRunner(clock=lambda: datetime.now(UTC))
    monkeypatch.setattr(service, "_runner", runner)
    yield runner
    runner.wait(timeout=30)
