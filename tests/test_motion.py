from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

from cs_brief.core.config import MotionWindows
from cs_brief.core.models import Account, Milestones, Motion
from cs_brief.core.scoring import classify_motion

REPO = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 19)
WINDOWS = MotionWindows()  # onboarding 90 days, renewal window 120 days
LONG_AGO = AS_OF - timedelta(days=1000)
FAR_RENEWAL = AS_OF + timedelta(days=400)


def _load_generator():
    # scripts/ is not a package, so import the generator by its file path.
    spec = importlib.util.spec_from_file_location("gen_demo", REPO / "scripts" / "gen_demo.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def _account(start: date, renewal: date, done: int = 2, total: int = 4) -> Account:
    return Account(
        id="acc-x",
        name="Test Co",
        arr=50_000,
        tier="Growth",
        owner="Owner",
        start_date=start,
        renewal_date=renewal,
        milestones=Milestones(done=done, total=total),
    )


def test_new_account_renewing_soon_is_onboarding():
    account = _account(start=AS_OF - timedelta(days=10), renewal=AS_OF + timedelta(days=30))
    assert classify_motion(account, AS_OF, WINDOWS) is Motion.ONBOARDING


def test_onboarding_with_complete_milestones_falls_through_to_renewal():
    account = _account(
        start=AS_OF - timedelta(days=10),
        renewal=AS_OF + timedelta(days=30),
        done=4,
        total=4,
    )
    assert classify_motion(account, AS_OF, WINDOWS) is Motion.RENEWAL


def test_onboarding_with_complete_milestones_and_far_renewal_is_churn():
    account = _account(start=AS_OF - timedelta(days=10), renewal=FAR_RENEWAL, done=4, total=4)
    assert classify_motion(account, AS_OF, WINDOWS) is Motion.CHURN


def test_new_account_with_open_milestones_is_onboarding_even_if_renewal_is_far():
    account = _account(start=AS_OF - timedelta(days=10), renewal=FAR_RENEWAL)
    assert classify_motion(account, AS_OF, WINDOWS) is Motion.ONBOARDING


@pytest.mark.parametrize(
    ("days_since_start", "expected"),
    [
        (WINDOWS.onboarding_days - 1, Motion.ONBOARDING),
        (WINDOWS.onboarding_days, Motion.CHURN),
    ],
    ids=["89-days-is-onboarding", "90-days-is-not"],
)
def test_onboarding_boundary_at_onboarding_days(days_since_start, expected):
    # Renewal is far away, so only the onboarding rule can decide the motion.
    account = _account(start=AS_OF - timedelta(days=days_since_start), renewal=FAR_RENEWAL)
    assert classify_motion(account, AS_OF, WINDOWS) is expected


@pytest.mark.parametrize(
    ("days_to_renewal", "expected"),
    [
        (0, Motion.RENEWAL),
        (1, Motion.RENEWAL),
        (WINDOWS.renewal_window_days, Motion.RENEWAL),
        (WINDOWS.renewal_window_days + 1, Motion.CHURN),
        (-1, Motion.CHURN),
    ],
    ids=["today", "tomorrow", "window-edge", "window-plus-one", "lapsed-yesterday"],
)
def test_renewal_window_boundaries(days_to_renewal, expected):
    # Start is long ago and milestones are complete, so onboarding cannot match.
    account = _account(
        start=LONG_AGO,
        renewal=AS_OF + timedelta(days=days_to_renewal),
        done=4,
        total=4,
    )
    assert classify_motion(account, AS_OF, WINDOWS) is expected


def test_custom_windows_move_the_boundaries():
    windows = MotionWindows(onboarding_days=30, renewal_window_days=60)
    account = _account(start=AS_OF - timedelta(days=30), renewal=AS_OF + timedelta(days=60))
    assert classify_motion(account, AS_OF, windows) is Motion.RENEWAL


@pytest.mark.parametrize("start_offset", [-5, 0, 89, 90, 91, 400])
@pytest.mark.parametrize("renewal_offset", [-2, -1, 0, 1, 119, 120, 121, 400])
@pytest.mark.parametrize("complete", [False, True])
def test_agrees_with_the_demo_generator(start_offset, renewal_offset, complete):
    account = _account(
        start=AS_OF - timedelta(days=start_offset),
        renewal=AS_OF + timedelta(days=renewal_offset),
        done=4 if complete else 2,
        total=4,
    )
    assert classify_motion(account, AS_OF, WINDOWS) is gen._motion(account, AS_OF)
