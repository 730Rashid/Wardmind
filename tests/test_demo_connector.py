from __future__ import annotations

import importlib.util
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from cs_brief.core.config import DEFAULT_SIGNAL_RULES, MotionWindows
from cs_brief.core.connectors.base import Connector
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.models import Account, Motion, Signal, SignalKind

REPO = Path(__file__).resolve().parents[1]
KINDS = {kind.value for kind in SignalKind}


def _load_generator() -> ModuleType:
    # scripts/ is not a package, so import the generator by its file path.
    spec = importlib.util.spec_from_file_location("gen_demo", REPO / "scripts" / "gen_demo.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()
ANCHOR: date = gen.ANCHOR


def _latest_by_kind(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: datetime.fromisoformat(r["observed_at"])):
        latest[(row["account_id"], row["kind"])] = row
    return latest


def _risk(values: dict[str, float]) -> float:
    """Risk recomputed the way the scorer does it, from the default rules."""
    total = 0.0
    for kind, rule in DEFAULT_SIGNAL_RULES.items():
        norm = (values[kind.value] - rule.start) / (rule.full - rule.start)
        total += rule.weight * min(1.0, max(0.0, norm))
    return min(100.0, total)


def _band(risk: float) -> str | None:
    """Health band per the brief's bands; None if the risk falls in a gap between them."""
    if risk < 15:
        return "healthy"
    if 25 <= risk <= 40:
        return "watch"
    if risk >= 55:
        return "at_risk"
    return None


def _motion(account: dict[str, Any], anchor: date, windows: MotionWindows) -> Motion:
    """The motion rule from the brief, applied directly: first match wins."""
    start = date.fromisoformat(account["start_date"])
    renewal = date.fromisoformat(account["renewal_date"])
    milestones = account["milestones"]
    if (anchor - start).days < windows.onboarding_days and milestones["done"] < milestones["total"]:
        return Motion.ONBOARDING
    if 0 <= (renewal - anchor).days <= windows.renewal_window_days:
        return Motion.RENEWAL
    return Motion.CHURN


def test_generate_is_deterministic():
    assert gen.generate(ANCHOR) == gen.generate(ANCHOR)


def test_committed_json_matches_generator():
    accounts, signals = gen.generate(ANCHOR)
    committed_accounts = json.loads((gen.DATA_DIR / "accounts.json").read_text(encoding="utf-8"))
    committed_signals = json.loads((gen.DATA_DIR / "signals.json").read_text(encoding="utf-8"))
    assert committed_accounts == accounts
    assert committed_signals == signals


def test_forty_accounts_with_unique_ids():
    accounts, _ = gen.generate(ANCHOR)
    ids = [a["id"] for a in accounts["accounts"]]
    assert ids == [f"acc-{n:03d}" for n in range(1, 41)]
    assert len(set(ids)) == 40


def test_every_account_has_all_eight_signal_kinds():
    accounts, signals = gen.generate(ANCHOR)
    kinds: dict[str, set[str]] = defaultdict(set)
    for row in signals["signals"]:
        kinds[row["account_id"]].add(row["kind"])
    assert set(kinds) == {a["id"] for a in accounts["accounts"]}
    assert all(found == KINDS for found in kinds.values())


def test_latest_signals_are_recent_and_onboarding_overdue_matches_milestones():
    accounts, signals = gen.generate(ANCHOR)
    latest = _latest_by_kind(signals["signals"])
    anchor_dt = datetime(ANCHOR.year, ANCHOR.month, ANCHOR.day)
    for row in latest.values():
        observed = datetime.fromisoformat(row["observed_at"])
        assert anchor_dt - timedelta(days=3) <= observed < anchor_dt
    for account in accounts["accounts"]:
        key = (account["id"], SignalKind.ONBOARDING_OVERDUE_MILESTONES.value)
        assert latest[key]["value"] == account["milestones"]["overdue"]
    # Some accounts carry an older reading too, so latest-wins is exercised.
    per_account = Counter(row["account_id"] for row in signals["signals"])
    assert any(n > len(KINDS) for n in per_account.values())


def test_all_three_motions_present_at_anchor():
    accounts, _ = gen.generate(ANCHOR)
    windows = MotionWindows()
    motions = {_motion(a, ANCHOR, windows) for a in accounts["accounts"]}
    assert motions == set(Motion)


def test_healthy_watch_and_at_risk_bands_all_present():
    accounts, signals = gen.generate(ANCHOR)
    latest = _latest_by_kind(signals["signals"])
    bands = []
    for account in accounts["accounts"]:
        values = {kind: latest[(account["id"], kind)]["value"] for kind in KINDS}
        bands.append(_band(_risk(values)))
    assert None not in bands, "an account's risk landed between two bands"
    assert set(bands) == {"healthy", "watch", "at_risk"}


def test_dates_stay_put_at_anchor_and_move_by_the_gap_otherwise():
    raw_accounts, raw_signals = gen.generate(ANCHOR)
    raw_accounts_by_id = {a["id"]: Account.model_validate(a) for a in raw_accounts["accounts"]}
    raw_signals_by_key = {
        (s.account_id, s.kind, s.observed_at): s
        for s in (Signal.model_validate(row) for row in raw_signals["signals"])
    }
    for gap_days in (0, 10):
        shift = timedelta(days=gap_days)
        connector = DemoConnector(today=ANCHOR + shift)
        for account in connector.list_accounts():
            original = raw_accounts_by_id[account.id]
            expected = original.model_copy(
                update={
                    "start_date": original.start_date + shift,
                    "renewal_date": original.renewal_date + shift,
                }
            )
            assert account == expected
        signals = connector.list_signals()
        assert len(signals) == len(raw_signals_by_key)
        for signal in signals:
            key = (signal.account_id, signal.kind, signal.observed_at - shift)
            original = raw_signals_by_key[key]
            shifted = original.observed_at + shift
            assert signal == original.model_copy(update={"observed_at": shifted})


def test_list_signals_returns_only_that_accounts_signals():
    _, raw_signals = gen.generate(ANCHOR)
    expected = [row for row in raw_signals["signals"] if row["account_id"] == "acc-001"]
    got = DemoConnector(today=ANCHOR).list_signals("acc-001")
    assert got
    assert len(got) == len(expected)
    assert {s.account_id for s in got} == {"acc-001"}


def test_demo_connector_satisfies_connector_protocol():
    connector = DemoConnector()
    assert isinstance(connector, Connector)
    assert connector.name == "demo"
