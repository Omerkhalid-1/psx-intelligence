"""Manual "refresh latest data": admin protection, single-flight, cooldown, and failure isolation.

Ingestion and the pipeline are replaced by fakes here; their own behaviour is tested elsewhere.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.models.operations import JobRun, JobStatus
from app.services import refresh

FAKE_SUMMARY = {"status": "succeeded", "quotes": {"status": "succeeded", "stored": 4},
                "pipeline": {"status": "skipped", "session": "2026-10-13", "steps": {"news": "succeeded"},
                             "new_session": False}}


@pytest.fixture()
def fake_refresh(monkeypatch):
    calls = []

    def fake(config, *, job_run_id=None):
        calls.append(job_run_id)
        return FAKE_SUMMARY

    monkeypatch.setattr(refresh, "run_refresh", fake)
    return calls


@pytest.mark.db
def test_refresh_requires_admin_token(client, fake_refresh):
    assert client.post("/api/v1/refresh", json={}).status_code == 401
    assert client.post("/api/v1/refresh", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert fake_refresh == []


@pytest.mark.db
def test_refresh_rejects_unexpected_fields(client, admin_headers, fake_refresh):
    resp = client.post("/api/v1/refresh", json={"force": True}, headers=admin_headers)
    assert resp.status_code == 400
    assert fake_refresh == []


@pytest.mark.db
def test_refresh_runs_as_tracked_job(client, admin_headers, fake_refresh):
    resp = client.post("/api/v1/refresh", json={}, headers=admin_headers)
    assert resp.status_code == 202
    body = resp.get_json()
    job_id = body["data"]["id"]
    assert body["meta"]["poll"] == f"/api/v1/jobs/{job_id}"
    # Celery runs eagerly in tests: the job has finished; poll it like the dashboard does.
    job = client.get(f"/api/v1/jobs/{job_id}").get_json()["data"]
    assert job["status"] == "succeeded"
    assert job["result_summary"] == FAKE_SUMMARY
    assert fake_refresh == [uuid.UUID(job_id)]
    listed = client.get("/api/v1/jobs?job_type=refresh.latest&page_size=1").get_json()["data"]
    assert listed[0]["id"] == job_id


@pytest.mark.db
def test_refresh_cooldown(client, admin_headers, fake_refresh):
    first = client.post("/api/v1/refresh", json={}, headers=admin_headers).get_json()["data"]["id"]
    resp = client.post("/api/v1/refresh", json={}, headers=admin_headers)
    assert resp.status_code == 429
    err = resp.get_json()["errors"][0]
    assert err["code"] == "refresh_cooldown" and err["details"]["job_id"] == first
    assert len(fake_refresh) == 1


@pytest.mark.db
def test_refresh_returns_job_already_in_flight(client, admin_headers, db_session, fake_refresh):
    active = JobRun(job_type=refresh.REFRESH_JOB, status=JobStatus.RUNNING, correlation_id="c" * 32,
                    requested_by="admin", parameters={})
    db_session.add(active)
    db_session.commit()
    resp = client.post("/api/v1/refresh", json={}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["id"] == str(active.id)
    assert resp.get_json()["meta"]["deduplicated"] is True
    assert fake_refresh == []


@pytest.mark.db
def test_dashboard_pages_include_refresh_control(client):
    html = client.get("/ops/").get_data(as_text=True)
    assert 'id="refresh-bar"' in html and "js/refresh.js" in html
    assert client.get("/static/js/refresh.js").status_code == 200


# ---- run_refresh: one part failing must not stop the other ----------------------------------------------


def _patch_parts(monkeypatch, *, quotes, pipeline, ready=True):
    from app.services import market_data
    from app.services import pipeline as pl
    from app.services.ingestion import psx

    problems = [] if ready else ["PSXDATA_ENABLED is false"]
    monkeypatch.setattr(market_data, "integration_status",
                        lambda config: SimpleNamespace(ready=ready, problems=problems))
    monkeypatch.setattr(psx, "run_quote_ingestion", quotes)
    monkeypatch.setattr(pl, "run_daily_pipeline", pipeline)


def _quotes_ok(config, *, job_run_id=None):
    return SimpleNamespace(status=SimpleNamespace(value="succeeded"), records_stored=4, id=uuid.uuid4(),
                           error_summary=None)


def _pipeline_ok(config, *, job_run_id=None):
    from datetime import date

    steps = [{"step": "market_data", "status": "succeeded"}, {"step": "features", "status": "succeeded"}]
    return SimpleNamespace(status="succeeded", id=uuid.uuid4(), session_date=date(2026, 10, 14), steps=steps)


def _boom(*args, **kwargs):
    raise RuntimeError("provider down token=supersecret")


def test_run_refresh_success(monkeypatch):
    _patch_parts(monkeypatch, quotes=_quotes_ok, pipeline=_pipeline_ok)
    out = refresh.run_refresh({})
    assert out["status"] == "succeeded"
    assert out["pipeline"]["new_session"] is True and out["pipeline"]["session"] == "2026-10-14"


def test_run_refresh_quote_failure_still_runs_pipeline(monkeypatch):
    _patch_parts(monkeypatch, quotes=_boom, pipeline=_pipeline_ok)
    out = refresh.run_refresh({})
    assert out["quotes"]["status"] == "failed" and out["pipeline"]["status"] == "succeeded"
    assert out["status"] == "partial"


def test_run_refresh_both_fail(monkeypatch):
    _patch_parts(monkeypatch, quotes=_boom, pipeline=_boom)
    out = refresh.run_refresh({})
    assert out["status"] == "failed"
    assert "supersecret" not in out["quotes"]["message"] + out["pipeline"]["message"]


def test_run_refresh_skips_quotes_when_integration_disabled(monkeypatch):
    _patch_parts(monkeypatch, quotes=_boom, pipeline=_pipeline_ok, ready=False)
    out = refresh.run_refresh({})
    assert out["quotes"]["status"] == "skipped" and "PSXDATA_ENABLED" in out["quotes"]["message"]
    assert out["status"] == "succeeded"


def test_run_refresh_pipeline_busy_is_not_a_failure(monkeypatch):
    from app.services.pipeline import PipelineBusy

    def busy(config, *, job_run_id=None):
        raise PipelineBusy("a pipeline run is already in progress")

    _patch_parts(monkeypatch, quotes=_quotes_ok, pipeline=busy)
    out = refresh.run_refresh({})
    assert out["pipeline"]["status"] == "skipped" and out["status"] == "succeeded"
