import pytest

from app.services import health
from app.services.health import DependencyStatus

pytestmark = pytest.mark.db


def _envelope_ok(body):
    assert set(body) == {"data", "meta", "warnings", "errors"}
    assert body["meta"]["correlation_id"]


def test_liveness(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.get_json()
    _envelope_ok(body)
    assert body["data"]["status"] == "ok"
    assert resp.headers["X-Correlation-ID"]


def test_correlation_id_is_propagated_when_safe(client):
    resp = client.get("/api/v1/health", headers={"X-Correlation-ID": "abcdef0123456789"})
    assert resp.headers["X-Correlation-ID"] == "abcdef0123456789"
    resp = client.get("/api/v1/health", headers={"X-Correlation-ID": "bad id; drop <script>"})
    assert resp.headers["X-Correlation-ID"] != "bad id; drop <script>"


def test_dependencies_reports_each_dependency(client, monkeypatch):
    monkeypatch.setattr(health, "check_workers", lambda: DependencyStatus(name="celery_workers", status="unavailable",
                                                                          detail="no Celery workers responded"))
    monkeypatch.setattr(health, "check_scheduler", lambda: DependencyStatus(name="scheduler", status="unknown"))
    resp = client.get("/api/v1/health/dependencies")
    body = resp.get_json()
    names = {d["name"]: d["status"] for d in body["data"]["dependencies"]}
    assert names["postgresql"] == "ok"
    assert names["celery_workers"] == "unavailable"
    # DB up but workers down: degraded, still served (200), with explicit warnings.
    assert body["data"]["status"] == "degraded"
    assert resp.status_code == 200
    assert any("celery_workers" in w for w in body["warnings"])


def test_dependencies_503_when_database_down(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda: DependencyStatus(name="postgresql", status="unavailable"))
    monkeypatch.setattr(health, "check_workers", lambda: DependencyStatus(name="celery_workers", status="ok"))
    monkeypatch.setattr(health, "check_scheduler", lambda: DependencyStatus(name="scheduler", status="ok"))
    resp = client.get("/api/v1/health/dependencies")
    assert resp.status_code == 503
    assert resp.get_json()["data"]["status"] == "unavailable"


def test_unknown_api_route_returns_json_envelope(client):
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["errors"][0]["code"] == "not_found"


def test_security_headers(client):
    resp = client.get("/api/v1/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
