"""Rendered pages must not reuse a cached script from an earlier deployment."""

from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
import pytest

from app.services import static_assets
from app.utils.datetime_fmt import as_utc_iso


_APP_DIR = Path(__file__).resolve().parents[1] / "app"


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.sources.extend(value for key, value in attrs if key == "src")


def _encoder_script(html):
    scripts = _Scripts()
    scripts.feed(html)
    matches = [src for src in scripts.sources
               if urlsplit(src).path == "/static/automatic_encoder.js"]
    assert len(matches) == 1
    return matches[0]


def _page_client(static_root):
    """Render the actual pages and serve assets through the production mount type."""
    application = FastAPI()
    templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))
    templates.env.globals["static_asset_url"] = static_assets.static_asset_url
    templates.env.filters["as_utc_iso"] = as_utc_iso
    application.mount("/static", StaticFiles(directory=str(static_root)), name="static")

    @application.get("/{page}")
    def page(request: Request, page: str):
        template = {"releases": "releases.html", "automatic-encoder": "automatic_encoder.html"}[page]
        return templates.TemplateResponse(request, template, {
            "groups": [], "releases": [], "queue": [], "counts": {},
            "page": 1, "total": 0, "total_pages": 1, "search": "",
            "source_filter": "", "block_filter": "", "abandoned_filter": "",
            "live_query": "page=1", "release_id": None,
        })

    return TestClient(application)


@pytest.mark.parametrize("page", ["releases", "automatic-encoder"])
def test_encoder_pages_version_the_script_the_static_server_actually_serves(page):
    with _page_client(static_assets.STATIC_ROOT) as client:
        response = client.get("/" + page)
        assert response.status_code == 200
        script_url = _encoder_script(response.text)
        script = client.get(script_url)

    assert script.status_code == 200
    assert script.content == (_APP_DIR / "static/automatic_encoder.js").read_bytes()
    assert parse_qs(urlsplit(script_url).query) == {
        "v": [sha256(script.content).hexdigest()[:16]],
    }


@pytest.mark.parametrize("page", ["releases", "automatic-encoder"])
def test_same_filename_deployment_bypasses_the_previous_browser_cache(page, tmp_path, monkeypatch):
    """A browser can keep the old URL cached, including across equal stat metadata."""
    old_script = b"window.encoderVersion = 'old';\n"
    new_script = b"window.encoderVersion = 'new';\n"
    script = tmp_path / "automatic_encoder.js"
    script.write_bytes(old_script)
    original_stat = script.stat()
    monkeypatch.setattr(static_assets, "STATIC_ROOT", tmp_path)
    browser_cache = {}

    with _page_client(tmp_path) as client:
        old_url = _encoder_script(client.get("/" + page).text)
        browser_cache[old_url] = client.get(old_url).content
        assert browser_cache[old_url] == old_script

        # Replacing an image may preserve timestamp and length; bytes identify it.
        script.write_bytes(new_script)
        os.utime(script, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        assert script.stat().st_size == original_stat.st_size
        new_url = _encoder_script(client.get("/" + page).text)
        assert new_url != old_url
        assert new_url not in browser_cache
        browser_cache[new_url] = client.get(new_url).content

        # A subsequent page load reuses the current cache entry without churn.
        assert _encoder_script(client.get("/" + page).text) == new_url

    assert browser_cache[old_url] == old_script
    assert browser_cache[new_url] == new_script


def test_versioned_url_quotes_nested_names_and_round_trips_through_static_files(tmp_path, monkeypatch):
    script = tmp_path / "nested" / "кодировщик #1?.js"
    script.parent.mkdir()
    script.write_bytes(b"window.encoderVersion = 'current';\n")
    monkeypatch.setattr(static_assets, "STATIC_ROOT", tmp_path)

    url = static_assets.static_asset_url("nested/кодировщик #1?.js")
    assert " " not in url and "#" not in url
    with _page_client(tmp_path) as client:
        response = client.get(url)
    assert response.status_code == 200
    assert response.content == script.read_bytes()


def test_versioned_asset_rejects_paths_outside_the_served_directory(tmp_path, monkeypatch):
    served = tmp_path / "static"
    served.mkdir()
    outside = tmp_path / "outside.js"
    outside.write_text("This is not a static asset", encoding="utf-8")
    (served / "link.js").symlink_to(outside)
    monkeypatch.setattr(static_assets, "STATIC_ROOT", served)

    for filename in ("../outside.js", str(outside), "link.js"):
        with pytest.raises(ValueError):
            static_assets.static_asset_url(filename)
