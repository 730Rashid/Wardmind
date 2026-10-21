"""A local fake HubSpot that serves the test fixtures on 127.0.0.1, for UI demos and manual checks.

Run from the repo root:
    uv run python scripts/fake_hubspot.py --port 8799
Then point cs-brief at it:
    CS_BRIEF_HUBSPOT_API_BASE=http://127.0.0.1:8799

Any Bearer token is accepted except "bad-token-for-demo-401", which gets the 401 body so the
error path can be shown. respond() holds the routing. tests/hubspot_fake.py uses it too, so the
demo server and the tests can't drift apart.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hubspot"
BAD_TOKEN = "bad-token-for-demo-401"
DATED_MARK = "/2026-09/"  # dated API paths contain this; the legacy v3 and v4 paths don't
PAGE_SIZE = 5  # search pages hold 5 results, so the 7 customers come back in two pages
MAX_SEARCH_LIMIT = 100
MAX_BATCH = 100
MAX_BATCH_WITH_HISTORY = 50

Body = dict[str, Any]
Params = Mapping[str, str]


class _BadRequest(Exception):
    """The request breaks a rule the real API enforces, so the fake answers 400."""


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _error(message: str) -> Body:
    return {"status": "error", "message": message, "correlationId": "fixture-0000"}


def route_name(method: str, path: str) -> str | None:
    """Which API call a request makes, or None. Dated and legacy paths share one name."""
    if method == "POST":
        if "/associations/" in path and path.endswith("/tickets/companies/batch/read"):
            return "ticket_associations"
        if path.endswith("/companies/search"):
            return "companies_search"
        if path.endswith("/companies/batch/read"):
            return "companies_batch_read"
        if path.endswith("/tickets/search"):
            return "tickets_search"
    if method == "GET" and "/owners/" in path:
        return "owners"
    return None


def _number(raw: Any, field: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _BadRequest(f"{field} should be a whole number.") from None


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _passes(props: Mapping[str, Any], flt: Mapping[str, Any]) -> bool:
    """One filter, the way HubSpot evaluates the operators cs-brief uses."""
    value = props.get(flt.get("propertyName"))
    operator = flt.get("operator")
    if operator == "EQ":
        return _present(value) and str(value) == str(flt.get("value"))
    if operator == "IN":
        if not isinstance(flt.get("values"), list):
            raise _BadRequest('IN filters need a "values" list.')
        return _present(value) and str(value) in {str(item) for item in flt["values"]}
    if operator == "NOT_HAS_PROPERTY":
        return not _present(value)
    if operator == "HAS_PROPERTY":
        return _present(value)
    raise _BadRequest(f"Unsupported filter operator {operator}.")


def _matches(props: Mapping[str, Any], groups: list[Body]) -> bool:
    """Filters inside a group must all pass; any group may pass."""
    if not groups:
        return True
    return any(all(_passes(props, flt) for flt in group.get("filters") or []) for group in groups)


def _record(record: Body, wanted: list[str]) -> Body:
    """A result as HubSpot returns it: only the requested properties that are set."""
    return {
        "id": record["id"],
        "properties": {k: record["properties"][k] for k in wanted if k in record["properties"]},
        "createdAt": record["createdAt"],
        "updatedAt": record["updatedAt"],
        "archived": False,
    }


def _search(records: list[Body], body: Body) -> Body:
    limit = _number(body.get("limit", 10), "limit")
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise _BadRequest(f"limit must be between 1 and {MAX_SEARCH_LIMIT}.")
    offset = _number(body.get("after", 0), "after")
    groups = body.get("filterGroups") or []
    wanted = body.get("properties") or []
    matches = [record for record in records if _matches(record["properties"], groups)]
    window = matches[offset : offset + min(limit, PAGE_SIZE)]
    payload: Body = {
        "total": len(matches),
        "results": [_record(record, wanted) for record in window],
    }
    if offset + len(window) < len(matches):
        payload["paging"] = {"next": {"after": str(offset + len(window))}}
    return payload


def _companies_search(body: Body, params: Params) -> Body:
    return _search(_load("portal_companies.json")["companies"], body)


def _tickets_search(body: Body, params: Params) -> Body:
    return _search(_load("portal_tickets.json")["tickets"], body)


def _companies_batch_read(body: Body, params: Params) -> Body:
    inputs = body.get("inputs") or []
    wanted = body.get("properties") or []
    history = body.get("propertiesWithHistory")
    limit = MAX_BATCH_WITH_HISTORY if history is not None else MAX_BATCH
    if len(inputs) > limit:
        raise _BadRequest(f"A batch read takes at most {limit} ids.")
    by_id = {record["id"]: record for record in _load("portal_companies.json")["companies"]}
    results: list[Body] = []
    for item in inputs:
        record = by_id.get(str(item["id"]))
        if record is None:
            continue
        out = _record(record, wanted)
        if history is not None:
            past = record.get("propertiesWithHistory") or {}
            out["propertiesWithHistory"] = {k: past[k] for k in history if k in past}
        results.append(out)
    return {
        "status": "COMPLETE",
        "results": results,
        "startedAt": "2026-10-19T12:00:00.000Z",
        "completedAt": "2026-10-19T12:00:00.200Z",
    }


def _ticket_associations(body: Body, params: Params) -> Body:
    inputs = body.get("inputs") or []
    if len(inputs) > MAX_BATCH:
        raise _BadRequest(f"A batch read takes at most {MAX_BATCH} ids.")
    wanted = {str(item["id"]) for item in inputs}
    entries = _load("portal_ticket_companies.json")["results"]
    return {
        "status": "COMPLETE",
        "results": [entry for entry in entries if str(entry["from"]["id"]) in wanted],
        "startedAt": "2026-10-19T12:00:00.000Z",
        "completedAt": "2026-10-19T12:00:00.200Z",
    }


def _owners(body: Body, params: Params) -> Body:
    # The first page carries a cursor; the second page is the last.
    return _load("owners_page2.json" if params.get("after") else "owners_page1.json")


_HANDLERS: dict[str, Callable[[Body, Params], Body]] = {
    "companies_search": _companies_search,
    "companies_batch_read": _companies_batch_read,
    "tickets_search": _tickets_search,
    "ticket_associations": _ticket_associations,
    "owners": _owners,
}


def respond(
    method: str,
    path: str,
    params: Params,
    body: Body | None,
    *,
    dated: bool = True,
) -> tuple[int, Body]:
    """The fake portal's answer to one request, as (status, JSON body).

    With dated=False the dated paths return 404, so the client's legacy fallback gets tested.
    """
    name = route_name(method, path)
    if name is None or (not dated and DATED_MARK in path):
        return 404, _error("Resource not found.")
    try:
        return 200, _HANDLERS[name](body or {}, params)
    except _BadRequest as exc:
        return 400, _error(str(exc))


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{rest:path}", methods=["GET", "POST"])
    async def hubspot(request: Request) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        if not token or token == BAD_TOKEN:
            return JSONResponse(_load("error_401.json"), status_code=401)
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return JSONResponse(_error("The request body isn't valid JSON."), status_code=400)
        if body is not None and not isinstance(body, dict):
            return JSONResponse(_error("The request body should be a JSON object."), 400)
        status, payload = respond(
            request.method, request.url.path, dict(request.query_params), body
        )
        return JSONResponse(payload, status_code=status)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the cs-brief HubSpot fixtures locally.")
    parser.add_argument("--port", type=int, default=8799, help="port on 127.0.0.1 (default 8799)")
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
