"""Generate the deterministic demo book: 40 invented accounts and their signals.

Run from the repo root:  uv run python scripts/gen_demo.py
Writes src/cs_brief/data/demo/accounts.json and signals.json. The tests check that the committed
files still equal generate(), so re-run this script after changing anything here.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from cs_brief.core.config import DEFAULT_SIGNAL_RULES, MotionWindows, SignalRule
from cs_brief.core.models import Account, Health, Milestones, Motion, Signal, SignalKind

SEED = 42
ANCHOR = date(2026, 10, 19)
DATA_DIR = Path(__file__).resolve().parents[1] / "src" / "cs_brief" / "data" / "demo"
WINDOWS = MotionWindows()  # default motion windows: onboarding 90 days, renewal 120 days

# Motion x health targets: 8 onboarding, 12 renewal, 20 churn; 18 healthy, 12 watch, 10 at risk.
MIX: dict[Motion, dict[Health, int]] = {
    Motion.ONBOARDING: {Health.HEALTHY: 3, Health.WATCH: 3, Health.AT_RISK: 2},
    Motion.RENEWAL: {Health.HEALTHY: 5, Health.WATCH: 4, Health.AT_RISK: 3},
    Motion.CHURN: {Health.HEALTHY: 10, Health.WATCH: 5, Health.AT_RISK: 5},
}

# Risk windows the sampler aims for. Each sits inside the required band with some margin:
# healthy < 15, watch 25..40, at risk >= 55.
RISK_WINDOWS: dict[Health, tuple[float, float]] = {
    Health.HEALTHY: (0.0, 10.0),
    Health.WATCH: (28.0, 37.0),
    Health.AT_RISK: (60.0, 90.0),
}

# Signals that may get one older (superseded) reading on about six accounts.
OLDER_KINDS = [
    SignalKind.USAGE_TREND_30D,
    SignalKind.DAYS_SINCE_LAST_LOGIN,
    SignalKind.OPEN_TICKETS_SEV1,
    SignalKind.NPS_LATEST,
    SignalKind.SEAT_UTILISATION,
    SignalKind.BILLING_OVERDUE_DAYS,
]
N_OLDER = 6

PREFIXES = [
    "Larkspur",
    "Quillon",
    "Brindle",
    "Tallowmere",
    "Fennick",
    "Orison",
    "Pellmar",
    "Corvane",
    "Dunmere",
    "Ashgrove",
    "Hessler",
    "Greyholt",
    "Sablewick",
    "Nimbrel",
    "Veldrin",
    "Pondero",
    "Ravelin",
    "Brackmoor",
    "Thistledown",
    "Oakhollow",
    "Ferrane",
    "Wrenfield",
    "Cindervale",
    "Mosswick",
    "Hollin",
    "Tidewell",
    "Stonecairn",
    "Lanternfall",
    "Pendrake",
    "Quarrowdale",
]
SUFFIXES = [
    "Freight",
    "Health",
    "Dental",
    "Analytics",
    "Bakery",
    "Labs",
    "Foods",
    "Works",
    "Print",
    "Clinics",
    "Roofing",
    "Payroll",
    "Legal",
    "Studio",
    "Supply",
    "Learning",
    "Logistics",
    "Energy",
    "Physio",
    "Veterinary",
    "Textiles",
    "Brewing",
    "Pharmacy",
    "Surveying",
    "Renovations",
    "Cargo",
    "Optics",
    "Fitness",
    "Events",
    "Media",
    "Fabrication",
    "Packaging",
    "Dairy",
    "Greenhouse",
]
OWNERS = ["Ilse Marrowby", "Corin Vashti", "Pell Anwick", "Odelia Fenwrick"]
TIERS: dict[str, tuple[int, int]] = {
    "Starter": (6_000, 25_000),
    "Growth": (25_000, 120_000),
    "Enterprise": (120_000, 600_000),
}
TIER_WEIGHTS = [0.35, 0.45, 0.20]


def _motion(account: Account, anchor: date) -> Motion:
    """First match wins: onboarding, then renewal, otherwise churn."""
    early = (anchor - account.start_date).days < WINDOWS.onboarding_days
    if early and not account.milestones.complete:
        return Motion.ONBOARDING
    if 0 <= (account.renewal_date - anchor).days <= WINDOWS.renewal_window_days:
        return Motion.RENEWAL
    return Motion.CHURN


def _norm(value: float, rule: SignalRule) -> float:
    return min(1.0, max(0.0, (value - rule.start) / (rule.full - rule.start)))


def _risk(values: dict[SignalKind, float]) -> float:
    """Risk as the scorer computes it: weighted, normalised signals, capped at 100."""
    total = 0.0
    for kind, value in values.items():
        rule = DEFAULT_SIGNAL_RULES[kind]
        total += rule.weight * _norm(value, rule)
    return min(100.0, total)


def _band(risk: float) -> Health | None:
    """Health band a risk falls in under the default thresholds, or None if it is in a gap."""
    if risk < 15:
        return Health.HEALTHY
    if 25 <= risk <= 40:
        return Health.WATCH
    if risk >= 55:
        return Health.AT_RISK
    return None


def _draw(health: Health, rng: random.Random) -> dict[SignalKind, float]:
    """Raw signal values for one health band. The onboarding overdue count is added later."""
    if health is Health.HEALTHY:
        return {
            SignalKind.USAGE_TREND_30D: round(rng.uniform(1, 12), 1),
            SignalKind.DAYS_SINCE_LAST_LOGIN: float(rng.randint(0, 5)),
            SignalKind.OPEN_TICKETS_SEV1: float(rng.choice([0, 0, 0, 1])),
            SignalKind.NPS_LATEST: float(rng.randint(8, 10)),
            SignalKind.CHAMPION_LEFT: 0.0,
            SignalKind.SEAT_UTILISATION: round(rng.uniform(75, 100), 1),
            SignalKind.BILLING_OVERDUE_DAYS: float(rng.choice([0, 0, 0, 5])),
        }
    if health is Health.WATCH:
        return {
            SignalKind.USAGE_TREND_30D: round(rng.uniform(-30, -5), 1),
            SignalKind.DAYS_SINCE_LAST_LOGIN: float(rng.randint(8, 20)),
            SignalKind.OPEN_TICKETS_SEV1: float(rng.randint(0, 2)),
            SignalKind.NPS_LATEST: float(rng.randint(4, 7)),
            SignalKind.CHAMPION_LEFT: 0.0,
            SignalKind.SEAT_UTILISATION: round(rng.uniform(45, 75), 1),
            SignalKind.BILLING_OVERDUE_DAYS: float(rng.choice([0, 0, 10, 20, 30])),
        }
    return {
        SignalKind.USAGE_TREND_30D: round(rng.uniform(-50, -25), 1),
        SignalKind.DAYS_SINCE_LAST_LOGIN: float(rng.randint(18, 45)),
        SignalKind.OPEN_TICKETS_SEV1: float(rng.randint(1, 3)),
        SignalKind.NPS_LATEST: float(rng.randint(0, 5)),
        SignalKind.CHAMPION_LEFT: float(rng.choice([0, 1])),
        SignalKind.SEAT_UTILISATION: round(rng.uniform(20, 55), 1),
        SignalKind.BILLING_OVERDUE_DAYS: float(rng.randint(0, 60)),
    }


def _sample_values(health: Health, overdue: int, rng: random.Random) -> dict[SignalKind, float]:
    """Draw signal values until the risk lands in the health band's window."""
    low, high = RISK_WINDOWS[health]
    for _ in range(10_000):
        values = _draw(health, rng)
        values[SignalKind.ONBOARDING_OVERDUE_MILESTONES] = float(overdue)
        if low <= _risk(values) <= high:
            return values
    raise RuntimeError(f"no {health.value} signal set found in {low}..{high}")


