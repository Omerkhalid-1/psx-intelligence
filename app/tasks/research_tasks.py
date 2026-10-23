"""Research workflow tasks: the daily pipeline and candidate training, bound to JobRun records."""

from __future__ import annotations

from typing import Any

from flask import current_app

from app.models.operations import JobRun
from app.services.orchestration import jobs
from app.tasks.base import tracked_job
from app.tasks.celery_app import celery
from app.utils.time import psx_local_date, utcnow


@tracked_job("pipeline.daily", max_retries=0)
def pipeline_daily(job: JobRun) -> dict[str, Any]:
    from app.services.pipeline import PipelineBusy, run_daily_pipeline

    try:
        run = run_daily_pipeline(current_app.config, job_run_id=job.id, force=bool(job.parameters.get("force")),
                                 skip_ingestion=bool(job.parameters.get("skip_ingestion")))
    except PipelineBusy as exc:
        raise jobs.PermanentJobError(str(exc)) from None
    summary = {"pipeline_run_id": str(run.id), "status": run.status,
               "session": run.session_date.isoformat() if run.session_date else None,
               "steps": {s["step"]: s["status"] for s in run.steps}}
    if run.status == "failed":
        raise jobs.PermanentJobError(f"pipeline failed: {summary['steps']}")
    return summary


@tracked_job("models.train", max_retries=0)
def models_train(job: JobRun) -> dict[str, Any]:
    from app.services.forecasting.registry import train_candidates

    if not current_app.config["ENABLE_MODEL_TRAINING"]:
        raise jobs.PermanentJobError("ENABLE_MODEL_TRAINING is false")
    cands = train_candidates(current_app.config, tuple(current_app.config["FORECAST_HORIZONS"]),
                             actor=str(job.parameters.get("actor", "api")))
    return {"candidates": [{"id": str(c.id), "key": c.model_key, "h": c.horizon, "v": c.version} for c in cands]}


@tracked_job("refresh.latest", max_retries=0)
def refresh_latest(job: JobRun) -> dict[str, Any]:
    """Manual "get the latest information": quote sync, then the pipeline (bars, news, analysis, forecasts)."""
    from app.services.refresh import run_refresh

    summary = run_refresh(current_app.config, job_run_id=job.id)
    if summary["status"] == "failed":
        raise jobs.PermanentJobError(f"refresh failed: quotes={summary['quotes']}, pipeline={summary['pipeline']}")
    return summary


@celery.task(name="pipeline.schedule_daily", ignore_result=True)
def schedule_daily() -> None:
    """Beat trigger (weekdays after the close); one pipeline job per PSX date."""
    config = current_app.config
    if not (config["ENABLE_SCHEDULED_INGESTION"] and config["ENABLE_RESEARCH_PIPELINE"]):
        return
    day = psx_local_date(utcnow()).isoformat()
    jobs.submit_job("pipeline.daily", requested_by="scheduler", idempotency_key=f"pipeline.daily:{day}")
