"""UI preferences saved locally to config/settings.yaml (appearance, default landing screen and
which data the screens show).

CONTRACT FILE: the Settings schema is what the Settings page writes. Implementers escalate
before changing it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from cs_brief.core.config import config_dir, write_atomic

# Fast and cheap, and good at short factual summaries. Any OpenRouter model id can be chosen.
DEFAULT_AI_MODEL = "anthropic/claude-haiku-5.5"
_MODEL_ID = re.compile(r"~?[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]{0,99}")


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


class DataSource(StrEnum):
    """Which book the screens show. Demo data is invented and never mixed with real data. Your
    data is the connections in the local store: HubSpot, spreadsheets and hand-added accounts."""

    DEMO = "demo"
    MINE = "mine"

    @property
    def label(self) -> str:
        return {"demo": "Demo data", "mine": "Your data"}[self.value]


class Settings(BaseModel):
    theme: Theme = Theme.SYSTEM
    accent: Accent = Accent.PLUM
    density: Density = Density.COMFORTABLE
    landing: Landing = Landing.BOOK
    source: DataSource = DataSource.DEMO
    # Web research (core/research.py): off until the CSM turns it on in Data & privacy.
    web_research: bool = False
    # AI summaries of what web research read (core/summaries.py): off until turned on, and only
    # used while web_research is on too. ai_model is an OpenRouter model id.
    ai_summaries: bool = False
    ai_model: str = DEFAULT_AI_MODEL

    @field_validator("ai_model")
    @classmethod
    def _model_id(cls, value: str) -> str:
        """An OpenRouter model id such as "anthropic/claude-haiku-5.5". Anything else is refused
        (load_settings then falls back to the default for this field alone)."""
        value = value.strip()
        if not _MODEL_ID.fullmatch(value):
            raise ValueError("That isn't an OpenRouter model id, like anthropic/claude-haiku-5.5.")
        return value

    @field_validator("source", mode="before")
    @classmethod
    def _legacy_hubspot_source(cls, value: Any) -> Any:
        """A settings file saved before "Your data" said "hubspot". That choice is now "mine"."""
        return DataSource.MINE if value == "hubspot" else value


def settings_path() -> Path:
    return config_dir() / "settings.yaml"


def load_settings(path: Path | None = None) -> Settings:
    """Read settings.yaml. A missing file gives defaults; an unknown or invalid value falls
    back to that field's default rather than failing, so a hand-edited file can't break the UI.
    """
    path = path or settings_path()
    if not path.exists():
        return Settings()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    values: dict[str, Any] = {}
    for name in Settings.model_fields:
        if name not in data:
            continue
        try:
            checked = Settings.model_validate({name: data[name]})
        except ValidationError:
            continue  # this field alone falls back to its default; the others are kept
        values[name] = getattr(checked, name)
    return Settings(**values)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Write settings.yaml atomically via config.write_atomic."""
    path = path or settings_path()
    body = yaml.safe_dump(
        settings.model_dump(mode="json"), sort_keys=False, default_flow_style=False
    )
    write_atomic(path, "# cs-brief settings, written by the Settings page.\n" + body)
