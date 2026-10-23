"""Static (GitHub Pages) export: structure, base-path links, static-mode markup, link checking and the
output-directory safety rule. Market data is SYNTHETIC; no network (listing patched, news skipped)."""

import json
import re

import pytest

from app.api import site as site_api
from app.services.news import search
from app.site import data as site_data
from app.site.export import MARKER, ExportError, export_site, normalize_base
from tests.factories import seed_bars

pytestmark = pytest.mark.db


@pytest.fixture()
def exported(app, db_session, tmp_path, monkeypatch):
    listing = {"SYS": "Systems Limited", "LUCK": "Lucky Cement Limited", "KSE100": "KSE-100 Index"}
    monkeypatch.setattr(search, "load_listing", lambda config: dict(listing))
    search.clear_caches()
    site_api.clear_caches()
    site_data._meta_cache.clear()
    monkeypatch.setitem(app.config, "SITE_STATIC_EXPORT", False)
    seed_bars(n=400)
    out = tmp_path / "site"
    report = export_site(app, out, "/demo", with_news=False)
    yield out, report
    app.config.pop("SITE_STATIC_EXPORT", None)
    app.config.pop("SITE_BUILD_INFO", None)
    site_api.clear_caches()


def test_export_writes_every_page_and_data_file(exported, app):
    out, report = exported
    symbols = app.config["PSXDATA_SYMBOLS"]
    assert report["pages"] == 4 + len(symbols) and report["warnings"] == []
    for rel in ["index.html", "learn/index.html", "news/index.html", "engine/index.html", "404.html", ".nojekyll",
                "robots.txt", "build-info.json", "data/diagnostics.json", "data/symbols.json", MARKER,
                "static/site/js/core.js",
                "static/vendor/lightweight-charts-4.2.3/lightweight-charts.standalone.production.js"]:
        assert (out / rel).exists(), rel
    for s in symbols:
        assert (out / f"stock/{s}/index.html").exists()
        engine = json.loads((out / f"data/engine/{s}.json").read_text())["data"]
        assert engine["status"] == "ok" and len(engine["series"]["time"]) > 60
    listing = json.loads((out / "data/symbols.json").read_text())["data"]
    assert {"symbol": "LUCK", "name": "Lucky Cement Limited", "tracked": False} in listing


def test_exported_pages_are_static_and_base_prefixed(exported):
    out, _ = exported
    home = (out / "index.html").read_text()
    assert 'data-static="1"' in home and 'data-base="/demo"' in home
    assert '<meta http-equiv="Content-Security-Policy"' in home and "frame-ancestors" not in home.split("</head>")[0]
    assert 'href="/demo/stock/SYS/"' in home and 'href="/demo/learn/"' in home  # directory links, no redirect
    assert "/demo/static/site/css/site.css" in home
    assert "Operations" not in home and "/ops" not in home  # no server-only pages in the static edition
    assert "Daily snapshot" in home
    assert not re.search(r'(href|src)="/(?!demo/)[^/"]', home)  # nothing escapes the base path


def test_export_refuses_to_overwrite_foreign_directory(app, db_session, tmp_path):
    seed_bars(n=120)
    foreign = tmp_path / "important"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("keep me")
    with pytest.raises(ExportError, match="refusing"):
        export_site(app, foreign, "", with_news=False)
    assert (foreign / "notes.txt").read_text() == "keep me"


def test_export_requires_market_data(app, db_session, tmp_path):
    with pytest.raises(ExportError, match="no market data"):
        export_site(app, tmp_path / "x", "", with_news=False)


@pytest.mark.parametrize("raw,expected", [("", ""), ("/", ""), ("psx-intelligence", "/psx-intelligence"),
                                          ("/a/b/", "/a/b")])
def test_normalize_base(raw, expected):
    assert normalize_base(raw) == expected


@pytest.mark.parametrize("raw", ["/a b", "/../x", "/x?y", '/"><script>'])
def test_normalize_base_rejects_unsafe(raw):
    with pytest.raises(ExportError):
        normalize_base(raw)
