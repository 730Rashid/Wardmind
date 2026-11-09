"""A fake OpenRouter for the AI tests: an httpx MockTransport that answers the two calls Wardmind
makes, and keeps every request it was sent. Nothing here opens a connection.

The key below is a made-up string, never a real key. Tests that check it never leaks use it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from wardmind.core import summaries
from wardmind.core.research import SiteResearch

KEY = "sk-or-v1-FAKEKEYFORTESTS0000000000000000000000000000000000000000"

ANSWER = {
    "about": "Acme builds freight software for regional carriers.",
    "highlights": [{"topic": "funding", "note": "Acme raises Series B to hire support"}],
}

Handler = Callable[[httpx.Request], httpx.Response]


def reply_json(status: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def completion(content: Any, *, cost: float | None = 0.0031) -> dict[str, Any]:
    """A chat completion whose message holds `content` (a dict is sent as JSON text)."""
    text = content if isinstance(content, str) else json.dumps(content)
    usage: dict[str, Any] = {"prompt_tokens": 1200, "completion_tokens": 90}
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "gen-1",
        "model": "anthropic/claude-haiku-5.5",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": usage,
    }


class FakeOpenRouter:
    """Answers chat completions with `answer` (a dict or a status-and-body route) and /key with
    `key_info`. `queue` holds handlers used first, in order, before the defaults."""

    def __init__(self, *, answer: Any = None, key_info: dict[str, Any] | None = None) -> None:
        self.answer: Any = ANSWER if answer is None else answer
        self.key_info: dict[str, Any] = key_info or {
            "data": {"label": "Wardmind test", "usage": 0.42, "limit_remaining": 9.58}
        }
        self.queue: list[Handler] = []
        self.requests: list[httpx.Request] = []

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests if request.content]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.queue:
            return self.queue.pop(0)(request)
        if request.url.path.endswith("/chat/completions"):
            return reply_json(200, completion(self.answer))
        if request.url.path.endswith("/key"):
            return reply_json(200, self.key_info)
        return reply_json(404, {"error": {"code": 404, "message": "Not found"}})

    def client(self, *, sleep: Callable[[float], None] | None = None) -> summaries.OpenRouterClient:
        return summaries.OpenRouterClient(
            KEY,
            transport=httpx.MockTransport(self.handle),
            sleep=sleep if sleep is not None else (lambda seconds: None),
        )


def research_text(research: SiteResearch) -> str:
    """The user message build_messages would send, for asserting on what leaves the machine."""
    return summaries.build_messages(research)[1]["content"]
