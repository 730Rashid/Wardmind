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

# Realism pass, applied to the current readings after _build. It has its own RNG, so the draws in
# _build (names, dates, ARR, and every value before the pass) do not move.
WATCH_SEV1_KEEP = 1  # watch accounts that keep one open severity-1 ticket (at most 2 allowed)
AT_RISK_SEV1_KEEP = 5  # at-risk accounts that keep one to three open severity-1 tickets
RENEWAL_CHAMPIONS_LEFT = 2  # at-risk accounts in renewal whose champion has left
CHURN_CHAMPIONS_LEFT = 1  # watch accounts in churn whose champion has left
USAGE_RANGE = (-50.0, 15.0)  # keeps a nudged usage reading realistic
LOGIN_RANGE = (0.0, 45.0)  # keeps a nudged days-since-login reading realistic

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


def _demo_domain(name: str) -> str:
    """An invented website from the account name: "Brindle Studio" gives "brindlestudio.example".
    The .example TLD can never be a real site. No random draws, so the rest stays the same."""
    return name.lower().replace(" ", "") + ".example"


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


def _build(anchor: date) -> tuple[list[Account], list[Signal], list[Signal]]:
    """Accounts, the current reading per (account, kind), and every existing reading.

    Every draw comes from the main RNG, so the draws are the same as before the realism pass
    existed. The pass then edits some current values, and the existing readings follow it.
    """
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
    current: list[Signal] = []
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
            domain=_demo_domain(name),
        )
        assert _motion(account, anchor) is motion, f"{acc_id} is not {motion.value}"

        values = _sample_values(health, milestones.overdue, rng)
        risk = _risk(values)
        assert _band(risk) is health, f"{acc_id} risk {risk:.1f} is not {health.value}"

        accounts.append(account)
        observed = {kind: anchor_dt - timedelta(hours=rng.randint(1, 72)) for kind in SignalKind}
        for kind in SignalKind:
            reading = Signal(
                account_id=acc_id,
                kind=kind,
                value=values[kind],
                observed_at=observed[kind],
                source="demo",
            )
            current.append(reading)
            signals.append(reading)
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

    current = _realism_pass(accounts, current, random.Random(SEED + 2), anchor)
    edited = {(r.account_id, r.kind, r.observed_at): r for r in current}
    signals = [edited.get((s.account_id, s.kind, s.observed_at), s) for s in signals]
    return accounts, current, signals


def _fit_to_window(values: dict[SignalKind, float], health: Health) -> bool:
    """Nudge usage, then days since last login, until the risk is back inside the band's window.

    Each step moves usage by one point or logins by one day, so the risk moves in small steps and
    cannot jump over a window. Stops at the first reading that lands inside it. Returns False if
    even the extreme readings cannot bring the risk into the window.
    """
    low, high = RISK_WINDOWS[health]

    def inside() -> bool:
        return low <= _risk(values) <= high

    too_high = _risk(values) > high
    usage_step = 1.0 if too_high else -1.0  # a flatter usage trend lowers the risk
    login_step = -1.0 if too_high else 1.0  # fewer days since the last login lowers it too
    usage_low, usage_high = USAGE_RANGE
    login_low, login_high = LOGIN_RANGE
    while not inside():
        usage = values[SignalKind.USAGE_TREND_30D] + usage_step
        if not usage_low <= usage <= usage_high:
            break
        values[SignalKind.USAGE_TREND_30D] = round(usage, 1)
    while not inside():
        logins = values[SignalKind.DAYS_SINCE_LAST_LOGIN] + login_step
        if not login_low <= logins <= login_high:
            break
        values[SignalKind.DAYS_SINCE_LAST_LOGIN] = logins
    return inside()


