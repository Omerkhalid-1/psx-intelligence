"""Background-job lifecycle: submission, dispatch and status transitions.

Lifecycle: queued -> running -> succeeded | failed, or queued -> dispatch_failed when the
broker refuses the message. A job is persisted *before* it is enqueued, so acceptance by
the API never implies completion and dispatch failures are always visible.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.operations import JobRun, JobStatus
from app.services.audit import record_audit
from app.utils.logging import get_context, new_correlation_id, redact
from app.utils.time import utcnow

log = logging.getLogger(__name__)

# job_type -> registered Celery task name. Only these job types can be submitted.
JOB_TASKS: dict[str, str] = {
    "system.self_check": "system.self_check",
    "ingestion.psx_daily_bars": "ingestion.psx_daily_bars",
    "ingestion.psx_quotes": "ingestion.psx_quotes",
    "pipeline.daily": "pipeline.daily",
    "models.train": "models.train",
    "refresh.latest": "refresh.latest",
}


class UnknownJobType(ValueError):
    pass


class TransientJobError(Exception):
    """A failure expected to succeed on retry (timeouts, temporary source outages)."""


class PermanentJobError(Exception):
    """A failure that retrying will not fix (invalid input, missing licence, bad data)."""


@dataclass(frozen=True)
class SubmitResult:
    job: JobRun
    deduplicated: bool


def submit_job(
    job_type: str,
    *,
    parameters: dict[str, Any] | None = None,
    requested_by: str,
    idempotency_key: str | None = None,
    dispatch: bool = True,
) -> SubmitResult:
    """Persist a job and (by default) enqueue it. `dispatch=False` lets a caller run the task
    in-process, e.g. the CLI's synchronous mode, while keeping the same job record."""
    if job_type not in JOB_TASKS:
        raise UnknownJobType(job_type)

    if idempotency_key:
        existing = db.session.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
        if existing is not None:
            return SubmitResult(existing, deduplicated=True)

    job = JobRun(
        id=uuid.uuid4(),
        job_type=job_type,
        status=JobStatus.QUEUED,
        idempotency_key=idempotency_key,
        correlation_id=get_context().get("correlation_id") or new_correlation_id(),
        requested_by=requested_by,
        parameters=parameters or {},
    )
    db.session.add(job)
    record_audit(actor=requested_by, action="job.submitted", entity_type="job_run", entity_id=str(job.id),
                 details={"job_type": job_type, "parameters": job.parameters})
    try:
        db.session.commit()
    except IntegrityError:
        # Concurrent submission with the same idempotency key won the race.
        db.session.rollback()
        existing = db.session.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return SubmitResult(existing, deduplicated=True)

    if dispatch:
        _dispatch(job)
    return SubmitResult(job, deduplicated=False)


def _dispatch(job: JobRun) -> None:
    from app.tasks.celery_app import celery

    task = celery.tasks[JOB_TASKS[job.job_type]]
    try:
        async_result = task.apply_async(kwargs={"job_id": str(job.id)}, headers={"correlation_id": job.correlation_id})
    except Exception as exc:  # noqa: BLE001 - broker failures must be recorded, not hidden
        log.error("job dispatch failed", extra={"job_id": str(job.id), "event_type": "job_dispatch_failed",
                                                "error_category": type(exc).__name__})
        db.session.rollback()
        job = db.session.get(JobRun, job.id)
        job.status = JobStatus.DISPATCH_FAILED
        job.error_category = "dispatch_error"
        job.error_message = redact(f"{type(exc).__name__}: {exc}")[:2000]
        job.finished_at = utcnow()
        db.session.commit()
        return
    # In eager mode the task already ran and committed; refresh rather than overwrite it.
    db.session.refresh(job)
    if job.celery_task_id is None:
        job.celery_task_id = async_result.id
        db.session.commit()
    log.info("job dispatched", extra={"job_id": str(job.id), "event_type": "job_dispatched",
                                      "details": {"job_type": job.job_type}})


def mark_running(job_id: uuid.UUID, celery_task_id: str | None) -> JobRun | None:
    """Transition to running. Returns None if the job already finished (redelivered message)."""
    job = db.session.get(JobRun, job_id, with_for_update=True)
    if job is None:
        raise PermanentJobError(f"job {job_id} does not exist")
    if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
        db.session.rollback()
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.celery_task_id = celery_task_id or job.celery_task_id
    job.started_at = job.started_at or utcnow()
    job.error_category = None
    job.error_message = None
    db.session.commit()
    return job


def mark_succeeded(job_id: uuid.UUID, summary: dict[str, Any]) -> None:
    job = db.session.get(JobRun, job_id)
    job.status = JobStatus.SUCCEEDED
    job.result_summary = summary
    job.finished_at = utcnow()
    db.session.commit()


def mark_failed(job_id: uuid.UUID, *, category: str, message: str, will_retry: bool) -> None:
    db.session.rollback()  # discard any partial work from the failed attempt
    job = db.session.get(JobRun, job_id)
    job.error_category = category
    job.error_message = redact(message)[:2000]
    if will_retry:
        job.status = JobStatus.QUEUED
    else:
        job.status = JobStatus.FAILED
        job.finished_at = utcnow()
    db.session.commit()


# A job still queued after this long was most likely lost (worker crashed before the
# wrapper ran, broker purged, no worker consuming). Running jobs beyond the Celery hard
# time limit (60 min) plus slack cannot still be executing.
STUCK_QUEUED_AFTER = timedelta(minutes=15)
STUCK_RUNNING_AFTER = timedelta(minutes=65)


def active_job(job_type: str) -> JobRun | None:
    """A queued or running job of this type that is not (yet) considered stuck."""
    now = utcnow()
    return db.session.scalar(
        select(JobRun)
        .where(JobRun.job_type == job_type, or_(
            and_(JobRun.status == JobStatus.QUEUED, JobRun.updated_at >= now - STUCK_QUEUED_AFTER),
            and_(JobRun.status == JobStatus.RUNNING, JobRun.updated_at >= now - STUCK_RUNNING_AFTER),
        ))
        .order_by(JobRun.created_at.desc())
        .limit(1))


def find_stuck_jobs(limit: int = 50) -> list[JobRun]:
    now = utcnow()
    return list(db.session.scalars(
        select(JobRun)
        .where(or_(
            and_(JobRun.status == JobStatus.QUEUED, JobRun.updated_at < now - STUCK_QUEUED_AFTER),
            and_(JobRun.status == JobStatus.RUNNING, JobRun.updated_at < now - STUCK_RUNNING_AFTER),
        ))
        .order_by(JobRun.created_at)
        .limit(limit)
    ))
