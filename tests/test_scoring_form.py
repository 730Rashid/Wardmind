"""Settings > Scoring form: field names, posted forms becoming configs, and where messages land."""

from __future__ import annotations

import pytest

from wardmind.core.config import ArrScaling, ScoringConfig
from wardmind.core.models import SignalKind
from wardmind.web.scoring_form import (
    FIELD_NAMES,
    NOT_A_NUMBER,
    UNIT_HINTS,
    form_values,
    parse_scoring_form,
    scoring_output,
    slider_max,
)

CUSTOM = ScoringConfig.model_validate(
    {
        "signals": {
            "usage_trend_30d": {"weight": 12, "start": 1.5, "full": -40},
            "days_since_last_login": {"weight": 15, "start": 7, "full": 30},
            "open_tickets_sev1": {"weight": 20, "start": 0, "full": 3},
            "nps_latest": {"weight": 15, "start": 7, "full": 0},
            "champion_left": {"weight": 33, "start": 0, "full": 1},
            "seat_utilisation": {"weight": 10, "start": 70, "full": 30},
            "billing_overdue_days": {"weight": 20, "start": 0, "full": 60},
            "onboarding_overdue_milestones": {"weight": 20, "start": 0, "full": 3},
        },
        "thresholds": {"at_risk": 60, "watch": 25},
        "motion": {"onboarding_days": 45, "renewal_window_days": 200},
        "arr": {"weight": 0.123, "floor": 2_000, "ceiling": 900_000},
    }
)


def _form(**overrides: str) -> dict[str, str]:
    """The default form, with some fields changed. Keys use dots, so pass them via a dict."""
    return {**form_values(ScoringConfig()), **overrides}


def test_the_form_has_a_field_for_every_setting_the_form_owns() -> None:
    assert len(FIELD_NAMES) == 3 * len(SignalKind) + 2 + 1 + 2
    assert "arr.weight_pct" in FIELD_NAMES
    assert not any(
        name.startswith("arr.floor") or name.startswith("arr.ceiling") for name in FIELD_NAMES
    )


def test_default_values_read_as_plain_numbers() -> None:
    values = form_values(ScoringConfig())
    assert values["signals.usage_trend_30d.weight"] == "25"
    assert values["signals.usage_trend_30d.start"] == "0"
    assert values["signals.usage_trend_30d.full"] == "-50"
    assert values["signals.seat_utilisation.start"] == "70"
    assert values["thresholds.at_risk"] == "45"
    assert values["thresholds.watch"] == "20"
    assert values["arr.weight_pct"] == "30"
    assert values["motion.onboarding_days"] == "90"
    assert values["motion.renewal_window_days"] == "120"


@pytest.mark.parametrize("config", [ScoringConfig(), CUSTOM], ids=["defaults", "custom"])
def test_round_trip_gives_an_equal_config(config: ScoringConfig) -> None:
    parsed, errors = parse_scoring_form(form_values(config), config)
    assert errors == {}
    assert parsed == config


def test_arr_percent_converts_to_a_weight_and_back() -> None:
    parsed, errors = parse_scoring_form(_form(**{"arr.weight_pct": "45"}), ScoringConfig())
    assert errors == {}
    assert parsed is not None
    assert parsed.arr.weight == pytest.approx(0.45)
    assert form_values(parsed)["arr.weight_pct"] == "45"


@pytest.mark.parametrize(("percent", "weight"), [("0", 0.0), ("100", 1.0)])
def test_arr_percent_bounds_are_accepted(percent: str, weight: float) -> None:
    parsed, errors = parse_scoring_form(_form(**{"arr.weight_pct": percent}), ScoringConfig())
    assert errors == {}
    assert parsed is not None
    assert parsed.arr.weight == weight


def test_arr_floor_and_ceiling_come_from_the_base_config() -> None:
    base = ScoringConfig(arr=ArrScaling(floor=1_000, ceiling=800_000))
    parsed, errors = parse_scoring_form(form_values(ScoringConfig()), base)
    assert errors == {}
    assert parsed is not None
    assert (parsed.arr.floor, parsed.arr.ceiling) == (1_000, 800_000)


def test_a_missing_field_keeps_its_value_from_the_base() -> None:
    base = CUSTOM
    parsed, errors = parse_scoring_form({"thresholds.watch": "30"}, base)
    assert errors == {}
    assert parsed is not None
    assert parsed.thresholds.watch == 30
    assert parsed.thresholds.at_risk == base.thresholds.at_risk
    assert parsed.signals == base.signals
    assert parsed.arr == base.arr
    assert parsed.motion == base.motion


