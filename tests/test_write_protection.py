"""Cross-site write protection, and the scoring.yaml round trip the Settings page relies on."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cs_brief.core.config import (
    ScoringConfig,
    SignalRule,
    load_scoring_config,
    save_scoring_config,
)
from cs_brief.core.models import SignalKind
from cs_brief.web.app import CROSS_SITE_MESSAGE, SECURITY_HEADERS, create_app, is_cross_site_write

HOST = "127.0.0.1:8000"


@pytest.fixture
def client() -> TestClient:
    app = create_app()

    @app.post("/_probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app, base_url=f"http://{HOST}")


@pytest.mark.parametrize(
    ("headers", "blocked"),
    [
        ({"origin": f"http://{HOST}"}, False),
        ({"origin": "https://evil.example"}, True),
        ({"origin": "null"}, True),
        ({"origin": "http://localhost:8000"}, True),  # a different origin from the Host
        ({"sec-fetch-site": "same-origin"}, False),
        ({"sec-fetch-site": "none"}, False),  # typed in the address bar / bookmark
        ({"sec-fetch-site": "cross-site"}, True),
        ({"sec-fetch-site": "same-site"}, True),
        ({}, False),  # non-browser client
    ],
)
def test_post_is_refused_unless_it_comes_from_this_origin(client, headers, blocked):
    response = client.post("/_probe", headers=headers)
    if blocked:
        assert response.status_code == 403
        assert response.text == CROSS_SITE_MESSAGE
        assert (
            response.headers["content-security-policy"]
            == SECURITY_HEADERS["Content-Security-Policy"]
        )
    else:
        assert response.status_code == 200


def test_reads_are_never_blocked(client):
    response = client.get("/", headers={"origin": "https://evil.example"})
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "post"])
def test_every_unsafe_method_is_checked(method):
    assert is_cross_site_write(method, {"origin": "https://evil.example", "host": HOST})


def test_scoring_config_round_trips_through_yaml(tmp_path):
    path = tmp_path / "config" / "scoring.yaml"
    cfg = ScoringConfig()
    cfg.signals[SignalKind.NPS_LATEST] = SignalRule(weight=7.5, start=6, full=1)
    cfg.thresholds.at_risk = 50
    cfg.arr.weight = 0.45
    save_scoring_config(cfg, path)
    assert load_scoring_config(path) == cfg
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# cs-brief scoring configuration")
    # Signals are written in SignalKind order so diffs of the file stay readable.
    positions = [text.index(kind.value + ":") for kind in SignalKind]
    assert positions == sorted(positions)
    assert not list(tmp_path.rglob("*.tmp"))  # the atomic write cleaned up after itself
