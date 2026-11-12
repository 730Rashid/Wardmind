"""Scoring configuration: schema, defaults, and loading from config/scoring.yaml.

CONTRACT FILE: the ScoringConfig schema is what config/scoring.yaml and the Settings
sliders write to. Implementers escalate before changing it.

Normalisation model: every signal has `start` (the value where risk begins, risk 0) and
`full` (the value where risk is maxed, risk 1). Risk is linear between them and clamped
to 0..1, which works in either direction (e.g. usage start=0, full=-50; NPS start=7, full=0).
A signal's contribution in risk points is `weight * normalised_risk`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from wardmind.core.models import CUSTOM_ID, CustomKind, SignalKey, SignalKind


class SignalRule(BaseModel):
    weight: float = Field(ge=0, le=100)  # risk points when the signal is fully triggered
    start: float
    full: float

    @model_validator(mode="after")
    def _distinct(self) -> SignalRule:
        if self.start == self.full:
            raise ValueError("start and full must differ")
        return self


class Thresholds(BaseModel):
    at_risk: float = Field(default=45, ge=0, le=100)  # risk >= this -> at_risk
    watch: float = Field(default=20, ge=0, le=100)  # risk >= this -> watch, else healthy

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if self.watch >= self.at_risk:
            raise ValueError("thresholds.watch must be below thresholds.at_risk")
        return self


class MotionWindows(BaseModel):
    onboarding_days: int = Field(default=90, ge=0)  # onboarding if started within this many days
    renewal_window_days: int = Field(default=120, ge=0)  # renewal if renewing within this


class ArrScaling(BaseModel):
    """priority = risk * ((1 - weight) + weight * arr_norm), arr_norm log-scaled 0..1."""

    weight: float = Field(default=0.3, ge=0, le=1)
    floor: float = Field(default=5_000, gt=0)  # ARR at or below this -> arr_norm 0
    ceiling: float = Field(default=500_000, gt=0)  # ARR at or above this -> arr_norm 1

    @model_validator(mode="after")
    def _ordered(self) -> ArrScaling:
        if self.floor >= self.ceiling:
            raise ValueError("arr.floor must be below arr.ceiling")
        return self


DEFAULT_SIGNAL_RULES: dict[SignalKind, SignalRule] = {
    SignalKind.USAGE_TREND_30D: SignalRule(weight=25, start=0, full=-50),
    SignalKind.DAYS_SINCE_LAST_LOGIN: SignalRule(weight=15, start=7, full=30),
    SignalKind.OPEN_TICKETS_SEV1: SignalRule(weight=20, start=0, full=3),
    SignalKind.NPS_LATEST: SignalRule(weight=15, start=7, full=0),
    SignalKind.CHAMPION_LEFT: SignalRule(weight=25, start=0, full=1),
    SignalKind.SEAT_UTILISATION: SignalRule(weight=10, start=70, full=30),
    SignalKind.BILLING_OVERDUE_DAYS: SignalRule(weight=20, start=0, full=60),
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: SignalRule(weight=20, start=0, full=3),
}


MAX_CUSTOM_SIGNALS = 12
MAX_CUSTOM_NAME = 40
MAX_CUSTOM_UNIT = 12
MAX_CUSTOM_TEXT = 160
DEFAULT_CUSTOM_STALE_DAYS = 30


class CustomSignal(BaseModel):
    """A signal the CSM defines for their book: any number a connection, a spreadsheet or a
    hand-typed reading can supply (API calls a week, reports exported, admins active).

    It scores like a built-in signal: `rule` turns the number into risk points (start = where
    risk begins, full = where it is maxed; either direction). The other fields are what the
    pages say about it."""

    id: str  # stable, lower case: "api_calls"; readings are stored under "custom:<id>"
    name: str = Field(min_length=1, max_length=MAX_CUSTOM_NAME)  # "API calls per week"
    unit: str = Field(default="", max_length=MAX_CUSTOM_UNIT)  # shown after the number: "%"
    rule: SignalRule
    # What to ask on a call when this is the account's biggest risk, and the next step. Empty
    # means a plain sentence made from the name.
    question: str = Field(default="", max_length=MAX_CUSTOM_TEXT)
    action: str = Field(default="", max_length=MAX_CUSTOM_TEXT)
    # A reading older than this is flagged as out of date on the account page.
    stale_days: int = Field(default=DEFAULT_CUSTOM_STALE_DAYS, ge=1, le=365)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not CUSTOM_ID.fullmatch(value):
            raise ValueError("a custom signal id is lower-case letters, digits and underscores")
        return value

    @field_validator("name", "unit", "question", "action")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return " ".join(value.split())

    @property
    def kind(self) -> CustomKind:
        return CustomKind.of(self.id)


class ScoringConfig(BaseModel):
    signals: dict[SignalKind, SignalRule] = Field(
        default_factory=lambda: dict(DEFAULT_SIGNAL_RULES)
    )
    thresholds: Thresholds = Field(default_factory=Thresholds)
    motion: MotionWindows = Field(default_factory=MotionWindows)
    arr: ArrScaling = Field(default_factory=ArrScaling)
    # The book's own signals, in the order the CSM added them.
    custom: list[CustomSignal] = Field(default_factory=list, max_length=MAX_CUSTOM_SIGNALS)

    @model_validator(mode="after")
    def _fill_missing_signals(self) -> ScoringConfig:
        for kind, rule in DEFAULT_SIGNAL_RULES.items():
            self.signals.setdefault(kind, rule)
        return self

    @model_validator(mode="after")
    def _unique_custom(self) -> ScoringConfig:
        ids = [signal.id for signal in self.custom]
        if len(ids) != len(set(ids)):
            raise ValueError("two custom signals have the same id")
        names = [signal.name.casefold() for signal in self.custom]
        if len(names) != len(set(names)):
            raise ValueError("two custom signals have the same name")
        return self

    def kinds(self) -> list[SignalKey]:
        """Every signal this book scores: the eight built in (in SignalKind order), then its
        own in the order they were added."""
        return [*SignalKind, *(signal.kind for signal in self.custom)]

    def custom_signal(self, kind: SignalKey | str) -> CustomSignal | None:
        """The definition of a custom kind, or None (a built-in kind, or one since removed)."""
        if isinstance(kind, SignalKind):
            return None
        for signal in self.custom:
            if signal.kind == kind:
                return signal
        return None

    def rule_for(self, kind: SignalKey | str) -> SignalRule | None:
        """The scoring rule for any kind; None for a custom kind this book no longer has."""
        if isinstance(kind, SignalKind):
            return self.signals.get(kind)
        custom = self.custom_signal(kind)
        return None if custom is None else custom.rule


def config_dir() -> Path:
    """Directory holding scoring.yaml / settings.yaml. Override with WARDMIND_CONFIG_DIR.

    Run from source, it is `config/` in the folder Wardmind starts from. The desktop app
    (Wardmind.exe, a frozen build) has no such folder of its own, so it keeps them in the
    per-user folder beside the data: %LOCALAPPDATA%\\wardmind\\config on Windows."""
    override = os.environ.get("WARDMIND_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        from wardmind.core.secrets import root_dir

        return root_dir() / "config"
    return Path.cwd() / "config"


def scoring_path(*, saving: bool = False) -> Path:
    """The open book's scoring.yaml (core/books.py). The first book uses config_dir()'s, as
    before books. Another book has its own file in its folder once its scoring is saved; until
    then it reads the first book's."""
    from wardmind.core import books

    shared = config_dir() / "scoring.yaml"
    book_id = books.active_id()
    if book_id == books.DEFAULT_BOOK:
        return shared
    own = books.book_folder(book_id) / "scoring.yaml"
    return own if saving or own.exists() else shared


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    """Load and validate scoring.yaml; fall back to built-in defaults if it doesn't exist.
    Without a path: the open book's scoring (scoring_path)."""
    path = path or scoring_path()
    if not path.exists():
        return ScoringConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ScoringConfig.model_validate(data)


_SCORING_HEADER = """\
# Wardmind scoring configuration, written by the Settings page.
# Each signal adds up to `weight` risk points. Risk starts at `start` and is maxed at `full`
# (linear in between). Total risk is capped at 100. See src/wardmind/core/config.py.
"""


def save_scoring_config(config: ScoringConfig, path: Path | None = None) -> None:
    """Write scoring.yaml atomically: a temp file in the same folder, then a replace. Without a
    path: the open book's own file (scoring_path(saving=True))."""
    path = path or scoring_path(saving=True)
    data = config.model_dump(mode="json")
    data["signals"] = {kind.value: data["signals"][kind.value] for kind in SignalKind}
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=None)
    write_atomic(path, _SCORING_HEADER + "\n" + body)


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` so a crash mid-write never leaves a half-written file.

    The temp file has a unique name (two processes, such as the web app and the MCP server,
    never share one) and is removed if the write fails. On POSIX the file is readable only by
    its owner (0600) and new folders are 0700: the data files hold a CSM's book and contacts.
    On Windows the per-user profile folder's ACL applies."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