def _schedule(
    motion: Motion, health: Health, anchor: date, rng: random.Random
) -> tuple[date, date, Milestones]:
    """Start date, renewal date and milestones that put an account in `motion`."""
    if motion is Motion.ONBOARDING:
        start = anchor - timedelta(days=rng.randint(10, 80))
        renewal = start + timedelta(days=365)
        total = rng.randint(5, 8)
        remaining = rng.randint(2, 4)
        if health is Health.HEALTHY:
            overdue = 0
        elif health is Health.WATCH:
            overdue = rng.randint(0, 1)
        else:
            overdue = rng.randint(1, min(3, remaining))
        return start, renewal, Milestones(done=total - remaining, total=total, overdue=overdue)

    start = anchor - timedelta(days=rng.randint(365, 4 * 365))
    if motion is Motion.RENEWAL:
        renewal = anchor + timedelta(days=rng.randint(7, 120))
    else:
        renewal = anchor + timedelta(days=rng.randint(121, 365))
    total = rng.randint(4, 8)
    done, overdue = total, 0
    if health is Health.AT_RISK and rng.random() < 0.5:
        done, overdue = total - 2, 2  # two milestones stalled past due on a long-standing account
    return start, renewal, Milestones(done=done, total=total, overdue=overdue)


def _older_value(kind: SignalKind, current: float, rng: random.Random) -> float:
    """An earlier, different reading for `kind`, so the latest-signal-wins rule is exercised."""
    if kind is SignalKind.USAGE_TREND_30D:
        older = round(current + rng.uniform(5, 20), 1)
    elif kind is SignalKind.DAYS_SINCE_LAST_LOGIN:
        older = float(max(0, int(current) - rng.randint(3, 10)))
    elif kind is SignalKind.OPEN_TICKETS_SEV1:
        older = float(rng.randint(0, 2))
    elif kind is SignalKind.NPS_LATEST:
        older = float(rng.randint(5, 10))
    elif kind is SignalKind.SEAT_UTILISATION:
        older = round(rng.uniform(60, 95), 1)
    else:  # BILLING_OVERDUE_DAYS
        older = float(rng.choice([0, 5, 10]))
    if older == current:
        older = current - 1 if current >= 1 else current + 1
    return older


