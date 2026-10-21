"""Shared test setup: config is read and written in a temp folder, never the real config/."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(autouse=True)
def isolated_config_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point CS_BRIEF_CONFIG_DIR at a temp folder holding only a copy of scoring.yaml. A stray
    config/settings.yaml on disk can't change what a test sees, and saves never reach config/."""
    folder = tmp_path_factory.mktemp("config")
    source = REPO_CONFIG / "scoring.yaml"
    if source.exists():
        shutil.copy(source, folder / "scoring.yaml")
    monkeypatch.setenv("CS_BRIEF_CONFIG_DIR", str(folder))
    yield folder
