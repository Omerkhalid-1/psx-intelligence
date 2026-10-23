"""Ingestion status and the (admin-only) manual sync trigger."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from flask import current_app, g
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from app.api import api_v1
from app.api.auth import require_admin
from app.api.responses import ApiError, ok
from app.api.schemas import JobOut, PaginationParams
from app.api.validation import parse_body, parse_query
from app.extensions import db
from app.models.market_data import IngestionRejection, IngestionRun, IngestionStatus
from app.services.ingestion.psx import DATASET_BARS, DATASET_QUOTES
from app.services.market_data import bars_freshness, integration_status, latest_run, quotes_freshness
from app.services.orchestration import jobs
from app.tasks.ingestion_tasks import BARS_JOB, QUOTES_JOB
from app.utils.time import utcnow

# Minimum gap between manual syncs of one dataset: every sync ends up as requests to PSX.
MANUAL_SYNC_COOLDOWN_SECONDS = 60
JOB_FOR_DATASET = {DATASET_BARS: BARS_JOB, DATASET_QUOTES: QUOTES_JOB}


class IngestionRunListParams(PaginationParams):
    status: IngestionStatus | None = None
    dataset: Literal["daily_bars", "quotes"] | None = None


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: Literal["daily_bars", "quotes"] = "daily_bars"
    full_history: bool = False
    symbols: list[str] | None = Field(default=None, max_length=50)

    @field_validator("symbols")
    @classmethod
    def _upper(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [s.strip().upper() for s in value]


def _run_out(run: IngestionRun, *, detail: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(run.id), "source_id": run.source_id, "dataset": run.dataset,
        "job_run_id": str(run.job_run_id) if run.job_run_id else None, "status": run.status.value,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "records": {"received": run.records_received, "accepted": run.records_accepted,
                    "rejected": run.records_rejected, "filtered": run.records_filtered,
                    "stored": run.records_stored},
        "error_category": run.error_category, "error_summary": run.error_summary,
    }
    if detail:
        out["request_metadata"] = run.request_metadata
        rejections = db.session.scalars(
            select(IngestionRejection).where(IngestionRejection.ingestion_run_id == run.id)
            .order_by(IngestionRejection.id).limit(200)).all()
        out["rejections"] = [{"record_index": r.record_index, "symbol": r.symbol, "reason": r.reason,
                              "raw_record": r.raw_record} for r in rejections]
    return out


@api_v1.get("/ingestion/status")
def ingestion_status():
    config = current_app.config
    status = integration_status(config)
    datasets = [bars_freshness(config, status), quotes_freshness(config, status)]
    warnings = list(status.problems)
    for f in datasets:
        if f.state == "stale":
            warnings.append(f"{f.dataset} stale: {f.detail}")
        if f.last_run is not None and f.last_run.status == IngestionStatus.FAILED:
            warnings.append(f"last {f.dataset} sync failed: {f.last_run.error_category}")
    return ok({
        "integration": status.as_dict(),
        "settings": {"service_url": config["PSXDATA_API_URL"], "tracked_symbols": list(config["PSXDATA_SYMBOLS"]),
                     "daily_sync_time_psx": config["PSXDATA_DAILY_SYNC_TIME"],
                     "quote_poll_interval_minutes": config["PSXDATA_QUOTE_POLL_INTERVAL_MINUTES"],
                     "quote_window_psx": config["PSXDATA_QUOTE_WINDOW"],
                     "request_spacing_seconds": config["PSXDATA_REQUEST_SPACING_SECONDS"]},
        "datasets": {f.dataset: f.as_dict() for f in datasets},
    }, warnings=warnings)


@api_v1.get("/ingestion/runs")
def list_runs():
    params = parse_query(IngestionRunListParams)
    query = select(IngestionRun)
    if params.status:
        query = query.where(IngestionRun.status == params.status)
    if params.dataset:
        query = query.where(IngestionRun.dataset == params.dataset)
    total = db.session.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.session.scalars(query.order_by(IngestionRun.started_at.desc())
                              .offset((params.page - 1) * params.page_size).limit(params.page_size)).all()
    return ok([_run_out(r) for r in rows], meta={"page": params.page, "page_size": params.page_size, "total": total})


@api_v1.get("/ingestion/runs/<run_id>")
def get_run(run_id: str):
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise ApiError(400, "invalid_id", "run_id must be a UUID") from None
    run = db.session.get(IngestionRun, rid)
    if run is None:
        raise ApiError(404, "not_found", "Ingestion run not found")
    return ok(_run_out(run, detail=True))


@api_v1.post("/ingestion/runs")
@require_admin
def trigger_sync():
    """Enqueue one sync. Never calls psxdata-api inside the request."""
    body = parse_body(SyncRequest)
    config = current_app.config
    status = integration_status(config)
    if not status.ready:
        raise ApiError(503, "integration_not_configured", "psxdata integration is not ready",
                       {"problems": status.problems})
    if body.symbols:
        unknown = sorted(set(body.symbols) - set(config["PSXDATA_SYMBOLS"]))
        if unknown:
            raise ApiError(400, "unknown_symbols", "Symbols must be in PSXDATA_SYMBOLS", {"unknown": unknown})
    if body.full_history and body.dataset != DATASET_BARS:
        raise ApiError(400, "invalid_body", "full_history applies only to daily_bars")
    job_type = JOB_FOR_DATASET[body.dataset]
    active = jobs.active_job(job_type)
    if active is not None:
        raise ApiError(409, "sync_in_progress", f"A {body.dataset} sync is already queued or running",
                       {"job_id": str(active.id)})
    last = latest_run(body.dataset)
    if last is not None:
        elapsed = (utcnow() - last.started_at).total_seconds()
        if elapsed < MANUAL_SYNC_COOLDOWN_SECONDS:
            raise ApiError(429, "sync_cooldown", f"Last {body.dataset} sync started {int(elapsed)}s ago; wait "
                                                 f"{MANUAL_SYNC_COOLDOWN_SECONDS}s between manual syncs")
    params: dict[str, Any] = {"full_history": body.full_history} if body.dataset == DATASET_BARS else {}
    if body.symbols:
        params["symbols"] = body.symbols
    result = jobs.submit_job(job_type, parameters=params, requested_by=g.actor)
    return ok(JobOut.model_validate(result.job).model_dump(mode="json"),
              meta={"poll": f"/api/v1/jobs/{result.job.id}"}, status=202)
