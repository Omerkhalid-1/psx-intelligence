import pytest

from app.services import health
from app.services.health import DependencyStatus

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _no_live_worker_probe(monkeypatch):
    monkeypatch.setattr(health, "check_workers", lambda: DependencyStatus(name="celery_workers", status="unavailable"))
    monkeypatch.setattr(health, "check_scheduler", lambda: DependencyStatus(name="scheduler", status="unknown"))


def test_overview_renders(client):
    resp = client.get("/ops/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "No market data has been ingested yet" in html
    assert "not investment advice" in html


def test_system_page_shows_failed_jobs(client, admin_headers):
    resp = client.get("/ops/system")
    assert resp.status_code == 200
    assert "No failed jobs." in resp.get_data(as_text=True)
