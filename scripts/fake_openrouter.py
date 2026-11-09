"""A local fake OpenRouter for the two calls Wardmind makes, for UI demos and manual checks.

Run from the repo root:
    uv run python scripts/fake_openrouter.py --port 8798
Then point Wardmind at it:
    WARDMIND_OPENROUTER_API_BASE=http://127.0.0.1:8798/api/v1

POST /api/v1/chat/completions answers with a summary written from the website text it is sent
(deterministic: the same text always gives the same answer), with a small cost in usage.
GET /api/v1/key answers with a key that has 9.58 of credit left. Both need a Bearer header, and
any key value is accepted. With --fail, completions answer 402 (out of credit) so that path can
be shown. Standard library only; it binds to 127.0.0.1 and never talks to the real OpenRouter.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CHAT_PATH = "/api/v1/chat/completions"
KEY_PATH = "/api/v1/key"
TOPIC_BY_LABEL = {
    "Acquisition or merger": "acquisition",
    "Funding": "funding",
    "Leadership change": "leadership",
    "Restructuring": "restructuring",
}
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}|undated): (.+?)(?: \(topics: (.+)\))?$")


def summary_for(user_text: str) -> dict[str, Any]:
    """The answer for one website text: an About from the title and description, and up to three
    highlights from the headlines that carry a topic. Always matches the schema."""
    title = _field(user_text, "Page title")
    description = _field(user_text, "Page description")
    host = _field(user_text, "Website") or "the website"
    about = description or f"{title or host} is a company whose website Wardmind read."
    highlights: list[dict[str, str]] = []
    for line in user_text.splitlines():
        match = LINE.match(line.strip())
        if match is None or not match.group(3):
            continue
        for label in match.group(3).split(", "):
            topic = TOPIC_BY_LABEL.get(label)
            if topic and len(highlights) < 3:
                highlights.append({"topic": topic, "note": match.group(2)})
                break
    return {"about": about[:280], "highlights": highlights}


def _field(text: str, name: str) -> str:
    prefix = f"{name}: "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def completion(body: dict[str, Any]) -> dict[str, Any]:
    """A chat completion whose content is the schema-valid answer, with usage and a cost."""
    user = next((m["content"] for m in body.get("messages", []) if m.get("role") == "user"), "")
    content = json.dumps(summary_for(user), ensure_ascii=False)
    return {
        "id": "gen-fake-0001",
        "object": "chat.completion",
        "model": body.get("model", ""),
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": max(1, len(user) // 4),
            "completion_tokens": max(1, len(content) // 4),
            "cost": 0.0021,
        },
    }


class Handler(BaseHTTPRequestHandler):
    fail_completions = False  # set by main() from --fail

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        return self.headers.get("Authorization", "").startswith("Bearer ")

    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        if self.path != KEY_PATH:
            self._send(404, {"error": {"code": 404, "message": "Not found"}})
            return
        if not self._authorised():
            self._send(401, {"error": {"code": 401, "message": "Missing API key"}})
            return
        self._send(
            200,
            {"data": {"label": "fake-key", "usage": 0.42, "limit_remaining": 9.58}},
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != CHAT_PATH:
            self._send(404, {"error": {"code": 404, "message": "Not found"}})
            return
        if not self._authorised():
            self._send(401, {"error": {"code": 401, "message": "Missing API key"}})
            return
        if self.fail_completions:
            self._send(402, {"error": {"code": 402, "message": "Insufficient credits"}})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": {"code": 400, "message": "The body isn't valid JSON."}})
            return
        self._send(200, completion(body if isinstance(body, dict) else {}))

    def log_message(self, format: str, *args: Any) -> None:
        """Quiet: the server prints nothing, so no request (or header) reaches a log."""


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a fake OpenRouter on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8798, help="port on 127.0.0.1 (default 8798)")
    parser.add_argument("--fail", action="store_true", help="answer completions with 402")
    args = parser.parse_args()
    Handler.fail_completions = args.fail
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Fake OpenRouter on http://127.0.0.1:{args.port}/api/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
