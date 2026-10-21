"""Settings > Scoring: the form's field names, how a posted form becomes a config, and the messages.

The form and ScoringConfig line up field by field. A field the form doesn't send keeps its value
from the base config. `arr.floor` and `arr.ceiling` are not on the form, so they always come from
the base. Pydantic's errors are translated to plain messages, each on the field that caused it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from cs_brief.core.config import ScoringConfig
from cs_brief.core.models import SignalKind

FORM_ERROR = "form"  # the key for a problem no single field owns

NOT_A_NUMBER = "Enter a number."
WHOLE_DAYS = "Enter a whole number of days."
RANGE_0_TO_100 = "Use a value from 0 to 100."
DAYS_MIN = "Use 0 or more days."
SAME_START_AND_FULL = "The maximum can't be the same as the start."
WATCH_BELOW_AT_RISK = "Watch has to be below At risk."

# The unit under each signal's start and full fields, from the SignalKind comments.
UNIT_HINTS: dict[SignalKind, str] = {
    SignalKind.USAGE_TREND_30D: "% change in active users over 30 days",
    SignalKind.DAYS_SINCE_LAST_LOGIN: "days since anyone logged in",
    SignalKind.OPEN_TICKETS_SEV1: "open severity-1 tickets",
    SignalKind.NPS_LATEST: "latest NPS response, 0 to 10",
    SignalKind.CHAMPION_LEFT: "1 means the champion has left",
    SignalKind.SEAT_UTILISATION: "% of purchased seats in use",
    SignalKind.BILLING_OVERDUE_DAYS: "days the oldest invoice is overdue",
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: "late onboarding milestones",
}


def form_values(config: ScoringConfig) -> dict[str, str]:
    """The text each form field shows for `config`. Whole numbers have no trailing '.0'."""
    values: dict[str, str] = {}
    for kind in SignalKind:
        rule = config.signals[kind]
        values[f"signals.{kind.value}.weight"] = _text(rule.weight)
        values[f"signals.{kind.value}.start"] = _text(rule.start)
        values[f"signals.{kind.value}.full"] = _text(rule.full)
    values["thresholds.at_risk"] = _text(config.thresholds.at_risk)
    values["thresholds.watch"] = _text(config.thresholds.watch)
    values["arr.weight_pct"] = _percent_text(config.arr.weight)
    values["motion.onboarding_days"] = _text(config.motion.onboarding_days)
    values["motion.renewal_window_days"] = _text(config.motion.renewal_window_days)
    return values


def parse_scoring_form(
    form: Mapping[str, str], base: ScoringConfig
) -> tuple[ScoringConfig | None, dict[str, str]]:
    """The config the form describes, or None with a message for each bad field.

    Numbers that don't parse are reported first, and the whole-config check is skipped while
    any are wrong, so each message is about what was typed. Messages are keyed by field name.
    """
    errors: dict[str, str] = {}

    def number(name: str, fallback: float) -> float:
        raw = form.get(name)
        if raw is None:
            return fallback
        value = _parse_float(raw)
        if value is None:
            errors.setdefault(name, NOT_A_NUMBER)
            return fallback
        return value

    def whole_days(name: str, fallback: int) -> int:
        value = number(name, float(fallback))
        if name not in errors and not value.is_integer():
            errors.setdefault(name, WHOLE_DAYS)
        return int(value)

    def arr_weight() -> float:
        raw = form.get("arr.weight_pct")
        if raw is None:
            return base.arr.weight
        percent = _parse_float(raw)
        if percent is None:
            errors.setdefault("arr.weight_pct", NOT_A_NUMBER)
            return base.arr.weight
        return _weight_from_percent(percent)

    signals = {
        kind.value: {
            "weight": number(f"signals.{kind.value}.weight", base.signals[kind].weight),
            "start": number(f"signals.{kind.value}.start", base.signals[kind].start),
            "full": number(f"signals.{kind.value}.full", base.signals[kind].full),
        }
        for kind in SignalKind
    }
    data: dict[str, Any] = {
        "signals": signals,
        "thresholds": {
            "at_risk": number("thresholds.at_risk", base.thresholds.at_risk),
            "watch": number("thresholds.watch", base.thresholds.watch),
        },
        "motion": {
            "onboarding_days": whole_days("motion.onboarding_days", base.motion.onboarding_days),
            "renewal_window_days": whole_days(
                "motion.renewal_window_days", base.motion.renewal_window_days
            ),
        },
        "arr": {"weight": arr_weight(), "floor": base.arr.floor, "ceiling": base.arr.ceiling},
    }
    if errors:
        return None, errors
    try:
        return ScoringConfig.model_validate(data), {}
    except ValidationError as error:
        for item in error.errors():
            name, message = _locate(item)
            errors.setdefault(name, message)
        return None, errors


def scoring_output(raw: str, suffix: str, singular: str = "") -> str:
    """What a slider's <output> shows: its value and unit, with the singular unit for exactly 1."""
    value = _parse_float(raw)
    if value is None:
        return raw + suffix
    return _text(value) + (singular if value == 1 and singular else suffix)


def slider_max(raw: str, default: int) -> int:
    """The top of a slider. A saved value above the usual top raises it, so saving the form can't
    silently cut a value the slider couldn't show."""
    value = _parse_float(raw)
    if value is None or value <= default:
        return default
    return math.ceil(value)


def _locate(error: Mapping[str, Any]) -> tuple[str, str]:
    """The form field a pydantic error belongs to, with its plain message."""
    loc = tuple(str(part) for part in error["loc"])
    raw_message = str(error["msg"]).removeprefix("Value error, ")
    if loc[:1] == ("signals",) and len(loc) == 2 and "start and full" in raw_message:
        return f"signals.{loc[1]}.full", SAME_START_AND_FULL
    if loc[:1] == ("signals",) and len(loc) == 3:
        _, kind, part = loc
        if part == "weight":
            return f"signals.{kind}.weight", RANGE_0_TO_100
        return f"signals.{kind}.{part}", NOT_A_NUMBER
    if loc == ("thresholds",):
        return "thresholds.watch", WATCH_BELOW_AT_RISK
    if loc in (("thresholds", "at_risk"), ("thresholds", "watch")):
        return f"thresholds.{loc[1]}", RANGE_0_TO_100
    if len(loc) == 2 and loc[0] == "motion":
        return f"motion.{loc[1]}", DAYS_MIN
    if loc == ("arr", "weight"):
        return "arr.weight_pct", RANGE_0_TO_100
    return FORM_ERROR, raw_message


def _parse_float(raw: str) -> float | None:
    """A finite number from the text, or None."""
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _text(value: float) -> str:
    """'25' for a whole number, otherwise the shortest text that reads back as the same float."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


def _weight_from_percent(percent: float) -> float:
    """The stored weight for a percent. Rounded to 12 places, so 12.3% is saved as 0.123 rather
    than 0.12300000000000001 (floating-point division leaves that tail)."""
    return round(percent / 100, 12)


def _percent_text(weight: float) -> str:
    """The ARR weight as a percent: the shortest text that parses back to the same weight."""
    for places in range(13):
        candidate = round(weight * 100, places)
        if _weight_from_percent(candidate) == weight:
            return _text(candidate)
    return _text(weight * 100)


# Every field name the form posts, in display order. Built after the helpers above it.
FIELD_NAMES: tuple[str, ...] = tuple(form_values(ScoringConfig()))