def _realism_pass(
    accounts: list[Account], current: list[Signal], rng: random.Random, anchor: date
) -> list[Signal]:
    """Edit current readings so the call-outs look realistic. Accounts and dates never change.

    Healthy accounts have no open severity-1 tickets, and a few watch and at-risk accounts keep
    some. Three accounts have lost their champion: two at-risk accounts in renewal and one watch
    account in churn. Each account whose readings changed is then nudged back into its health
    band's risk window, so the health and motion mixes stay exactly the same.
    """
    motion_of = {account.id: _motion(account, anchor) for account in accounts}
    values: dict[str, dict[SignalKind, float]] = {}
    for reading in current:
        values.setdefault(reading.account_id, {})[reading.kind] = reading.value
    ids = sorted(values)

    health: dict[str, Health] = {}
    for acc_id in ids:
        band = _band(_risk(values[acc_id]))
        assert band is not None, f"{acc_id} risk falls between two bands"
        health[acc_id] = band
    before = {acc_id: dict(values[acc_id]) for acc_id in ids}

    def pick(pool: list[str], count: int) -> list[str]:
        shuffled = sorted(pool)
        rng.shuffle(shuffled)
        return shuffled[:count]

    watch = [i for i in ids if health[i] is Health.WATCH]
    at_risk = [i for i in ids if health[i] is Health.AT_RISK]

    # Open severity-1 tickets. An at-risk account that cannot drop to zero and stay in its window
    # keeps its tickets. The remaining at-risk keeps go to the busiest accounts first.
    def can_clear(acc_id: str) -> bool:
        trial = {**values[acc_id], SignalKind.OPEN_TICKETS_SEV1: 0.0}
        return _fit_to_window(trial, health[acc_id])

    tickets = SignalKind.OPEN_TICKETS_SEV1
    keep_at_risk = [i for i in at_risk if values[i][tickets] >= 1 and not can_clear(i)]
    assert len(keep_at_risk) <= AT_RISK_SEV1_KEEP, "too many at-risk accounts cannot clear tickets"
    busy = [i for i in at_risk if values[i][tickets] >= 2 and i not in keep_at_risk]
    quiet = [i for i in at_risk if values[i][tickets] == 1 and i not in keep_at_risk]
    keep_at_risk += pick(busy, AT_RISK_SEV1_KEEP - len(keep_at_risk))
    keep_at_risk += pick(quiet, AT_RISK_SEV1_KEEP - len(keep_at_risk))
    keep_watch = pick([i for i in watch if values[i][tickets] == 1], WATCH_SEV1_KEEP)
    kept = {*keep_at_risk, *keep_watch}
    for acc_id in ids:
        if acc_id not in kept:
            values[acc_id][tickets] = 0.0

    # Champions who have left. Candidates are tried in shuffled order, and one is taken only if
    # its risk can be brought back into its window, so the account keeps its health band.
    def take(pool: list[str], count: int) -> list[str]:
        taken: list[str] = []
        for acc_id in pick(pool, len(pool)):
            if len(taken) == count:
                break
            trial = {**values[acc_id], SignalKind.CHAMPION_LEFT: 1.0}
            if _fit_to_window(trial, health[acc_id]):
                values[acc_id] = trial
                taken.append(acc_id)
        assert len(taken) == count, f"only {len(taken)} of {count} champion candidates fit"
        return taken

    left = take([i for i in at_risk if motion_of[i] is Motion.RENEWAL], RENEWAL_CHAMPIONS_LEFT)
    left += take([i for i in watch if motion_of[i] is Motion.CHURN], CHURN_CHAMPIONS_LEFT)
    for acc_id in ids:
        if acc_id not in left:
            values[acc_id][SignalKind.CHAMPION_LEFT] = 0.0

    for acc_id in ids:
        if values[acc_id] != before[acc_id]:
            assert _fit_to_window(values[acc_id], health[acc_id]), f"{acc_id} cannot be kept"
        assert _band(_risk(values[acc_id])) is health[acc_id], f"{acc_id} left its band"
    assert sum(values[i][SignalKind.CHAMPION_LEFT] == 1.0 for i in ids) == len(left) == 3

    edited: list[Signal] = []
    for reading in current:
        value = values[reading.account_id][reading.kind]
        if value == reading.value:
            edited.append(reading)
        else:
            edited.append(reading.model_copy(update={"value": value}))
    return edited


def _usage_history(current: float, rng: random.Random) -> list[float]:
    """Usage readings 14, 28, 42 and 56 days back, newest first."""
    if current <= -10:  # a clear decline: each older reading is less negative, the oldest near flat
        oldest = round(rng.uniform(-5, 5), 1)
        span = oldest - current
        steps = [round(current + span * (k / 4 + rng.uniform(-0.05, 0.05)), 1) for k in (1, 2, 3)]
        return [*steps, oldest]
    return [round(current + rng.uniform(-6, 6), 1) for _ in range(4)]


