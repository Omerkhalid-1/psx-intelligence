import uuid

import pytest
from sqlalchemy import select

from app.models.operations import AuditEvent, JobRun, JobStatus

pytestmark = pytest.mark.db


def test_submit_requires_token(client):
    resp = client.post("/api/v1/jobs", json={"job_type": "system.self_check"})
    assert resp.status_code == 401
    assert resp.get_json()["errors"][0]["code"] == "unauthorized"


def test_submit_rejects_wrong_token(client):
    resp = client.post("/api/v1/jobs", json={"job_type": "system.self_check"},
                       headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 403


def test_admin_disabled_without_configured_token(app, client):
    original = app.config["ADMIN_API_TOKEN"]
    app.config["ADMIN_API_TOKEN"] = None
    try:
        resp = client.post("/api/v1/jobs", json={"job_type": "system.self_check"},
                           headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 503
    finally:
        app.config["ADMIN_API_TOKEN"] = original


def test_submit_unknown_job_type(client, admin_headers):
    resp = client.post("/api/v1/jobs", json={"job_type": "rm.rf"}, headers=admin_headers)
    assert resp.status_code == 400
    assert resp.get_json()["errors"][0]["code"] == "unknown_job_type"


def test_submit_rejects_extra_fields_and_non_json(client, admin_headers):
    resp = client.post("/api/v1/jobs", json={"job_type": "system.self_check", "sql": "drop"}, headers=admin_headers)
    assert resp.status_code == 400
    resp = client.post("/api/v1/jobs", data="not json", headers=admin_headers)
    assert resp.status_code == 400


def test_submit_runs_job_and_records_audit(client, admin_headers, db_session):
    resp = client.post("/api/v1/jobs", json={"job_type": "system.self_check"}, headers=admin_headers)
    assert resp.status_code == 202
    job_id = resp.get_json()["data"]["id"]

    # Tests run Celery eagerly, so the job has completed by now; poll the status endpoint like a client would.
    status = client.get(f"/api/v1/jobs/{job_id}").get_json()["data"]
    assert status["status"] == "succeeded"
    assert status["attempts"] == 1
    assert status["result_summary"]["database"] == "ok"
    assert status["duration_ms"] is not None

    audit = db_session.scalars(select(AuditEvent).where(AuditEvent.entity_id == job_id)).all()
    assert [a.action for a in audit] == ["job.submitted"]
    assert audit[0].actor == "admin"


def test_idempotency_key_deduplicates(client, admin_headers, db_session):
    payload = {"job_type": "system.self_check", "idempotency_key": "self-check-2026-10-13"}
    first = client.post("/api/v1/jobs", json=payload, headers=admin_headers)
    second = client.post("/api/v1/jobs", json=payload, headers=admin_headers)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.get_json()["meta"]["deduplicated"] is True
    assert first.get_json()["data"]["id"] == second.get_json()["data"]["id"]
    assert len(db_session.scalars(select(JobRun)).all()) == 1


def test_get_job_validation(client):
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 400
    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404


def test_list_jobs_pagination_and_filters(client, admin_headers):
    for _ in range(3):
        client.post("/api/v1/jobs", json={"job_type": "system.self_check"}, headers=admin_headers)
    body = client.get("/api/v1/jobs?page=1&page_size=2").get_json()
    assert len(body["data"]) == 2
    assert body["meta"]["total"] == 3
    body = client.get("/api/v1/jobs?status=failed").get_json()
    assert body["data"] == []
    assert client.get("/api/v1/jobs?page_size=10000").status_code == 400
    assert client.get("/api/v1/jobs?status=bogus").status_code == 400
    assert client.get("/api/v1/jobs?unknown=1").status_code == 400


def test_dispatch_failure_is_recorded(client, admin_headers, db_session, monkeypatch):
    from app.tasks.celery_app import celery

    task = celery.tasks["system.self_check"]

    def broken(*args, **kwargs):
        raise ConnectionError("redis://:topsecret@broker:6379 refused")

    monkeypatch.setattr(task, "apply_async", broken)
    resp = client.post("/api/v1/jobs", json={"job_type": "system.self_check"}, headers=admin_headers)
    assert resp.status_code == 202  # the request was accepted and recorded...
    job = resp.get_json()["data"]
    assert job["status"] == JobStatus.DISPATCH_FAILED.value  # ...but the response says it never reached a worker
    assert "topsecret" not in job["error_message"]


def test_internal_errors_do_not_leak_details(client, monkeypatch):
    from app.api import jobs as jobs_api

    def boom(*_a, **_k):
        raise RuntimeError("postgresql+psycopg://psx:hunter2@db/x exploded")

    monkeypatch.setattr(jobs_api, "parse_query", boom)
    resp = client.get("/api/v1/jobs")
    assert resp.status_code == 500
    text = resp.get_data(as_text=True)
    assert "hunter2" not in text and "exploded" not in text
    assert resp.get_json()["errors"][0]["code"] == "internal_error"
