"""Ingestion and market-data endpoints, dashboard rendering and health reporting (psxdata)."""

import uuid
from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy import update

from app.models.market_data import DailyBar, IngestionRun
from app.models.operations import JobRun, JobStatus
from app.services import health
from app.services.health import DependencyStatus
from app.utils.time import psx_local_date, utcnow
from tests.fakes import INDICES, bar, bars_body, err_body, ok_body, quote_body

pytestmark = pytest.mark.db

SYNC = "/api/v1/ingestion/runs"
SYMBOLS = ("SYS", "FFC", "MEBL", "MARI", "KSE100")
# Bars are dated relative to today (the latest weekday strictly before it) so freshness checks do not
# depend on the date the suite runs.
LATEST = str(np.busday_offset(psx_local_date(utcnow()), -1, roll="forward"))
PREV = str(np.busday_offset(LATEST, -1))
ROUTES = {
    "/health": (200, ok_body({"status": "ok"})),
    "/indices": (200, INDICES),
    **{f"/stocks/{s}/historical": (200, bars_body(bar(PREV, 100 + i), bar(LATEST, 101.5 + i)))
       for i, s in enumerate(SYMBOLS)},
    **{f"/stocks/{s}/quote": (200, quote_body(s, 102 + i, change_pct=-0.34)) for i, s in enumerate(SYMBOLS[:4])},
}


@pytest.fixture(autouse=True)
def _stub_probes(monkeypatch):
    monkeypatch.setattr(health, "check_workers", lambda: DependencyStatus(name="celery_workers", status="ok"))
    monkeypatch.setattr(health, "check_scheduler", lambda: DependencyStatus(name="scheduler", status="ok"))


def _sync(client, headers, body=None):
    return client.post(SYNC, json=body or {}, headers=headers)


def _load(client, headers):
    assert _sync(client, headers, {"dataset": "daily_bars"}).status_code == 202
    assert _sync(client, headers, {"dataset": "quotes"}).status_code == 202


# -- trigger protection ----------------------------------------------------------------------------------


def test_sync_requires_admin_and_never_calls_provider_unauthenticated(client, routed_http):
    adapter = routed_http(ROUTES)
    assert client.post(SYNC, json={}).status_code == 401
    assert client.post(SYNC, json={}, headers={"Authorization": "Bearer nope"}).status_code == 403
    assert adapter.requests == []


def test_sync_enqueues_and_persists(client, admin_headers, routed_http):
    routed_http(ROUTES)
    resp = _sync(client, admin_headers, {"dataset": "daily_bars", "full_history": True})
    assert resp.status_code == 202
    job = client.get(resp.get_json()["meta"]["poll"]).get_json()["data"]
    assert job["status"] == "succeeded" and job["result_summary"]["stored"] == 10
    assert job["parameters"] == {"full_history": True}


@pytest.mark.parametrize("body,code", [
    ({"dataset": "tickers"}, "invalid_body"),
    ({"url": "http://evil.example"}, "invalid_body"),
    ({"dataset": "quotes", "full_history": True}, "invalid_body"),
    ({"symbols": ["NOPE"]}, "unknown_symbols"),
])
def test_sync_validation(client, admin_headers, routed_http, body, code):
    routed_http(ROUTES)
    resp = _sync(client, admin_headers, body)
    assert resp.status_code == 400 and resp.get_json()["errors"][0]["code"] == code


def test_sync_refused_when_disabled(client, admin_headers, routed_http, app, monkeypatch):
    adapter = routed_http(ROUTES)
    monkeypatch.setitem(app.config, "PSXDATA_ENABLED", False)
    resp = _sync(client, admin_headers)
    assert resp.status_code == 503 and resp.get_json()["errors"][0]["code"] == "integration_not_configured"
    assert adapter.requests == []


def test_sync_refused_while_active_and_during_cooldown(client, admin_headers, routed_http, db_session):
    routed_http(ROUTES)
    active = JobRun(id=uuid.uuid4(), job_type="ingestion.psx_quotes", status=JobStatus.RUNNING,
                    correlation_id="c" * 16, requested_by="test", parameters={})
    db_session.add(active)
    db_session.commit()
    assert _sync(client, admin_headers, {"dataset": "quotes"}).status_code == 409
    assert _sync(client, admin_headers, {"dataset": "daily_bars"}).status_code == 202  # datasets independent
    resp = _sync(client, admin_headers, {"dataset": "daily_bars"})
    assert resp.status_code == 429 and resp.get_json()["errors"][0]["code"] == "sync_cooldown"


