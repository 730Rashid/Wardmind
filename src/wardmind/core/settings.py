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
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from wardmind.core.config import config_dir, write_atomic

# Fast and cheap, and good at short factual summaries. Any OpenRouter model id can be chosen.
DEFAULT_AI_MODEL = "anthropic/claude-haiku-5.5"
_MODEL_ID = re.compile(r"~?[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]{0,99}")

# A model running on this computer (Ollama, LM Studio, llama.cpp's server and others that speak
# the OpenAI chat API). Ollama's address is the default; LM Studio's is http://127.0.0.1:1234/v1.
DEFAULT_LOCAL_AI_URL = "http://127.0.0.1:11434/v1"
LM_STUDIO_URL = "http://127.0.0.1:1234/v1"  # shown in the help, as the other common address
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_LOCAL_MODEL = 200  # characters
LOCAL_URL_MESSAGE = (
    "Use an address on this computer, such as http://127.0.0.1:11434/v1 (Ollama) or "
    "http://127.0.0.1:1234/v1 (LM Studio)."
)
LOCAL_MODEL_MESSAGE = (
    f"Use the model's name as your model app shows it, such as llama3.2, in at most "
    f"{MAX_LOCAL_MODEL} characters."
)


def check_local_url(text: str) -> str:
    """The address of a model server on this computer, trimmed and without a trailing "/".
    Only http(s) on 127.0.0.1, localhost or [::1] is accepted, with no user name, password,
    query or fragment, so summaries can never be sent off this computer by this setting.
    ValueError(LOCAL_URL_MESSAGE) otherwise."""
    value = text.strip()
    try:
        parts = urlsplit(value)
        host = parts.hostname
        _ = parts.port  # raises ValueError for a bad port
    except ValueError:
        raise ValueError(LOCAL_URL_MESSAGE) from None
    if (
        parts.scheme not in ("http", "https")
        or host not in LOCAL_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError(LOCAL_URL_MESSAGE)
    return value.rstrip("/")


def check_local_model(text: str) -> str:
    """A local model's name, trimmed ("" means none chosen yet). Any printable text up to
    MAX_LOCAL_MODEL characters: Ollama names look like "llama3.2:3b", LM Studio's like
    "qwen2.5-7b-instruct". ValueError(LOCAL_MODEL_MESSAGE) otherwise."""
    value = text.strip()
    if len(value) > MAX_LOCAL_MODEL or not value.isprintable():
        raise ValueError(LOCAL_MODEL_MESSAGE)
    return value


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


class AIProvider(StrEnum):
    """Who writes AI summaries: OpenRouter (a model online, with the CSM's own key) or a model
    running on this computer, so nothing leaves it."""

    OPENROUTER = "openrouter"
    LOCAL = "local"


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


class McpWrites(StrEnum):
    """What Claude (the MCP server, mcp/writes.py) may change in the book. Settings > Assistant.

    OFF: nothing; the write tools answer with how to turn them on.
    PROPOSE: Claude's changes arrive as suggestions on Today (core/proposals.py), and nothing
    reaches the journal until the CSM accepts one.
    DIRECT: Claude may also add notes and follow-ups, complete follow-ups and snooze accounts
    itself, after a preview it must confirm. It can never delete or edit anything.
    """

    OFF = "off"
    PROPOSE = "propose"
    DIRECT = "direct"


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
    # Or a model on this computer (AIProvider.LOCAL): its server's address and model name.
    ai_provider: AIProvider = AIProvider.OPENROUTER
    local_ai_url: str = DEFAULT_LOCAL_AI_URL
    local_ai_model: str = ""
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

    # --- Assistant (Settings > Assistant) -------------------------------------------------------
    # Suggestions on Today (core/proposals.py): on by default, because a suggestion changes
    # nothing until the CSM accepts it.
    suggestions: bool = True
    # Suggestions learn from what the CSM accepts and dismisses (core/taste.py): a kind they keep
    # dismissing is shown less. Shown and reversible in Settings > Assistant.
    suggestions_learn: bool = True
    # What Claude may change through the MCP server (McpWrites). Off until the CSM chooses.
    mcp_writes: McpWrites = McpWrites.OFF
    # Email drafts (core/drafts.py) are always written from templates. With ai_drafts on, the
    # configured AI may polish the wording: a model on this computer straight away, a model online
    # (OpenRouter) only when ai_drafts_cloud is on as well. Wardmind never sends an email.
    ai_drafts: bool = False
    ai_drafts_cloud: bool = False

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

    @field_validator("local_ai_url")
    @classmethod
    def _local_url(cls, value: str) -> str:
        return check_local_url(value)

    @field_validator("local_ai_model")
    @classmethod
    def _local_model(cls, value: str) -> str:
        return check_local_model(value)

    @field_validator("source", mode="before")
    @classmethod
    def _legacy_hubspot_source(cls, value: Any) -> Any:
        """A settings file saved before "Your data" said "hubspot". That choice is now "mine"."""
        return DataSource.MINE if value == "hubspot" else value


def settings_path() -> Path:
    return config_dir() / "settings.yaml"


# libyaml's parser when PyYAML was built with it (about ten times faster), else the pure one.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# The last settings read, by file: (path, its (mtime_ns, size, inode)) -> Settings. A page asks
# for the settings many times; the file is parsed again only when it changed (save_settings
# replaces it, so a save always shows at once). Callers get a copy, so the cached one is never
# changed.
_LOADED: dict[str, tuple[tuple[int, int, int], Settings]] = {}


def load_settings(path: Path | None = None) -> Settings:
    """Read settings.yaml. A missing file gives defaults; an unknown or invalid value falls
    back to that field's default rather than failing, so a hand-edited file can't break the UI.
    """
    path = path or settings_path()
    try:
        info = path.stat()
    except OSError:
        return Settings()
    stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
    cached = _LOADED.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1].model_copy()
    settings = _read_settings(path)
    _LOADED.clear()  # one file at a time: the current config folder's
    _LOADED[str(path)] = (stamp, settings)
    return settings.model_copy()


def _read_settings(path: Path) -> Settings:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER)  # noqa: S506
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
