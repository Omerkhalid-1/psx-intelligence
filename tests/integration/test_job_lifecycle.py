"""Job wrapper behaviour: retries, permanent failures, redelivery idempotency, rollback."""

import uuid

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.operations import AuditEvent, JobRun, JobStatus
from app.services.orchestration import jobs
from app.tasks.base import tracked_job

pytestmark = pytest.mark.db

_calls: dict[str, int] = {}


@tracked_job("test.transient", max_retries=2, retry_backoff_seconds=0)
def _transient(job):
    _calls["transient"] = _calls.get("transient", 0) + 1
    raise jobs.TransientJobError("source timed out")


@tracked_job("test.flaky", max_retries=2, retry_backoff_seconds=0)
def _flaky(job):
    _calls["flaky"] = _calls.get("flaky", 0) + 1
    if _calls["flaky"] < 2:
        raise jobs.TransientJobError("temporary")
    return {"ok": True}


@tracked_job("test.permanent", max_retries=3)
def _permanent(job):
    _calls["permanent"] = _calls.get("permanent", 0) + 1
    raise jobs.PermanentJobError("licence missing for source")


@tracked_job("test.partial_write")
def _partial_write(job):
    db.session.add(AuditEvent(actor="t", action="partial", entity_type="x"))
    db.session.flush()
    raise ValueError("crashed after a partial write")


def _new_job(db_session, job_type: str) -> JobRun:
    job = JobRun(id=uuid.uuid4(), job_type=job_type, correlation_id="c" * 16, requested_by="test", parameters={})
    db_session.add(job)
    db_session.commit()
    return job


def _run(task_name: str, job: JobRun):
    from app.tasks.celery_app import celery

    return celery.tasks[task_name].apply(kwargs={"job_id": str(job.id)})


@pytest.fixture(autouse=True)
def _reset_calls():
    _calls.clear()


def test_transient_failure_retries_then_fails(db_session):
    job = _new_job(db_session, "test.transient")
    _run("test.transient", job)
    db_session.expire_all()
    job = db_session.get(JobRun, job.id)
    assert _calls["transient"] == 3  # first attempt + 2 bounded retries
    assert job.status == JobStatus.FAILED
    assert job.attempts == 3
    assert job.error_category == "transient"


def test_transient_failure_recovers(db_session):
    job = _new_job(db_session, "test.flaky")
    _run("test.flaky", job)
    db_session.expire_all()
    job = db_session.get(JobRun, job.id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.attempts == 2
    assert job.error_message is None
    assert job.result_summary == {"ok": True}


def test_permanent_failure_is_not_retried(db_session):
    job = _new_job(db_session, "test.permanent")
    _run("test.permanent", job)
    db_session.expire_all()
    job = db_session.get(JobRun, job.id)
    assert _calls["permanent"] == 1
    assert job.status == JobStatus.FAILED
    assert job.error_category == "permanent"
    assert "licence" in job.error_message


def test_redelivered_finished_job_is_skipped(db_session):
    job = _new_job(db_session, "test.flaky")
    _calls["flaky"] = 5  # make it succeed immediately
    _run("test.flaky", job)
    _run("test.flaky", job)  # simulated redelivery after worker restart
    assert _calls["flaky"] == 6
    db_session.expire_all()
    assert db_session.get(JobRun, job.id).attempts == 1


def test_failed_job_rolls_back_partial_writes(db_session):
    job = _new_job(db_session, "test.partial_write")
    _run("test.partial_write", job)
    db_session.expire_all()
    assert db_session.get(JobRun, job.id).status == JobStatus.FAILED
    assert db_session.scalars(select(AuditEvent).where(AuditEvent.action == "partial")).all() == []


def test_stuck_jobs_are_detected(db_session):
    from datetime import timedelta

    from sqlalchemy import update

    from app.utils.time import utcnow

    fresh = _new_job(db_session, "test.flaky")
    lost = _new_job(db_session, "test.flaky")
    db_session.execute(update(JobRun).where(JobRun.id == lost.id)
                       .values(updated_at=utcnow() - timedelta(minutes=30)))
    db_session.commit()
    stuck_ids = {j.id for j in jobs.find_stuck_jobs()}
    assert lost.id in stuck_ids
    assert fresh.id not in stuck_ids


def test_database_session_returns_utc(db_session):
    from datetime import timedelta

    job = _new_job(db_session, "test.flaky")
    db_session.expire_all()
    created = db_session.get(JobRun, job.id).created_at
    assert created.utcoffset() == timedelta(0)