def test_an_empty_form_gives_the_base_config() -> None:
    parsed, errors = parse_scoring_form({}, CUSTOM)
    assert errors == {}
    assert parsed == CUSTOM


@pytest.mark.parametrize("bad", ["seven", "", "nan", "inf", "1,5"])
def test_a_bad_number_says_enter_a_number(bad: str) -> None:
    parsed, errors = parse_scoring_form(_form(**{"signals.nps_latest.start": bad}), ScoringConfig())
    assert parsed is None
    assert errors == {"signals.nps_latest.start": NOT_A_NUMBER}


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        (
            {"signals.usage_trend_30d.weight": "150"},
            "signals.usage_trend_30d.weight",
            "Use a value from 0 to 100.",
        ),
        (
            {"signals.champion_left.weight": "-1"},
            "signals.champion_left.weight",
            "Use a value from 0 to 100.",
        ),
        (
            {"signals.nps_latest.start": "7", "signals.nps_latest.full": "7"},
            "signals.nps_latest.full",
            "The maximum can't be the same as the start.",
        ),
        (
            {"thresholds.at_risk": "30", "thresholds.watch": "30"},
            "thresholds.watch",
            "Watch has to be below At risk.",
        ),
        (
            {"thresholds.at_risk": "30", "thresholds.watch": "50"},
            "thresholds.watch",
            "Watch has to be below At risk.",
        ),
        ({"thresholds.at_risk": "120"}, "thresholds.at_risk", "Use a value from 0 to 100."),
        ({"thresholds.watch": "-5"}, "thresholds.watch", "Use a value from 0 to 100."),
        ({"arr.weight_pct": "101"}, "arr.weight_pct", "Use a value from 0 to 100."),
        ({"arr.weight_pct": "-5"}, "arr.weight_pct", "Use a value from 0 to 100."),
        ({"motion.onboarding_days": "-1"}, "motion.onboarding_days", "Use 0 or more days."),
        (
            {"motion.renewal_window_days": "-30"},
            "motion.renewal_window_days",
            "Use 0 or more days.",
        ),
        (
            {"motion.renewal_window_days": "2.5"},
            "motion.renewal_window_days",
            "Enter a whole number of days.",
        ),
    ],
)
def test_each_error_lands_on_the_field_that_caused_it(
    overrides: dict[str, str], field: str, message: str
) -> None:
    parsed, errors = parse_scoring_form(_form(**overrides), ScoringConfig())
    assert parsed is None
    assert errors[field] == message


def test_a_number_that_does_not_parse_is_reported_alone() -> None:
    # The config is not checked while a number is unreadable, so the messages stay about the typing.
    parsed, errors = parse_scoring_form(
        _form(**{"thresholds.at_risk": "x", "thresholds.watch": "50"}), ScoringConfig()
    )
    assert parsed is None
    assert errors == {"thresholds.at_risk": NOT_A_NUMBER}


def test_every_message_is_keyed_to_a_real_field() -> None:
    _, errors = parse_scoring_form(
        _form(**{"signals.nps_latest.weight": "200", "motion.onboarding_days": "-2"}),
        ScoringConfig(),
    )
    assert set(errors) <= set(FIELD_NAMES)


def test_every_signal_has_a_unit_hint() -> None:
    assert set(UNIT_HINTS) == set(SignalKind)
    assert UNIT_HINTS[SignalKind.CHAMPION_LEFT] == "1 means the champion has left"


@pytest.mark.parametrize(
    ("raw", "suffix", "singular", "expected"),
    [
        ("25", " points", " point", "25 points"),
        ("1", " points", " point", "1 point"),
        ("0", " points", " point", "0 points"),
        ("30", "%", "", "30%"),
        ("45", "", "", "45"),
        ("12.5", " points", " point", "12.5 points"),
        ("not a number", " points", " point", "not a number points"),
    ],
)
def test_slider_output_text(raw: str, suffix: str, singular: str, expected: str) -> None:
    assert scoring_output(raw, suffix, singular) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("25", 50), ("50", 50), ("60", 60), ("60.5", 61), ("oops", 50), ("-4", 50)],
)
def test_slider_top_is_raised_only_to_show_a_saved_value(raw: str, expected: int) -> None:
    assert slider_max(raw, 50) == expected