# -- reads -----------------------------------------------------------------------------------------------------


def test_status_and_runs(client, admin_headers, routed_http):
    routed_http(ROUTES)
    _load(client, admin_headers)
    data = client.get("/api/v1/ingestion/status").get_json()["data"]
    assert data["integration"]["ready"] is True
    assert set(data["datasets"]) == {"daily_bars", "quotes"}
    assert data["datasets"]["quotes"]["state"] == "fresh"
    runs = client.get("/api/v1/ingestion/runs?dataset=daily_bars").get_json()
    assert runs["meta"]["total"] == 1
    detail = client.get(f"/api/v1/ingestion/runs/{runs['data'][0]['id']}").get_json()["data"]
    assert detail["request_metadata"]["symbols"]["SYS"]["inserted"] == 2
    assert client.get("/api/v1/ingestion/runs/not-a-uuid").status_code == 400
    assert client.get(f"/api/v1/ingestion/runs/{uuid.uuid4()}").status_code == 404


def test_empty_state(client):
    body = client.get("/api/v1/stocks").get_json()
    assert all(r["latest_bar"] is None and r["latest_quote"] is None for r in body["data"])
    assert body["meta"]["data_status"] == {"daily_bars": "no_data", "quotes": "no_data"}
    assert body["warnings"]


def test_stocks_output(client, admin_headers, routed_http):
    routed_http(ROUTES)
    _load(client, admin_headers)
    body = client.get("/api/v1/stocks").get_json()
    rows = {r["symbol"]: r for r in body["data"]}
    assert [r["symbol"] for r in body["data"]][0] == "KSE100"  # indices first
    assert rows["KSE100"]["instrument_kind"] == "index" and rows["KSE100"]["latest_quote"] is None
    sys = rows["SYS"]
    assert sys["instrument_kind"] == "stock"
    assert sys["latest_bar"]["session_date"] == LATEST
    assert sys["latest_bar"]["close"] == "101.500000" and sys["latest_bar"]["day_change"] == "1.500000"
    assert sys["latest_bar"]["day_change_pct"] == "1.50"
    assert sys["latest_quote"]["price"] == "102.000000" and sys["latest_quote"]["change_pct"] == "-0.340000"
    assert "unadjusted" in body["meta"]["note"]


def test_history_endpoint(client, admin_headers, routed_http):
    routed_http(ROUTES)
    _load(client, admin_headers)
    body = client.get(f"/api/v1/stocks/sys/history?start={LATEST}").get_json()
    assert [r["session_date"] for r in body["data"]] == [LATEST]
    assert body["meta"]["total_stored"] == 2 and body["meta"]["adjusted"] is False
    assert client.get("/api/v1/stocks/NOPE/history").status_code == 404
    assert client.get("/api/v1/stocks/SYS/history?limit=99999").status_code == 400
    assert client.get(f"/api/v1/stocks/SYS/history?start={LATEST}&end=2026-01-01").status_code == 400


def test_quotes_endpoint(client, admin_headers, routed_http):
    routed_http(ROUTES)
    _load(client, admin_headers)
    body = client.get("/api/v1/market/quotes").get_json()
    assert sorted(r["symbol"] for r in body["data"]) == ["FFC", "MARI", "MEBL", "SYS"]
    assert body["meta"]["data_status"] == "fresh"


def _age(db_session, *, quotes_minutes=None, bars_days=None):
    if quotes_minutes is not None:
        past = utcnow() - timedelta(minutes=quotes_minutes)
        db_session.execute(update(IngestionRun).values(finished_at=past, started_at=past))
    if bars_days is not None:
        db_session.execute(update(DailyBar).values(session_date=DailyBar.session_date - timedelta(days=bars_days)))
    db_session.commit()


