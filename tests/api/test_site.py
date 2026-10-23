"""Public website and its API: pages, security headers, engine endpoints, PSX news search (with a fake
news provider), rate limiting, static caching and ops-page protection. Market data is SYNTHETIC."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.api import site as site_api
from app.services.news import search
from app.services.news.google_news import RawArticle
from app.site import data as site_data
from app.utils.ratelimit import RateLimiter
from tests.factories import seed_bars

pytestmark = pytest.mark.db

LISTING = {"SYS": "Systems Limited", "FFC": "Fauji Fertilizer Company Limited", "MEBL": "Meezan Bank Limited",
           "MARI": "Mari Energies Limited", "LUCK": "Lucky Cement Limited", "LCI": "Lucky Core Industries Limited",
           "AKBL": "Askari Bank Limited", "KSE100": "KSE-100 Index"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, app):
    """No network, no shared Redis state: fixed listing, fresh caches, an in-process rate limiter."""
    monkeypatch.setattr(search, "load_listing", lambda config: dict(LISTING))
    monkeypatch.setattr(search, "SharedCache", _NoCache)
    search.clear_caches()
    site_api.clear_caches()
    site_data._meta_cache.clear()
    monkeypatch.setattr(site_api, "_limiter", RateLimiter("redis://127.0.0.1:1/0", timeout=0.05))
    yield
    search.clear_caches()
    site_api.clear_caches()


class _NoCache:
    def __init__(self, *a, **k):
        pass

    def get(self, key):
        return None

    def set(self, key, value, ttl_seconds):
        pass


@pytest.fixture()
def seeded(db_session):
    seed_bars(n=400)
    return db_session


def _scripts_are_external(html: str) -> bool:
    """CSP forbids inline scripts: every <script> must load a file, except the JSON data block."""
    for tag in re.findall(r"<script\b[^>]*>", html):
        if 'type="application/json"' in tag:
            continue
        if "src=" not in tag:
            return False
    return True


@pytest.mark.parametrize("path", ["/", "/stock/SYS", "/stock/KSE100", "/learn", "/learn?symbol=FFC", "/news",
                                  "/news?symbol=LUCK", "/engine"])
def test_pages_render_with_strict_csp(client, seeded, path):
    resp = client.get(path)
    assert resp.status_code == 200
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    html = resp.get_data(as_text=True)
    assert _scripts_are_external(html)
    assert 'id="stocks-menu"' in html and "TradingView" in html  # navigation and chart-library attribution


def test_home_lists_watchlist_with_indicator_matrix(client, seeded):
    html = client.get("/").get_data(as_text=True)
    for s in ("SYS", "FFC", "MEBL", "MARI"):
        assert f'href="/stock/{s}"' in html
    assert 'class="matrix"' in html and "Indicator balance" in html.replace("INDICATOR BALANCE", "Indicator balance")


def test_stock_page_shows_explained_readings(client, seeded):
    html = client.get("/stock/SYS").get_data(as_text=True)
    assert "How it works" in html and "Rule used here" in html and "RSI (14)" in html


def test_unknown_stock_is_a_site_404_and_api_404_is_json(client, seeded):
    resp = client.get("/stock/NOPE")
    assert resp.status_code == 404 and "Signal" in resp.get_data(as_text=True)
    api = client.get("/api/v1/does-not-exist")
    assert api.status_code == 404 and api.get_json()["errors"][0]["code"] == "not_found"


def test_engine_api(client, seeded):
    body = client.get("/api/v1/engine/SYS?series=true&limit=120").get_json()["data"]
    assert body["status"] == "ok" and body["engine_version"] == "ta_engine_v1"
    assert len(body["series"]["time"]) == 120 and body["readings"]
    assert client.get("/api/v1/engine/LUCK").status_code == 404  # listed, but not on the tracked watchlist
    assert client.get("/api/v1/engine/SYS?limit=5").status_code == 400
    params = client.get("/api/v1/engine/parameters").get_json()["data"]
    assert len(params["parameters"]) >= 20 and "trend" in params["groups"]


def test_engine_diagnostics_has_no_failures_on_clean_data(client, seeded):
    report = client.get("/api/v1/engine/diagnostics").get_json()["data"]
    assert report["summary"]["fail"] == 0
    names = {c["name"] for c in report["checks"]}
    assert {"rsi_reference", "no_look_ahead", "identities", "bounds", "determinism"} <= names


class FakeNews:
    calls = 0

    def __init__(self, *a, **k):
        pass

    def search(self, query, *, after=None, before=None):
        FakeNews.calls += 1
        now = datetime.now(UTC)
        return [
            RawArticle("Lucky Cement posts record profit", "https://example.test/1", "Dawn", "https://www.dawn.com",
                       now - timedelta(days=1), "exact"),
            RawArticle("Lucky Core Industries announces payout", "https://example.test/2", "Dawn",
                       "https://www.dawn.com", now - timedelta(days=2), "exact"),
            RawArticle("LUCK Share Price Today - Lucky Cement", "https://example.test/3", "Quotes", "https://quotes.test",
                       now - timedelta(days=1), "exact"),
            RawArticle("Lucky Cement opens plant in Iraq", "https://example.test/4", "Global", "https://global.test",
                       now - timedelta(days=3), "exact"),  # no Pakistani context
        ]


def test_news_search_returns_only_that_company(client, seeded, monkeypatch):
    """LUCK has curated rules (config/news.yaml): look-alike group companies and quote pages are dropped."""
    monkeypatch.setattr(search, "GoogleNewsRss", FakeNews)
    body = client.get("/api/v1/news/search?symbol=luck").get_json()["data"]
    assert body["symbol"] == "LUCK" and body["company"] == "Lucky Cement Limited"
    assert [r["title"] for r in body["results"]] == ["Lucky Cement posts record profit",
                                                     "Lucky Cement opens plant in Iraq"]
    assert body["counts"]["rejected_as_unrelated"] >= 2
    assert "Lucky Core" in body["match_rules"]["excluded_names"]


class FakeBankNews(FakeNews):
    def search(self, query, *, after=None, before=None):
        now = datetime.now(UTC)
        return [
            RawArticle("Askari Bank posts higher profit", "https://example.test/a1", "Dawn", "https://www.dawn.com",
                       now - timedelta(days=1), "exact"),
            RawArticle("Askari Bank opens new branch", "https://example.test/a2", "Elsewhere", "https://news.example",
                       now - timedelta(days=1), "exact"),  # no Pakistani publisher or keyword
        ]


def test_derived_rules_require_pakistani_context(client, seeded, monkeypatch):
    """A symbol without curated rules gets rules from its listed name, and they require Pakistani context."""
    monkeypatch.setattr(search, "GoogleNewsRss", FakeBankNews)
    body = client.get("/api/v1/news/search?symbol=AKBL").get_json()["data"]
    assert body["match_rules"]["requires_pakistan_context"] is True
    assert body["match_rules"]["aliases"][0] == "Askari Bank"
    assert [r["title"] for r in body["results"]] == ["Askari Bank posts higher profit"]


def test_news_search_errors_and_rate_limit(client, seeded, monkeypatch, app):
    monkeypatch.setattr(search, "GoogleNewsRss", FakeNews)
    assert client.get("/api/v1/news/search?symbol=BAD!").status_code == 400
    assert client.get("/api/v1/news/search?symbol=ZZZZ").status_code == 404
    monkeypatch.setitem(app.config, "NEWS_SEARCH_PER_IP_PER_MINUTE", 2)
    site_api.clear_caches()
    monkeypatch.setattr(site_api, "_limiter", RateLimiter("redis://127.0.0.1:1/0", timeout=0.05))
    codes = [client.get("/api/v1/news/search?symbol=LUCK").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_symbol_suggestions(client, seeded):
    data = client.get("/api/v1/symbols/search?q=luc").get_json()["data"]
    assert data[0]["symbol"] == "LUCK" and {d["symbol"] for d in data} >= {"LUCK", "LCI"}


def test_static_cache_policy(client):
    plain = client.get("/static/site/js/core.js")
    assert plain.status_code == 200 and "max-age=0" in plain.headers["Cache-Control"]
    versioned = client.get("/static/site/js/core.js?v=abc123")
    assert "max-age=31536000" in versioned.headers["Cache-Control"]


def test_ops_dashboard_requires_basic_auth_when_enabled(client, seeded, monkeypatch, app):
    monkeypatch.setitem(app.config, "OPS_DASHBOARD_AUTH", True)
    assert client.get("/ops/").status_code == 401
    import base64

    good = base64.b64encode(f"admin:{app.config['ADMIN_API_TOKEN']}".encode()).decode()
    bad = base64.b64encode(b"admin:wrong").decode()
    assert client.get("/ops/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert client.get("/ops/", headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert client.get("/").status_code == 200  # the public site is unaffected
