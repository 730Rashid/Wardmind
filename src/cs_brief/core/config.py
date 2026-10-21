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
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from cs_brief.core.models import SignalKind


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


class ScoringConfig(BaseModel):
    signals: dict[SignalKind, SignalRule] = Field(
        default_factory=lambda: dict(DEFAULT_SIGNAL_RULES)
    )
    thresholds: Thresholds = Field(default_factory=Thresholds)
    motion: MotionWindows = Field(default_factory=MotionWindows)
    arr: ArrScaling = Field(default_factory=ArrScaling)

    @model_validator(mode="after")
    def _fill_missing_signals(self) -> ScoringConfig:
        for kind, rule in DEFAULT_SIGNAL_RULES.items():
            self.signals.setdefault(kind, rule)
        return self


def config_dir() -> Path:
    """Directory holding scoring.yaml / settings.yaml. Override with CS_BRIEF_CONFIG_DIR."""
    return Path(os.environ.get("CS_BRIEF_CONFIG_DIR", Path.cwd() / "config"))


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    """Load and validate scoring.yaml; fall back to built-in defaults if it doesn't exist."""
    path = path or config_dir() / "scoring.yaml"
    if not path.exists():
        return ScoringConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ScoringConfig.model_validate(data)


_SCORING_HEADER = """\
# cs-brief scoring configuration, written by the Settings page.
# Each signal adds up to `weight` risk points. Risk starts at `start` and is maxed at `full`
# (linear in between). Total risk is capped at 100. See src/cs_brief/core/config.py.
"""


def save_scoring_config(config: ScoringConfig, path: Path | None = None) -> None:
    """Write scoring.yaml atomically: a temp file in the same folder, then a replace."""
    path = path or config_dir() / "scoring.yaml"
    data = config.model_dump(mode="json")
    data["signals"] = {kind.value: data["signals"][kind.value] for kind in SignalKind}
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=None)
    write_atomic(path, _SCORING_HEADER + "\n" + body)


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` so a crash mid-write never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
