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

from wardmind.core.config import config_dir, write_atomic

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
    """The screen `wardmind ui` opens on, and where the product name links to."""

    BOOK = "book"
    BRIEF = "brief"
    TODAY = "today"

    @property
    def path(self) -> str:
        return {"book": "/", "brief": "/brief", "today": "/today"}[self.value]


class DataSource(StrEnum):
    """Which book the screens show. Demo data is invented and never mixed with real data. Your
    data is the connections in the local store: HubSpot, spreadsheets and hand-added accounts."""

    DEMO = "demo"
    MINE = "mine"

    @property
    def label(self) -> str:
        return {"demo": "Demo data", "mine": "Your data"}[self.value]


AUTO_LOCK_CHOICES = (5, 15, 30, 60, 0)  # minutes; 0 means never


class Currency(StrEnum):
    """How money is shown. Display only: Wardmind never converts amounts between currencies."""

    USD = "usd"
    GBP = "gbp"
    EUR = "eur"
    CAD = "cad"
    AUD = "aud"
    NZD = "nzd"
    INR = "inr"
    JPY = "jpy"

    @property
    def symbol(self) -> str:
        return {
            "usd": "$",
            "gbp": "£",
            "eur": "€",
            "cad": "CA$",
            "aud": "A$",
            "nzd": "NZ$",
            "inr": "₹",
            "jpy": "¥",
        }[self.value]

    @property
    def label(self) -> str:
        return {
            "usd": "US dollar",
            "gbp": "Pound sterling",
            "eur": "Euro",
            "cad": "Canadian dollar",
            "aud": "Australian dollar",
            "nzd": "New Zealand dollar",
            "inr": "Indian rupee",
            "jpy": "Japanese yen",
        }[self.value]


class DateStyle(StrEnum):
    """How a full date is written."""

    DAY_MONTH = "day_month"  # 21 Oct 2026
    MONTH_DAY = "month_day"  # Oct 21, 2026
    ISO = "iso"  # 2026-10-21


MAX_NAME = 60  # characters in your_name
RENEWAL_RULE_CHOICES = (30, 60, 90, 120)  # days before renewal for the renewal rule


def _bounded(low: int, high: int, message: str) -> Any:
    """A field validator for a whole number of days between low and high, inclusive."""

    def check(value: int) -> int:
        if not low <= value <= high:
            raise ValueError(message)
        return value

    return check


class Settings(BaseModel):
    theme: Theme = Theme.LIGHT  # light unless the CSM picks Dark or Match my computer
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
    # Encryption at rest (core/vault.py): lock after this many idle minutes; 0 never locks.
    auto_lock_minutes: int = 15

    # --- Profile & region (Settings > Profile & region) ----------------------------------------
    # Your name, shown as "Prepared by" on the renewal sheet and on the brief. Plain text, one
    # line, at most MAX_NAME characters; "" means not set.
    your_name: str = ""
    currency: Currency = Currency.USD
    date_style: DateStyle = DateStyle.DAY_MONTH

    # --- Follow-ups (Settings > Follow-ups) ----------------------------------------------------
    # These replace the journal's fixed constants of the same meaning; the defaults are the old
    # constants, so a settings file without them behaves exactly as before.
    follow_up_days: int = 7  # a new follow-up is due this many days out (1..90)
    skip_weekends: bool = False  # a due day on Saturday or Sunday moves to the Monday
    quiet_days: int = 14  # "Needs attention" skips accounts contacted this recently (3..90)
    reply_wait_days: int = 5  # a sent email unanswered this long awaits their reply (1..30)
    answer_wait_days: int = 2  # a received email unanswered this long awaits yours (1..14)

    # Automatic follow-ups (core/rules.py). Each is off until turned on. A rule adds one
    # follow-up per account per occasion and never adds the same one twice.
    rule_at_risk: bool = False  # an account turns At risk: "Check in" due in rule_at_risk_days
    rule_at_risk_days: int = 2  # 0..30
    rule_renewal: bool = False  # rule_renewal_days before renewal: "Prepare the renewal"
    rule_renewal_days: int = 60  # one of RENEWAL_RULE_CHOICES
    rule_champion_left: bool = False  # the champion leaves: "Find a new champion", due in 3 days

    @field_validator("your_name")
    @classmethod
    def _name(cls, value: str) -> str:
        """One line of plain text: control characters are refused, spaces are trimmed."""
        value = " ".join(value.split())
        if len(value) > MAX_NAME:
            raise ValueError(f"Keep your name to {MAX_NAME} characters.")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("Your name can't contain control characters.")
        return value

    _follow_up_days = field_validator("follow_up_days")(
        _bounded(1, 90, "Pick between 1 and 90 days.")
    )
    _quiet_days = field_validator("quiet_days")(_bounded(3, 90, "Pick between 3 and 90 days."))
    _reply_wait = field_validator("reply_wait_days")(_bounded(1, 30, "Pick between 1 and 30 days."))
    _answer_wait = field_validator("answer_wait_days")(
        _bounded(1, 14, "Pick between 1 and 14 days.")
    )
    _at_risk_days = field_validator("rule_at_risk_days")(
        _bounded(0, 30, "Pick between 0 and 30 days.")
    )

    @field_validator("rule_renewal_days")
    @classmethod
    def _renewal_days(cls, value: int) -> int:
        if value not in RENEWAL_RULE_CHOICES:
            raise ValueError("Pick 30, 60, 90 or 120 days.")
        return value

    @field_validator("auto_lock_minutes")
    @classmethod
    def _auto_lock(cls, value: int) -> int:
        """One of AUTO_LOCK_CHOICES; anything else falls back to the default."""
        if value not in AUTO_LOCK_CHOICES:
            raise ValueError("Pick 5, 15, 30 or 60 minutes, or never.")
        return value

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
    write_atomic(path, "# Wardmind settings, written by the Settings page.\n" + body)