def generate(anchor: date) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accounts and signals as (accounts payload, signals payload). Same output on every call."""
    rng = random.Random(SEED)
    anchor_dt = datetime(anchor.year, anchor.month, anchor.day)

    slots = [
        (motion, health)
        for motion, by_health in MIX.items()
        for health, count in by_health.items()
        for _ in range(count)
    ]
    rng.shuffle(slots)
    names = rng.sample([f"{p} {s}" for p in PREFIXES for s in SUFFIXES], len(slots))
    older_ids = set(rng.sample(range(len(slots)), N_OLDER))

    accounts: list[Account] = []
    signals: list[Signal] = []
    for i, ((motion, health), name) in enumerate(zip(slots, names, strict=True)):
        acc_id = f"acc-{i + 1:03d}"
        start, renewal, milestones = _schedule(motion, health, anchor, rng)
        tier = rng.choices(list(TIERS), weights=TIER_WEIGHTS)[0]
        low, high = TIERS[tier]
        account = Account(
            id=acc_id,
            name=name,
            arr=float(rng.randrange(low, high + 1, 500)),
            tier=tier,
            owner=OWNERS[i % len(OWNERS)],
            start_date=start,
            renewal_date=renewal,
            milestones=milestones,
        )
        assert _motion(account, anchor) is motion, f"{acc_id} is not {motion.value}"

        values = _sample_values(health, milestones.overdue, rng)
        risk = _risk(values)
        assert _band(risk) is health, f"{acc_id} risk {risk:.1f} is not {health.value}"

        accounts.append(account)
        observed = {kind: anchor_dt - timedelta(hours=rng.randint(1, 72)) for kind in SignalKind}
        for kind in SignalKind:
            signals.append(
                Signal(
                    account_id=acc_id,
                    kind=kind,
                    value=values[kind],
                    observed_at=observed[kind],
                    source="demo",
                )
            )
        if i in older_ids:
            kind = rng.choice(OLDER_KINDS)
            signals.append(
                Signal(
                    account_id=acc_id,
                    kind=kind,
                    value=_older_value(kind, values[kind], rng),
                    observed_at=observed[kind] - timedelta(days=rng.randint(10, 20)),
                    source="demo",
                )
            )

    accounts_payload = {
        "anchor": anchor.isoformat(),
        "accounts": [a.model_dump(mode="json") for a in accounts],
    }
    signals_payload = {
        "anchor": anchor.isoformat(),
        "signals": [s.model_dump(mode="json") for s in signals],
    }
    return accounts_payload, signals_payload


def main() -> None:
    accounts_payload, signals_payload = generate(ANCHOR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, payload in (
        ("accounts.json", accounts_payload),
        ("signals.json", signals_payload),
    ):
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        path = DATA_DIR / filename
        path.write_bytes(text.encode("utf-8"))  # bytes, so the line endings stay LF on Windows
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
