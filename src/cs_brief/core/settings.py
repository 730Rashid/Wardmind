"""UI preferences saved locally to config/settings.yaml (appearance and default landing screen).

CONTRACT FILE: the Settings schema is what the Settings page writes. Implementers escalate
before changing it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from cs_brief.core.config import config_dir


class Theme(StrEnum):
    SYSTEM = "system"  # follow the operating system's light/dark setting
    LIGHT = "light"
    DARK = "dark"


class Accent(StrEnum):
    """Interaction colour (links, focus, selection). None of them clash with health colours."""

    PLUM = "plum"
    BLUE = "blue"
    PETROL = "petrol"
    GRAPHITE = "graphite"


class Density(StrEnum):
    COMFORTABLE = "comfortable"
    COMPACT = "compact"


class Landing(StrEnum):
    """The screen `cs-brief ui` opens on, and where the product name links to."""

    BOOK = "book"
    BRIEF = "brief"

    @property
    def path(self) -> str:
        return {"book": "/", "brief": "/brief"}[self.value]


class Settings(BaseModel):
    theme: Theme = Theme.SYSTEM
    accent: Accent = Accent.PLUM
    density: Density = Density.COMFORTABLE
    landing: Landing = Landing.BOOK


def settings_path() -> Path:
    return config_dir() / "settings.yaml"


def load_settings(path: Path | None = None) -> Settings:
    """Read settings.yaml. A missing file gives defaults; an unknown or invalid value falls
    back to that field's default rather than failing, so a hand-edited file can't break the UI.
    """
    raise NotImplementedError


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings.yaml atomically via config.write_atomic."""
    raise NotImplementedError