def test_stale_data_flagged_everywhere(client, admin_headers, routed_http, db_session):
    routed_http(ROUTES)
    _load(client, admin_headers)
    _age(db_session, quotes_minutes=120, bars_days=14)
    stocks = client.get("/api/v1/stocks").get_json()
    assert stocks["meta"]["data_status"] == {"daily_bars": "stale", "quotes": "stale"}
    assert len(stocks["data"]) == 5  # stale data is still shown, but labelled
    warnings = client.get("/api/v1/health/dependencies").get_json()["warnings"]
    assert any("daily_bars: stale" in w for w in warnings) and any("quotes: stale" in w for w in warnings)
    html = client.get("/ops/").get_data(as_text=True)
    assert "daily bars stale:" in html and "quotes stale:" in html


def test_health_reports_service_and_failed_sync(client, admin_headers, routed_http):
    routes = dict(ROUTES)
    routes["/stocks/SYS/historical"] = (502, err_body(502, "upstream_data_error"))
    routed_http(routes)
    _sync(client, admin_headers, {"dataset": "daily_bars", "symbols": ["SYS"]})
    body = client.get("/api/v1/health/dependencies").get_json()
    deps = {d["name"]: d["status"] for d in body["data"]["dependencies"]}
    assert deps["psxdata_api"] == "ok"
    assert body["data"]["data_sources"][0]["datasets"]["daily_bars"]["last_run"]["error_category"] == \
        "upstream_data_error"
    assert any("last sync failed" in w for w in body["warnings"])


def test_health_service_down(client, routed_http):
    routes = dict(ROUTES)
    routes["/health"] = (503, err_body(503, "psx_unavailable"))
    routed_http(routes)
    body = client.get("/api/v1/health/dependencies").get_json()
    assert {d["name"]: d["status"] for d in body["data"]["dependencies"]}["psxdata_api"] == "unavailable"
    assert body["data"]["status"] == "degraded"


# -- dashboard ---------------------------------------------------------------------------------------------------


def test_overview_with_data(client, admin_headers, routed_http):
    routed_http(ROUTES)
    _load(client, admin_headers)
    html = client.get("/ops/").get_data(as_text=True)
    assert "source: psxdata-api" in html
    for symbol in SYMBOLS:
        assert f"<strong>{symbol}</strong>" in html
    assert "not available for indices" in html
    assert "+1.50%" in html and "-0.34%" in html
    assert "daily bars: fresh" in html
    assert "plotly" in html and 'id="chart-SYS"' in html
    assert "unadjusted" in html


def test_overview_not_configured(client, app, monkeypatch):
    monkeypatch.setitem(app.config, "PSXDATA_ENABLED", False)
    html = client.get("/ops/").get_data(as_text=True)
    assert "No market data has been ingested yet" in html and "PSXDATA_ENABLED is false" in html


def test_overview_failed_sync_shows_last_data(client, admin_headers, routed_http, db_session):
    routed_http(ROUTES)
    _load(client, admin_headers)
    _age(db_session, quotes_minutes=5)  # step past the manual-sync cooldown
    routes = dict(ROUTES)
    routes["/indices"] = (503, err_body(503, "psx_unavailable"))
    routes.update({f"/stocks/{s}/historical": (503, err_body(503, "psx_unavailable")) for s in SYMBOLS})
    routed_http(routes)
    _sync(client, admin_headers, {"dataset": "daily_bars"})
    html = client.get("/ops/").get_data(as_text=True)
    assert "most recent daily bars sync failed" in html
    assert "<strong>SYS</strong>" in html and "101.50" in html  # last stored data still shown


def test_overview_database_error_state(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.dashboard import routes

    def broken(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("db down"))

    monkeypatch.setattr(routes, "symbol_summaries", broken)
    assert "Market data could not be loaded" in client.get("/ops/").get_data(as_text=True)


def test_system_page_data_sources(client, admin_headers, routed_http):
    routed_http(ROUTES)
    _load(client, admin_headers)
    html = client.get("/ops/system").get_data(as_text=True)
    assert "Data sources" in html and "daily bars" in html and "quotes" in html


def test_pages_never_call_provider(client, admin_headers, routed_http):
    adapter = routed_http(ROUTES)
    _load(client, admin_headers)
    data_calls = lambda: [r for r in adapter.requests if "/stocks/" in r.url or "/indices" in r.url]  # noqa: E731
    before = len(data_calls())
    for url in ("/", "/system", "/api/v1/stocks", "/api/v1/market/quotes", "/api/v1/stocks/SYS/history"):
        client.get(url)
    assert len(data_calls()) == before  # only /health probes, never data endpoints
