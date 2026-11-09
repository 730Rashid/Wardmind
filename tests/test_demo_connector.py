from __future__ import annotations

import hashlib
import importlib.util
import json
import random
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from wardmind.core.config import DEFAULT_SIGNAL_RULES, MotionWindows
from wardmind.core.connectors.base import Connector
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Account, Health, Motion, Signal, SignalKind

REPO = Path(__file__).resolve().parents[1]
KINDS = {kind.value for kind in SignalKind}
# SHA-256 of the committed accounts.json. The realism pass must never change it.
ACCOUNTS_SHA256 = "868acebcbed84ca051e86d140c8be6adb0ea8f9330a02bc9f6fb3c33d1b60efd"


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


def test_every_demo_account_has_its_own_invented_website():
    accounts, _ = gen.generate(ANCHOR)
    domains = [a["domain"] for a in accounts["accounts"]]
    assert len(set(domains)) == 40
    assert all(domain.endswith(".example") for domain in domains)
    assert domains[0] == "hesslerfitness.example"  # the name, lower case, with no spaces


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


def _readings_by_key(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[Signal]]:
    grouped: dict[tuple[str, str], list[Signal]] = defaultdict(list)
    for row in rows:
        signal = Signal.model_validate(row)
        grouped[(signal.account_id, signal.kind.value)].append(signal)
    return grouped


def test_history_readings_are_older_than_the_latest_and_never_share_a_timestamp():
    _, signals = gen.generate(ANCHOR)
    stamps = [(row["account_id"], row["kind"], row["observed_at"]) for row in signals["signals"]]
    assert len(set(stamps)) == len(stamps)
    for readings in _readings_by_key(signals["signals"]).values():
        newest = max(readings, key=lambda s: s.observed_at)
        for reading in readings:
            if reading is not newest:
                assert reading.observed_at < newest.observed_at


def test_every_reading_is_demo_sourced_and_within_84_days_before_the_anchor():
    _, signals = gen.generate(ANCHOR)
    anchor_dt = datetime(ANCHOR.year, ANCHOR.month, ANCHOR.day)
    for row in signals["signals"]:
        observed = datetime.fromisoformat(row["observed_at"])
        assert anchor_dt - timedelta(days=84) <= observed < anchor_dt
        assert row["source"] == "demo"


def test_every_account_has_at_least_five_usage_trend_readings():
    accounts, signals = gen.generate(ANCHOR)
    usage = Counter(
        row["account_id"]
        for row in signals["signals"]
        if row["kind"] == SignalKind.USAGE_TREND_30D.value
    )
    assert all(usage[a["id"]] >= 5 for a in accounts["accounts"])


def test_every_champion_who_has_left_was_still_in_place_earlier():
    accounts, signals = gen.generate(ANCHOR)
    grouped = _readings_by_key(signals["signals"])
    for account in accounts["accounts"]:
        readings = grouped[(account["id"], SignalKind.CHAMPION_LEFT.value)]
        newest = max(readings, key=lambda s: s.observed_at)
        if newest.value == 1.0:
            assert any(s.value == 0.0 and s.observed_at < newest.observed_at for s in readings)


def test_champion_rule_adds_an_earlier_zero_when_the_champion_has_left():
    # Flip one account's champion reading to "left", so the history rule runs on any account.
    accounts, signals = gen.generate(ANCHOR)
    target = "acc-001"
    current = [
        reading.model_copy(update={"value": 1.0})
        if (reading.account_id, reading.kind) == (target, SignalKind.CHAMPION_LEFT)
        else reading
        for reading in gen.current_readings(ANCHOR)
    ]
    existing = [Signal.model_validate(row) for row in signals["signals"]]
    accounts_models = [Account.model_validate(a) for a in accounts["accounts"]]
    history = gen._history(accounts_models, current, existing, ANCHOR, random.Random(0))

    newest = next(
        r for r in current if (r.account_id, r.kind) == (target, SignalKind.CHAMPION_LEFT)
    )
    champion = [s for s in history if (s.account_id, s.kind) == (target, SignalKind.CHAMPION_LEFT)]
    assert [(s.value, newest.observed_at - s.observed_at) for s in champion] == [
        (0.0, timedelta(days=35))
    ]


def test_latest_reading_per_account_and_kind_is_the_current_reading():
    current = gen.current_readings(ANCHOR)
    _, signals = gen.generate(ANCHOR)
    latest = _latest_by_kind(signals["signals"])
    assert len(current) == 40 * len(KINDS)
    assert len(latest) == len(current)
    for reading in current:
        assert Signal.model_validate(latest[(reading.account_id, reading.kind.value)]) == reading


# Realism pass: the call-outs the weekly brief shows should look like a real book.


def _current_values_by_account(signals: dict[str, Any]) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = defaultdict(dict)
    for (account_id, kind), row in _latest_by_kind(signals["signals"]).items():
        values[account_id][kind] = row["value"]
    return values


def test_accounts_json_is_unchanged():
    # The realism pass edits signal values only. Names, ids, ARR and dates must stay as they are.
    digest = hashlib.sha256((gen.DATA_DIR / "accounts.json").read_bytes()).hexdigest()
    assert digest == ACCOUNTS_SHA256


def test_five_to_seven_accounts_have_an_open_severity_one_ticket():
    _, signals = gen.generate(ANCHOR)
    values = _current_values_by_account(signals)
    with_tickets = [a for a, v in values.items() if v[SignalKind.OPEN_TICKETS_SEV1.value] >= 1]
    assert 5 <= len(with_tickets) <= 7


def test_no_healthy_account_has_an_open_severity_one_ticket():
    accounts, signals = gen.generate(ANCHOR)
    values = _current_values_by_account(signals)
    for account in accounts["accounts"]:
        current = values[account["id"]]
        if _band(_risk(current)) == Health.HEALTHY:
            assert current[SignalKind.OPEN_TICKETS_SEV1.value] == 0


def test_every_risk_sits_inside_its_health_band_window():
    accounts, signals = gen.generate(ANCHOR)
    values = _current_values_by_account(signals)
    for account in accounts["accounts"]:
        current = values[account["id"]]
        low, high = gen.RISK_WINDOWS[Health(_band(_risk(current)))]
        assert low <= _risk(current) <= high


def test_exactly_three_champions_have_left_two_at_risk_in_renewal_and_one_watch_in_churn():
    accounts, signals = gen.generate(ANCHOR)
    values = _current_values_by_account(signals)
    windows = MotionWindows()
    split: Counter[tuple[str, Motion]] = Counter()
    for account in accounts["accounts"]:
        current = values[account["id"]]
        if current[SignalKind.CHAMPION_LEFT.value] == 1.0:
            split[(_band(_risk(current)), _motion(account, ANCHOR, windows))] += 1
    assert split == {("at_risk", Motion.RENEWAL): 2, ("watch", Motion.CHURN): 1}


def test_each_champion_who_has_left_had_an_earlier_zero_reading():
    _, signals = gen.generate(ANCHOR)
    grouped = _readings_by_key(signals["signals"])
    left = [
        readings
        for (_, kind), readings in grouped.items()
        if kind == SignalKind.CHAMPION_LEFT.value
        and max(readings, key=lambda s: s.observed_at).value == 1.0
    ]
    assert len(left) == 3
    for readings in left:
        newest = max(readings, key=lambda s: s.observed_at)
        assert any(s.value == 0.0 and s.observed_at < newest.observed_at for s in readings)
