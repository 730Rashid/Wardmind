"""Domain models shared by every surface (CLI, MCP server, web UI).

CONTRACT FILE: changes here affect every module. Implementers escalate before editing.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, GetCoreSchemaHandler, computed_field, model_validator
from pydantic_core import core_schema


class Motion(StrEnum):
    """The CS motion an account is in right now. First match wins, in this order."""

    ONBOARDING = "onboarding"
    RENEWAL = "renewal"
    CHURN = "churn"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class Health(StrEnum):
    AT_RISK = "at_risk"
    WATCH = "watch"
    HEALTHY = "healthy"

    @property
    def label(self) -> str:
        return {"at_risk": "At risk", "watch": "Watch", "healthy": "Healthy"}[self.value]


class SignalKind(StrEnum):
    """Signal value conventions (the unit of Signal.value for each kind)."""

    USAGE_TREND_30D = "usage_trend_30d"  # % change in active users over 30 days, e.g. -38.0
    DAYS_SINCE_LAST_LOGIN = "days_since_last_login"  # days since any user logged in
    OPEN_TICKETS_SEV1 = "open_tickets_sev1"  # count of open severity-1 tickets
    NPS_LATEST = "nps_latest"  # latest NPS response from the account, 0-10
    CHAMPION_LEFT = "champion_left"  # 1.0 if the main champion left, else 0.0
    SEAT_UTILISATION = "seat_utilisation"  # % of purchased seats in use, 0-100+
    BILLING_OVERDUE_DAYS = "billing_overdue_days"  # days the oldest invoice is overdue
    ONBOARDING_OVERDUE_MILESTONES = "onboarding_overdue_milestones"  # count of late milestones


CUSTOM_PREFIX = "custom:"
# A custom signal's id: lower-case letters, digits and underscores, starting with a letter.
CUSTOM_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")


class CustomKind(str):
    """A signal the CSM defined for their book (config.CustomSignal), written "custom:<id>".

    It stands wherever a SignalKind does: `.value` is the string itself, so code that reads
    `kind.value` (sorting, form names, templates) needs no change. Its value is whatever
    unit the CSM chose; config.CustomSignal says how to read it."""

    __slots__ = ()

    def __new__(cls, value: str) -> CustomKind:
        text = str(value)
        if not text.startswith(CUSTOM_PREFIX) or not CUSTOM_ID.fullmatch(
            text[len(CUSTOM_PREFIX) :]
        ):
            raise ValueError(f"not a custom signal kind: {text!r}")
        return super().__new__(cls, text)

    @classmethod
    def of(cls, custom_id: str) -> CustomKind:
        return cls(CUSTOM_PREFIX + custom_id)

    @property
    def value(self) -> str:
        return str(self)

    @property
    def id(self) -> str:
        return self[len(CUSTOM_PREFIX) :]

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )


# A signal's kind: one of the eight built in, or one the CSM defined.
SignalKey = SignalKind | CustomKind


def signal_key(value: str) -> SignalKey:
    """The kind written as `value`: a SignalKind, else a CustomKind (ValueError for neither)."""
    try:
        return SignalKind(value)
    except ValueError:
        return CustomKind(value)


class Milestones(BaseModel):
    done: int = Field(ge=0)
    total: int = Field(ge=0)
    overdue: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Milestones:
        if self.done > self.total:
            raise ValueError("milestones.done cannot exceed milestones.total")
        if self.overdue > self.total - self.done:
            raise ValueError("milestones.overdue cannot exceed remaining milestones")
        return self

    @property
    def complete(self) -> bool:
        return self.done >= self.total


class Account(BaseModel):
    id: str
    name: str
    arr: float = Field(ge=0)
    tier: str
    owner: str
    # When they became a customer. None when no source knows it: Wardmind never invents one,
    # because a made-up date would read as "started today" and could trigger onboarding.
    start_date: date | None = None
    renewal_date: date
    milestones: Milestones
    domain: str | None = None  # website, e.g. "acme.com"; the strongest duplicate-matching key


class Signal(BaseModel):
    account_id: str
    kind: SignalKey
    value: float
    observed_at: datetime
    source: str = "demo"


class Reason(BaseModel):
    kind: SignalKey
    contribution: float = Field(ge=0)  # risk points this signal added (before the 100 cap)
    plain_english: str  # e.g. "Active users down 38% in 30 days"


class ScoredAccount(BaseModel):
    account: Account
    motion: Motion
    health: Health
    risk: float = Field(ge=0, le=100)  # sum of contributions, capped at 100
    priority: float = Field(ge=0, le=100)  # risk scaled by ARR; the book is sorted by this
    reasons: list[Reason]  # only signals that contributed, largest contribution first
    suggested_action: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def top_reason(self) -> str:
        return self.reasons[0].plain_english if self.reasons else "No risk signals"
