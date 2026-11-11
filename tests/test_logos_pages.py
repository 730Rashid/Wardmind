"""Account logos in the web UI: GET /logos/{id}, the upload, remove and look-up actions on the
account page (each refusal and each notice code), cross-site refusals, the look-up-everything
job's logo step, and deleting local data. Sites are fake (research_fake.py); nothing reaches the
network, and the store is a temp folder (conftest)."""

from __future__ import annotations

import html
import struct
import zlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from research_fake import FakeSite, Route, raising, reply
from wardmind.core import bulk, logos, research, service
from wardmind.core.logos import ImageType, LogoSource
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import LOGOS_DIR
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
CROSS_SITE = {"origin": "https://evil.example"}
DEMO_ID = "acc-001"
MAX = logos.MAX_LOGO_BYTES


def png(width: int = 32, height: int = 32) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (3 * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def image(data: bytes) -> Route:
    return reply(data, content_type="image/png")


def acme(*links: str, routes: dict[str, Route] | None = None) -> FakeSite:
    body = "".join(links)
    home = f"<!doctype html><html><head><title>Acme</title>{body}</head><body></body></html>"
    table: dict[str, Route] = {"https://acme.example/": reply(home.encode("utf-8"))}
    table.update(routes or {})
    return FakeSite(table)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _tail(location: str) -> str:
    return location.split("?", 1)[0].rsplit("/", 1)[-1]


def _query(location: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def _on(source: DataSource = DataSource.MINE, web_research: bool = True) -> None:
    save_settings(Settings(web_research=web_research, source=source))
    service.reset_connectors()


def _site(monkeypatch: pytest.MonkeyPatch, site: FakeSite) -> list[int]:
    """Serve website look-ups from a fake site; the list grows with every client made."""
    calls: list[int] = []

    def factory() -> research.SiteClient:
        calls.append(1)
        return site.client()

    monkeypatch.setattr(service, "_site_client_factory", factory)
    return calls


def _logo_files() -> list[str]:
    folder = service.get_store().root / LOGOS_DIR
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _your_account(client: TestClient, domain: str = "acme.example") -> str:
    response = client.post(
        "/accounts/new",
        data={
            "name": "Acme Corporation",
            "arr": "$120,000",
            "renewal_date": "2027-01-10",
            "domain": domain,
        },
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, _text(response)
    return _tail(str(response.headers["location"]))


def _upload(
    client: TestClient,
    account_id: str,
    data: bytes,
    *,
    name: str = "logo.png",
    content_type: str = "image/png",
    headers: dict[str, str] | None = None,
) -> Any:
    return client.post(
        f"/accounts/{account_id}/logo",
        files={"logo": (name, data, content_type)},
        headers=headers if headers is not None else ORIGIN,
        follow_redirects=False,
    )


def _code(response: Any) -> str:
    assert response.status_code == 303, _text(response)
    return _query(str(response.headers["location"])).get("logo", "")


# --- GET /logos/{account_id} -------------------------------------------------------------------


def test_an_account_with_no_logo_gets_a_plain_404(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    response = client.get(f"/logos/{account_id}")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "No logo."


def test_a_saved_logo_is_served_with_its_checked_type_and_nosniff(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    data = png(64, 64)
    assert _code(_upload(client, account_id, data)) == "logo-saved"
    response = client.get(f"/logos/{account_id}")
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_logo_is_sent_with_the_no_content_policy(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png())
    response = client.get(f"/logos/{account_id}")
    assert response.headers["content-security-policy"] == "default-src 'none'"


def test_a_logo_is_cached_privately_for_an_hour(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png())
    response = client.get(f"/logos/{account_id}")
    assert response.headers["cache-control"] == "private, max-age=3600"


# --- Upload --------------------------------------------------------------------------------------


def test_upload_saves_the_logo_and_redirects_with_the_saved_code(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    response = _upload(client, account_id, png(32, 32))
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{account_id}?logo=logo-saved"
    info = service.logo_info(account_id)
    assert info is not None and info.source is LogoSource.UPLOAD
    assert info.content_type is ImageType.PNG


def test_the_saved_code_shows_its_notice_on_the_account_page(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png())
    page = client.get(f"/accounts/{account_id}?logo=logo-saved")
    assert page.status_code == 200
    assert "Logo saved." in _text(page)


@pytest.mark.parametrize(
    ("data", "name", "content_type", "code"),
    [
        (png(16, 16) + bytes(MAX), "big.png", "image/png", "logo-too-big"),
        (b"", "empty.png", "image/png", "logo-refused"),
        (b"hello, not an image", "notes.txt", "text/plain", "logo-refused"),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "logo.svg",
            "image/svg+xml",
            "logo-refused",
        ),
        (png(8, 8), "tiny.png", "image/png", "logo-refused"),
    ],
    ids=["too-big", "empty", "text", "svg", "too-small"],
)
def test_each_refused_upload_keeps_nothing_and_says_why(
    client: TestClient, data: bytes, name: str, content_type: str, code: str
) -> None:
    _on()
    account_id = _your_account(client)
    response = _upload(client, account_id, data, name=name, content_type=content_type)
    assert _code(response) == code
    assert service.logo_info(account_id) is None
    assert _logo_files() == []
    assert client.get(f"/logos/{account_id}").status_code == 404


def test_a_refused_upload_shows_its_notice_without_any_message_in_the_url(
    client: TestClient,
) -> None:
    _on()
    account_id = _your_account(client)
    response = _upload(client, account_id, b"<svg></svg>", name="logo.svg")
    location = str(response.headers["location"])
    assert "svg" not in location.lower() and "image" not in location.lower()
    page = client.get(location)
    assert "SVG files can't be used." in _text(page)


def test_a_too_big_image_is_refused_by_size_not_kept(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    response = _upload(client, account_id, png(16, 16) + bytes(MAX))
    assert _code(response) == "logo-too-big"
    page = client.get(f"/accounts/{account_id}?logo=logo-too-big")
    assert "larger than 200 KB" in _text(page)


def test_posting_no_file_asks_for_one(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    response = client.post(
        f"/accounts/{account_id}/logo", data={"note": "x"}, headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-no-file"
    assert _logo_files() == []


def test_a_demo_account_cannot_have_a_logo_uploaded(client: TestClient) -> None:
    _on(DataSource.DEMO)
    response = _upload(client, DEMO_ID, png())
    assert response.status_code == 404
    assert "Editing is available for your own data." in _text(response)
    assert service.all_logos() == {}
    assert _logo_files() == []


def test_an_unknown_account_is_a_not_found_page(client: TestClient) -> None:
    _on()
    _your_account(client)
    response = _upload(client, "m-deadbeef", png())
    assert response.status_code == 404
    assert "No account with id m-deadbeef." in _text(response)


# --- Remove --------------------------------------------------------------------------------------


def test_remove_takes_the_logo_away_and_says_so(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png())
    response = client.post(
        f"/accounts/{account_id}/logo/remove", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-removed"
    assert client.get(f"/logos/{account_id}").status_code == 404
    assert service.logo_info(account_id) is None
    assert _logo_files() == [logos.INDEX_FILE]
    page = client.get(f"/accounts/{account_id}?logo=logo-removed")
    assert "Logo removed." in _text(page)


def test_remove_with_no_logo_still_redirects_as_removed(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    response = client.post(
        f"/accounts/{account_id}/logo/remove", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-removed"


def test_a_demo_account_cannot_have_its_logo_removed(client: TestClient) -> None:
    _on(DataSource.DEMO)
    response = client.post(f"/accounts/{DEMO_ID}/logo/remove", headers=ORIGIN)
    assert response.status_code == 404
    assert "Editing is available for your own data." in _text(response)


# --- Look up on the website ----------------------------------------------------------------------


def test_find_saves_the_logo_from_the_website(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    account_id = _your_account(client)
    site = acme(
        '<link rel="apple-touch-icon" href="/apple.png">',
        routes={"https://acme.example/apple.png": image(png(180, 180))},
    )
    calls = _site(monkeypatch, site)
    response = client.post(
        f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-found"
    assert calls == [1]
    served = client.get(f"/logos/{account_id}")
    assert served.status_code == 200
    assert served.content == png(180, 180)
    info = service.logo_info(account_id)
    assert info is not None and info.source is LogoSource.WEBSITE
    assert info.origin_url == "https://acme.example/apple.png"
    assert "Found a logo on their website and saved it." in _text(
        client.get(f"/accounts/{account_id}?logo=logo-found")
    )


def test_find_with_web_research_off_says_so_and_connects_to_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(web_research=False)
    account_id = _your_account(client)
    calls = _site(monkeypatch, acme())
    response = client.post(
        f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-research-off"
    assert calls == []


def test_find_for_an_account_with_no_website_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    account_id = _your_account(client, domain="")
    calls = _site(monkeypatch, acme())
    response = client.post(
        f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-no-website"
    assert calls == []


def test_find_keeps_an_uploaded_logo_and_connects_to_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png(48, 48))
    calls = _site(monkeypatch, acme(routes={"https://acme.example/favicon.ico": image(png())}))
    response = client.post(
        f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-uploaded-kept"
    assert calls == []
    info = service.logo_info(account_id)
    assert info is not None and info.source is LogoSource.UPLOAD
    assert client.get(f"/logos/{account_id}").content == png(48, 48)


def test_find_says_when_the_site_has_no_logo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    account_id = _your_account(client)
    calls = _site(monkeypatch, acme())
    response = client.post(
        f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-not-found"
    assert calls == [1]
    assert service.logo_info(account_id) is None
    assert client.get(f"/logos/{account_id}").status_code == 404


def test_find_says_not_found_for_a_site_that_cannot_be_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    account_id = _your_account(client)
    _site(monkeypatch, FakeSite({"https://acme.example/": reply(b"", status=500)}))
    response = client.post(
        f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-not-found"
    assert service.logo_info(account_id) is None


def test_find_says_not_found_for_a_private_website_and_connects_to_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    account_id = _your_account(client, domain="192.168.4.20")
    calls = _site(monkeypatch, acme())
    response = client.post(
        f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False
    )
    assert _code(response) == "logo-not-found"
    assert calls == []


def test_find_on_a_demo_account_is_refused_before_any_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.DEMO)
    calls = _site(monkeypatch, acme())
    response = client.post(f"/accounts/{DEMO_ID}/logo/find", headers=ORIGIN)
    assert response.status_code == 404
    assert calls == []


def test_an_unexpected_error_in_a_look_up_is_not_hidden_as_a_notice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the lookup's own refusals have codes. A bug (or any other exception) is not hidden as a
    # notice: it reaches the server's error handling.
    _on()
    account_id = _your_account(client)
    site = acme(routes={"https://acme.example/favicon.ico": raising(RuntimeError("boom"))})
    _site(monkeypatch, site)
    with pytest.raises(RuntimeError):
        client.post(f"/accounts/{account_id}/logo/find", headers=ORIGIN, follow_redirects=False)


@pytest.mark.parametrize("code", sorted(web._LOGO_NOTICES))
def test_every_logo_code_has_a_notice_on_the_account_page(client: TestClient, code: str) -> None:
    _on()
    account_id = _your_account(client)
    page = client.get(f"/accounts/{account_id}?logo={code}")
    assert page.status_code == 200
    assert web._LOGO_NOTICES[code] in _text(page)


def test_an_unknown_logo_code_shows_no_notice(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    page = _text(client.get(f"/accounts/{account_id}?logo=anything-at-all"))
    assert "anything-at-all" not in page
    assert all(message not in page for message in web._LOGO_NOTICES.values())


# --- Cross-site refusals -------------------------------------------------------------------------


def test_a_cross_site_upload_is_refused_and_keeps_nothing(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    response = _upload(client, account_id, png(), headers=CROSS_SITE)
    assert response.status_code == 403
    assert service.logo_info(account_id) is None
    assert _logo_files() == []


def test_a_cross_site_remove_and_find_are_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png())
    calls = _site(monkeypatch, acme())
    removed = client.post(f"/accounts/{account_id}/logo/remove", headers=CROSS_SITE)
    found = client.post(f"/accounts/{account_id}/logo/find", headers=CROSS_SITE)
    assert removed.status_code == 403
    assert found.status_code == 403
    assert calls == []
    assert service.logo_info(account_id) is not None


# --- The look-up-everything job ------------------------------------------------------------------


def _start_job(runner: bulk.JobRunner) -> bulk.Job:
    service.start_bulk_research(summarise=False, refresh=False)
    job = runner.wait(timeout=30)
    assert job is not None and not job.running
    return job


def test_the_job_looks_for_a_missing_logo_after_each_lookup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_job_runner: bulk.JobRunner
) -> None:
    _on()
    account_id = _your_account(client)
    site = acme(routes={"https://acme.example/apple-touch-icon.png": image(png(180, 180))})
    calls = _site(monkeypatch, site)
    job = _start_job(fresh_job_runner)
    assert job.outcomes[0].lookup is bulk.LookupResult.LOOKED_UP
    assert calls == [1, 1]  # the lookup's client, then the logo's own client
    info = service.logo_info(account_id)
    assert info is not None and info.source is LogoSource.WEBSITE


def test_the_job_keeps_an_uploaded_logo_and_fetches_no_icon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_job_runner: bulk.JobRunner
) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png(48, 48))
    site = acme(routes={"https://acme.example/apple-touch-icon.png": image(png(180, 180))})
    _site(monkeypatch, site)
    job = _start_job(fresh_job_runner)
    assert job.outcomes[0].lookup is bulk.LookupResult.LOOKED_UP
    assert not any(url.endswith(("apple-touch-icon.png", "favicon.ico")) for url in site.seen)
    info = service.logo_info(account_id)
    assert info is not None and info.source is LogoSource.UPLOAD


def test_the_job_tolerates_a_site_with_no_logo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_job_runner: bulk.JobRunner
) -> None:
    _on()
    account_id = _your_account(client)
    _site(monkeypatch, acme())
    job = _start_job(fresh_job_runner)
    assert job.outcomes[0].lookup is bulk.LookupResult.LOOKED_UP
    assert job.outcomes[0].message is None
    assert service.logo_info(account_id) is None


def test_the_job_tolerates_an_unexpected_logo_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fresh_job_runner: bulk.JobRunner,
) -> None:
    _on()
    account_id = _your_account(client)
    _site(monkeypatch, acme())

    def broken(site_client: research.SiteClient, domain: str) -> Any:
        raise RuntimeError("a bug in the logo step")

    monkeypatch.setattr(logos, "find_on_website", broken)
    job = _start_job(fresh_job_runner)
    assert job.outcomes[0].lookup is bulk.LookupResult.LOOKED_UP
    assert job.outcomes[0].message is None
    assert service.logo_info(account_id) is None


def test_the_job_looks_for_no_logo_when_the_lookup_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_job_runner: bulk.JobRunner
) -> None:
    _on()
    _your_account(client)
    site = FakeSite({"https://acme.example/": reply(b"", status=500)})
    _site(monkeypatch, site)
    job = _start_job(fresh_job_runner)
    assert job.outcomes[0].lookup is bulk.LookupResult.FAILED
    assert site.seen == ["https://acme.example/"]


def test_the_job_never_looks_for_logos_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch, fresh_job_runner: bulk.JobRunner
) -> None:
    _on(DataSource.DEMO)
    calls = _site(monkeypatch, acme())
    _start_job(fresh_job_runner)
    assert calls == []
    assert service.all_logos() == {}


# --- Deleting local data -------------------------------------------------------------------------


def test_delete_all_local_data_removes_every_logo_file(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    _upload(client, account_id, png())
    assert _logo_files() != []
    service.delete_all_local_data()
    assert not (service.get_store().root / LOGOS_DIR).exists()
    assert client.get(f"/logos/{account_id}").status_code == 404
