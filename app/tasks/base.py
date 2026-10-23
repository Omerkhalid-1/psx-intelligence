"""`tracked_job` turns a plain function into a Celery task bound to a `JobRun` record.

The wrapper owns the status transitions, structured logging, bounded retries for
`TransientJobError`, and idempotency on redelivery (a finished job is never re-run).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from celery import Task

from app.models.operations import JobRun
from app.services.orchestration import jobs
from app.tasks.celery_app import celery
from app.utils.logging import log_context

log = logging.getLogger(__name__)

JobFn = Callable[[JobRun], dict[str, Any]]


def tracked_job(name: str, *, max_retries: int = 3, retry_backoff_seconds: int = 30) -> Callable[[JobFn], Any]:
    def decorator(fn: JobFn) -> Any:
        @celery.task(name=name, bind=True, max_retries=max_retries)
        def task(self: Task, job_id: str) -> dict[str, Any] | None:
            jid = uuid.UUID(job_id)
            with log_context(job_id=job_id, analysis_module=name):
                job = jobs.mark_running(jid, self.request.id)
                if job is None:
                    log.info("job already finished; skipping redelivered message",
                             extra={"event_type": "job_skipped_redelivery"})
                    return None
                # The persisted job is the authoritative source of the correlation ID.
                with log_context(correlation_id=job.correlation_id):
                    return _execute(self, fn, job, max_retries, retry_backoff_seconds)

        return task

    return decorator


def _execute(task: Task, fn: JobFn, job: JobRun, max_retries: int, retry_backoff_seconds: int) -> dict[str, Any] | None:
    jid = job.id
    start = time.perf_counter()
    log.info("job started", extra={"event_type": "job_started", "details": {"attempt": job.attempts}})
    try:
        summary = fn(job)
    except jobs.TransientJobError as exc:
        will_retry = task.request.retries < max_retries
        jobs.mark_failed(jid, category="transient", message=str(exc), will_retry=will_retry)
        log.warning("job attempt failed (transient)", extra={
            "event_type": "job_retry" if will_retry else "job_failed", "error_category": "transient",
            "duration_ms": _ms(start)})
        if will_retry:
            countdown = retry_backoff_seconds * (2**task.request.retries)
            raise task.retry(exc=exc, countdown=countdown) from exc
        return None
    except jobs.PermanentJobError as exc:
        jobs.mark_failed(jid, category="permanent", message=str(exc), will_retry=False)
        log.error("job failed (permanent)", extra={
            "event_type": "job_failed", "error_category": "permanent", "duration_ms": _ms(start)})
        return None
    except Exception as exc:
        jobs.mark_failed(jid, category="unexpected", message=f"{type(exc).__name__}: {exc}", will_retry=False)
        log.exception("job failed (unexpected)", extra={
            "event_type": "job_failed", "error_category": "unexpected", "duration_ms": _ms(start)})
        return None
    jobs.mark_succeeded(jid, summary)
    log.info("job succeeded", extra={"event_type": "job_succeeded", "duration_ms": _ms(start), "status": "succeeded"})
    return summary


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