def _seat_history(current: float, rng: random.Random) -> list[float]:
    """Seat readings 28 and 56 days back, newest first."""
    if current < 70:  # fuller before, drifting down to the current level
        oldest = round(rng.uniform(80, 90), 1)
        middle = round(current + (oldest - current) * rng.uniform(0.35, 0.65), 1)
        return [middle, oldest]
    return [round(min(100.0, max(0.0, current + rng.uniform(-5, 5))), 1) for _ in range(2)]


def _nps_history(current: float, rng: random.Random) -> float:
    """The NPS reading 63 days back: a fall to 7-9 if the score is now 5 or lower, else within 1."""
    if current <= 5:
        return float(rng.randint(7, 9))
    return float(min(10, max(0, current + rng.randint(-1, 1))))


def _history(
    accounts: list[Account],
    current: list[Signal],
    existing: list[Signal],
    anchor: date,
    rng: random.Random,
) -> list[Signal]:
    """Earlier readings that give each account a timeline. No current value changes.

    Every reading lands strictly before the newest reading of its (account, kind), so latest-wins
    and every score stay the same. A reading that would share a timestamp with an existing one
    moves one day earlier.
    """
    newest = {(s.account_id, s.kind): s for s in current}
    taken = {(s.account_id, s.kind, s.observed_at) for s in existing}
    history: list[Signal] = []

    def add(account_id: str, kind: SignalKind, days_back: int, value: float) -> None:
        observed = newest[(account_id, kind)].observed_at - timedelta(days=days_back)
        while (account_id, kind, observed) in taken:
            observed -= timedelta(days=1)
        taken.add((account_id, kind, observed))
        history.append(
            Signal(
                account_id=account_id,
                kind=kind,
                value=value + 0.0,  # adding 0.0 turns a negative zero from rounding into 0.0
                observed_at=observed,
                source="demo",
            )
        )

    for account in accounts:
        acc_id = account.id
        now = {kind: newest[(acc_id, kind)].value for kind in SignalKind}
        usage = _usage_history(now[SignalKind.USAGE_TREND_30D], rng)
        for days_back, value in zip((14, 28, 42, 56), usage, strict=True):
            add(acc_id, SignalKind.USAGE_TREND_30D, days_back, value)
        seats = _seat_history(now[SignalKind.SEAT_UTILISATION], rng)
        for days_back, value in zip((28, 56), seats, strict=True):
            add(acc_id, SignalKind.SEAT_UTILISATION, days_back, value)
        add(acc_id, SignalKind.DAYS_SINCE_LAST_LOGIN, 28, float(rng.randint(0, 5)))
        add(acc_id, SignalKind.NPS_LATEST, 63, _nps_history(now[SignalKind.NPS_LATEST], rng))
        if now[SignalKind.CHAMPION_LEFT] == 1.0:
            add(acc_id, SignalKind.CHAMPION_LEFT, 35, 0.0)  # the champion was still there
        if now[SignalKind.OPEN_TICKETS_SEV1] >= 1:
            tickets = now[SignalKind.OPEN_TICKETS_SEV1]
            add(acc_id, SignalKind.OPEN_TICKETS_SEV1, 21, max(0.0, tickets - rng.choice([1, 2])))
        if now[SignalKind.BILLING_OVERDUE_DAYS] >= 30:
            add(
                acc_id,
                SignalKind.BILLING_OVERDUE_DAYS,
                30,
                now[SignalKind.BILLING_OVERDUE_DAYS] - 30,
            )
        overdue = now[SignalKind.ONBOARDING_OVERDUE_MILESTONES]
        if _motion(account, anchor) is Motion.ONBOARDING and overdue >= 1:
            add(acc_id, SignalKind.ONBOARDING_OVERDUE_MILESTONES, 21, overdue - 1)
    return history


def generate(anchor: date) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accounts and signals as (accounts payload, signals payload). Same output on every call."""
    accounts, current, existing = _build(anchor)
    # History is drawn last, from its own RNG, so no current value above changes.
    history = _history(accounts, current, existing, anchor, random.Random(SEED + 1))
    accounts_payload = {
        "anchor": anchor.isoformat(),
        "accounts": [a.model_dump(mode="json") for a in accounts],
    }
    signals_payload = {
        "anchor": anchor.isoformat(),
        "signals": [s.model_dump(mode="json") for s in existing + history],
    }
    return accounts_payload, signals_payload


def current_readings(anchor: date) -> list[Signal]:
    """The newest reading per (account, kind), as generated before any history was added."""
    _, current, _ = _build(anchor)
    return current


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
