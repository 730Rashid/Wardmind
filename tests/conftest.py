"""Shared test setup: config and secrets are read and written in temp folders, never the real
config/ or the user's profile, and no HubSpot token from the real environment can reach a test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from cs_brief.core import service
from cs_brief.core.secrets import ENV_SECRETS_DIR, ENV_TOKEN

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
ENV_API_BASE = "CS_BRIEF_HUBSPOT_API_BASE"


@pytest.fixture(autouse=True)
def isolated_config_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point CS_BRIEF_CONFIG_DIR at a temp folder holding only a copy of scoring.yaml, and the
    secrets folder at another temp folder. A stray config/settings.yaml on disk can't change what a
    test sees, saves never reach config/, and a real token or test base URL is never read."""
    folder = tmp_path_factory.mktemp("config")
    source = REPO_CONFIG / "scoring.yaml"
    if source.exists():
        shutil.copy(source, folder / "scoring.yaml")
    monkeypatch.setenv("CS_BRIEF_CONFIG_DIR", str(folder))
    monkeypatch.setenv(ENV_SECRETS_DIR, str(tmp_path_factory.mktemp("secrets")))
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.delenv(ENV_API_BASE, raising=False)
    service.reset_connectors()
    yield folder
    service.reset_connectors()
