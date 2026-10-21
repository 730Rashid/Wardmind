"""Shared fake HubSpot transport for the tests: httpx.MockTransport over tests/fixtures/hubspot.

The answers come from respond() in scripts/fake_hubspot.py, which the local demo server uses as
well, so the tests and the demo can't drift apart. Nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hubspot"
Override = Callable[[httpx.Request], httpx.Response]


def _load_server() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "fake_hubspot.py"
    spec = importlib.util.spec_from_file_location("fake_hubspot", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_server = _load_server()
route_name = _server.route_name
respond = _server.respond


def fixture(name: str) -> Any:
    """A fixture file, parsed. Copy it before editing."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def serve(request: httpx.Request, *, dated: bool = True) -> httpx.Response:
    """The fake portal's answer to one request."""
    body = json.loads(request.content) if request.content else None
    status, payload = respond(
        request.method, request.url.path, dict(request.url.params), body, dated=dated
    )
    return httpx.Response(status, json=payload)


def fake_transport(
    *,
    overrides: dict[tuple[str, str], httpx.Response | Override] | None = None,
    dated: bool = True,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport over the fake portal, and the list of every request it has seen.

    `overrides` maps (method, route) to a replacement answer. The route is a name from
    route_name(): "companies_search", "companies_batch_read", "tickets_search",
    "ticket_associations" or "owners". A Response is sent as given; a callable gets the request
    and returns the answer (it can call serve() to get the normal one and edit it).
    """
    seen: list[httpx.Request] = []
    rules = dict(overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = rules.get((request.method, route_name(request.method, request.url.path)))
        if isinstance(answer, httpx.Response):
            return httpx.Response(
                answer.status_code, headers=answer.headers, content=answer.content
            )
        if answer is not None:
            return answer(request)
        return serve(request, dated=dated)

    return httpx.MockTransport(handler), seen
