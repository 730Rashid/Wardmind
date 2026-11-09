from pathlib import Path

import pytest
from pydantic import ValidationError

from wardmind.core.config import (
    DEFAULT_SIGNAL_RULES,
    ScoringConfig,
    SignalRule,
    Thresholds,
    load_scoring_config,
)
from wardmind.core.models import Milestones, SignalKind

REPO = Path(__file__).resolve().parents[1]


def test_repo_scoring_yaml_matches_defaults():
    assert load_scoring_config(REPO / "config" / "scoring.yaml") == ScoringConfig()


def test_missing_config_falls_back_to_defaults(tmp_path):
    assert load_scoring_config(tmp_path / "nope.yaml") == ScoringConfig()


def test_partial_signals_are_filled_from_defaults():
    nps = {"weight": 5, "start": 7, "full": 0}
    cfg = ScoringConfig.model_validate({"signals": {"nps_latest": nps}})
    assert cfg.signals[SignalKind.NPS_LATEST].weight == 5
    assert set(cfg.signals) == set(DEFAULT_SIGNAL_RULES)


@pytest.mark.parametrize(
    "build",
    [
        lambda: SignalRule(weight=10, start=5, full=5),
        lambda: Thresholds(at_risk=20, watch=30),
        lambda: Milestones(done=4, total=3),
        lambda: Milestones(done=2, total=3, overdue=2),
    ],
)
def test_invalid_contracts_are_rejected(build):
    with pytest.raises(ValidationError):
        build()
