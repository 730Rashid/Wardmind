"""Email drafts the CSM copies into their own email: Wardmind writes them, never sends them.

CONTRACT FILE. DraftKind, Draft, the constants and the function signatures are the contract;
the bodies are built against it (workstream B).

Every draft starts from a fixed template keyed by DraftKind and filled from the account's facts
(the contact's first name, the account name, the motion, the plain-English reason labels, the
renewal date). This path needs no AI and always works.

With Settings.ai_drafts on, polish() asks the configured AI to improve the wording:
- a model on this computer (AIProvider.LOCAL) straight away;
- a model online (OpenRouter) only when Settings.ai_drafts_cloud is on too, and never a model
  that trains on data (summaries.trains_on_data) unless the caller passes allow_training, the
  same guard as AI summaries.
The prompt holds only the template's own text, which is built from the fields above: never ARR,
notes, logged emails, website text or contact email addresses, so nothing untrusted reaches the
model and the least possible leaves the computer. The answer is plain text, cut to the limits
below. Any failure (no AI, a timeout, an unreadable answer, a refusal) returns the template
draft with made_by "template" and `fallback_reason` saying why.

There is no send path: no SMTP, no email API. The page offers Copy and a mailto: link only.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from wardmind.core.models import ScoredAccount

MAX_SUBJECT = 120
MAX_BODY = 2000
TEMPLATE = "template"  # Draft.made_by when no AI changed the wording


class DraftKind(StrEnum):
    CHECK_IN = "check_in"  # a friendly check-in; the reasons decide the angle
    RENEWAL = "renewal"  # prepare the renewal conversation
    RE_ENGAGE = "re_engage"  # they have gone quiet
    CHASE_REPLY = "chase_reply"  # our email is waiting for an answer
    NEW_CHAMPION = "new_champion"  # the champion left: introduce yourself to someone new

    @property
    def label(self) -> str:
        return {
            "check_in": "Check-in",
            "renewal": "Renewal",
            "re_engage": "Re-engage",
            "chase_reply": "Chase a reply",
            "new_champion": "New champion",
        }[self.value]


class Draft(BaseModel):
    account_id: str
    kind: DraftKind
    to_name: str = ""  # the contact's first name, or "" (the greeting then says "Hi there")
    subject: str = Field(max_length=MAX_SUBJECT)
    body: str = Field(max_length=MAX_BODY)
    # The facts the draft was built from, in plain English, shown under it ("Usage down 38% in
    # 30 days", "Renews 14 Jan 2027"), so the CSM can check them before sending.
    basis: list[str] = Field(default_factory=list)
    made_by: str = TEMPLATE  # TEMPLATE, or summaries.model_label() of the model that polished it
    fallback_reason: str = ""  # why the AI wasn't used, when ai_drafts is on but it fell back


def default_kind(motion: str, reason_kinds: list[str], *, champion_left: bool) -> DraftKind:
    """The draft kind an account page offers first: NEW_CHAMPION when the champion left,
    RENEWAL in the renewal motion, RE_ENGAGE when the top reason is inactivity (last login or a
    usage drop), else CHECK_IN."""
    raise NotImplementedError


def template_draft(
    scored: ScoredAccount,
    kind: DraftKind,
    *,
    first_name: str,
    today: date,
) -> Draft:
    """The deterministic draft. Pure."""
    raise NotImplementedError


class PolishClient(Protocol):
    """What polish() needs from summaries.OpenRouterClient / summaries.LocalModelClient. The
    implementer adds the one method both clients need (a plain chat call returning text) to
    core/summaries.py, keeping their existing safety settings (no redirects, trust_env off for
    the local one, the training guard for OpenRouter)."""


def polish(draft: Draft, client: object, *, model: str, allow_training: bool = False) -> Draft:
    """The draft with the AI's wording, or the template draft with fallback_reason on any
    failure. Never raises. The caller (web/draft_pages.py) decides whether AI may be used at all
    (Settings.ai_drafts, ai_drafts_cloud, service.ai_problem()) and passes service.ai_client()."""
    raise NotImplementedError
