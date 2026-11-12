"""DemoConnector: the bundled demo book, with dates moved so it always looks current."""

from __future__ import annotations

import json
from datetime import date
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from wardmind.core.models import Account, Signal

# The packaged demo book, read and shifted once per day and shared by every DemoConnector (a new
# one is made for each request). Keyed by the day the dates are shifted to; the files are part of
# the install, so they don't change while Wardmind runs. Callers copy models, never change them.
_PACKAGED: dict[date, tuple[list[Account], list[Signal]]] = {}


class DemoConnector:
    """Serves data/demo/accounts.json and signals.json.

    Every date is shifted by (today - anchor), so the demo keeps the same relative picture
    (onboarding, renewal and churn windows) whenever it is opened.
    """

    name = "demo"

    def __init__(self, data_dir: Path | None = None, today: date | None = None) -> None:
        self._dir: Path | Traversable = (
            data_dir if data_dir is not None else files("wardmind") / "data" / "demo"
        )
        self.packaged = data_dir is None  # the bundled files, not a folder a test passed
        self._today = today or date.today()
        self._cache: tuple[list[Account], list[Signal]] | None = None

    def list_accounts(self) -> list[Account]:
        accounts, _ = self._loaded()
        return list(accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        _, signals = self._loaded()
        if account_id is None:
            return list(signals)
        return [s for s in signals if s.account_id == account_id]

    @property
    def today(self) -> date:
        """The day the demo's dates are shifted to."""
        return self._today

    def _loaded(self) -> tuple[list[Account], list[Signal]]:
        """Read and shift the JSON files on first use, then reuse them. The packaged files are
        shared across connectors for the same day."""
        if self._cache is None:
            if self.packaged:
                cached = _PACKAGED.get(self._today)
                if cached is None:
                    cached = self._load()
                    _PACKAGED.clear()  # only today's (or a test's chosen day's) copy is kept
                    _PACKAGED[self._today] = cached
                self._cache = cached
            else:
                self._cache = self._load()
        return self._cache

    def _load(self) -> tuple[list[Account], list[Signal]]:
        accounts_doc = json.loads((self._dir / "accounts.json").read_text(encoding="utf-8"))
        signals_doc = json.loads((self._dir / "signals.json").read_text(encoding="utf-8"))
        if accounts_doc["anchor"] != signals_doc["anchor"]:
            raise ValueError("accounts.json and signals.json have different anchor dates")
        delta = self._today - date.fromisoformat(accounts_doc["anchor"])

        accounts: list[Account] = []
        for row in accounts_doc["accounts"]:
            account = Account.model_validate(row)
            accounts.append(
                account.model_copy(
                    update={
                        "start_date": (
                            None if account.start_date is None else account.start_date + delta
                        ),
                        "renewal_date": account.renewal_date + delta,
                    }
                )
            )
        signals: list[Signal] = []
        for row in signals_doc["signals"]:
            signal = Signal.model_validate(row)
            signals.append(signal.model_copy(update={"observed_at": signal.observed_at + delta}))
        return accounts, signals
