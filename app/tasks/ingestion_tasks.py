"""Ingestion tasks (psxdata-api): tracked jobs for daily bars and quotes, plus beat triggers."""

from __future__ import annotations

import logging
from typing import Any

from flask import current_app

from app.models.market_data import IngestionRun, IngestionStatus
from app.models.operations import JobRun
from app.services.ingestion.psx import IngestionAlreadyRunning, run_daily_bar_ingestion, run_quote_ingestion
from app.services.market_data import integration_status, within_window
from app.services.orchestration import jobs
from app.tasks.base import tracked_job
from app.tasks.celery_app import celery
from app.utils.time import psx_local_date, utcnow

log = logging.getLogger(__name__)

BARS_JOB = "ingestion.psx_daily_bars"
QUOTES_JOB = "ingestion.psx_quotes"
TRANSIENT_CATEGORIES = {"provider_unavailable", "rate_limited", "database_error"}


def _symbols(job: JobRun) -> tuple[str, ...] | None:
    """Optional subset of the configured symbols (validated at submission)."""
    requested = job.parameters.get("symbols")
    return tuple(requested) if requested else None


def _ensure_ready() -> None:
    status = integration_status(current_app.config)
    if not status.ready:
        raise jobs.PermanentJobError("psxdata integration not ready: " + "; ".join(status.problems))


def _summarise(run: IngestionRun) -> dict[str, Any]:
    summary = {"ingestion_run_id": str(run.id), "dataset": run.dataset, "ingestion_status": run.status.value,
               "received": run.records_received, "accepted": run.records_accepted,
               "rejected": run.records_rejected, "filtered": run.records_filtered, "stored": run.records_stored,
               "summary": run.error_summary}
    if run.status == IngestionStatus.FAILED:
        message = f"ingestion run {run.id} failed ({run.error_category}): {run.error_summary}"
        if run.error_category in TRANSIENT_CATEGORIES:
            raise jobs.TransientJobError(message)
        raise jobs.PermanentJobError(message)
    return summary


# max_retries=0: the HTTP client already retries transient failures (bounded, with backoff),
# and the next scheduled run is the retry. Job-level retries would multiply requests to PSX.
@tracked_job(BARS_JOB, max_retries=0)
def psx_daily_bars(job: JobRun) -> dict[str, Any]:
    _ensure_ready()
    try:
        run = run_daily_bar_ingestion(current_app.config, full=bool(job.parameters.get("full_history")),
                                      symbols=_symbols(job), job_run_id=job.id)
    except IngestionAlreadyRunning as exc:
        raise jobs.PermanentJobError(f"skipped: {exc}") from None
    return _summarise(run)


@tracked_job(QUOTES_JOB, max_retries=0)
def psx_quotes(job: JobRun) -> dict[str, Any]:
    _ensure_ready()
    try:
        run = run_quote_ingestion(current_app.config, symbols=_symbols(job), job_run_id=job.id)
    except IngestionAlreadyRunning as exc:
        raise jobs.PermanentJobError(f"skipped: {exc}") from None
    return _summarise(run)


def _schedule_enabled() -> bool:
    config = current_app.config
    if config["ENABLE_SCHEDULED_INGESTION"] and integration_status(config).ready:
        return True
    log.info("scheduled psx ingestion skipped: not enabled", extra={"event_type": "ingestion_schedule_skipped"})
    return False


@celery.task(name="ingestion.schedule_psx_daily_bars", ignore_result=True)
def schedule_psx_daily_bars() -> None:
    """Beat fires this on weekdays after the close. One job per PSX date (idempotency key)."""
    if not _schedule_enabled():
        return
    day = psx_local_date(utcnow()).isoformat()
    jobs.submit_job(BARS_JOB, requested_by="scheduler", idempotency_key=f"{BARS_JOB}:{day}")


@celery.task(name="ingestion.schedule_psx_quotes", ignore_result=True)
def schedule_psx_quotes() -> None:
    """Beat fires this every PSXDATA_QUOTE_POLL_INTERVAL_MINUTES; only submits inside market hours."""
    config = current_app.config
    if not _schedule_enabled() or not within_window(config["PSXDATA_QUOTE_WINDOW"]):
        return
    interval = int(config["PSXDATA_QUOTE_POLL_INTERVAL_MINUTES"]) * 60
    slot = int(utcnow().timestamp()) // interval
    jobs.submit_job(QUOTES_JOB, requested_by="scheduler", idempotency_key=f"{QUOTES_JOB}:{interval}:{slot}")
